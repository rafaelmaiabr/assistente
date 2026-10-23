"""Read-only finance tools as an approved local MCP server.

The agent can look up imported records and ask for an analysis. It cannot import, edit, pay, charge or send:
those need the operator, an approval and a specific connector. Free text from the model is never an action.
"""
import asyncio

from .enums import RECORD_KINDS
from .finance import MAX_LIST, FinanceRejected, FinanceService
from .gateway import McpTransport

CATALOG_KEY = "financeiro-local"
TOOLS = ("consultar-registros", "analisar-pendencias")
_PARTY = {"type": "string", "required": True, "max_length": 200}
TOOL_SPECS = {
    "consultar-registros": {"operation": "read", "timeout_seconds": 20, "max_result_bytes": 262144, "max_items": MAX_LIST,
                            "arguments": {"contraparte": _PARTY, "tipo": {"type": "string", "enum": list(RECORD_KINDS)},
                                          "documento": {"type": "string", "max_length": 80},
                                          "limite": {"type": "integer", "minimum": 1, "maximum": MAX_LIST}}},
    "analisar-pendencias": {"operation": "read", "timeout_seconds": 30, "max_result_bytes": 262144, "max_items": 100,
                            "arguments": {"contraparte": _PARTY, "documento": {"type": "string", "max_length": 80}}},
}


def approve_catalog(connections, operator, entry_version="1.0.0"):
    """Operator step: approve the built-in finance server (read-only tools) in the global catalog."""
    return connections.approve_mcp_server(operator, catalog_key=CATALOG_KEY, entry_version=entry_version, transport="local",
                                          tools=list(TOOLS), tool_specs=TOOL_SPECS)


class FinanceTransport(McpTransport):
    def __init__(self, service: FinanceService, access):
        self.service, self.access = service, access

    async def describe(self, server, tool):
        return {"read_only": True} if server.get("key") == CATALOG_KEY and tool in TOOLS else None

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        tenant = identity["tenant_id"]
        try:
            if tool == "consultar-registros":
                records = await asyncio.to_thread(self.service.list_records, tenant, counterparty=arguments["contraparte"],
                                                  kind=arguments.get("tipo"), document=arguments.get("documento"),
                                                  limit=arguments.get("limite", MAX_LIST))
                return {"registros": records, "dados_nao_confiaveis": True}
            if tool == "analisar-pendencias":
                ctx = self.access.agent_context(self.access.local_operator(), tenant, identity["run_id"])
                return await asyncio.to_thread(self.service.analyze, tenant, arguments["contraparte"], document=arguments.get("documento"),
                                               agent_ctx=ctx)
        except FinanceRejected as rejected:
            return {"erro": "pedido_invalido", "mensagem": str(rejected)}
        return {"erro": "ferramenta_desconhecida", "mensagem": "Ferramenta nao disponivel."}
