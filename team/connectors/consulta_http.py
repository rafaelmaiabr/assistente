"""Read-only HTTP lookups on the connector layer (#120, part b): capability ``consulta-http``, driver ``http``.

The driver is an in-process MCP server, like IMAP, so the gateway runs it (``read`` classification, audit, company
isolation by connection). The tool takes a route *name* and one value; the address is never an argument. The routes are the
operator's global ones (``RouteStore``, the same file the chat reads): what the panel changes here changes there. The
secure client (``http_get``) does all the network work. No credential exists, so the vault is never used.
"""
import asyncio

from ..connections import ConnectionRejected
from ..gateway import McpTransport
from .http_get import SafeHttp
from .http_routes import RouteRejected, consult
from .registry import Capability
from .setup import SetupAdapter

PROVIDER = CATALOG_KEY = "http"
CAPABILITY = "consulta-http"
TOOL = "consultar-rota"
_ARGUMENTS = {"rota": {"type": "string", "required": True, "max_length": 40},
              "valor": {"type": "string", "required": True, "max_length": 253}}

HTTP_CONSULT = Capability(
    CAPABILITY, label="Consulta HTTP a rotas liberadas (somente leitura)",
    required={TOOL: "read"},
    arguments={TOOL: {name: (rule["type"], rule["required"]) for name, rule in _ARGUMENTS.items()}})

DESCRIPTOR = {
    "label": "Consulta HTTP (rotas liberadas)", "aliases": ["http", "consulta http", "brasilapi"],
    "provider": PROVIDER, "capability": CAPABILITY, "version": "1.0.0", "catalog_key": CATALOG_KEY,
    "protocol": "mcp", "mcp_transport": "local",
    "auth": {"type": "none", "scopes": []},
    "tools": {TOOL: {"operation": "read", "timeout_seconds": 20, "arguments": _ARGUMENTS}},
    "steps": ["As rotas valem para toda a instalacao (o chat usa as mesmas): ligar ou desligar aqui muda para todas as empresas.",
              "So GET publico, sem credenciais; o modelo escolhe a rota pelo nome e nunca envia o endereco."],
    "fields": [{"name": "rotas", "label": "Rotas ligadas", "kind": "text", "required": False,
                "help": "Nomes separados por virgula, por exemplo registro-dominio. Use nenhuma para desligar todas."},
               {"name": "nova-rota-nome", "label": "Nova rota: nome", "kind": "text", "required": False,
                "help": "Cadastra uma rota propria, que fica desligada ate constar em Rotas ligadas."},
               {"name": "nova-rota-descricao", "label": "Nova rota: descricao", "kind": "text", "required": False},
               {"name": "nova-rota-modelo", "label": "Nova rota: endereco com {valor}", "kind": "url", "required": False,
                "help": "https, com exatamente um {valor} fora do nome do servidor."},
               {"name": "nova-rota-tipo", "label": "Nova rota: tipo do valor", "kind": "text", "required": False,
                "help": "dominio, cnpj, cep ou numero."}],
}


class HttpConsultTransport(McpTransport):
    """In-process transport for the catalog entry ``http``; the blocking client runs in a thread."""

    def __init__(self, store, client=None):
        self.store, self.client = store, client or SafeHttp()

    async def describe(self, server, tool):
        return {"read_only": True} if server.get("key") == CATALOG_KEY and tool == TOOL else None

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        if tool != TOOL:
            return {"erro": "ferramenta_desconhecida", "mensagem": "Ferramenta nao disponivel."}
        return await asyncio.to_thread(consult, self.store, self.client, arguments.get("rota"), arguments.get("valor"))


def _names(text):
    return [part.strip() for part in str(text).replace(";", ",").split(",") if part.strip()]


class HttpSetup(SetupAdapter):
    """Panel adapter over the operator's ``RouteStore``. There is nothing per connection to store: the routes are global."""

    def __init__(self, store):
        self.store = store

    def configure(self, operator, ctx, connection_id, values):
        wanted = [] if str(values.get("rotas", "")).strip().lower() in ("nenhuma", "nenhum", "-") else _names(values.get("rotas", ""))
        known = {route.name for route, _ in self.store.routes()}
        new = [values.get(f"nova-rota-{part}") for part in ("nome", "descricao", "modelo", "tipo")]
        if any(new) and not all(new):
            raise ConnectionRejected("Para cadastrar uma rota informe nome, descricao, endereco e tipo.")
        try:
            if all(new):
                self.store.add(*new)  # validated by the store; stays off until it is listed below
                known.add(new[0])
            unknown = [name for name in wanted if name not in known]
            if unknown:
                raise ConnectionRejected(f"Rota desconhecida: {unknown[0][:40]}.")
            for route, on in self.store.routes():
                if on != (route.name in wanted):
                    self.store.enable(route.name, route.name in wanted)
        except RouteRejected as error:
            raise ConnectionRejected(str(error)) from None

    def current(self, tenant_id, connection_id):
        on = [route.name for route in self.store.enabled_routes()]
        return {"rotas": ", ".join(on)} if on else None

    def test(self, ctx, connection_id):
        """No request is made: a real lookup is the operator's call. This only says whether a route is on."""
        on = self.store.enabled_routes()
        if not on:
            return {"ok": False, "erro": "sem_rota_ligada", "mensagem": "Nenhuma rota esta ligada."}
        return {"ok": True, "mensagem": f"{len(on)} rota(s) ligada(s). Nenhuma consulta real foi feita neste teste."}


def register_http_driver(registry, store, client=None):
    """Register the capability and its driver. ``store`` is the same ``RouteStore`` the chat reads."""
    registry.register_capability(HTTP_CONSULT)
    return registry.register_driver(DESCRIPTOR, HttpConsultTransport(store, client), HttpSetup(store))
