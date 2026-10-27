"""Connector descriptor: what a driver declares about itself, validated before it can be registered.

A descriptor is trusted backend code (never model, message or tool output). It reuses the approved-catalog
tool classification (``tool_specs``) so the gateway keeps a single source of truth for operations and arguments.
MCP is the preferred protocol: any other protocol must say why in ``non_mcp_justification``.
"""
import re

from ..connections import _GENERIC_EXEC, ConnectionRejected, _check_tool_specs
from ..enums import MCP_TRANSPORTS

DESCRIPTOR_VERSION = 1
PROTOCOLS = ("mcp", "imap", "smtp", "http", "outro")
AUTH_TYPES = ("none", "secret", "oauth")
FIELD_KINDS = ("text", "secret", "url", "number")
MAX_TOOLS = 40
MAX_FIELDS = 12
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
_DESCRIPTOR_KEYS = {"provider", "capability", "version", "protocol", "mcp_transport", "non_mcp_justification", "auth",
                    "tools", "idempotency_arguments", "fields", "catalog_key"}
_FIELD_KEYS = {"name", "label", "kind", "required", "help"}


class DescriptorRejected(ConnectionRejected):
    """Invalid descriptor; the message is safe to show and never contains secrets."""


def _text(value, low, high, what):
    if not (isinstance(value, str) and low <= len(value.strip()) <= high):
        raise DescriptorRejected(f"{what} deve ter entre {low} e {high} caracteres.")
    return value.strip()


def _check_fields(fields):
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise DescriptorRejected(f"fields deve ser uma lista de ate {MAX_FIELDS} campos.")
    seen, clean = set(), []
    for field in fields:
        if not isinstance(field, dict) or set(field) - _FIELD_KEYS or not {"name", "label", "kind"} <= set(field):
            raise DescriptorRejected("Campo de configuracao invalido: informe name, label e kind.")
        if not (isinstance(field["name"], str) and _SLUG.match(field["name"])) or field["name"] in seen:
            raise DescriptorRejected("Nome de campo invalido ou repetido.")
        if field["kind"] not in FIELD_KINDS:
            raise DescriptorRejected(f"kind do campo {field['name']} deve ser um de {', '.join(FIELD_KINDS)}.")
        if not isinstance(field.get("required", True), bool):
            raise DescriptorRejected("required deve ser booleano.")
        seen.add(field["name"])
        clean.append({"name": field["name"], "label": _text(field["label"], 1, 80, "label"), "kind": field["kind"],
                      "required": field.get("required", True),
                      "help": _text(field["help"], 1, 300, "help") if field.get("help") else None})
    return clean


def validate_descriptor(raw):
    """Return a normalized copy of ``raw`` or raise ``DescriptorRejected``."""
    if not isinstance(raw, dict) or set(raw) - _DESCRIPTOR_KEYS:
        raise DescriptorRejected("Descritor invalido: chaves desconhecidas.")
    for key in ("provider", "capability", "version", "protocol", "auth", "tools"):
        if key not in raw:
            raise DescriptorRejected(f"Descritor sem {key}.")
    provider, capability = raw["provider"], raw["capability"]
    catalog_key = raw.get("catalog_key", provider)
    if not (isinstance(catalog_key, str) and _SLUG.match(catalog_key)):
        raise DescriptorRejected("catalog_key deve ser um identificador simples.")
    if not (isinstance(provider, str) and _SLUG.match(provider)) or not (isinstance(capability, str) and _SLUG.match(capability)):
        raise DescriptorRejected("provider e capability devem ser identificadores simples (a-z, 0-9, ponto, hifen).")
    if not (isinstance(raw["version"], str) and _VERSION.match(raw["version"])):
        raise DescriptorRejected("version deve ter o formato 1.0.0.")
    protocol = raw["protocol"]
    if protocol not in PROTOCOLS:
        raise DescriptorRejected(f"protocol deve ser um de {', '.join(PROTOCOLS)}.")
    mcp_transport, justification = raw.get("mcp_transport"), raw.get("non_mcp_justification")
    if protocol == "mcp":
        if mcp_transport not in MCP_TRANSPORTS:
            raise DescriptorRejected(f"mcp_transport deve ser um de {', '.join(MCP_TRANSPORTS)}.")
        if justification:
            raise DescriptorRejected("non_mcp_justification so se aplica a protocolos que nao sao MCP.")
    else:
        if mcp_transport:
            raise DescriptorRejected("mcp_transport so se aplica ao protocolo mcp.")
        justification = _text(justification, 30, 500, "non_mcp_justification (MCP e o protocolo preferido; explique por que nao serve)")
    auth = raw["auth"]
    if not isinstance(auth, dict) or set(auth) - {"type", "scopes"} or auth.get("type") not in AUTH_TYPES:
        raise DescriptorRejected(f"auth.type deve ser um de {', '.join(AUTH_TYPES)}.")
    scopes = auth.get("scopes", [])
    if not (isinstance(scopes, list) and len(scopes) <= 20 and all(isinstance(s, str) and 0 < len(s) <= 200 for s in scopes)):
        raise DescriptorRejected("auth.scopes deve ser uma lista de ate 20 textos.")
    tools = raw["tools"]
    if not isinstance(tools, dict) or not 1 <= len(tools) <= MAX_TOOLS:
        raise DescriptorRejected(f"tools deve descrever de 1 a {MAX_TOOLS} ferramentas.")
    for name, spec in tools.items():
        if not (isinstance(name, str) and _SLUG.match(name)) or _GENERIC_EXEC.search(name):
            raise DescriptorRejected(f"Nome de ferramenta invalido ou generico de execucao: {str(name)[:40]}.")
        if not isinstance(spec, dict) or "operation" not in spec:
            raise DescriptorRejected(f"Ferramenta {name} sem classificacao (operation).")
    specs = _check_tool_specs(list(tools), tools)
    for name, spec in specs.items():
        if spec["operation"] != "read" and spec.get("arguments", {}).get("dry_run", {}).get("type") != "boolean":
            raise DescriptorRejected(f"Ferramenta {name} altera algo e deve aceitar o argumento booleano dry_run (padrao: simular).")
    idempotency = raw.get("idempotency_arguments", {})
    if not isinstance(idempotency, dict):
        raise DescriptorRejected("idempotency_arguments deve mapear ferramenta -> argumento.")
    for name, argument in idempotency.items():
        if name not in specs or specs[name]["operation"] == "read" or argument not in specs[name].get("arguments", {}):
            raise DescriptorRejected("idempotency_arguments deve apontar para um argumento de uma ferramenta que altera algo.")
    return {"descriptor_version": DESCRIPTOR_VERSION, "provider": provider, "capability": capability, "catalog_key": catalog_key,
            "version": raw["version"], "protocol": protocol, "mcp_transport": mcp_transport,
            "non_mcp_justification": justification, "auth": {"type": auth["type"], "scopes": list(scopes)},
            "tools": specs, "idempotency_arguments": dict(idempotency), "fields": _check_fields(raw.get("fields", []))}
