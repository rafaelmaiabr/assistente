"""Network diagnostics as an approved local MCP server, plus the per-company list of authorized hosts."""
import asyncio
from datetime import datetime, timezone

from .access import AccessContext, NotFound, PermissionDenied
from .diagnostics import DiagnosticRefused, Diagnostics, SystemNetwork, normalize_host
from .gateway import McpTransport
from .ids import new_id
from .repository import IntegrityViolation, RevisionConflict

CATALOG_KEY = "diagnostico-rede"
TOOLS = ("verificar-dns", "verificar-http", "verificar-tls", "consultar-registro", "diagnosticar-site")
_DOMAIN = {"dominio": {"type": "string", "required": True, "max_length": 253}}
TOOL_SPECS = {tool: {"operation": "read", "arguments": _DOMAIN, "timeout_seconds": 60 if tool == "diagnosticar-site" else 30,
                     "max_result_bytes": 65536} for tool in TOOLS}
_CHECKS = {"verificar-dns": "check_dns", "verificar-http": "check_http", "verificar-tls": "check_tls",
           "consultar-registro": "check_registration", "diagnosticar-site": "diagnose"}


def approve_catalog(connections, operator, entry_version="1.0.0"):
    """Operator step: approve the built-in diagnostics server in the global catalog."""
    return connections.approve_mcp_server(operator, catalog_key=CATALOG_KEY, entry_version=entry_version, transport="local",
                                          tools=list(TOOLS), tool_specs=TOOL_SPECS)


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AssetService:
    """Hosts a company authorizes for diagnostics. Only the operator edits the list; agents cannot add targets."""

    def __init__(self, repository, access):
        self.repository, self.access = repository, access

    def _operator(self, ctx):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied()

    def add(self, ctx, hostname, note=None):
        self._operator(ctx)
        try:
            host = normalize_host(hostname)
        except DiagnosticRefused as refused:
            raise ValueError(str(refused)) from None
        now = _stamp()
        doc = {"kind": "monitored_asset", "schema_version": 1, "id": new_id("monitored_asset"), "tenant_id": ctx.tenant_id,
               "revision": 1, "created_at": now, "updated_at": now, "hostname": host, "note": note, "status": "ativo",
               "created_by": ctx.principal_id}
        try:
            with self.repository.transaction(ctx.tenant_id) as session:
                return session.save(doc)
        except IntegrityViolation:
            raise ValueError("Este dominio ja esta cadastrado.") from None

    def set_status(self, ctx, asset_id, expected_revision, status):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            asset = session.get("monitored_asset", asset_id) if isinstance(asset_id, str) else None
            if asset is None:
                raise NotFound()
            if asset["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            return session.save({**asset, "status": status, "revision": expected_revision + 1, "updated_at": _stamp()})

    def list(self, ctx):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            return sorted(session.list("monitored_asset", limit=1000), key=lambda a: a["hostname"])

    def authorized(self, tenant_id, host):
        """True when ``host`` is an active asset or a subdomain of one (backend check; never model input)."""
        with self.repository.transaction(tenant_id) as session:
            return any(a["status"] == "ativo" and (host == a["hostname"] or host.endswith("." + a["hostname"]))
                       for a in session.list("monitored_asset", limit=1000))


class DiagnosticsTransport(McpTransport):
    """In-process transport for the catalog entry ``transport="local"``; blocking network calls run in a thread."""

    def __init__(self, assets: AssetService, network=None, clock=None):
        self.assets, self.network = assets, network or SystemNetwork()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def describe(self, server, tool):
        return {"read_only": True} if server.get("key") == CATALOG_KEY and tool in TOOLS else None

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        tenant = identity["tenant_id"]
        diagnostics = Diagnostics(self.network, lambda host: self.assets.authorized(tenant, host), self.clock)
        method = getattr(diagnostics, _CHECKS[tool])
        try:
            return await asyncio.to_thread(method, arguments.get("dominio"))
        except DiagnosticRefused as refused:
            return {"erro": "nao_autorizado", "mensagem": str(refused)}
