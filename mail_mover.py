"""Move messages of the personal mailbox to Trash or Spam, only with the person's exact approval (#140).

This is the first thing in the project that writes to a mailbox, so it is small and closed:

* It is a separate module on purpose: ``team/imap_client.py`` can never write. Here only five write commands exist (SELECT, UID MOVE,
  UID COPY, UID STORE of ``\\Deleted`` on one UID, UID EXPUNGE of one UID) and nothing else can be sent. There is **no general EXPUNGE**,
  no permanent deletion, no emptying of Trash/Spam, no folder creation, no flag other than ``\\Deleted`` on a message that has already
  been copied and verified. Without MOVE or UIDPLUS on the server it refuses instead of improvising.
* A message is identified by account + folder + UIDVALIDITY + UID + Message-ID. Before moving, the message at that UID must still be the
  one that was approved; after moving, it must be gone from the source and present at the destination. If any check fails the batch
  stops; a timeout is never repeated blindly.
* The approval is exact: the card shows the messages and the destination, carries a hash of them, and the click sends that hash back.
  Anything else (no click, another hash, an expired or used proposal) moves nothing.
* A local journal records, per message, what was about to happen and what happened (folders, UIDs, Message-ID, new UID, result),
  never subject, sender, body or attachments. Nothing here logs.
"""
import hashlib
import hmac
import imaplib
import json
import os
import re
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.imap_client import ImapConfig, ImapError, ReadOnlyImap, _quote, parse_message

FILE = "movimentos-de-email.json"
MAX_BATCH = 20
KEEP_LOG = 500
PROPOSAL_TTL = timedelta(hours=1)
DESTINATIONS = {"lixeira": "Lixeira", "spam": "Spam"}
_ATTRIBUTE = {"lixeira": "\\trash", "spam": "\\junk"}
_NAMES = {"lixeira": ("Trash", "Lixeira", "Deleted Items", "Deleted Messages", "INBOX.Trash", "[Gmail]/Trash", "[Gmail]/Lixeira"),
          "spam": ("Junk", "Spam", "Junk E-mail", "Junk Email", "INBOX.Junk", "INBOX.Spam", "[Gmail]/Spam")}
_LIST_LINE = re.compile(r'^\((?P<attrs>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')
_WRITE_COMMANDS = ("SELECT", "UID MOVE", "UID COPY", "UID STORE", "UID EXPUNGE")  # the whole write vocabulary
_LOCK = threading.Lock()

REASONS = {
    "destino_inexistente": "Não encontrei a pasta {} desta conta e não crio pastas.",
    "sem_suporte": "O servidor não tem MOVE nem UIDPLUS; sem eles eu não movo, para nunca usar um EXPUNGE geral.",
    "uidvalidity_mudou": "O servidor reorganizou a pasta (UIDVALIDITY mudou); liste as mensagens de novo.",
    "nao_encontrada": "A mensagem não está mais na pasta de origem nem no destino.",
    "mudou": "A mensagem naquele número já não é a que foi aprovada.",
    "copia_nao_verificada": "A cópia no destino não pôde ser confirmada; a mensagem original foi mantida.",
    "verificacao_origem": "Depois de mover, a mensagem ainda aparece na origem.",
    "verificacao_destino": "Depois de mover, a mensagem não apareceu no destino.",
    "tempo_esgotado": "O servidor não respondeu a tempo; não repeti o comando. Confira a Lixeira/Spam antes de pedir de novo.",
    "erro": "O servidor recusou ou interrompeu o comando.",
}


class MoveError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _now():
    return datetime.now(timezone.utc)


# -- the write side of IMAP ----------------------------------------------------------------------------------------------
class ImapMover:
    """Moves one message at a time. Uses ``ReadOnlyImap`` for the connection, LIST and header reads (``BODY.PEEK`` only)."""

    def __init__(self, config: ImapConfig, secret, ssl_context=None, factory=None):
        self.imap = ReadOnlyImap(config, secret, ssl_context, factory)
        self.config = config

    @property
    def sent(self):
        return self.imap.sent

    def __enter__(self):
        self.imap.connect()
        return self

    def __exit__(self, *exc):
        self.imap.close()

    @property
    def _conn(self):
        return self.imap._need()

    def _write(self, label, function, *args):
        if label not in _WRITE_COMMANDS:
            raise ImapError("comando_proibido", "Comando de escrita não permitido.")
        return self.imap._call(label, function, *args)

    def capabilities(self):
        caps = {str(c).upper() for c in getattr(self._conn, "capabilities", ())}
        if not ({"MOVE", "UIDPLUS"} & caps):
            data = self.imap._call("CAPABILITY", self._conn.capability)
            caps |= {part.upper() for line in data for part in (line.decode() if isinstance(line, bytes) else str(line)).split()}
        return caps

    def source_folder(self):
        return self.config.folders[0]

    def destination(self, kind):
        """The existing Trash or Spam folder: by the server's special-use attribute, else by a well-known name. Never created."""
        names, by_attribute = [], None
        for line in self.imap._call("LIST", self._conn.list):
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            found = _LIST_LINE.match(text.strip())
            if not found:
                continue
            name = found.group("name").strip().strip('"').replace('\\"', '"')
            names.append(name)
            if _ATTRIBUTE[kind] in found.group("attrs").lower().split():
                by_attribute = by_attribute or name
        if by_attribute:
            return by_attribute
        for candidate in _NAMES[kind]:
            if candidate in names:
                return candidate
        raise MoveError("destino_inexistente")

    def open(self, folder, write=False):
        """Opens a folder and returns its UIDVALIDITY: EXAMINE (read-only) by default, SELECT only for the authorised source folder."""
        if not write:
            return self._examine(folder)
        if folder not in self.config.folders:
            raise ImapError("pasta_nao_autorizada", "Esta pasta não está autorizada para a conta.")
        self._write("SELECT", self._conn.select, _quote(folder), False)
        return self._uidvalidity()

    def _uidvalidity(self):
        data = self._conn.response("UIDVALIDITY")[1]
        try:
            return int(data[-1])
        except (TypeError, ValueError, IndexError):
            raise ImapError("erro_do_servidor", "O servidor não informou UIDVALIDITY.") from None

    def _examine(self, folder):
        self.imap._call("EXAMINE", self._conn.select, _quote(folder), True)
        return self._uidvalidity()

    def headers(self, uid):
        """``{"message_id", "assunto", "de"}`` of a UID in the open folder, or ``None`` when there is no such message."""
        data = self.imap._fetch(uid, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")
        raw = next((part[1] for part in data if isinstance(part, tuple)), b"")
        if not raw:
            return None
        head = parse_message(raw, 0)
        return {"message_id": head["ids"]["message_id"], "assunto": head["subject"], "de": head["from"]}

    def uid_exists(self, uid):
        data = self.imap._call("UID SEARCH", self._conn.uid, "SEARCH", f"UID {int(uid)}")
        return bool((data[0] or b"").split())

    def has_message(self, message_id):
        if not message_id or not re.fullmatch(r"<[^<>\s\"\\]{1,250}>", message_id):
            return False
        data = self.imap._call("UID SEARCH", self._conn.uid, "SEARCH", f"HEADER Message-ID {_quote(message_id)}")
        return bool((data[0] or b"").split())

    @staticmethod
    def _new_uid(conn):
        try:
            code = conn.response("COPYUID")[1]
            return int(str(code[-1].decode() if isinstance(code[-1], bytes) else code[-1]).split()[-1])
        except (TypeError, ValueError, IndexError, AttributeError):
            return None

    def move_one(self, source, validity, uid, message_id, dest):
        """Moves one approved message and checks the result. ``{"estado": "movida"|"ja_movida", "novo_uid": int|None}`` or ``MoveError``."""
        uid = int(uid)
        if self.open(source, write=True) != validity:
            raise MoveError("uidvalidity_mudou")
        head = self.headers(uid)
        if head is None:
            if self._has_in(dest, message_id):
                return {"estado": "ja_movida", "novo_uid": None}
            raise MoveError("nao_encontrada")
        if head["message_id"] != message_id:
            raise MoveError("mudou")
        caps = self.capabilities()
        new_uid = None
        if "MOVE" in caps:
            self._write("UID MOVE", self._conn.uid, "MOVE", str(uid), _quote(dest))
            new_uid = self._new_uid(self._conn)
        elif "UIDPLUS" in caps:
            self._write("UID COPY", self._conn.uid, "COPY", str(uid), _quote(dest))
            new_uid = self._new_uid(self._conn)
            if not self._has_in(dest, message_id):  # the copy is verified before anything is removed
                raise MoveError("copia_nao_verificada")
            self.open(source, write=True)
            self._write("UID STORE", self._conn.uid, "STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
            self._write("UID EXPUNGE", self._conn.uid, "EXPUNGE", str(uid))  # this one UID only, never the whole folder
        else:
            raise MoveError("sem_suporte")
        self.open(source, write=True)
        if self.uid_exists(uid):
            raise MoveError("verificacao_origem")
        if not self._has_in(dest, message_id):
            raise MoveError("verificacao_destino")
        return {"estado": "movida", "novo_uid": new_uid}

    def _has_in(self, folder, message_id):
        self._examine(folder)
        return self.has_message(message_id)


# -- proposals, exact approval and the journal --------------------------------------------------------------------------
def digest(proposal):
    """The hash the card shows and the click sends back: it covers everything that would be done."""
    core = {"conta": proposal["conta"], "pasta": proposal["pasta"], "uidvalidity": proposal["uidvalidity"], "destino": proposal["destino"],
            "destino_pasta": proposal["destino_pasta"], "itens": [[i["uid"], i["message_id"]] for i in proposal["itens"]]}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def reason_text(code, folder=""):
    return REASONS.get(code, "Não foi possível mover.").format(folder)


class MoveService:
    def __init__(self, root, clock=_now):
        self.path, self.clock = Path(root) / FILE, clock

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            pending, log = data.get("pending", {}), data.get("log", [])
            return pending if isinstance(pending, dict) else {}, log if isinstance(log, list) else []
        except (OSError, ValueError, AttributeError):
            return {}, []

    def _save(self, pending, log):
        limit = (self.clock() - PROPOSAL_TTL).isoformat()
        pending = {k: v for k, v in pending.items() if isinstance(v, dict) and v.get("criada_em", "") >= limit}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps({"pending": pending, "log": log[-KEEP_LOG:]}, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)

    def journal(self):
        return self._load()[1]

    def pending(self):
        pending, _ = self._load()
        limit = (self.clock() - PROPOSAL_TTL).isoformat()
        return [self._view(p) for p in pending.values() if p.get("criada_em", "") >= limit]

    @staticmethod
    def _view(proposal):
        return {k: proposal[k] for k in ("id", "conta", "pasta", "destino", "destino_pasta", "itens", "hash", "criada_em")}

    @staticmethod
    def _refuse(why, folder=""):
        return {"ok": False, "motivo": why, "erro": reason_text(why, folder)}

    # -- 1. the model or a plain request proposes; nothing moves ---------------------------------------------------------------
    def propose(self, mail, uids, destination):
        if destination not in DESTINATIONS:
            return self._refuse("erro")
        try:
            ordered = sorted({int(u) for u in uids})
        except (TypeError, ValueError):
            return self._refuse("erro")
        if not ordered or len(ordered) > MAX_BATCH or ordered[0] < 1:
            return {"ok": False, "motivo": "lote", "erro": f"Informe de 1 a {MAX_BATCH} mensagens."}
        opener = getattr(mail, "open_mover", None)
        if opener is None:
            return {"ok": False, "motivo": "somente_leitura", "erro": "Esta caixa é só de leitura aqui: mover exige uma concessão de escrita que ela não tem."}
        try:
            with opener() as mover:
                source = mover.source_folder()
                dest = mover.destination(destination)
                if dest == source:
                    return self._refuse("erro")
                validity = mover.open(source)
                items = []
                for uid in ordered:
                    head = mover.headers(uid)
                    if head is None or not head["message_id"]:
                        return {"ok": False, "motivo": "nao_encontrada", "erro": f"A mensagem {uid} não existe na pasta {source}, ou não tem Message-ID."}
                    items.append({"uid": uid, "message_id": head["message_id"], "assunto": head["assunto"][:120], "de": head["de"][:120]})
        except MoveError as error:
            return self._refuse(error.code, DESTINATIONS[destination])
        except ImapError as error:
            return {"ok": False, "motivo": error.code, "erro": error.message}
        proposal = {"id": "m-" + uuid.uuid4().hex[:8], "conta": "pessoal", "pasta": source, "uidvalidity": validity, "destino": destination,
                    "destino_pasta": dest, "itens": items, "criada_em": self.clock().isoformat()}
        proposal["hash"] = digest(proposal)
        with _LOCK:
            pending, log = self._load()
            pending[proposal["id"]] = proposal
            self._save(pending, log)
        return {"ok": True, "proposal": self._view(proposal)}

    # -- 2. only the person's click, with the hash it was shown ---------------------------------------------------------------
    def discard(self, proposal_id):
        with _LOCK:
            pending, log = self._load()
            existed = pending.pop(proposal_id, None) is not None
            self._save(pending, log)
        return {"ok": existed, "estado": "descartada" if existed else "inexistente"}

    def approve(self, mail, proposal_id, presented_hash):
        """Moves exactly what was approved, one message at a time, stopping at the first check that fails."""
        with _LOCK:
            pending, log = self._load()
            proposal = pending.pop(proposal_id, None) if isinstance(proposal_id, str) else None
            self._save(pending, log)  # a proposal is single use: this one cannot be approved twice
        if proposal is None:
            return {"ok": False, "estado": "inexistente", "erro": "Essa proposta não existe mais ou já foi usada. Nada foi movido."}
        if proposal.get("criada_em", "") < (self.clock() - PROPOSAL_TTL).isoformat():
            return {"ok": False, "estado": "expirada", "erro": "A proposta expirou. Nada foi movido; peça de novo."}
        if not (isinstance(presented_hash, str) and hmac.compare_digest(presented_hash, digest(proposal)) and hmac.compare_digest(presented_hash, proposal["hash"])):
            return {"ok": False, "estado": "alterada", "erro": "A aprovação não corresponde ao que foi proposto. Nada foi movido."}
        opener = getattr(mail, "open_mover", None)
        if opener is None:
            return {"ok": False, "estado": "somente_leitura", "erro": "Esta caixa é só de leitura aqui. Nada foi movido."}
        results, stopped = [], None
        try:
            with opener() as mover:
                for item in proposal["itens"]:
                    base = {"proposta": proposal["id"], "conta": proposal["conta"], "pasta": proposal["pasta"], "uidvalidity": proposal["uidvalidity"],
                            "destino": proposal["destino_pasta"], "uid": item["uid"], "message_id": item["message_id"]}
                    if stopped:
                        results.append({"uid": item["uid"], "estado": "nao_executada"})
                        continue
                    self._record({**base, "fase": "antes", "em": self.clock().isoformat()})
                    try:
                        outcome = mover.move_one(proposal["pasta"], proposal["uidvalidity"], item["uid"], item["message_id"], proposal["destino_pasta"])
                        row = {"uid": item["uid"], "estado": outcome["estado"], "novo_uid": outcome["novo_uid"]}
                    except MoveError as error:
                        row, stopped = {"uid": item["uid"], "estado": "parou", "motivo": error.code}, error.code
                    except ImapError as error:
                        timeout = error.code == "tempo_esgotado"
                        row = {"uid": item["uid"], "estado": "desconhecida" if timeout else "falhou", "motivo": error.code}
                        stopped = "tempo_esgotado" if timeout else "erro"  # never retried blindly
                    self._record({**base, "fase": "depois", "em": self.clock().isoformat(), **{k: v for k, v in row.items() if k != "uid"}})
                    results.append(row)
        except ImapError as error:
            return {"ok": False, "estado": "sem_conexao", "erro": error.message + " Nada foi movido."}
        moved = [r for r in results if r["estado"] in ("movida", "ja_movida")]
        return {"ok": stopped is None, "estado": "concluida" if stopped is None else "parada", "resultados": results, "movidas": len(moved),
                "destino": proposal["destino_pasta"], "motivo_da_parada": stopped, "erro": reason_text(stopped, proposal["destino_pasta"]) if stopped else None}

    def _record(self, entry):
        with _LOCK:
            pending, log = self._load()
            log.append(entry)
            self._save(pending, log)


def render(result):
    """The assistant's text after the click. Plain text; nothing from the messages themselves is repeated."""
    if not result.get("ok") and result.get("estado") not in ("parada",):
        return result.get("erro") or "Nada foi movido."
    rows = result.get("resultados") or []
    moved, already = [r for r in rows if r["estado"] == "movida"], [r for r in rows if r["estado"] == "ja_movida"]
    lines = [f"Movi {len(moved)} mensagem(ns) para {result['destino']}, confirmando na origem e no destino."]
    if already:
        lines.append(f"{len(already)} já estava(m) no destino.")
    if result.get("estado") == "parada":
        waiting = [r for r in rows if r["estado"] == "nao_executada"]
        unknown = [r for r in rows if r["estado"] == "desconhecida"]
        lines.append("Parei o lote: " + (result.get("erro") or "uma verificação falhou") + f" Não executei {len(waiting)} mensagem(ns).")
        if unknown:
            lines.append("O estado de uma mensagem ficou desconhecido: confira a pasta antes de pedir de novo.")
    return " ".join(lines)


# -- plain requests -----------------------------------------------------------------------------------------------------
_MOVE_REQUEST = re.compile(
    r"^\s*(?:(?:por favor|pode|poderia|quero que voc[eê])[,\s]+)*(?:mova|mover|jogue|manda|mande|envie)\s+"
    r"(?:(?:o|os|as|a)\s+)?(?P<alvo>suspeit\w+|(?:e-?mails?|mensagens?)\s+(?:uid\s+|n[uú]meros?\s+)?\d+(?:\s*(?:,|e)\s*\d+)*|(?:uid\s+)?\d+(?:\s*(?:,|e)\s*\d+)*)"
    r"(?:\s+(?:e-?mails?|mensagens?|da\s+triagem))?\s+para\s+(?:a\s+|o\s+|à\s+|ao\s+)?(?P<dest>lixeira|lixo\s+eletr[oô]nico|spam|lixo)\s*[.!]?\s*$",
    re.IGNORECASE)


def parse_request(text):
    """``{"uids": [..] | None, "suspeitos": bool, "destino": "lixeira"|"spam"}`` for a short, plain request to move, else ``None``."""
    if not isinstance(text, str) or len(text) > 160 or text.rstrip().endswith("?"):
        return None
    found = _MOVE_REQUEST.match(text)
    if not found:
        return None
    target, dest = found.group("alvo").lower(), found.group("dest").lower()
    destination = "spam" if dest in ("spam",) or "eletr" in dest else "lixeira"
    if target.startswith("suspeit"):
        return {"uids": None, "suspeitos": True, "destino": destination}
    return {"uids": [int(n) for n in re.findall(r"\d+", target)], "suspeitos": False, "destino": destination}
