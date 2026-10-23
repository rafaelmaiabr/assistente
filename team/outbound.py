"""Outbound communication gateway: drafts are data, sending is a separate, re-validated step.

* A draft is stored and edited freely; nothing touches a provider.
* Sending needs an exact, single-use approval (T13) and is re-checked **immediately before** the
  call: current owner and ownership version, recipient policy, outbound account and credential.
* A message is serialized per case (one in flight at a time, enforced by the database), carries a
  deduplication key that is also sent to the provider, and records the provider's identifier.
* A transfer of the case blocks the previous owner's pending messages; the new owner revalidates.
* A timeout after the call started is **unknown**, not delivered: it waits for reconciliation. The
  only claim made on success is "accepted by the provider", never "read" or "delivered".
* Notes and AI summaries never become replies: there is no path from them to a draft.
"""
import asyncio
import re
import uuid
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from .access import AccessContext, AccessError, NotFound, PermissionDenied
from .approvals import ApprovalInvalid, ApprovalRejected, normalize_request
from .attachments import AttachmentRejected
from .attachments import AttachmentRejected
from .cases import TERMINAL_STATES, clean_text
from .ids import new_id
from .repository import IntegrityViolation, RevisionConflict
from .schema import content_hash
from .worker import Done, Fail, Retry

MAX_RECIPIENTS = 5
MAX_BODY = 8000
SEND_TIMEOUT = 30
STUCK_AFTER_SECONDS = 600
ACTION = "enviar_mensagem"
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_ACCOUNT_KIND = {"email": "smtp", "chat": "outro"}


class OutboundError(ValueError):
    """The message cannot go through this step; the message is safe to show."""


class Busy(OutboundError):
    """Another message of the same case is being sent; try again shortly."""


class ProviderRejected(AccessError):
    """The provider definitively refused the message: nothing was sent. The text is a short technical reason."""


class OutboundTransport(ABC):
    @abstractmethod
    async def send(self, account: dict, message: dict, *, dedup_key: str, secret) -> dict:
        """Send and return ``{"provider_id": …}``. Raise ProviderRejected if refused; anything else means unknown."""

    async def lookup(self, account: dict, dedup_key: str):
        """Provider id of a message previously sent with this key, or None when the provider cannot tell."""
        return None


def _now_default():
    return datetime.now(timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_recipients(channel, recipients):
    cleaned = sorted({str(r).strip().lower() for r in recipients if str(r).strip()})
    if not cleaned or len(cleaned) > MAX_RECIPIENTS:
        raise OutboundError(f"Informe de 1 a {MAX_RECIPIENTS} destinatarios.")
    if channel == "email" and not all(_EMAIL.match(r) for r in cleaned):
        raise OutboundError("Destinatario com endereco de email invalido.")
    return cleaned


def message_hash(message):
    """What the approver sees and what is approved: channel, account, recipients, subject and body."""
    payload = {"case_id": message["case_id"], "channel": message["channel"], "account_id": message["account_id"],
               "to": message["to"], "subject": message.get("subject"), "body": message["body"]}
    extras = {"cc": message.get("cc") or [], "bcc": message.get("bcc") or [], "message_id": message.get("message_id"),
              "in_reply_to": message.get("in_reply_to"), "references": message.get("references") or [],
              "attachments": [(a["filename"], a["sha256"], a["size"]) for a in message.get("attachments") or []]}
    if any(extras.values()):  # messages without these parts keep the exact hash they always had
        payload["extras"] = extras
    return content_hash(payload)


def all_recipients(message):
    """Everyone who receives the message: To, Cc and Bcc (the approver sees all of them)."""
    return [*message["to"], *(message.get("cc") or []), *(message.get("bcc") or [])]


def approval_fields(message):
    """The exact arguments of the approval request; ``submit`` and ``dispatch`` must build the same ones."""
    return {"resource": message["account_id"], "recipients": all_recipients(message),
            "params": {"draft_id": message["id"], "draft_hash": message["draft_hash"], "channel": message["channel"],
                       "owner_version": message["owner_version"], "to": message["to"], "cc": message.get("cc") or [],
                       "bcc": message.get("bcc") or [], "attachments": [a["filename"] for a in message.get("attachments") or []]}}


class OutboundGateway:
    def __init__(self, repository, access, cases, approvals, connections, transports, handoffs=None,
                 clock=_now_default, send_timeout=SEND_TIMEOUT, attachments=None, message_id_domain=None):
        self.repository, self.access, self.cases, self.approvals = repository, access, cases, approvals
        self.connections, self.transports, self.clock, self.send_timeout = connections, transports, clock, send_timeout
        self.attachments = attachments  # AttachmentStore, or None while attachments are not configured
        self.message_id_domain = message_id_domain or (lambda tenant_id, account_id: "assistente.invalid")
        self._operator = access.local_operator()
        if handoffs is not None:
            handoffs.owner_hooks.append(self._block_pending)

    # -- helpers -----------------------------------------------------------------------------------------------------
    def _ctx(self, ctx):
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()

    def _tenant_ctx(self, tenant_id):
        return self.access.tenant_context(self._operator, tenant_id)

    def _message(self, session, message_id):
        message = session.get("outbound_message", message_id) if isinstance(message_id, str) else None
        if message is None:
            raise NotFound()
        return message

    def _visible(self, ctx, message):
        if ctx.role == "agent" and message["owner_agent_id"] != ctx.principal_id and message["created_by"] != ctx.principal_id:
            raise NotFound()

    def _save(self, session, message, **changes):
        return session.save({**message, **changes, "revision": message["revision"] + 1,
                             "updated_at": _stamp(self.clock())})

    def _case_note(self, ctx, session, case_id, text):
        self.cases.commit_note(ctx, session, case_id, text)

    def _check_policy(self, session, account, recipients):
        """Every recipient must be a linked contact address or belong to a domain the account allows."""
        allowed_domains = {d.lower() for d in account.get("allowed_recipient_domains") or []}
        violations = []
        for address in recipients:
            linked = any(a["link_state"] == "vinculado" for a in session.list(
                "contact_address", address_key=f"email:{address}", limit=1))
            if not linked and address.rsplit("@", 1)[-1] not in allowed_domains:
                violations.append(address)
        return violations

    def _owner_state(self, case, message):
        """Why the case no longer lets this message go out (None when it still may)."""
        if case["state"] in TERMINAL_STATES:
            return "demanda_encerrada"
        if case.get("operator_control") or case["state"] == "pausada":
            return "demanda_sob_controle_do_operador"
        if case["owner_agent_id"] != message["owner_agent_id"] or (case.get("owner_version") or 1) != message["owner_version"]:
            return "dono_mudou"
        return None

    # -- drafts ------------------------------------------------------------------------------------------------------
    def _lists(self, channel, to, cc, bcc):
        """Normalized To / Cc / Bcc: disjoint, email only for cc/bcc, at most MAX_RECIPIENTS in total."""
        primary = _normalize_recipients(channel, to)
        copies = _normalize_recipients(channel, cc) if cc else []
        hidden = _normalize_recipients(channel, bcc) if bcc else []
        if (copies or hidden) and channel != "email":
            raise OutboundError("Copia e copia oculta so existem no email.")
        everyone = [*primary, *copies, *hidden]
        if len(set(everyone)) != len(everyone):
            raise OutboundError("Um mesmo endereco aparece mais de uma vez entre Para, Cc e Cco.")
        if len(everyone) > MAX_RECIPIENTS:
            raise OutboundError(f"No maximo {MAX_RECIPIENTS} destinatarios no total (Para, Cc e Cco).")
        return primary, copies, hidden

    @staticmethod
    def _thread(session, case_id):
        """Reply headers from the newest incoming email of the case (T21), or ``(None, [])``."""
        incoming = [m for m in session.list("email_message", case_id=case_id, limit=1000)
                    if m["classification"] in ("nova_demanda", "resposta") and m.get("message_id")]
        if not incoming:
            return None, []
        last = max(incoming, key=lambda m: (m["uidvalidity"], m["uid"]))
        return last["message_id"], [*last.get("references", []), last["message_id"]][-10:]

    def create_draft(self, ctx, case_id, *, account_id, to, body, subject=None, channel="email", dedup_key=None, cc=(), bcc=(),
                     attachments=(), reply=True):
        """Store a draft. Only the current owner (or the operator for the owner) can draft; nothing is sent."""
        self._ctx(ctx)
        if channel not in _ACCOUNT_KIND:
            raise OutboundError("Canal desconhecido.")
        recipients, copies, hidden = self._lists(channel, to, cc, bcc)
        text = clean_text(body)
        if len(text) > MAX_BODY:
            raise OutboundError("Mensagem longa demais.")
        title = " ".join(clean_text(subject).split())[:200] if subject else None
        key = dedup_key or f"rascunho:{new_id('correlation')}"
        now = _stamp(self.clock())
        with self.repository.transaction(ctx.tenant_id) as session:
            for existing in session.list("outbound_message", dedup_key=key, limit=1):
                return existing, False
            case = session.get("case", case_id) if isinstance(case_id, str) else None
            if case is None:
                raise NotFound()
            if ctx.role == "agent" and (case["owner_agent_id"] != ctx.principal_id or case.get("operator_control")):
                raise NotFound()
            if case["owner_agent_id"] is None:
                raise OutboundError("A demanda ainda nao tem responsavel para falar com o cliente.")
            if case["state"] in TERMINAL_STATES:
                raise OutboundError("A demanda esta encerrada.")
            account = session.get("connection", account_id) if isinstance(account_id, str) else None
            if account is None:
                raise NotFound()
            if account["status"] != "ativo" or account["connection_kind"] != _ACCOUNT_KIND[channel]:
                raise OutboundError("Conta de saida inativa ou de tipo incompativel com o canal.")
            files = []
            if attachments:
                if channel != "email" or self.attachments is None:
                    raise OutboundError("Anexos nao estao disponiveis neste canal ou nao foram configurados.")
                try:
                    files = self.attachments.put_all(ctx.tenant_id, case_id, attachments)
                except AttachmentRejected as error:
                    raise OutboundError(str(error)) from None
            in_reply_to, references = self._thread(session, case_id) if (channel == "email" and reply) else (None, [])
            doc = {"kind": "outbound_message", "schema_version": 1, "id": new_id("outbound_message"),
                   "tenant_id": ctx.tenant_id, "revision": 1, "created_at": now, "updated_at": now, "case_id": case_id,
                   "channel": channel, "account_id": account_id, "to": recipients, "subject": title, "body": text,
                   "state": "rascunho", "dedup_key": key, "owner_agent_id": case["owner_agent_id"],
                   "owner_version": case.get("owner_version") or 1, "draft_hash": "0" * 64, "approval_id": None,
                   "provider_id": None, "sent_at": None, "attempts": 0, "last_error": None, "created_by": ctx.principal_id}
            if channel == "email":
                doc.update(cc=copies, bcc=hidden, attachments=files, in_reply_to=in_reply_to, references=references,
                           message_id=f"<{uuid.uuid4().hex}@{self.message_id_domain(ctx.tenant_id, account_id)}>")
            doc["draft_hash"] = message_hash(doc)
            try:
                return session.save(doc), True
            except IntegrityViolation:
                raise OutboundError("Chave de deduplicacao ja usada.") from None

    def update_draft(self, ctx, message_id, expected_revision, /, **changes):
        self._ctx(ctx)
        if set(changes) - {"to", "cc", "bcc", "subject", "body"}:
            raise OutboundError("Somente destinatarios, assunto e texto podem mudar.")
        with self.repository.transaction(ctx.tenant_id) as session:
            message = self._message(session, message_id)
            self._visible(ctx, message)
            if message["state"] != "rascunho":
                raise OutboundError("Somente rascunhos podem ser editados; retire do envio primeiro.")
            if message["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            updated = {**message}
            if {"to", "cc", "bcc"} & set(changes):
                updated["to"], updated["cc"], updated["bcc"] = self._lists(
                    message["channel"], changes.get("to", message["to"]), changes.get("cc", message.get("cc") or []),
                    changes.get("bcc", message.get("bcc") or []))
            if "subject" in changes:
                updated["subject"] = " ".join(clean_text(changes["subject"]).split())[:200] if changes["subject"] else None
            if "body" in changes:
                updated["body"] = clean_text(changes["body"])
                if len(updated["body"]) > MAX_BODY:
                    raise OutboundError("Mensagem longa demais.")
            extra = {k: updated[k] for k in ("cc", "bcc") if k in updated}
            return self._save(session, message, to=updated["to"], subject=updated["subject"], body=updated["body"],
                              draft_hash=message_hash(updated), **extra)

    def get_message(self, ctx, message_id):
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            message = self._message(session, message_id)
            self._visible(ctx, message)
            return message

    def list_messages(self, ctx, case_id=None, state=None):
        self._ctx(ctx)
        filters = {k: v for k, v in (("case_id", case_id), ("state", state)) if v}
        with self.repository.transaction(ctx.tenant_id) as session:
            items = session.list("outbound_message", limit=1000, **filters)
        return [m for m in items if ctx.role == "operator" or m["owner_agent_id"] == ctx.principal_id
                or m["created_by"] == ctx.principal_id]

    # -- asking for approval -----------------------------------------------------------------------------------------
    def submit(self, ctx, message_id):
        """Validate the draft against the rules that apply *now* and ask the operator to approve exactly this message."""
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            message = self._message(session, message_id)
            self._visible(ctx, message)
            if message["state"] == "aguardando_aprovacao":
                return message
            if message["state"] != "rascunho":
                raise OutboundError("Somente rascunhos podem ser enviados para aprovacao.")
            case = session.get("case", message["case_id"])
            reason = self._owner_state(case, message)
            if reason:
                raise OutboundError(f"Nao e possivel enviar agora: {reason}.")
            account = session.get("connection", message["account_id"])
            if account["status"] != "ativo":
                raise OutboundError("Conta de saida inativa.")
            violations = self._check_policy(session, account, all_recipients(message))
        if violations:
            raise OutboundError("Destinatarios fora da politica (vincule o contato ou autorize o dominio): "
                                + ", ".join(violations))
        approval, _ = self.approvals.request(
            ctx, message["case_id"], ACTION, **approval_fields(message), justification="Envio de mensagem ao cliente")
        with self.repository.transaction(ctx.tenant_id) as session:
            current = self._message(session, message_id)
            return self._save(session, current, state="aguardando_aprovacao", approval_id=approval["id"])

    def withdraw(self, ctx, message_id):
        """Take a submitted message back to draft (the open approval is revoked)."""
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            message = self._message(session, message_id)
            self._visible(ctx, message)
            if message["state"] != "aguardando_aprovacao":
                raise OutboundError("A mensagem nao esta aguardando aprovacao.")
        try:
            self.approvals.revoke(self._tenant_ctx(ctx.tenant_id), message["approval_id"], "mensagem retirada do envio")
        except ApprovalRejected:
            pass  # already decided or expired: the draft still goes back to editable
        with self.repository.transaction(ctx.tenant_id) as session:
            return self._save(session, self._message(session, message_id), state="rascunho", approval_id=None)

    def cancel(self, ctx, message_id):
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            message = self._message(session, message_id)
            self._visible(ctx, message)
            if message["state"] not in ("rascunho", "bloqueada", "aguardando_aprovacao", "falhou"):
                raise OutboundError("Esta mensagem nao pode mais ser cancelada.")
            return self._save(session, message, state="cancelada")

    # -- ownership changes -------------------------------------------------------------------------------------------
    def _block_pending(self, ctx, session, case_id):
        """Called inside the transfer: drafts and queued messages of the previous owner stop here."""
        for message in session.list("outbound_message", case_id=case_id, limit=1000):
            if message["state"] in ("rascunho", "aguardando_aprovacao"):
                self._save(session, message, state="bloqueada", last_error="dono_mudou")
                self.cases.commit_note(ctx, session, case_id,
                                       "Mensagem pendente do responsavel anterior bloqueada; o novo responsavel deve revalidar.")

    def revalidate(self, ctx, message_id):
        """The new owner adopts a blocked message: it returns to draft under the current owner, needing a new approval."""
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            message = self._message(session, message_id)
            case = session.get("case", message["case_id"])
            if message["state"] != "bloqueada":
                raise OutboundError("A mensagem nao esta bloqueada.")
            if ctx.role == "agent" and case["owner_agent_id"] != ctx.principal_id:
                raise NotFound()
            if case["state"] in TERMINAL_STATES or case.get("operator_control"):
                raise OutboundError("A demanda nao aceita mensagens agora.")
            return self._save(session, message, state="rascunho", approval_id=None, last_error=None,
                              owner_agent_id=case["owner_agent_id"], owner_version=case.get("owner_version") or 1)

    # -- sending -----------------------------------------------------------------------------------------------------
    def _block(self, tenant_id, message_id, reason, note=None):
        ctx = self._tenant_ctx(tenant_id)
        with self.repository.transaction(tenant_id) as session:
            message = self._message(session, message_id)
            blocked = self._save(session, message, state="bloqueada", last_error=reason)
            self.cases.commit_note(ctx, session, message["case_id"], note or f"Envio bloqueado: {reason}")
            return blocked

    async def dispatch(self, tenant_id, message_id):
        """Send an approved message if — and only if — everything is still true right now."""
        ctx = self._tenant_ctx(tenant_id)
        # A. validate and take the per-case sending slot
        with self.repository.transaction(tenant_id) as session:
            message = self._message(session, message_id)
            if message["state"] in ("enviada", "enviando", "desconhecido", "falhou", "cancelada", "bloqueada"):
                return message
            if message["state"] != "aguardando_aprovacao":
                raise OutboundError("A mensagem ainda e um rascunho: envie para aprovacao.")
            case = session.get("case", message["case_id"])
            approval = session.get("approval_request", message["approval_id"]) if message["approval_id"] else None
            account = session.get("connection", message["account_id"])
            policy = self._check_policy(session, account, all_recipients(message))
        reason = None
        if approval is None or approval["state"] == "pendente":
            return message  # still waiting for the operator
        if approval["state"] != "aprovada":
            reason = f"aprovacao_{approval['state']}"
        elif message_hash(message) != message["draft_hash"]:
            reason = "rascunho_alterado"
        elif self._owner_state(case, message):
            reason = self._owner_state(case, message)
        elif account["status"] != "ativo":
            reason = "conta_inativa"
        elif policy:
            reason = "destinatario_fora_da_politica"
        elif message["channel"] not in self.transports:
            reason = "canal_sem_transporte"
        if reason:
            return self._block(tenant_id, message_id, reason)
        with self.repository.transaction(tenant_id) as session:
            current = self._message(session, message_id)
            if current["state"] != "aguardando_aprovacao":
                return current  # somebody else got here first
            try:
                self._save(session, current, state="enviando", attempts=current["attempts"] + 1, last_error=None)
            except IntegrityViolation:
                raise Busy("Ha outra mensagem desta demanda em envio; tente novamente.") from None
        # B. spend the exact approval
        normalized = normalize_request(tenant_id, message["case_id"], ACTION, **approval_fields(message))
        try:
            self.approvals.consume(ctx, approval["id"], normalized)
        except ApprovalInvalid:
            return self._block(tenant_id, message_id, "aprovacao_invalida")
        # C. last look at the owner immediately before the call
        with self.repository.transaction(tenant_id) as session:
            case = session.get("case", message["case_id"])
        reason = self._owner_state(case, message)
        if reason:
            self.approvals.record_effect(ctx, approval["id"], "falhou", "nao enviada: " + reason)
            return self._block(tenant_id, message_id, reason)
        # D. call the provider; the outcome of a timeout is unknown, not "sent"
        return await self._send(tenant_id, ctx, message, account, approval)

    async def _send(self, tenant_id, ctx, message, account, approval):
        invoked = {"flag": False}

        payload = {k: message.get(k) for k in ("to", "cc", "bcc", "subject", "body", "channel", "message_id", "in_reply_to",
                                               "references", "attachments", "case_id")}

        async def call(secret):
            invoked["flag"] = True
            return await asyncio.wait_for(
                self.transports[message["channel"]].send(account, payload, dedup_key=message["dedup_key"], secret=secret),
                self.send_timeout)

        outcome, provider_id, error, extra = "falhou", None, "nao_enviada", {}
        try:
            reply = await self.connections.use_credential_async(
                ctx, account["id"], account["provider"], call, optional=True)
            provider_id = reply["provider_id"]
            extra = {"provider_result": (str(reply["provider_result"])[:200] if reply.get("provider_result") else None),
                     "simulated": bool(reply.get("simulated"))}
            outcome, error = "enviada", None
        except ProviderRejected as rejected:
            reason = re.sub(r"[^a-z0-9_]+", "_", str(rejected).lower())[:60].strip("_")
            error = "recusada_pelo_provedor" + (f":{reason}" if reason else "")
        except asyncio.CancelledError:
            self._finish(tenant_id, message["id"], approval["id"], "desconhecido", None, "cancelado_durante_o_envio")
            raise
        except (KeyError, TypeError):
            outcome, error = "desconhecido", "resposta_invalida_do_provedor"
        except Exception:
            # Without proof the call never left, assume it may have: unknown, to be reconciled.
            outcome, error = ("desconhecido", "sem_resposta_do_provedor") if invoked["flag"] else ("falhou", "credencial_ou_conta_indisponivel")
        return self._finish(tenant_id, message["id"], approval["id"], outcome, provider_id, error, **extra)

    def _finish(self, tenant_id, message_id, approval_id, outcome, provider_id, error, provider_result=None, simulated=False):
        ctx = self._tenant_ctx(tenant_id)
        now = _stamp(self.clock())
        with self.repository.transaction(tenant_id) as session:
            message = self._message(session, message_id)
            if outcome == "enviada":
                extra = {k: v for k, v in (("provider_result", provider_result), ("simulated", True if simulated else None)) if v is not None}
                saved = self._save(session, message, state="enviada", provider_id=provider_id, sent_at=now, last_error=None, **extra)
                session.save({"kind": "case_entry", "schema_version": 1, "id": new_id("case_entry"),
                              "tenant_id": tenant_id, "revision": 1, "created_at": now, "updated_at": now,
                              "case_id": message["case_id"], "entry_type": "mensagem_externa", "direction": "saida",
                              "channel": message["channel"], "author": message["owner_agent_id"],
                              "body": ("[ENVIO SIMULADO - nada saiu do computador] " if simulated else "") + message["body"]})
            else:
                saved = self._save(session, message, state=outcome, last_error=error)
                self.cases.commit_note(ctx, session, message["case_id"],
                                       "Envio sem confirmacao: aguarda reconciliacao" if outcome == "desconhecido"
                                       else f"Envio nao realizado: {error}")
        effect = {"enviada": "confirmado", "desconhecido": "desconhecido"}.get(outcome, "falhou")
        try:
            self.approvals.record_effect(ctx, approval_id, effect, error or f"aceita pelo provedor ({provider_id})")
        except (ApprovalRejected, AccessError):
            pass
        return saved

    def waiting_for_approval(self, tenant_id, message_id):
        """True while the operator has not decided yet (the worker should come back later)."""
        with self.repository.transaction(tenant_id) as session:
            message = self._message(session, message_id)
            approval = session.get("approval_request", message["approval_id"]) if message["approval_id"] else None
        return message["state"] == "aguardando_aprovacao" and approval is not None and approval["state"] == "pendente"

    # -- unknown outcomes ---------------------------------------------------------------------------------------------
    def mark_stuck(self, tenant_id):
        """Messages left ``enviando`` too long (a worker died mid-send) become unknown."""
        limit = self.clock() - timedelta(seconds=STUCK_AFTER_SECONDS)
        moved = []
        with self.repository.transaction(tenant_id) as session:
            for message in session.list("outbound_message", state="enviando", limit=1000):
                if datetime.fromisoformat(message["updated_at"].replace("Z", "+00:00")) <= limit:
                    moved.append(self._save(session, message, state="desconhecido", last_error="envio_interrompido"))
        return moved

    def needing_reconciliation(self, ctx):
        self._ctx(ctx)
        if ctx.role != "operator":
            raise PermissionDenied()
        return self.list_messages(ctx, state="desconhecido")

    async def auto_reconcile(self, tenant_id, message_id):
        """Ask the provider whether it has a message with this deduplication key (absence proves nothing)."""
        with self.repository.transaction(tenant_id) as session:
            message = self._message(session, message_id)
            account = session.get("connection", message["account_id"])
        if message["state"] != "desconhecido":
            raise OutboundError("A mensagem nao esta em reconciliacao.")
        found = await self.transports[message["channel"]].lookup(account, message["dedup_key"])
        if found is None:
            return message
        return self._resolve(tenant_id, message_id, "enviada", found, "confirmado pelo provedor")

    def resolve_unknown(self, ctx, message_id, outcome, provider_id=None, detail=None):
        """The operator checked the provider: ``enviada`` (needs the provider id) or ``nao_enviada``."""
        self._ctx(ctx)
        if not self.access.is_operator(ctx, "aprovador"):
            raise PermissionDenied()
        if outcome not in ("enviada", "nao_enviada"):
            raise OutboundError("Informe enviada ou nao_enviada.")
        if outcome == "enviada" and not provider_id:
            raise OutboundError("Informe o identificador do provedor para confirmar o envio.")
        return self._resolve(ctx.tenant_id, message_id, outcome, provider_id, detail or "verificado pelo operador")

    def _resolve(self, tenant_id, message_id, outcome, provider_id, detail):
        ctx = self._tenant_ctx(tenant_id)
        with self.repository.transaction(tenant_id) as session:
            message = self._message(session, message_id)
            if message["state"] != "desconhecido":
                raise OutboundError("A mensagem nao esta em reconciliacao.")
        saved = self._finish(tenant_id, message_id, message["approval_id"], "enviada" if outcome == "enviada" else "falhou",
                             provider_id, None if outcome == "enviada" else "confirmada_como_nao_enviada")
        with self.repository.transaction(tenant_id) as session:
            self.cases.commit_note(ctx, session, message["case_id"], f"Reconciliacao do envio: {outcome} ({detail})")
        return saved


def outbound_handler(gateway):
    """Queue handler for ``enviar_mensagem`` jobs. Payload: ``message_id``."""

    async def handler(job, control):
        tenant_id, message_id = job["tenant_id"], job["payload"]["message_id"]
        if gateway.waiting_for_approval(tenant_id, message_id):
            return Retry("aguardando_aprovacao")  # nothing started yet: no effect is opened
        key = f"enviar:{message_id}"
        try:
            control.begin_effect(key)
        except Exception as error:  # confirmed by an earlier attempt: do not repeat; unconfirmed: reconcile first
            result = getattr(error, "result", None)
            return Done(result) if result is not None else Fail("efeito_em_reconciliacao")
        try:
            message = await gateway.dispatch(tenant_id, message_id)
        except Busy:
            control.cancel_effect(key)  # nothing left the process: the effect never started
            return Retry("outra_mensagem_em_envio")
        if message["state"] == "enviada":
            control.finish_effect(key, {"provider_id": message["provider_id"]})
            return Done({"provider_id": message["provider_id"]})
        return Fail(message["state"])

    return handler
