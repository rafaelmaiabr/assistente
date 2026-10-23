"""What a conversation is attached to: company, case and the agent who currently owns it.

The binding is read from the database **at the start of each turn** and never from what the screen shows, so a
concurrent click cannot change who answers mid-turn. The owner (and therefore the voice profile) changes only when a
transfer is *accepted*, because that is when the case's owner changes. This module only describes the attachment: running
a team agent inside the chat is the runtime's job, and the interactive chat keeps using the local assistant for now.
"""
from . import overview
from .access import NotFound

LOCAL = {"mode": "local", "company": None, "case": None, "agent": None, "waiting_human": False, "paused": False, "operator_control": False}


def local_binding():
    return dict(LOCAL)


def team_binding(services, tenant_id, case_id):
    """Snapshot of the attachment for one case. ``NotFound`` when the company or case is unknown or not visible."""
    if not (isinstance(tenant_id, str) and isinstance(case_id, str)):
        raise NotFound()
    companies = {t["id"]: t for t in services.repo.list_global("tenant", limit=1000)}
    if tenant_id not in companies:
        raise NotFound()
    ctx = services.access.tenant_context(services.operator, tenant_id)
    with services.repo.transaction(tenant_id) as session:
        case = session.get("case", case_id)
        if case is None or not services.access.allowed(ctx, session, "read", "case", case_id):
            raise NotFound()
        agents = {a["id"]: a for a in session.list("agent_definition", limit=1000)}
        summary = overview.summarize(session, case, {i: a["name"] for i, a in agents.items()})
    owner = agents.get(case["owner_agent_id"])
    return {
        "mode": "equipe", "company": {"id": tenant_id, "name": companies[tenant_id]["name"]},
        "case": {"id": case_id, "label": case["objective"][:80], "state": case["state"], "phase": summary["phase"],
                 "revision": case["revision"]},
        "agent": None if owner is None else {"id": owner["id"], "name": owner["name"], "version": owner["agent_version"],
                                              "voice_profile_ref": owner.get("voice_profile_ref")},
        "waiting_human": summary["phase"] == "aguardando_humano", "paused": case["state"] == "pausada",
        "operator_control": bool(case.get("operator_control"))}


def fingerprint(binding):
    """What makes two bindings 'the same conversation partner': company, case, owner and its version."""
    agent = binding.get("agent") or {}
    return (binding.get("mode"), (binding.get("company") or {}).get("id"), (binding.get("case") or {}).get("id"),
            agent.get("id"), agent.get("version"))


def compact(binding):
    """The few identifiers attached to every message of a turn (no names, no content)."""
    agent = binding.get("agent") or {}
    return {"mode": binding.get("mode"), "company_id": (binding.get("company") or {}).get("id"),
            "case_id": (binding.get("case") or {}).get("id"), "agent_id": agent.get("id"), "agent_version": agent.get("version")}
