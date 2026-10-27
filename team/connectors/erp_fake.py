"""Reference ERP driver (fake, in memory): the shape a real driver such as Bling follows, and the fixture for the
``erp`` contract tests. It never contacts anything. ``alterar-cobranca`` and ``consultar-pedidos`` are deliberately
left out, to show how a driver declares what it does not support.
"""
import hashlib
import hmac
import json

from ..gateway import McpTransport
from .erp import ERP, normalize_charge
from .erp_ledger import UnknownResult, run_emission

PROVIDER = "erp-fake"
WEBHOOK_SECRET = b"segredo-de-webhook-somente-para-testes"
_CHARGE_ARGS = {"cliente": {"type": "string", "required": True, "max_length": 200},
                "beneficiario": {"type": "string", "required": True, "max_length": 200},
                "documento": {"type": "string", "required": True, "max_length": 80},
                "valor_centavos": {"type": "integer", "required": True, "minimum": 1, "maximum": 100_000_000_00},
                "moeda": {"type": "string", "required": True, "max_length": 3},
                "vencimento": {"type": "string", "max_length": 10},
                "chave": {"type": "string", "required": True, "max_length": 64},
                "dry_run": {"type": "boolean"}}
DESCRIPTOR = {
    "provider": PROVIDER, "capability": "erp", "version": "1.0.0", "protocol": "mcp", "mcp_transport": "local",
    "auth": {"type": "secret", "scopes": ["clientes.read", "cobrancas.read", "cobrancas.write"]},
    "tools": {
        "consultar-clientes": {"operation": "read", "max_items": 100, "arguments": {"busca": {"type": "string", "max_length": 80}}},
        "consultar-cobrancas": {"operation": "read", "max_items": 100, "arguments": {
            "cliente": {"type": "string", "max_length": 200}, "documento": {"type": "string", "max_length": 80},
            "status": {"type": "string", "max_length": 20}, "limite": {"type": "integer", "minimum": 1, "maximum": 100}}},
        "status-cobranca": {"operation": "read", "arguments": {"documento": {"type": "string", "required": True, "max_length": 80}}},
        "emitir-cobranca": {"operation": "write", "arguments": _CHARGE_ARGS, "recipient_argument": "cliente",
                            "amount_argument": "valor_centavos", "currency_argument": "moeda"},
    },
    "idempotency_arguments": {"emitir-cobranca": "chave"},
    "fields": [{"name": "chave-de-api", "label": "Chave de API", "kind": "secret"}],
}


def sign(body: bytes, secret: bytes = WEBHOOK_SECRET) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def parse_webhook(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


class FakeErpTransport(McpTransport):
    def __init__(self, ledger):
        self.ledger = ledger
        self.customers = {}  # tenant -> [names]
        self.charges = {}    # tenant -> {documento -> charge}
        self.by_key = {}     # (tenant, chave) -> documento: what the vendor remembers
        self.lose_next_response = False
        self.issued = 0

    # -- test controls ---------------------------------------------------------------------------------------------------
    def add_customer(self, tenant, name):
        self.customers.setdefault(tenant, []).append(name)

    def webhook(self, documento, status, *, event_id, occurred_at, cliente=None, valor_centavos=None, moeda=None):
        body = json.dumps({"event_id": event_id, "documento": documento, "status": status, "occurred_at": occurred_at,
                           "cliente": cliente, "valor_centavos": valor_centavos, "moeda": moeda}).encode()
        return body, sign(body)

    # -- McpTransport ----------------------------------------------------------------------------------------------------
    async def describe(self, server, tool):
        spec = DESCRIPTOR["tools"].get(tool)
        return None if spec is None else {"read_only": spec["operation"] == "read"}

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        tenant = identity["tenant_id"]
        mine = self.charges.setdefault(tenant, {})
        if tool == "consultar-clientes":
            busca = arguments.get("busca", "").casefold()
            return {"clientes": [{"nome": n} for n in self.customers.get(tenant, []) if busca in n.casefold()]}
        if tool == "consultar-cobrancas":
            wanted = [c for c in mine.values() if all(
                arguments.get(k) in (None, c[k if k != "cliente" else "cliente"]) for k in ("cliente", "documento", "status"))]
            return {"cobrancas": wanted[: arguments.get("limite", 100)]}
        if tool == "status-cobranca":
            charge = mine.get(arguments["documento"])
            return {"cobranca": charge} if charge else {"erro": "nao_encontrada", "mensagem": "Cobranca nao encontrada nesta empresa."}
        if tool == "emitir-cobranca":
            return await self._emit(tenant, mine, arguments)
        return {"erro": "ferramenta_desconhecida", "mensagem": "Ferramenta nao disponivel."}

    async def _emit(self, tenant, mine, arguments):
        if arguments.get("dry_run", True):
            return {"dry_run": True, "emitiria": {k: arguments[k] for k in ("cliente", "beneficiario", "documento", "valor_centavos", "moeda")}}
        payload = {k: v for k, v in arguments.items() if k != "chave"}
        charge = {"documento": arguments["documento"], "cliente": arguments["cliente"], "beneficiario": arguments["beneficiario"],
                  "valor_centavos": arguments["valor_centavos"], "moeda": arguments["moeda"], "status": "emitida",
                  "vencimento": arguments.get("vencimento")}

        async def call():
            self.by_key[(tenant, arguments["chave"])] = charge["documento"]  # the vendor did it...
            mine[charge["documento"]] = dict(charge)
            self.issued += 1
            if self.lose_next_response:
                self.lose_next_response = False
                raise ConnectionError("resposta perdida")  # ...but the answer never came back
            return normalize_charge(charge)

        try:
            done = await run_emission(self.ledger, tenant, PROVIDER, arguments["chave"], payload, call)
        except UnknownResult as unknown:
            return {"erro": "resultado_desconhecido", "mensagem": str(unknown)}
        return {"dry_run": False, **done}
