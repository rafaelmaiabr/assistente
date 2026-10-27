"""Email on the connector layer: two capabilities, one driver each, nothing about the existing behaviour changed.

* ``email-leitura`` (read): the four tools the read-only IMAP MCP server already offers. The contract is derived from
  ``imap_mcp.TOOL_SPECS``, so the tool names, arguments and classification the gateway sees are exactly the ones that
  were approved before this layer existed. The IMAP driver keeps the in-process MCP transport.
* ``email-envio`` (send): ``enviar-mensagem``. SMTP is not an MCP server and is not run by the gateway: messages leave
  through the outbound service (draft, exact single-use approval, re-validation, reconciliation; ``team/outbound.py``,
  ``team/smtp_transport.py``). The SMTP driver is registered so a company's SMTP connection resolves to a driver and so a
  second driver (for example an email MCP server that can send) must meet the same approval contract.

A different mail vendor (Gmail or Microsoft by API, an email MCP server) becomes a new driver for these capabilities.
"""
from ..imap_mcp import CATALOG_KEY, TOOL_SPECS, TOOLS
from .registry import Capability
from .setup import SetupAdapter

IMAP_PROVIDER = "imap"
SMTP_PROVIDER = "smtp"
READ_CAPABILITY, SEND_CAPABILITY = "email-leitura", "email-envio"


def _arguments(spec):
    return {name: (rule["type"], bool(rule.get("required"))) for name, rule in spec.get("arguments", {}).items()}


EMAIL_READ = Capability(
    READ_CAPABILITY, label="Email: leitura da caixa",
    required={tool: "read" for tool in TOOLS},
    arguments={tool: _arguments(TOOL_SPECS[tool]) for tool in TOOLS})

# The send contract. ``para`` is what the approver sees as the recipient; every argument is part of what is approved.
_SEND_ARGS = {"para": ("string", True), "assunto": ("string", True), "corpo": ("string", True),
              "chave": ("string", True), "dry_run": ("boolean", False)}
EMAIL_SEND = Capability(
    SEND_CAPABILITY, label="Email: envio de mensagens",
    required={"enviar-mensagem": "send"},
    arguments={"enviar-mensagem": _SEND_ARGS},
    bindings={"enviar-mensagem": {"recipient_argument": "para"}},
    idempotent={"enviar-mensagem": "chave"})

IMAP_DESCRIPTOR = {
    "provider": IMAP_PROVIDER, "capability": READ_CAPABILITY, "version": "1.0.0", "catalog_key": CATALOG_KEY,
    "protocol": "mcp", "mcp_transport": "local",
    # Password (app password) or OAuth (XOAUTH2, #34/#35) is chosen per account by the operator; both are read-only.
    "auth": {"type": "secret", "scopes": []},
    "tools": TOOL_SPECS,
    "fields": [{"name": "servidor", "label": "Servidor IMAP", "kind": "text"},
               {"name": "porta", "label": "Porta", "kind": "number", "required": False},
               {"name": "usuario", "label": "Usuario", "kind": "text"},
               {"name": "pastas", "label": "Pastas autorizadas", "kind": "text",
                "help": "Somente estas pastas podem ser lidas pelos agentes."},
               {"name": "senha", "label": "Senha de aplicativo", "kind": "secret"}],
}

SMTP_DESCRIPTOR = {
    "provider": SMTP_PROVIDER, "capability": SEND_CAPABILITY, "version": "1.0.0",
    "protocol": "smtp",
    "non_mcp_justification": ("O envio de email sai pelo servico de saida (rascunho, aprovacao exata de uso unico, "
                              "revalidacao e reconciliacao), nao por uma ferramenta MCP do gateway."),
    "auth": {"type": "secret", "scopes": []},
    "tools": {"enviar-mensagem": {"operation": "send", "timeout_seconds": 30,
                                  "recipient_argument": "para",
                                  "arguments": {"para": {"type": "string", "required": True, "max_length": 200},
                                                "assunto": {"type": "string", "required": True, "max_length": 300},
                                                "corpo": {"type": "string", "required": True, "max_length": 8000},
                                                "chave": {"type": "string", "required": True, "max_length": 64},
                                                "dry_run": {"type": "boolean"}}}},
    "idempotency_arguments": {"enviar-mensagem": "chave"},
    "fields": [{"name": "servidor", "label": "Servidor SMTP", "kind": "text"},
               {"name": "porta", "label": "Porta", "kind": "number", "required": False},
               {"name": "usuario", "label": "Usuario", "kind": "text"},
               {"name": "remetente", "label": "Endereco do remetente", "kind": "text"},
               {"name": "senha", "label": "Senha de aplicativo", "kind": "secret"}],
}


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}  # the only hosts the clients accept without TLS (a local bridge or a test server)


def _folders(text):
    return tuple(part.strip() for part in str(text).split(",") if part.strip())


class ImapSetup(SetupAdapter):
    """Panel adapter over ``ImapAccounts`` (host, user, authorized folders; the password goes to the vault)."""

    def __init__(self, accounts):
        self.accounts = accounts

    def configure(self, operator, ctx, connection_id, values):
        self.accounts.configure(operator, ctx, connection_id, host=values["servidor"], username=values["usuario"],
                                folders=_folders(values["pastas"]), port=values.get("porta", 993),
                                tls=values["servidor"] not in _LOOPBACK)

    def current(self, tenant_id, connection_id):
        config = self.accounts.registry.get(tenant_id, connection_id)
        if config is None:
            return None
        return {"servidor": config.host, "porta": config.port, "usuario": config.username, "pastas": ", ".join(config.folders)}

    def test(self, ctx, connection_id):
        return self.accounts.test(ctx, connection_id)


class SmtpSetup(SetupAdapter):
    """Panel adapter over ``SmtpAccounts``."""

    def __init__(self, accounts):
        self.accounts = accounts

    def configure(self, operator, ctx, connection_id, values):
        self.accounts.configure(operator, ctx, connection_id, host=values["servidor"], username=values["usuario"],
                                from_address=values["remetente"], port=values.get("porta", 587),
                                security="plain" if values["servidor"] in _LOOPBACK else "starttls")

    def current(self, tenant_id, connection_id):
        config = self.accounts.registry.get(tenant_id, connection_id)
        if config is None:
            return None
        return {"servidor": config.host, "porta": config.port, "usuario": config.username, "remetente": config.from_address}

    def test(self, ctx, connection_id):
        return self.accounts.test(ctx, connection_id)


def register_email_drivers(registry, imap_transport, imap_setup=None, smtp_setup=None):
    """Register both capabilities and both drivers. ``imap_transport`` is the ``ImapTransport`` of the application;
    the optional setup adapters let the panel configure and test them."""
    registry.register_capability(EMAIL_READ)
    registry.register_capability(EMAIL_SEND)
    return (registry.register_driver(IMAP_DESCRIPTOR, imap_transport, imap_setup), registry.register_driver(SMTP_DESCRIPTOR, setup=smtp_setup))
