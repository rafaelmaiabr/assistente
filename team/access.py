"""Access contexts and default-deny authorization (local mode: one trusted operator).

Contexts are produced only by ``AccessService``, which derives them from backend state (the local
operator, a persisted agent run). A ``tenant_id``, role or principal supplied by an LLM, a message
or a tool argument is data, never a context. This is *logical* isolation inside one process under
one OS account: it is not SaaS authentication and does not resist other processes of that account.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .enums import RESOURCE_KINDS


# Company roles of a signed-in person, lowest to highest. The local owner is not one of them (see ``is_operator``).
ROLE_LEVEL = {"leitor": 1, "atendente": 2, "aprovador": 3, "admin": 4}


class AccessError(Exception):
    pass


class NotFound(AccessError):
    """Missing *or not visible*: callers cannot tell the two apart (no existence leak)."""

    def __init__(self):
        super().__init__("recurso nao encontrado")


class PermissionDenied(AccessError):
    def __init__(self, reason="acesso negado"):
        super().__init__(reason)


_TOKEN = object()


@dataclass(frozen=True)
class OperatorContext:
    """The trusted local operator; may manage companies, never reads tenant data by itself."""
    principal_id: str
    _token: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._token is not _TOKEN:
            raise PermissionDenied("contexto so pode ser criado pelo backend")


@dataclass(frozen=True)
class AccessContext:
    tenant_id: str
    principal_id: str
    role: str  # "operator": full access inside the tenant; "agent": only explicit grants
    _token: object = field(repr=False, compare=False)
    # Set only for a signed-in person acting through the remote listener (see identity.py): the company role that
    # was checked for this request. The local owner never has one.
    member_role: str | None = None

    def __post_init__(self):
        if self._token is not _TOKEN or self.role not in ("operator", "agent"):
            raise PermissionDenied("contexto so pode ser criado pelo backend")
        if self.member_role is not None and (self.role != "operator" or self.member_role not in ROLE_LEVEL):
            raise PermissionDenied("contexto so pode ser criado pelo backend")


def _not_expired(grant, now):
    expires = grant.get("expires_at")
    return expires is None or datetime.fromisoformat(expires.replace("Z", "+00:00")) > now


class AccessService:
    def __init__(self, repository, operator_id):
        self.repository, self.operator_id = repository, operator_id

    def local_operator(self) -> OperatorContext:
        return OperatorContext(self.operator_id, _TOKEN)

    def tenant_context(self, operator, tenant_id) -> AccessContext:
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.operator_id:
            raise PermissionDenied()
        if not isinstance(tenant_id, str) or self.repository.get_global("tenant", tenant_id) is None:
            raise NotFound()
        return AccessContext(tenant_id, operator.principal_id, "operator", _TOKEN)

    def agent_context(self, operator, tenant_id, run_id) -> AccessContext:
        """Context of a queued/running agent execution; the principal is the run's agent definition."""
        base = self.tenant_context(operator, tenant_id)
        with self.repository.transaction(base.tenant_id) as session:
            run = session.get("agent_run", run_id) if isinstance(run_id, str) else None
        if run is None:
            raise NotFound()
        if run["state"] not in ("enfileirada", "em_execucao"):
            raise PermissionDenied("execucao encerrada")
        return AccessContext(base.tenant_id, run["agent_id"], "agent", _TOKEN)

    def member_context(self, identity, tenant_id, role) -> AccessContext:
        """Context of a signed-in person for one company. ``identity`` is proof that the backend verified the session
        and ``role`` the membership of this very request; the person's own id is the principal, so every record made
        under it names who did it."""
        from .identity import Identity  # imported here: identity.py imports this module
        if not isinstance(identity, Identity) or role not in ROLE_LEVEL or identity.memberships.get(tenant_id) != role:
            raise PermissionDenied()
        if self.repository.get_global("tenant", tenant_id) is None:
            raise NotFound()
        return AccessContext(tenant_id, identity.user_id, "operator", _TOKEN, role)

    def is_operator(self, ctx, minimum="atendente") -> bool:
        """The trusted local owner, or a signed-in person whose company role for this request is at least ``minimum``."""
        if not isinstance(ctx, AccessContext) or ctx.role != "operator":
            return False
        if ctx.member_role is None:
            return ctx.principal_id == self.operator_id
        return ROLE_LEVEL[ctx.member_role] >= ROLE_LEVEL[minimum]

    def allowed(self, ctx, session, action, kind, resource_id=None) -> bool:
        """Default-deny. ``resource_id=None`` asks about the whole kind (grants with no id only)."""
        if not isinstance(ctx, AccessContext) or session.tenant_id != ctx.tenant_id:
            return False
        if ctx.role == "operator":  # grants are managed by the operator only, never granted to agents
            if ctx.member_role == "leitor" and action != "read":
                return False
            return kind in RESOURCE_KINDS or kind == "access_grant"
        if kind not in RESOURCE_KINDS:
            return False
        return self.principal_allowed(session, ctx.principal_id, action, kind, resource_id)

    def principal_allowed(self, session, principal_id, action, kind, resource_id=None) -> bool:
        """Grant lookup for a principal that is not the caller (for example the recipient of a handoff)."""
        if kind not in RESOURCE_KINDS:
            return False
        now = datetime.now(timezone.utc)
        for grant in session.list("access_grant", principal_id=principal_id, resource_kind=kind, limit=1000):
            if action in grant["actions"] and _not_expired(grant, now) and (
                    grant["resource_id"] is None or grant["resource_id"] == resource_id):
                return True
        return False
