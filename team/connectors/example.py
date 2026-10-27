"""Minimal fake driver that shows the shape of a connector and feeds the contract kit.

Capability ``exemplo``: list items (read) and create one (write, dry-run by default). Items live in memory,
per company. Copy this file as the starting point for a real driver (see docs/TEAM-CONNECTORS.md).
"""
from ..gateway import McpTransport
from .registry import Capability

CAPABILITY = Capability("exemplo", {"listar-itens": "read", "criar-item": "write"})
DESCRIPTOR = {
    "provider": "exemplo",
    "capability": "exemplo",
    "version": "1.0.0",
    "protocol": "mcp",
    "mcp_transport": "local",
    "auth": {"type": "secret", "scopes": ["itens.read", "itens.write"]},
    "tools": {
        "listar-itens": {"operation": "read", "max_items": 100,
                         "arguments": {"busca": {"type": "string", "max_length": 80}}},
        "criar-item": {"operation": "write",
                       "arguments": {"nome": {"type": "string", "required": True, "max_length": 80},
                                     "chave": {"type": "string", "required": True, "max_length": 64},
                                     "dry_run": {"type": "boolean"}}},
    },
    "idempotency_arguments": {"criar-item": "chave"},
    "fields": [{"name": "chave-de-acesso", "label": "Chave de acesso", "kind": "secret",
                "help": "Chave gerada no painel do servico."}],
}


class ExampleTransport(McpTransport):
    def __init__(self):
        self.items = {}  # tenant_id -> {idempotency key -> item}
        self.effects = 0

    async def describe(self, server, tool):
        specs = DESCRIPTOR["tools"]
        return {"read_only": specs[tool]["operation"] == "read"} if tool in specs else None

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        mine = self.items.setdefault(identity["tenant_id"], {})
        if tool == "listar-itens":
            busca = arguments.get("busca", "").lower()
            return {"itens": [item for item in mine.values() if busca in item["nome"].lower()]}
        if tool == "criar-item":
            if arguments.get("dry_run", True):  # simulate unless the caller explicitly (and, in the gateway, with approval) asks
                return {"dry_run": True, "criaria": {"nome": arguments["nome"]}}
            if arguments["chave"] not in mine:
                mine[arguments["chave"]] = {"nome": arguments["nome"]}
                self.effects += 1
            return {"dry_run": False, "criado": mine[arguments["chave"]]}
        return {"erro": "ferramenta_desconhecida", "mensagem": "Ferramenta nao disponivel."}
