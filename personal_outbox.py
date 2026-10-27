"""Replies to the person's own email, sent only after the person approves the exact draft on screen.

The model can only *prepare* a draft (``prepare``). Approving, discarding and sending are methods the chat's tools never
call: they run when the browser sends a decision over the WebSocket, which the model cannot produce. A draft is immutable,
goes only to the sender of the message being answered (no cc, bcc or attachments), is approved by the hash of its exact
content, expires, and is sent at most once. A send that dies after ``DATA`` is recorded as unknown and never repeated.
"""
import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from email.utils import make_msgid
from pathlib import Path

from team.outbound import ProviderRejected
from team.smtp_transport import SmtpOutcomeUnknown, SmtpTransport, build_email

FILE = "mail-outbox.json"
TTL = timedelta(hours=1)
KEEP = timedelta(days=30)
MAX_PENDING = 5
MAX_SENT_PER_DAY = 20
MAX_BODY = 5000
_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LOCK = threading.Lock()
_SENDING = set()  # drafts being sent by this process; an "enviando" draft found on disk that is not here died with its process
_REASONS = {"autenticacao_recusada": "O servidor recusou o usuário ou a senha.", "tls_invalido": "Falha de TLS ou certificado não aceito.",
            "sem_conexao": "Não foi possível conectar ao servidor SMTP.", "remetente_recusado": "O servidor recusou o remetente.",
            "destinatario_recusado": "O servidor recusou o destinatário.", "dados_recusados": "O servidor recusou a mensagem.",
            "falha_antes_do_envio": "Falha antes do envio."}


def _now():
    return datetime.now(timezone.utc)


def digest(draft):
    """What the person approves: recipient, subject, text and Message-ID. Any difference is a different draft."""
    return hashlib.sha256(json.dumps([draft["to"], draft["subject"], draft["body"], draft["message_id"]], ensure_ascii=False).encode()).hexdigest()


def view(draft):
    """The draft as the screen and the model see it (never the credential, never the original message text)."""
    return {key: draft[key] for key in ("id", "state", "to", "subject", "body", "message_id", "hash", "expires_at", "detail") if key in draft}


class PersonalOutbox:
    def __init__(self, root, mail, clock=_now, ssl_context=None, factory=None):
        self.path, self.mail, self.clock = Path(root) / FILE, mail, clock
        self.transport = SmtpTransport(None, ssl_context=ssl_context, factory=factory)

    # -- storage (all under one lock; a restart turns a half-done send into "unknown") -------------------------------------
    def _load(self):
        try:
            drafts = json.loads(self.path.read_text(encoding="utf-8"))["drafts"]
            drafts = drafts if isinstance(drafts, dict) else {}
        except (OSError, ValueError, KeyError):
            drafts = {}
        for draft in drafts.values():
            if draft.get("state") == "enviando" and draft["id"] not in _SENDING:
                draft["state"], draft["detail"] = "desconhecida", "O envio foi interrompido; confira a pasta Enviados antes de tentar de novo."
        return drafts

    def _save(self, drafts):
        limit = (self.clock() - KEEP).isoformat()
        kept = {k: v for k, v in drafts.items() if v.get("created_at", "") >= limit}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"drafts": kept}, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)

    def _expire(self, drafts):
        now = self.clock().isoformat()
        for draft in drafts.values():
            if draft["state"] == "rascunho" and draft["expires_at"] <= now:
                draft["state"], draft["detail"] = "expirada", "O rascunho expirou sem aprovação."

    @staticmethod
    def _sent_today(drafts, now):
        since = (now - timedelta(hours=24)).isoformat()
        return sum(1 for d in drafts.values() if d["state"] in ("enviada", "enviando", "desconhecida") and d.get("decided_at", "") >= since)

    # -- model side: only this one ------------------------------------------------------------------------------------------
    def prepare(self, uid, uidvalidity, text):
        """Create an immutable draft that answers one message. Returns the draft view, or ``{"erro": ...}``."""
        if self.mail.smtp_config() is None:
            return {"erro": "O envio não está configurado. Rode scripts/mail_setup.py com --smtp-host para configurar."}
        text = (text or "").strip() if isinstance(text, str) else ""
        if not text or len(text) > MAX_BODY or _CONTROL.search(text):
            return {"erro": f"O texto da resposta deve ter entre 1 e {MAX_BODY} caracteres, sem caracteres de controle."}
        original = self.mail.original(uid, uidvalidity)
        if "erro" in original:
            return original
        config = self.mail.smtp_config()
        sender = original.get("sender") or ""
        if not _ADDRESS.match(sender) or sender == config.from_address.lower():
            return {"erro": "Não encontrei um remetente válido para responder."}
        auto = original.get("auto") or {}
        if auto.get("auto_submitted", "no") not in ("", "no") or auto.get("precedence") in ("bulk", "junk", "list", "auto_reply") \
                or "list_id" in auto or "x_assistente_origem" in auto:
            return {"erro": "Este email é automático ou de lista; não respondo a ele."}
        ids = original.get("ids") or {}
        subject = re.sub(r"\s+", " ", original.get("subject") or "").strip() or "(sem assunto)"
        if not re.match(r"(?i)^re\s*:", subject):
            subject = f"Re: {subject}"
        now = self.clock()
        draft = {"id": secrets.token_hex(8), "state": "rascunho", "to": sender, "subject": subject[:200], "body": text,
                 "message_id": make_msgid(domain=config.from_address.rsplit("@", 1)[-1]),
                 "in_reply_to": ids.get("message_id"), "references": [*(ids.get("references") or [])[-9:], *([ids["message_id"]] if ids.get("message_id") else [])][-10:],
                 "created_at": now.isoformat(), "expires_at": (now + TTL).isoformat(), "original_uid": int(uid)}
        draft["hash"] = digest(draft)
        with _LOCK:
            drafts = self._load()
            self._expire(drafts)
            if sum(1 for d in drafts.values() if d["state"] == "rascunho") >= MAX_PENDING:
                return {"erro": "Há rascunhos demais esperando aprovação. Aprove ou descarte alguns primeiro."}
            drafts[draft["id"]] = draft
            self._save(drafts)
        return view(draft)

    # -- person side: called by the web server on a click, never by a tool --------------------------------------------------
    def pending(self):
        with _LOCK:
            drafts = self._load()
            self._expire(drafts)
            return [view(d) for d in drafts.values() if d["state"] == "rascunho"]

    def get(self, draft_id):
        with _LOCK:
            draft = self._load().get(draft_id)
            return view(draft) if draft else None

    def discard(self, draft_id):
        with _LOCK:
            drafts = self._load()
            self._expire(drafts)
            draft = drafts.get(draft_id)
            if draft is None:
                return {"erro": "Rascunho não encontrado."}
            if draft["state"] == "rascunho":
                draft["state"], draft["decided_at"] = "descartada", self.clock().isoformat()
                self._save(drafts)
            return view(draft)

    def approve(self, draft_id, expected_hash):
        """Send the draft the person saw. Blocking; one attempt; the state is persisted before the network is touched."""
        with _LOCK:
            drafts = self._load()
            self._expire(drafts)
            draft = drafts.get(draft_id)
            if draft is None:
                return {"erro": "Rascunho não encontrado."}
            if draft["state"] != "rascunho":
                self._save(drafts)
                return {**view(draft), "erro": {"expirada": "O rascunho expirou. Peça de novo.", "enviada": "Este rascunho já foi enviado.",
                                                "descartada": "Este rascunho foi descartado."}.get(draft["state"], "Este rascunho não pode mais ser enviado.")}
            if not isinstance(expected_hash, str) or digest(draft) != draft["hash"] or expected_hash != draft["hash"]:
                return {"erro": "O conteúdo mudou desde que você viu o rascunho. Nada foi enviado."}
            config, secret = self.mail.smtp_config(), self.mail.secret()
            if config is None or not secret:
                return {"erro": "O envio não está configurado. Nada foi enviado."}
            if self._sent_today(drafts, self.clock()) >= MAX_SENT_PER_DAY:
                return {"erro": f"Limite de {MAX_SENT_PER_DAY} envios em 24 horas atingido. Nada foi enviado."}
            message = {"to": [draft["to"]], "subject": draft["subject"], "body": draft["body"], "message_id": draft["message_id"],
                       "in_reply_to": draft["in_reply_to"], "references": draft["references"]}
            raw = build_email(config, message, []).as_bytes()
            draft.update(state="enviando", decided_at=self.clock().isoformat())
            _SENDING.add(draft_id)
            self._save(drafts)
        try:
            self.transport._dialog(config, secret, raw, [draft["to"]], draft["message_id"])
            state, detail = "enviada", "Aceita pelo servidor de saída. Aceitar não garante entrega nem leitura."
        except ProviderRejected as error:
            state, detail = "falhou", _REASONS.get(str(error), "O servidor recusou o envio.") + " Nada foi enviado."
        except SmtpOutcomeUnknown:
            state = "desconhecida"
            detail = f"Sem resposta do servidor depois de enviar. Confira a pasta Enviados (Message-ID {draft['message_id']}) antes de tentar de novo."
        except Exception:
            state, detail = "desconhecida", "Erro inesperado durante o envio. Confira a pasta Enviados antes de tentar de novo."
        with _LOCK:
            drafts = self._load()
            current = drafts[draft_id]
            current.update(state=state, detail=detail)
            _SENDING.discard(draft_id)
            self._save(drafts)
            return view(current)
