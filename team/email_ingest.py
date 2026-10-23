"""Incoming email as cases: manual ingestion from an authorized mailbox folder, with a per-folder cursor,
header-based threading, unverified sender identity and explicit loop / auto-reply rules.

* **Cursor**: ``(UIDVALIDITY, last UID)`` per connection and folder. A new UIDVALIDITY resets the UID cursor, and
  nothing is ingested twice because every message is also deduplicated by ``Message-ID`` per connection.
* **Atomic**: a message, its case, entry, evidence, source record and the cursor commit in one transaction, so a crash
  between messages leaves the cursor at the last complete one and a reprocessed message changes nothing.
* **Threading** uses ``In-Reply-To`` / ``References`` only, and only attaches a reply to an existing case when the
  sender already took part in that conversation. The subject never joins conversations: two customers writing
  "Fatura" get two cases. Forged or unconfirmable headers open a new case flagged for review.
* **Identity**: the sender of an email is *not verified* (no SPF/DKIM/DMARC check is made here). A sender whose
  address is a linked contact is marked ``contato_vinculado``, which is still not authentication.
* **Loops and auto-replies** (explicit rule, see ``ignore_reason``): own mailbox, our own marker header,
  ``Auto-Submitted`` other than ``no``, ``Precedence`` bulk/junk/list/auto_reply, ``X-Autoreply``/``X-Autorespond``,
  ``X-Auto-Response-Suppress``, ``List-Id``, empty ``Return-Path``, postmaster/mailer-daemon senders and common
  out-of-office / bounce subjects. Ignored messages are recorded but never open a case.
* The text is untrusted data: secret-looking strings are masked, length is bounded and instruction-like text is flagged
  in the evidence. Every case entry traces back to the source message (folder, UIDVALIDITY, UID, Message-ID).
"""
import re
from datetime import datetime, timezone

from .access import AccessContext, NotFound, PermissionDenied
from .cases import clean_text
from .ids import new_id
from .imap_client import ImapError, ReadOnlyImap
from .repository import address_key
from .schema import content_hash, redact_secrets
from .triage import looks_like_injection

BODY_LIMIT = 8000
MAX_BATCH = 50
_AUTO_SUBJECT = re.compile(r"^\s*((re|res|enc|fwd?)\s*:\s*)*(out of office|resposta autom|automatic reply|auto[- ]?reply|autoreply|"
                           r"ausente|fora do escrit|delivery status|undeliverable|mail delivery failed|returned mail|"
                           r"falha na entrega|mensagem nao entregue)", re.I)
_SYSTEM_SENDERS = {"mailer-daemon", "postmaster"}


def _mask(value, secret):
    """Replace the mailbox password wherever a message happens to contain it (the vault must never see it come back)."""
    if isinstance(value, str):
        return value.replace(secret, "***") if secret else value
    if isinstance(value, list):
        return [_mask(v, secret) for v in value]
    if isinstance(value, dict):
        return {k: _mask(v, secret) for k, v in value.items()}
    return value


class EmailIngestRejected(ValueError):
    """The request is not valid or the mailbox is not set up; the message is safe to show."""


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ignore_reason(parsed, own_address):
    """``(classification, reason)`` when the message must not open or extend a case, else ``None``."""
    auto, sender = parsed.get("auto") or {}, parsed.get("sender") or ""
    if own_address and sender == own_address.lower():
        return "ignorada_loop", "remetente e a propria caixa monitorada"
    if "x_assistente_origem" in auto:
        return "ignorada_loop", "mensagem marcada como enviada por este sistema"
    submitted = auto.get("auto_submitted")
    if submitted and submitted != "no":
        return "ignorada_autoresposta", f"Auto-Submitted: {submitted}"
    if auto.get("precedence") in ("bulk", "junk", "list", "auto_reply"):
        return "ignorada_autoresposta", f"Precedence: {auto['precedence']}"
    for header in ("x_autoreply", "x_autorespond", "x_auto_response_suppress", "list_id"):
        if header in auto:
            return "ignorada_autoresposta", header.replace("_", "-")
    if auto.get("return_path") == "<>":
        return "ignorada_autoresposta", "Return-Path vazio (devolucao automatica)"
    if sender.split("@")[0] in _SYSTEM_SENDERS:
        return "ignorada_autoresposta", "remetente de sistema"
    if _AUTO_SUBJECT.match(parsed.get("subject") or ""):
        return "ignorada_autoresposta", "assunto de resposta automatica"
    return None


class EmailIngest:
    def __init__(self, repository, access, cases, connections, accounts, clock=lambda: datetime.now(timezone.utc),
                 ssl_context=None, factory=None):
        self.repository, self.access, self.cases, self.connections, self.accounts = repository, access, cases, connections, accounts
        self.clock, self.ssl_context, self.factory = clock, ssl_context, factory

    def _operator(self, ctx):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied()

    # -- setup ---------------------------------------------------------------------------------------------------
    def bind_mailbox(self, ctx, connection_id, folder, department_id):
        """Bind one authorized folder of an approved ``imap`` connection to the department that receives its cases."""
        self._operator(ctx)
        config = self.accounts.get(ctx.tenant_id, connection_id)
        if config is None:
            raise EmailIngestRejected("Configure servidor, usuario e pastas desta conexao antes de vincular a caixa.")
        if folder not in config.folders:
            raise EmailIngestRejected("Esta pasta nao esta entre as pastas autorizadas da conta.")
        now = _stamp(self.clock())
        with self.repository.transaction(ctx.tenant_id) as session:
            connection = session.get("connection", connection_id)
            if connection is None or connection["provider"] != "imap" or connection["status"] != "ativo":
                raise NotFound()
            if session.get("department", department_id) is None:
                raise NotFound()
            if [c for c in session.list("mailbox_cursor", folder=folder, limit=100) if c["connection_id"] == connection_id]:
                raise EmailIngestRejected("Esta pasta ja esta vinculada.")
            return session.save({"kind": "mailbox_cursor", "schema_version": 1, "id": new_id("mailbox_cursor"), "tenant_id": ctx.tenant_id,
                                 "revision": 1, "created_at": now, "updated_at": now, "connection_id": connection_id,
                                 "department_id": department_id, "folder": folder, "uidvalidity": None, "last_uid": 0, "resets": 0,
                                 "status": "ativo"})

    def _cursor(self, session, connection_id, folder):
        found = [c for c in session.list("mailbox_cursor", folder=folder, status="ativo", limit=100) if c["connection_id"] == connection_id]
        if not found:
            raise EmailIngestRejected("Vincule a pasta a um departamento antes de ingerir.")
        return found[0]

    # -- ingestion -----------------------------------------------------------------------------------------------
    def ingest(self, ctx, connection_id, folder, *, limit=20):
        """Fetch the next messages after the cursor and turn them into cases. Manual: nothing runs in the background."""
        self._operator(ctx)
        limit = max(1, min(int(limit), MAX_BATCH))
        with self.repository.transaction(ctx.tenant_id) as session:
            cursor = self._cursor(session, connection_id, folder)
        config = self.accounts.get(ctx.tenant_id, connection_id)
        if config is None or folder not in config.folders:
            raise EmailIngestRejected("A conta ou a pasta nao esta autorizada.")

        def fetch(secret):
            try:
                with ReadOnlyImap(config, secret, ssl_context=self.ssl_context, factory=self.factory) as imap:
                    validity = imap.validity(folder)
                    after = cursor["last_uid"] if cursor["uidvalidity"] == validity else 0
                    _, uids = imap.uids_after(folder, after, limit)
                    messages = [_mask(imap.read_message(folder, uid, validity), secret) for uid in uids]
                    return {"ok": True, "validity": validity, "messages": messages}
            except ImapError as error:
                return {"ok": False, "erro": error.code, "mensagem": error.message}

        result = self.connections.use_credential(ctx, connection_id, "imap", fetch)
        if not result["ok"]:
            return {"processadas": 0, "por_classificacao": {}, "erro": result["erro"], "mensagem": result["mensagem"], "parou_em": None}
        validity, reset = result["validity"], False
        if cursor["uidvalidity"] is not None and cursor["uidvalidity"] != validity:
            reset = True
        if cursor["uidvalidity"] != validity:
            with self.repository.transaction(ctx.tenant_id) as session:
                cursor = session.save({**cursor, "uidvalidity": validity, "last_uid": 0, "resets": cursor["resets"] + (1 if reset else 0),
                                       "revision": cursor["revision"] + 1, "updated_at": _stamp(self.clock())})
        counts, stopped = {}, None
        for parsed in result["messages"]:
            try:
                classification = self._process(ctx, connection_id, cursor["id"], folder, validity, parsed, config.username)
            except Exception as error:  # the message rolled back; earlier ones stay committed and the cursor did not move
                stopped = {"uid": parsed["uid"], "motivo": type(error).__name__}
                break
            counts[classification] = counts.get(classification, 0) + 1
        with self.repository.transaction(ctx.tenant_id) as session:
            cursor = session.get("mailbox_cursor", cursor["id"])
        return {"processadas": sum(counts.values()), "por_classificacao": counts, "erro": None, "parou_em": stopped,
                "uidvalidity": validity, "uidvalidity_reiniciada": reset, "ultimo_uid": cursor["last_uid"]}

    def _process(self, ctx, connection_id, cursor_id, folder, validity, parsed, own_address):
        tenant, uid, ids = ctx.tenant_id, parsed["uid"], parsed["ids"]
        sender = parsed["sender"] or "desconhecido"
        with self.repository.transaction(tenant) as session:
            cursor = session.get("mailbox_cursor", cursor_id)

            def advance():
                nonlocal cursor
                cursor = session.save({**cursor, "uidvalidity": validity, "last_uid": max(cursor["last_uid"], uid),
                                       "revision": cursor["revision"] + 1, "updated_at": _stamp(self.clock())})

            same = [m for m in session.list("email_message", folder=folder, uidvalidity=validity, uid=uid, limit=20)
                    if m["connection_id"] == connection_id]
            if same:
                advance()
                return "ja_processada"
            identity = self._identity(session, sender)
            base = dict(connection_id=connection_id, folder=folder, validity=validity, uid=uid, parsed=parsed, identity=identity)
            mid = ids["message_id"]
            if mid and [m for m in session.list("email_message", message_id=mid, limit=50)
                        if m["connection_id"] == connection_id and m["classification"] != "duplicada"]:
                self._record(session, ctx, classification="duplicada", case_id=None, reason="Message-ID ja ingerido", **base)
                advance()
                return "duplicada"
            ignored = ignore_reason(parsed, own_address)
            if ignored:
                self._record(session, ctx, classification=ignored[0], case_id=None, reason=ignored[1], **base)
                advance()
                return ignored[0]
            classification, case_id, reason = self._thread(session, connection_id, sender, ids)
            body = self._body(parsed)
            if case_id is None:
                case = self.cases.open_case_in(ctx, session, department_id=cursor["department_id"], customer_ref=f"email:{sender}"[:300],
                                               objective=f"E-mail: {parsed['subject'] or '(sem assunto)'}"[:500], origin="email")
                case_id = case["id"]
            self.cases.add_entry_in(ctx, session, case_id, "mensagem_externa", body, "entrada", "email")
            self.cases.add_evidence_in(
                ctx, session, case_id, source=f"email:{folder}/{validity}/{uid}", summary=self._evidence(parsed, classification, reason, identity),
                evidence_type="fato", reliability="declarada", content_hash=content_hash({"b": parsed["text"]}))
            self._record(session, ctx, classification=classification, case_id=case_id, reason=reason, **base)
            advance()
            return classification

    def _identity(self, session, sender):
        linked = [a for a in session.list("contact_address", address_key=address_key("email", sender), limit=10) if a["link_state"] == "vinculado"]
        return "contato_vinculado" if linked else "nao_verificada"

    def _thread(self, session, connection_id, sender, ids):
        """Attach to an existing case only through headers and only when the sender took part in that conversation."""
        refs = list(dict.fromkeys(([ids["in_reply_to"]] if ids["in_reply_to"] else []) + ids["references"]))[-10:]
        if not refs:
            return "nova_demanda", None, None
        parents = [m for rid in refs for m in session.list("email_message", message_id=rid, limit=20)
                   if m["connection_id"] == connection_id and m["case_id"]]
        case_ids = {m["case_id"] for m in parents}
        if not case_ids:
            return "nova_demanda", None, "resposta a mensagem desconhecida; aberta como nova demanda"
        if len(case_ids) > 1:
            return "resposta_nao_confirmada", None, "as referencias apontam para varias demandas; revisar manualmente"
        case_id = next(iter(case_ids))
        participants = set()
        for m in session.list("email_message", case_id=case_id, limit=1000):
            participants.add(m["sender"])
            participants.update(m["recipients"])
        if sender not in participants:
            return "resposta_nao_confirmada", None, "remetente fora da conversa referenciada; cabecalhos nao confirmados"
        return "resposta", case_id, None

    @staticmethod
    def _body(parsed):
        header = (f"De: {parsed['sender'] or 'desconhecido'} ({parsed['sender_name'] or 'sem nome'}) - remetente nao verificado\n"
                  f"Para: {', '.join(parsed['recipients'][:5]) or '-'}\nAssunto: {parsed['subject'] or '(sem assunto)'}\n\n")
        text = redact_secrets(parsed["text"]).strip() or "(mensagem sem texto)"
        return clean_text((header + text)[:BODY_LIMIT])

    @staticmethod
    def _evidence(parsed, classification, reason, identity):
        ids = parsed["ids"]
        attachments = parsed["attachments"]
        flags = []
        if looks_like_injection(parsed["subject"] + " " + parsed["text"]):
            flags.append("texto com instrucoes suspeitas: tratado apenas como dado")
        if parsed["truncated"]:
            flags.append("conteudo truncado")
        if reason:
            flags.append(reason)
        return clean_text(
            f"Mensagem de email recebida (identidade {identity.replace('_', ' ')}; nao autenticada por SPF/DKIM). "
            f"Message-ID: {ids['message_id'] or 'ausente'}; In-Reply-To: {ids['in_reply_to'] or '-'}; "
            f"References: {', '.join(ids['references'][:5]) or '-'}. Anexos: {len(attachments)}"
            f"{' (' + ', '.join(sorted({a['content_type'] for a in attachments})[:5]) + ')' if attachments else ''}. "
            f"Classificacao: {classification}. {' '.join(flags)}")[:2000]

    def _record(self, session, ctx, *, classification, case_id, reason, connection_id, folder, validity, uid, parsed, identity):
        now, ids = _stamp(self.clock()), parsed["ids"]
        session.save({"kind": "email_message", "schema_version": 1, "id": new_id("email_message"), "tenant_id": ctx.tenant_id, "revision": 1,
                      "created_at": now, "updated_at": now, "connection_id": connection_id, "folder": folder, "uidvalidity": validity,
                      "uid": uid, "message_id": ids["message_id"], "in_reply_to": ids["in_reply_to"], "references": ids["references"][:20],
                      "sender": (parsed["sender"] or "desconhecido")[:300], "sender_name": parsed["sender_name"],
                      "recipients": parsed["recipients"], "classification": classification, "case_id": case_id, "identity": identity,
                      "subject_hash": content_hash({"s": parsed["subject"]}), "body_hash": content_hash({"b": parsed["text"]}),
                      "reason": (reason or None) and reason[:300]})

    # -- traceability --------------------------------------------------------------------------------------------
    def sources_for_case(self, ctx, case_id):
        """The source messages behind a case: where each entry came from, without any message text."""
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            if session.get("case", case_id) is None:
                raise NotFound()
            rows = sorted(session.list("email_message", case_id=case_id, limit=1000), key=lambda m: (m["folder"], m["uidvalidity"], m["uid"]))
        return [{"pasta": m["folder"], "uidvalidity": m["uidvalidity"], "uid": m["uid"], "message_id": m["message_id"],
                 "in_reply_to": m["in_reply_to"], "classificacao": m["classification"], "identidade": m["identity"],
                 "remetente": m["sender"], "recebida_em_registro": m["created_at"]} for m in rows]
