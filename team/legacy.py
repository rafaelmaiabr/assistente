"""Import the single-assistant ``config.json`` into a draft team bundle.

Pure function: never reads or writes ``config.json``. The caller decides whether to persist the
result; the legacy file keeps working untouched (see docs/TEAM-CONTRACTS.md).
"""
from datetime import datetime, timezone
from urllib.parse import urlparse

from .ids import new_id
from .schema import ContractError, validate_bundle


def import_legacy_config(config: dict, now: str | None = None) -> list:
    """Return a validated bundle (tenant, department, agent, one MCP connection per server)."""
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamps = {"revision": 1, "created_at": stamp, "updated_at": stamp}
    tenant, dept, agent = new_id("tenant"), new_id("department"), new_id("agent_definition")
    docs = [
        {"kind": "tenant", "schema_version": 1, "id": tenant, "name": "Local", "status": "ativo", **stamps},
        {"kind": "department", "schema_version": 1, "id": dept, "tenant_id": tenant, "name": "Geral",
         "scope": "Assistente local importado da configuracao anterior", "status": "ativo", **stamps},
        {"kind": "agent_definition", "schema_version": 1, "id": agent, "tenant_id": tenant,
         "department_id": dept, "agent_version": 1, "name": "Assistente",
         "scope": "Conversa geral e ferramentas MCP configuradas",
         "personality": {"tone": "cordial e objetivo", "initiative": "responder o que foi pedido"},
         "out_of_scope": "Acoes sem ferramenta autorizada", "skill_grant_ids": [],
         "handoff_target_ids": [], "model_policy_ref": "legacy-local", "voice_profile_ref": None,
         "status": "ativo", **stamps},
    ]
    for number, server in enumerate(config.get("mcp_servers") or [], start=1):
        host = urlparse(server["url"]).hostname or "mcp"
        docs.append({
            "kind": "connection", "schema_version": 1, "id": new_id("connection"), "tenant_id": tenant,
            "label": f"MCP {number} ({host})", "provider": "mcp-legado", "connection_kind": "mcp",
            "transport": "http", "endpoint": server["url"], "credential_ref": None, "status": "ativo", **stamps})
    try:
        return validate_bundle(docs)
    except ContractError as error:
        raise ContractError(
            [f"configuracao antiga nao importavel: {e}" for e in error.errors], error.code) from error
