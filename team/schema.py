"""Executable schemas for the team domain, validated with the standard library only.

A field spec is a string ``<type>[?]`` (``?`` = optional/nullable) or a nested dict of specs.
Types: str, text, int, posint, bool, ts, sha256, semver, slug, url, actor, obj,
``id:<kind>`` / ``ref:<kind>`` (opaque id of an entity kind; ``ref`` is also checked across a
bundle), ``enum:<Name>`` and ``list:<type>``. Unknown fields are rejected.
"""
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from .enums import ENUMS
from .ids import ACTOR_PREFIXES, PREFIXES, is_id

SCHEMA_VERSION = 1
SUPPORTED_VERSIONS = (1,)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_TEXT = 8000

# Secrets live in the vault and are referenced by id; never inline (as keys or values).
_SECRET_KEY = re.compile(
    r"(^|_)(password|passwd|secret|token|apikey|api_key|authorization|private_key|bearer)(_|$)", re.I)
_SAFE_KEYS = {"token_budget", "max_tokens"}
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}|Bearer\s+\S{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|://[^/\s:@]+:[^/\s@]+@)")


class ContractError(ValueError):
    def __init__(self, errors, code="invalid"):
        self.errors = list(errors)
        self.code = code
        super().__init__("; ".join(self.errors))


def _entity(kind, tenant=True, envelope=False, **fields):
    spec = {"kind": "str", "schema_version": "posint", "id": f"id:{kind}"}
    if tenant:
        spec["tenant_id"] = "ref:tenant"
    spec.update(fields)
    if not envelope:
        spec.update(revision="posint", created_at="ts", updated_at="ts")
    return spec


SCHEMAS = {
    "tenant": _entity("tenant", tenant=False, name="str", status="enum:Status"),
    "department": _entity("department", name="str", scope="text", status="enum:Status"),
    "agent_definition": _entity(
        "agent_definition", department_id="ref:department", agent_version="posint", name="str",
        personality={"tone": "text", "initiative": "text"}, scope="text", out_of_scope="text",
        skill_grant_ids="list:ref:skill_grant", handoff_target_ids="list:ref:agent_definition",
        model_policy_ref="slug", voice_profile_ref="slug?", status="enum:Status",
        goals="list:text?", routing_policy="enum:RoutingPolicy?"),
    # Immutable snapshot of one published agent version; only ``status`` (disabling) may change later.
    "agent_profile_version": _entity(
        "agent_profile_version", agent_id="ref:agent_definition", agent_version="posint", definition="obj",
        content_hash="sha256", status="enum:Status", created_by="actor"),
    "agent_run": _entity(
        "agent_run", agent_id="ref:agent_definition", agent_version="posint", department_id="ref:department",
        case_id="ref:case?", task_id="ref:task?", state="enum:RunState",
        budget={"max_steps": "posint", "token_budget": "posint", "deadline_seconds": "posint"},
        skill_versions="list:str", started_at="ts?", finished_at="ts?"),
    # Skills are a global declarative catalog: instructions never grant access by themselves.
    "skill_definition": _entity(
        "skill_definition", tenant=False, skill_key="slug", skill_version="semver", content_hash="sha256",
        instructions="text", required_tools="list:slug", operations="list:enum:Operation",
        status="enum:Status", description="text?", inputs="list:slug?", outputs="list:slug?",
        risks="list:enum:RiskTag?", settings="list:slug?", tool_operations="obj?", fictitious="bool?"),
    "skill_grant": _entity(
        "skill_grant", agent_id="ref:agent_definition", skill_id="ref:skill_definition",
        skill_version="semver", connection_id="ref:connection?", operations="list:enum:Operation",
        resource="str?", expires_at="ts?", granted_by="actor", revoked_at="ts?"),
    "connection": _entity(
        "connection", label="str", provider="slug", connection_kind="enum:ConnectionKind",
        transport="enum:McpTransport?", endpoint="url?", credential_ref="ref:credential?",
        catalog_entry_id="ref:mcp_catalog_entry?", scopes="list:slug?", status="enum:Status"),
    # Metadata only: the secret value lives in the vault under (tenant_id, credential id).
    "credential": _entity("credential", label="str", store="enum:CredentialStore", state="enum:CredentialState"),
    # Approved MCP servers (global, operator-approved). stdio names a launcher id, never a command line.
    "mcp_catalog_entry": _entity(
        "mcp_catalog_entry", tenant=False, catalog_key="slug", entry_version="semver",
        transport="enum:McpTransport", url="url?", launcher="slug?", tools="list:slug", approved_by="actor",
        status="enum:Status"),
    "case": _entity(
        "case", department_id="ref:department", state="enum:CaseState",
        owner_agent_id="ref:agent_definition?", customer_ref="str?", objective="text", closed_at="ts?"),
    "task": _entity(
        "task", case_id="ref:case", parent_task_id="ref:task?", assignee_agent_id="ref:agent_definition",
        state="enum:TaskState", objective="text", expected_result="text", due_at="ts?"),
    "approval_request": _entity(
        "approval_request", case_id="ref:case", requested_by="actor", action="slug",
        payload_hash="sha256", expires_at="ts", state="enum:ApprovalState", single_use="bool",
        decided_by="actor?"),
    "handoff": _entity(
        "handoff", case_id="ref:case", task_id="ref:task?", handoff_kind="enum:HandoffKind",
        from_agent_id="ref:agent_definition", to_agent_id="ref:agent_definition", reason="text",
        context_summary="text", sources="list:str", state="enum:HandoffState", idempotency_key="str",
        expected_case_revision="posint", return_requested="bool", expires_at="ts?"),
    "evidence": _entity(
        "evidence", case_id="ref:case", source="str", collected_at="ts", summary="text",
        content_hash="sha256?"),
    # Contacts: a person's identity is separate from the addresses seen or linked to them.
    "contact": _entity("contact", display_name="str", status="enum:Status"),
    "contact_address": _entity(
        "contact_address", contact_id="ref:contact?", channel="enum:Channel", address="str",
        link_state="enum:LinkState"),
    "contact_link": _entity(
        "contact_link", contact_id="ref:contact", department_id="ref:department", relation="enum:Relation"),
    # Default-deny: an agent or operator reaches a resource only through an explicit grant.
    "access_grant": _entity(
        "access_grant", principal_id="actor", resource_kind="enum:ResourceKind", resource_id="str?",
        actions="list:enum:AccessAction", granted_by="actor", expires_at="ts?"),
    # Envelopes. ``case_id`` may be null only for configuration messages (type "config.*").
    "command": _entity(
        "command", envelope=True, type="slug", case_id="ref:case?", task_id="ref:task?",
        correlation_id="id:correlation", causation_id="id:envelope?", actor_id="actor",
        expected_revision="posint?", idempotency_key="str", issued_at="ts", payload="obj"),
    "event": _entity(
        "event", envelope=True, type="slug", case_id="ref:case?", correlation_id="id:correlation",
        causation_id="id:envelope?", actor_id="actor", revision="posint", occurred_at="ts", payload="obj"),
}


def content_hash(definition) -> str:
    """sha256 of the canonical JSON of a document; identifies a published version's content."""
    canonical = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def kinds():
    return tuple(SCHEMAS)


def _is_ts(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _split(spec):
    optional = spec.endswith("?")
    return (spec[:-1] if optional else spec), optional


def _check(spec, value, path, errors):
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            errors.append(f"{path}: esperado objeto")
        else:
            _check_fields(spec, value, path, errors)
        return
    base, optional = _split(spec)
    if value is None:
        if not optional:
            errors.append(f"{path}: obrigatorio")
        return
    head, _, rest = base.partition(":")
    ok, label = True, base
    if head == "list":
        if not isinstance(value, list):
            errors.append(f"{path}: esperado lista")
            return
        for index, item in enumerate(value):
            _check(rest, item, f"{path}[{index}]", errors)
        return
    if head in ("id", "ref"):
        ok = is_id(value, "cmd") or is_id(value, "evt") if rest == "envelope" else is_id(value, PREFIXES[rest])
        label = f"id opaco {rest}"
    elif head == "enum":
        ok, label = value in ENUMS[rest], f"um de {', '.join(ENUMS[rest])}"
    elif base in ("str", "text"):
        limit = MAX_TEXT if base == "text" else 300
        ok, label = isinstance(value, str) and value.strip() != "" and len(value) <= limit, f"texto 1-{limit}"
    elif base in ("int", "posint"):
        ok = isinstance(value, int) and not isinstance(value, bool) and (base == "int" or value >= 1)
    elif base == "bool":
        ok = isinstance(value, bool)
    elif base == "ts":
        ok, label = _is_ts(value), "data/hora ISO-8601 com fuso"
    elif base == "sha256":
        ok = isinstance(value, str) and bool(_SHA256.match(value))
    elif base == "semver":
        ok, label = isinstance(value, str) and bool(_SEMVER.match(value)), "versao X.Y.Z"
    elif base == "slug":
        ok = isinstance(value, str) and bool(_SLUG.match(value))
    elif base == "url":
        parts = urlparse(value) if isinstance(value, str) else None
        ok = bool(parts and parts.scheme in ("http", "https") and parts.hostname and not parts.username)
        label = "url http(s) sem credenciais"
    elif base == "actor":
        ok, label = isinstance(value, str) and any(is_id(value, p) for p in ACTOR_PREFIXES), "id de ator (usr/agt/sys)"
    elif base == "obj":
        ok = isinstance(value, dict)
    if not ok:
        errors.append(f"{path}: valor invalido, esperado {label}")


def _check_fields(spec, doc, path, errors):
    for name in doc:
        if name not in spec:
            errors.append(f"{path}.{name}: campo desconhecido")
    for name, field in spec.items():
        if name in doc:
            _check(field, doc[name], f"{path}.{name}", errors)
        elif not (isinstance(field, str) and field.endswith("?")):
            errors.append(f"{path}.{name}: obrigatorio")


def find_secrets(value, path="$"):
    """Paths of inline secrets: suspicious key names (except ``*_ref``/``*_id``) or secret-like values."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            safe = key in _SAFE_KEYS or key.endswith(("_ref", "_id", "_ids", "_hash"))
            if not safe and _SECRET_KEY.search(key):
                found.append(f"{path}.{key}")
            else:
                found += find_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += find_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        found.append(path)
    return found


def _rule_address(doc):
    linked = doc["link_state"] == "vinculado"
    if linked != (doc["contact_id"] is not None):
        return ["$.contact_id: obrigatorio se vinculado e proibido se observado"]
    value = doc["address"]
    if doc["channel"] == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return ["$.address: email invalido"]
    if doc["channel"] == "telefone" and not re.fullmatch(r"\+?[0-9 ()-]{6,20}", value):
        return ["$.address: telefone invalido"]
    return []


def _rule_grant(doc):
    if doc["resource_id"] is not None and not is_id(doc["resource_id"], PREFIXES[doc["resource_kind"]]):
        return ["$.resource_id: deve ser um id opaco do tipo do recurso ou nulo (todos da empresa)"]
    return [] if doc["actions"] else ["$.actions: informe ao menos uma acao"]


def _rule_catalog(doc):
    if doc["transport"] == "stdio":
        return [] if doc["launcher"] and doc["url"] is None else ["$.launcher: stdio exige launcher e proibe url"]
    return [] if doc["url"] and doc["launcher"] is None else ["$.url: http/sse exige url e proibe launcher"]


_RULES = {"contact_address": _rule_address, "access_grant": _rule_grant, "mcp_catalog_entry": _rule_catalog}


def validate(kind, doc):
    """Return ``doc`` when valid; raise ContractError otherwise (no mutation, no I/O)."""
    if kind not in SCHEMAS:
        raise ContractError([f"tipo desconhecido: {kind}"], "unknown_kind")
    if not isinstance(doc, dict):
        raise ContractError(["$: esperado objeto"])
    version = doc.get("schema_version")
    if isinstance(version, bool) or version not in SUPPORTED_VERSIONS:
        raise ContractError([f"schema_version nao suportada: {version!r}"], "unsupported_schema_version")
    secrets = [f"{p}: segredo embutido; use credential_ref" for p in find_secrets(doc)]
    if secrets:
        raise ContractError(secrets, "secret")
    errors = []
    if doc.get("kind") != kind:
        errors.append(f"$.kind: esperado '{kind}'")
    _check_fields(SCHEMAS[kind], doc, "$", errors)
    if kind in ("command", "event") and doc.get("case_id") is None \
            and not str(doc.get("type", "")).startswith("config."):
        errors.append("$.case_id: obrigatorio exceto em mensagens config.*")
    if not errors and kind in _RULES:
        errors += _RULES[kind](doc)
    if errors:
        raise ContractError(errors)
    return doc


def _refs(spec, value, found):
    if isinstance(spec, dict):
        for name, field in spec.items():
            if isinstance(value, dict) and name in value:
                _refs(field, value[name], found)
    elif value is not None:
        base, _ = _split(spec)
        if base.startswith("list:"):
            for item in value:
                _refs(base[5:], item, found)
        elif base.startswith("ref:"):
            found.append((base[4:], value))


def validate_bundle(docs):
    """Validate documents and cross references; every reference must resolve inside one tenant."""
    errors, index = [], {}
    for position, doc in enumerate(docs):
        try:
            validate(doc.get("kind") if isinstance(doc, dict) else None, doc)
        except ContractError as error:
            errors += [f"[{position}] {e}" for e in error.errors]
            continue
        if doc["id"] in index:
            errors.append(f"[{position}] id duplicado: {doc['id']}")
        index[doc["id"]] = doc
    for doc in index.values():
        tenant = doc["id"] if doc["kind"] == "tenant" else doc.get("tenant_id")
        found = []
        _refs(SCHEMAS[doc["kind"]], doc, found)
        for target_kind, target_id in found:
            target = index.get(target_id)
            if target is None or target["kind"] != target_kind:
                errors.append(f"{doc['id']}: referencia invalida para {target_kind} {target_id}")
            elif target_kind not in ("skill_definition", "mcp_catalog_entry") and (
                    target["id"] if target_kind == "tenant" else target.get("tenant_id")) != tenant:
                errors.append(f"{doc['id']}: referencia {target_id} pertence a outra empresa")
    if errors:
        raise ContractError(errors)
    return docs


def _json_type(spec):
    if isinstance(spec, dict):
        return _object_schema(spec)
    base, optional = _split(spec)
    head, _, rest = base.partition(":")
    if head == "list":
        out = {"type": "array", "items": _json_type(rest)}
    elif head in ("id", "ref"):
        prefix = "(cmd|evt)" if rest == "envelope" else PREFIXES[rest]
        out = {"type": "string", "pattern": f"^{prefix}_[0-9a-f]{{32}}$"}
    elif head == "enum":
        out = {"type": "string", "enum": list(ENUMS[rest])}
    elif base == "actor":
        out = {"type": "string", "pattern": f"^({'|'.join(ACTOR_PREFIXES)})_[0-9a-f]{{32}}$"}
    else:
        out = {
            "str": {"type": "string", "minLength": 1, "maxLength": 300},
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
            "int": {"type": "integer"}, "posint": {"type": "integer", "minimum": 1},
            "bool": {"type": "boolean"}, "ts": {"type": "string", "format": "date-time"},
            "sha256": {"type": "string", "pattern": _SHA256.pattern},
            "semver": {"type": "string", "pattern": _SEMVER.pattern},
            "slug": {"type": "string", "pattern": _SLUG.pattern},
            "url": {"type": "string", "format": "uri", "pattern": "^https?://"},
            "obj": {"type": "object"},
        }[base]
    return {"anyOf": [out, {"type": "null"}]} if optional else out


def _object_schema(spec):
    required = [n for n, f in spec.items() if not (isinstance(f, str) and f.endswith("?"))]
    return {"type": "object", "additionalProperties": False, "required": required,
            "properties": {n: _json_type(f) for n, f in spec.items()}}


def export_json_schema(kind):
    """JSON Schema (draft 2020-12) mirroring SCHEMAS; structural only, ``validate`` is authoritative."""
    schema = _object_schema(SCHEMAS[kind])
    schema["properties"]["kind"] = {"const": kind}
    schema["properties"]["schema_version"] = {"const": SCHEMA_VERSION}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": kind, **schema}
