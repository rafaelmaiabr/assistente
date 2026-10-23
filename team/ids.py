"""Opaque identifiers: ``<prefix>_<32 hex>``. The prefix only aids debugging."""
import re
import uuid

PREFIXES = {
    "tenant": "ten", "department": "dep", "agent_definition": "agt", "agent_run": "run",
    "skill_definition": "skl", "skill_grant": "grt", "connection": "con", "case": "cas",
    "task": "tsk", "approval_request": "apr", "handoff": "hof", "evidence": "evd",
    "command": "cmd", "event": "evt",
    "mcp_catalog_entry": "mcp", "contact": "ctc", "contact_address": "adr", "contact_link": "lnk", "access_grant": "acl",
    # not stored entities: correlation chains, credential vault entries, actors
    "correlation": "cor", "credential": "crd", "user": "usr", "system": "sys",
}
ACTOR_PREFIXES = ("usr", "agt", "sys")
_PATTERN = re.compile(r"^([a-z]{3})_[0-9a-f]{32}$")


def new_id(kind: str) -> str:
    """Create a random id for an entity kind (see PREFIXES)."""
    return f"{PREFIXES[kind]}_{uuid.uuid4().hex}"


def is_id(value, prefix: str | None = None) -> bool:
    match = _PATTERN.match(value) if isinstance(value, str) else None
    return bool(match) and (prefix is None or match.group(1) == prefix)
