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

IMAP_PROVIDER = "imap"
SMTP_PROVIDER = "smtp"
READ_CAPABILITY, SEND_CAPABILITY = "email-leitura", "email-envio"


def _arguments(spec):
    return {name: (rule["type"], bool(rule.get("required"))) for name, rule in spec.get("arguments", {}).items()}


EMAIL_READ = Capability(
    READ_CAPABILITY,
    required={tool: "read" for tool in TOOLS},
    arguments={tool: _arguments(TOOL_SPECS[tool]) for tool in TOOLS})

# The send contract. ``para`` is what the approver sees as the recipient; every argument is part of what is approved.
_SEND_ARGS = {"para": ("string", True), "assunto": ("string", True), "corpo": ("string", True),
              "chave": ("string", True), "dry_run": ("boolean", False)}
EMAIL_SEND = Capability(
    SEND_CAPABILITY,
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


def register_email_drivers(registry, imap_transport):
    """Register both capabilities and both drivers. ``imap_transport`` is the ``ImapTransport`` of the application."""
    registry.register_capability(EMAIL_READ)
    registry.register_capability(EMAIL_SEND)
    return (registry.register_driver(IMAP_DESCRIPTOR, imap_transport), registry.register_driver(SMTP_DESCRIPTOR))
