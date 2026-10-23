"""Access contexts and default-deny authorization (local mode: one trusted operator).

Contexts are produced only by ``AccessService``, which derives them from backend state (the local
operator, a persisted agent run). A ``tenant_id``, role or principal supplied by an LLM, a message
or a tool argument is data, never a context. This is *logical* isolation inside one process under
one OS account: it is not SaaS authentication and does not resist other processes of that account.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .enums import RESOURCE_KINDS


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

    def __post_init__(self):
        if self._token is not _TOKEN or self.role not in ("operator", "agent"):
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

    def allowed(self, ctx, session, action, kind, resource_id=None) -> bool:
        """Default-deny. ``resource_id=None`` asks about the whole kind (grants with no id only)."""
        if not isinstance(ctx, AccessContext) or session.tenant_id != ctx.tenant_id:
            return False
        if ctx.role == "operator":  # grants are managed by the operator only, never granted to agents
            return kind in RESOURCE_KINDS or kind == "access_grant"
        if kind not in RESOURCE_KINDS:
            return False
        now = datetime.now(timezone.utc)
        for grant in session.list("access_grant", principal_id=ctx.principal_id, resource_kind=kind, limit=1000):
            if action in grant["actions"] and _not_expired(grant, now) and (
                    grant["resource_id"] is None or grant["resource_id"] == resource_id):
                return True
        return False
