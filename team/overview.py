"""Read models for the cases center: phases, summaries and one authoritative snapshot per case.

Everything here is derived from the database on request, so a client that reloads or reconnects gets
the same truth: a snapshot plus a cursor, then events after that cursor (deduplicated by event id).
"""
from .access import NotFound

# What the person looking at the list needs to tell apart at a glance.
PHASES = {
    "andamento": "Em andamento",
    "aguardando_cliente": "Aguardando o cliente",
    "aguardando_humano": "Aguardando uma pessoa",
    "falha": "Falhou",
    "concluida": "Concluída",
    "cancelada": "Cancelada",
}
_WORKING = ("recebida", "triagem", "em_execucao", "aguardando_subtarefa")


def phase_of(case, needs_human=False):
    """Phase shown to the operator. A closed case keeps its closed phase even when it still needs reconciliation."""
    state = case["state"]
    if state == "resolvida":
        return "concluida"
    if state == "cancelada":
        return "cancelada"
    if state == "falhou":
        return "falha"
    if needs_human or state in ("aguardando_aprovacao", "pausada") or case.get("operator_control"):
        return "aguardando_humano"
    if state == "aguardando_cliente":
        return "aguardando_cliente"
    return "andamento"


def _signals(session, case):
    """Human-attention signals of one case: pending approvals, unknown effects, handoffs that need the operator."""
    case_id = case["id"]
    approvals = session.list("approval_request", case_id=case_id, limit=1000)
    messages = session.list("outbound_message", case_id=case_id, limit=1000)
    handoffs = session.list("handoff", case_id=case_id, limit=1000)
    decisions = session.list("triage_decision", case_id=case_id, limit=1000) if case["state"] in ("recebida", "triagem") else []
    return {
        "pending_approvals": sum(1 for a in approvals if a["state"] == "pendente"),
        "reconciliation": sum(1 for a in approvals if a.get("effect_outcome") == "desconhecido")
        + sum(1 for m in messages if m["state"] == "desconhecido"),
        "handoff_attention": sum(1 for h in handoffs if h.get("attention")),
        "human_queue": int(bool(decisions and decisions[-1]["decision"] == "fila_humana")),
    }, approvals, messages, handoffs


def _needs_human(signals):
    return bool(signals["pending_approvals"] or signals["reconciliation"] or signals["handoff_attention"]
                or signals["human_queue"])


def summarize(session, case, names):
    signals, *_ = _signals(session, case)
    return {
        "id": case["id"], "objective": case["objective"], "state": case["state"], "revision": case["revision"],
        "phase": phase_of(case, _needs_human(signals)), "priority": case.get("priority"), "origin": case.get("origin"),
        "owner_agent_id": case["owner_agent_id"], "owner_name": names.get(case["owner_agent_id"]),
        "operator_control": bool(case.get("operator_control")), "customer_ref": case["customer_ref"],
        "updated_at": case["updated_at"], **signals,
    }


def list_cases(services, ctx, state=None, phase=None, limit=200):
    with services.repo.transaction(ctx.tenant_id) as session:
        names = {a["id"]: a["name"] for a in session.list("agent_definition", limit=1000)}
        cases = [c for c in session.list("case", limit=limit, **({"state": state} if state else {}))
                 if services.access.allowed(ctx, session, "read", "case", c["id"])]
        summaries = [summarize(session, c, names) for c in cases]
        cursor = session.last_event_seq()
    if phase:
        summaries = [s for s in summaries if s["phase"] == phase]
    order = {"aguardando_humano": 0, "falha": 1, "andamento": 2, "aguardando_cliente": 3, "concluida": 4, "cancelada": 5}
    summaries.sort(key=lambda s: (order[s["phase"]], s["updated_at"]), reverse=False)
    return {"cases": summaries, "cursor": cursor}


def snapshot(services, ctx, case_id):
    """Everything about one case at one instant, with the event cursor to continue from."""
    with services.repo.transaction(ctx.tenant_id) as session:
        case = session.get("case", case_id) if isinstance(case_id, str) else None
        if case is None or not services.access.allowed(ctx, session, "read", "case", case_id):
            raise NotFound()
        names = {a["id"]: a["name"] for a in session.list("agent_definition", limit=1000)}
        signals, approvals, messages, handoffs = _signals(session, case)
        tasks = session.list("task", case_id=case_id, limit=1000)
        entries = [("entrada", e) for e in session.list("case_entry", case_id=case_id, limit=1000)]
        entries += [("evidencia", e) for e in session.list("evidence", case_id=case_id, limit=1000)]
        events = [("evento", e) for _, e in session.events(case_id, 0, 1000)]
        decisions = session.list("triage_decision", case_id=case_id, limit=1000)
        cursor = session.last_event_seq(case_id)
    key = {"entrada": "created_at", "evidencia": "created_at", "evento": "occurred_at"}
    timeline = [{"tipo": kind, "em": doc[key[kind]], "item": _public(kind, doc)}
                for kind, doc in sorted(entries + events, key=lambda p: (p[1][key[p[0]]], p[1]["id"]))]
    return {
        "case": summarize_from(case, names, signals), "timeline": timeline, "cursor": cursor,
        "tasks": [{k: t[k] for k in ("id", "state", "objective", "expected_result", "due_at", "assignee_agent_id")}
                  | {"assignee_name": names.get(t["assignee_agent_id"])} for t in tasks],
        "handoffs": [{k: h.get(k) for k in ("id", "handoff_kind", "state", "from_agent_id", "to_agent_id", "reason",
                                             "expires_at", "attention", "resolution", "created_at")}
                     | {"from_name": names.get(h["from_agent_id"]), "to_name": names.get(h["to_agent_id"])}
                     for h in sorted(handoffs, key=lambda h: h["created_at"])],
        "approvals": [_approval_view(a) for a in sorted(approvals, key=lambda a: a["created_at"])],
        "messages": [{k: m[k] for k in ("id", "state", "channel", "to", "subject", "provider_id", "last_error", "created_at")}
                     for m in sorted(messages, key=lambda m: m["created_at"])],
        "triage": [{k: d[k] for k in ("decision", "method", "capabilities", "confidence_pct", "reasons", "question", "created_at")}
                   for d in decisions],
    }


def summarize_from(case, names, signals):
    return {"id": case["id"], "objective": case["objective"], "state": case["state"], "revision": case["revision"],
            "phase": phase_of(case, _needs_human(signals)), "priority": case.get("priority"), "origin": case.get("origin"),
            "owner_agent_id": case["owner_agent_id"], "owner_name": names.get(case["owner_agent_id"]),
            "operator_control": bool(case.get("operator_control")), "customer_ref": case["customer_ref"],
            "updated_at": case["updated_at"], "closed_at": case.get("closed_at"), **signals}


def _approval_view(approval):
    """What the approver must see: the concrete action, recipients, amount and what exactly the hash covers."""
    normalized = approval.get("normalized_action") or {}
    return {k: approval.get(k) for k in ("id", "state", "action", "payload_hash", "expires_at", "resource", "recipient",
                                          "amount_cents", "currency", "reason", "decided_by", "decided_at",
                                          "effect_outcome", "effect_detail", "revision", "created_at", "requested_by")} \
        | {"params": normalized.get("params", {})}


def _public(kind, doc):
    if kind == "evento":
        return {k: doc[k] for k in ("id", "type", "revision", "occurred_at", "actor_id", "payload")}
    keys = {"entrada": ("id", "entry_type", "direction", "channel", "author", "body", "created_at"),
            "evidencia": ("id", "source", "summary", "collected_at", "evidence_type", "reliability", "created_at")}[kind]
    return {k: doc.get(k) for k in keys}


def events_after(services, ctx, after, case_id=None, limit=200):
    """Events after a cursor, oldest first, plus the new cursor. Clients deduplicate by event id."""
    with services.repo.transaction(ctx.tenant_id) as session:
        if case_id is not None:
            case = session.get("case", case_id)
            if case is None or not services.access.allowed(ctx, session, "read", "case", case_id):
                raise NotFound()
        rows = session.events(case_id, max(int(after), 0), limit)
        latest = session.last_event_seq(case_id)
    return {"events": [{"seq": seq, **{k: e[k] for k in ("id", "type", "case_id", "revision", "occurred_at", "actor_id")},
                        "payload": e["payload"]} for seq, e in rows],
            "cursor": rows[-1][0] if rows else max(int(after), 0), "latest": latest}


def attention(services, ctx):
    """The operator's inbox: pending approvals, unknown outcomes, cases waiting in the human queue, handoffs to review."""
    with services.repo.transaction(ctx.tenant_id) as session:
        approvals = session.list("approval_request", limit=1000)
        messages = session.list("outbound_message", limit=1000)
        handoffs = session.list("handoff", limit=1000)
        cases = {c["id"]: c for c in session.list("case", limit=1000)}
    return {
        "pending_approvals": [_approval_view(a) | {"case_id": a["case_id"]} for a in approvals if a["state"] == "pendente"],
        "reconciliation": [{"kind": "approval", "id": a["id"], "case_id": a["case_id"], "action": a["action"]}
                           for a in approvals if a.get("effect_outcome") == "desconhecido"]
        + [{"kind": "message", "id": m["id"], "case_id": m["case_id"], "action": "enviar_mensagem"}
           for m in messages if m["state"] == "desconhecido"],
        "handoffs": [{"id": h["id"], "case_id": h["case_id"], "state": h["state"], "resolution": h.get("resolution")}
                     for h in handoffs if h.get("attention")],
        "paused": [c["id"] for c in cases.values() if c.get("operator_control")],
    }
