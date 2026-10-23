"""Human approvals bound to one exact action, and operator control of a case.

An approval authorizes **one** execution of **one** normalized payload (action, company, case,
resource, recipient, amount, currency and parameters, hashed). Changing any of those makes the hash
differ and the approval useless; using it once consumes it; time, a revoke or a change of owner voids it.
Only the trusted local operator can approve, and only after confirming the hash that was shown. Text
from a customer, an email, a tool or a model has no path to this decision.

An effect that was released but whose outcome is unknown stays visible for reconciliation. Nothing
here can undo a payment or a sent message: corrections are new, separately approved actions.
"""
import re
from datetime import datetime, timedelta, timezone

from .access import AccessContext, AccessError, NotFound, PermissionDenied
from .cases import TERMINAL_STATES, CaseRejected, clean_text
from .ids import new_id
from .repository import IntegrityViolation
from .schema import content_hash, find_secrets

POLICY_REF = "aprovacao-padrao"
POLICY_VERSION = "aprovacao-1"
DEFAULT_TTL = 86_400
MAX_TTL = 7 * 86_400
MAX_PARAMS = 4096
# Everything outside this short list needs approval: external writes, renewals and billing included (fail closed).
READ_ONLY_ACTIONS = frozenset({"consultar", "ler", "listar", "buscar"})
_SLUG = re.compile(r"^[a-z0-9][a-z0-9:._~-]{0,95}$")
_CURRENCY = re.compile(r"^[a-z]{3}$")


class ApprovalRejected(ValueError):
    """The request or decision is not allowed; the message is safe to show."""


class ApprovalInvalid(AccessError):
    """The approval cannot authorize this execution (wrong payload, used, expired, revoked or stale)."""

    def __init__(self):
        super().__init__("aprovacao invalida para esta execucao")


def requires_approval(action: str) -> bool:
    return action not in READ_ONLY_ACTIONS


def _now_default():
    return datetime.now(timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_request(tenant_id, case_id, action, *, resource=None, recipients=(), amount_cents=None, currency=None,
                      params=None):
    """The canonical description that is shown to the approver and hashed. Same input, same hash."""
    if not (isinstance(action, str) and _SLUG.match(action)):
        raise ApprovalRejected("Acao invalida.")
    emails = sorted({str(r).strip().lower() for r in (recipients or ()) if str(r).strip()})
    if amount_cents is not None and (isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents < 0):
        raise ApprovalRejected("Valor invalido (use centavos inteiros).")
    if (amount_cents is None) != (currency is None):
        raise ApprovalRejected("Valor e moeda devem ser informados juntos.")
    if currency is not None and not (isinstance(currency, str) and _CURRENCY.match(currency.lower())):
        raise ApprovalRejected("Moeda invalida.")
    params = {} if params is None else params
    if not isinstance(params, dict) or len(str(params)) > MAX_PARAMS:
        raise ApprovalRejected("Parametros invalidos ou grandes demais.")
    if find_secrets(params):
        raise ApprovalRejected("Parametros nao podem conter segredos.")
    return {"action": action, "tenant_id": tenant_id, "case_id": case_id, "resource": resource,
            "recipients": emails, "amount_cents": amount_cents, "currency": currency.lower() if currency else None,
            "params": params}


class ApprovalService:
    def __init__(self, repository, access, cases, clock=_now_default):
        self.repository, self.access, self.cases, self.clock = repository, access, cases, clock

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _ctx(self, ctx):
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()

    def _trusted_operator(self, ctx):
        """Only the single trusted local operator decides; agents and any derived context cannot."""
        self._ctx(ctx)
        if ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied("apenas o operador confiavel decide aprovacoes")

    def _case(self, session, case_id):
        case = session.get("case", case_id) if isinstance(case_id, str) else None
        if case is None:
            raise NotFound()
        return case

    def _approval(self, session, approval_id):
        approval = session.get("approval_request", approval_id) if isinstance(approval_id, str) else None
        if approval is None:
            raise NotFound()
        return approval

    def _save(self, session, approval, **changes):
        return session.save({**approval, **changes, "revision": approval["revision"] + 1,
                             "updated_at": _stamp(self.clock())})

    def _note(self, ctx, session, case_id, text):
        now = _stamp(self.clock())
        session.save({"kind": "case_entry", "schema_version": 1, "id": new_id("case_entry"), "tenant_id": ctx.tenant_id,
                      "revision": 1, "created_at": now, "updated_at": now, "case_id": case_id,
                      "entry_type": "nota_interna", "direction": None, "channel": None,
                      "author": ctx.principal_id, "body": clean_text(text)[:2000]})

    def _settle_case(self, ctx, session, case, event_type, payload):
        """Back to work once no approval is pending; the case revision moves with every approval event."""
        pending = [a for a in session.list("approval_request", case_id=case["id"], state="pendente", limit=1000)]
        changes = {}
        if case["state"] == "aguardando_aprovacao" and not pending:
            changes["state"] = "em_execucao"
        return self.cases.commit_change(ctx, session, case, changes, event_type, payload)

    # -- requesting -----------------------------------------------------------------------------------------------
    def request(self, ctx, case_id, action, *, resource=None, recipients=(), amount_cents=None, currency=None,
                params=None, ttl_seconds=DEFAULT_TTL, justification=None):
        """The case owner (or the operator) asks for approval of one exact action. Idempotent per payload."""
        self._ctx(ctx)
        if not requires_approval(action):
            raise ApprovalRejected("Esta acao somente de leitura nao exige aprovacao.")
        if not (isinstance(ttl_seconds, int) and 60 <= ttl_seconds <= MAX_TTL):
            raise ApprovalRejected("Validade deve ficar entre 1 minuto e 7 dias.")
        reason = clean_text(justification) if justification else None
        with self.repository.transaction(ctx.tenant_id) as session:
            case = self._case(session, case_id)
            if ctx.role == "agent" and (case["owner_agent_id"] != ctx.principal_id or case.get("operator_control")):
                raise NotFound()
            if case["state"] in TERMINAL_STATES:
                raise ApprovalRejected("A demanda esta encerrada.")
            normalized = normalize_request(ctx.tenant_id, case_id, action, resource=resource, recipients=recipients,
                                           amount_cents=amount_cents, currency=currency, params=params)
            digest = content_hash(normalized)
            for existing in session.list("approval_request", case_id=case_id, limit=1000):
                if existing["payload_hash"] == digest and existing["state"] in ("pendente", "aprovada"):
                    return existing, False
            now = self.clock()
            doc = {"kind": "approval_request", "schema_version": 1, "id": new_id("approval_request"),
                   "tenant_id": ctx.tenant_id, "revision": 1, "created_at": _stamp(now), "updated_at": _stamp(now),
                   "case_id": case_id, "requested_by": ctx.principal_id, "action": action, "payload_hash": digest,
                   "expires_at": _stamp(now + timedelta(seconds=ttl_seconds)), "state": "pendente", "single_use": True,
                   "decided_by": None, "resource": resource, "recipient": {"to": normalized["recipients"]},
                   "amount_cents": amount_cents, "currency": normalized["currency"], "normalized_action": normalized,
                   "policy_ref": POLICY_VERSION, "decided_at": None, "consumed_at": None, "reason": reason,
                   "effect_outcome": None, "effect_detail": None}
            try:
                session.save(doc)
            except IntegrityViolation:
                raise ApprovalRejected("Ja existe uma solicitacao aberta para este mesmo payload.") from None
            changes = {"state": "aguardando_aprovacao"} if case["state"] == "em_execucao" else {}
            self.cases.commit_change(ctx, session, case, changes, "approval.requested",
                                     {"approval_id": doc["id"], "action": action, "payload_hash": digest})
            return doc, True

    # -- deciding (trusted operator only) -------------------------------------------------------------------------
    def _decide(self, ctx, approval_id, state, event_type, comment):
        with self.repository.transaction(ctx.tenant_id) as session:
            approval = self._approval(session, approval_id)
            case = self._case(session, approval["case_id"])
            if approval["state"] != "pendente":
                raise ApprovalRejected("Esta solicitacao ja foi decidida ou encerrada.")
            expired = _parse(approval["expires_at"]) <= self.clock()
            if expired:
                self._save(session, approval, state="expirada", decided_at=_stamp(self.clock()))
                self._settle_case(ctx, session, case, "approval.expired", {"approval_id": approval_id})
                message = "A solicitacao expirou; peca uma nova."
            else:
                message = None
                decided = self._save(session, approval, state=state, decided_by=ctx.principal_id,
                                     decided_at=_stamp(self.clock()), reason=comment or approval.get("reason"))
                self._note(ctx, session, case["id"], f"Aprovacao {state}: {approval['action']}"
                           + (f" ({comment})" if comment else ""))
                self._settle_case(ctx, session, case, event_type, {"approval_id": approval_id, "state": state})
                return decided
        raise ApprovalRejected(message)

    def approve(self, ctx, approval_id, shown_hash, comment=None):
        """Approve exactly what was shown: ``shown_hash`` must equal the request's payload hash."""
        self._trusted_operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            approval = self._approval(session, approval_id)
        if shown_hash != approval["payload_hash"]:
            raise ApprovalRejected("O conteudo exibido nao corresponde a esta solicitacao.")
        return self._decide(ctx, approval_id, "aprovada", "approval.approved", clean_text(comment) if comment else None)

    def deny(self, ctx, approval_id, reason):
        self._trusted_operator(ctx)
        return self._decide(ctx, approval_id, "negada", "approval.denied", clean_text(reason))

    def revoke(self, ctx, approval_id, reason):
        """Withdraw a pending or approved request that was not consumed yet."""
        self._trusted_operator(ctx)
        text = clean_text(reason)
        with self.repository.transaction(ctx.tenant_id) as session:
            approval = self._approval(session, approval_id)
            if approval["state"] not in ("pendente", "aprovada"):
                raise ApprovalRejected("Somente solicitacoes ainda nao consumidas podem ser revogadas.")
            case = self._case(session, approval["case_id"])
            revoked = self._save(session, approval, state="cancelada", reason=text)
            self._note(ctx, session, case["id"], f"Aprovacao revogada: {approval['action']} ({text})")
            self._settle_case(ctx, session, case, "approval.revoked", {"approval_id": approval_id})
            return revoked

    def find_approved(self, ctx, case_id, normalized):
        """The approved, unconsumed, unexpired approval for exactly this payload, if any (read-only lookup)."""
        self._ctx(ctx)
        digest = content_hash(normalized)
        with self.repository.transaction(ctx.tenant_id) as session:
            for approval in session.list("approval_request", case_id=case_id, state="aprovada", limit=1000):
                if approval["payload_hash"] == digest and _parse(approval["expires_at"]) > self.clock():
                    return approval
        return None

    def expire_due(self, tenant_id, now=None):
        ctx = self.access.tenant_context(self.access.local_operator(), tenant_id)
        limit, expired = now or self.clock(), []
        with self.repository.transaction(tenant_id) as session:
            for approval in session.list("approval_request", limit=1000):
                if approval["state"] in ("pendente", "aprovada") and _parse(approval["expires_at"]) <= limit:
                    case = self._case(session, approval["case_id"])
                    expired.append(self._save(session, approval, state="expirada"))
                    self._settle_case(ctx, session, case, "approval.expired", {"approval_id": approval["id"]})
        return expired

    # -- using (backend / gateway) ----------------------------------------------------------------------------------
    def consume(self, ctx, approval_id, request: dict):
        """Spend the approval for one execution. ``request`` holds the same fields as ``normalize_request``.

        Atomic: of two simultaneous attempts only one succeeds. Every failure raises the same
        ``ApprovalInvalid`` so a caller learns nothing about why or about other approvals.
        """
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            approval = session.get("approval_request", approval_id) if isinstance(approval_id, str) else None
            if approval is None or approval["state"] != "aprovada":
                raise ApprovalInvalid()
            case = session.get("case", approval["case_id"])
            try:
                normalized = normalize_request(ctx.tenant_id, request.get("case_id"), request["action"],
                                               resource=request.get("resource"), recipients=request.get("recipients", ()),
                                               amount_cents=request.get("amount_cents"), currency=request.get("currency"),
                                               params=request.get("params"))
            except (ApprovalRejected, KeyError):
                raise ApprovalInvalid() from None
            same_owner = approval["requested_by"] == case["owner_agent_id"] or approval["requested_by"] == self.access.operator_id
            if (content_hash(normalized) != approval["payload_hash"] or normalized["case_id"] != approval["case_id"]
                    or _parse(approval["expires_at"]) <= self.clock() or case["state"] in TERMINAL_STATES
                    or case["state"] == "pausada" or case.get("operator_control") or not same_owner):
                raise ApprovalInvalid()
            used = self._save(session, approval, state="consumida", consumed_at=_stamp(self.clock()))
            return {"approval_id": used["id"], "payload_hash": used["payload_hash"], "policy": POLICY_VERSION}

    def record_effect(self, ctx, approval_id, outcome, detail=None):
        """Note what happened after the released action ran. ``desconhecido`` demands reconciliation."""
        self._ctx(ctx)
        if outcome not in ("desconhecido", "confirmado", "falhou"):
            raise ApprovalRejected("Resultado desconhecido.")
        with self.repository.transaction(ctx.tenant_id) as session:
            approval = self._approval(session, approval_id)
            if approval["state"] != "consumida" or approval["effect_outcome"] not in (None, "desconhecido"):
                raise ApprovalRejected("O efeito desta aprovacao ja foi registrado.")
            return self._save(session, approval, effect_outcome=outcome,
                              effect_detail=clean_text(detail)[:1000] if detail else None)

    def needing_reconciliation(self, ctx):
        """Released actions whose outcome is unknown: someone must check the external system."""
        self._trusted_operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            return [a for a in session.list("approval_request", limit=1000) if a.get("effect_outcome") == "desconhecido"]

    def resolve_effect(self, ctx, approval_id, outcome, detail):
        """The operator verified the external system. This records the truth; it does not undo anything."""
        self._trusted_operator(ctx)
        if outcome not in ("confirmado", "falhou"):
            raise ApprovalRejected("Informe confirmado ou falhou.")
        with self.repository.transaction(ctx.tenant_id) as session:
            approval = self._approval(session, approval_id)
            if approval["effect_outcome"] != "desconhecido":
                raise ApprovalRejected("Nao ha efeito em reconciliacao.")
            self._note(ctx, session, approval["case_id"],
                       f"Reconciliacao de {approval['action']}: {outcome} ({clean_text(detail)})")
            return self._save(session, approval, effect_outcome=outcome, effect_detail=clean_text(detail)[:1000])

    # -- reading ---------------------------------------------------------------------------------------------------
    def get(self, ctx, approval_id):
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            approval = self._approval(session, approval_id)
            if ctx.role == "agent" and approval["requested_by"] != ctx.principal_id:
                raise NotFound()
            return approval

    def list_approvals(self, ctx, case_id=None, state=None):
        self._ctx(ctx)
        filters = {k: v for k, v in (("case_id", case_id), ("state", state)) if v}
        with self.repository.transaction(ctx.tenant_id) as session:
            items = session.list("approval_request", limit=1000, **filters)
        return [a for a in items if ctx.role == "operator" or a["requested_by"] == ctx.principal_id]
