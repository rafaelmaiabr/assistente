"""Delegation, transfer of responsibility and return between agents.

* **Delegation** creates a subtask for another agent; the case keeps its owner, who is the only one
  allowed to speak to the customer. The assignee works from a small envelope and returns a result.
* **Transfer** is a proposal that the recipient must accept. Acceptance swaps the owner atomically and
  only if the case did not change since the proposal (expected revision). Until then, and after a
  refusal, expiry or failure, the previous owner stays responsible.

A handoff widens nobody's access: the recipient needs the access its own grants already give, the
envelope carries only what the proposer chose to share, and every limit (hops, depth, open subtasks,
cycles) is enforced here. Failures leave the case with its owner and flag the handoff for the operator.
"""
from datetime import datetime, timedelta, timezone

from .access import AccessContext, AccessError, NotFound, PermissionDenied
from .cases import CaseRejected, clean_text
from .ids import new_id
from .repository import IntegrityViolation

MAX_TRANSFERS = 5
MAX_DEPTH = 3
MAX_OPEN_SUBTASKS = 5
DEFAULT_TTL = 3600
MAX_SUMMARY = 1000
OPEN_TASK = ("pendente", "em_execucao", "aguardando")
TERMINAL_CASE = ("resolvida", "cancelada", "falhou")
_TECHNICAL = ("resultado_ferramenta", "recibo", "documento")


class HandoffRejected(ValueError):
    """The handoff is not allowed; the message is safe to show."""


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class HandoffService:
    def __init__(self, repository, access, cases, clock=lambda: datetime.now(timezone.utc)):
        self.repository, self.access, self.cases, self.clock = repository, access, cases, clock

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _ctx(self, ctx):
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()

    def _now(self):
        return self.clock()

    def _text(self, value, limit=MAX_SUMMARY):
        text = clean_text(value)
        if len(text) > limit:
            raise HandoffRejected(f"Texto longo demais (limite de {limit} caracteres).")
        return text

    def _agent(self, session, agent_id):
        agent = session.get("agent_definition", agent_id) if isinstance(agent_id, str) else None
        if agent is None:
            raise NotFound()
        if agent["status"] != "ativo":
            raise HandoffRejected("Agente desativado.")
        return agent

    def _owner_view(self, ctx, session, case_id):
        case = session.get("case", case_id) if isinstance(case_id, str) else None
        if case is None:
            raise NotFound()
        return case

    def _involved(self, ctx, handoff):
        return ctx.role == "operator" or ctx.principal_id in (
            handoff["from_agent_id"], handoff["to_agent_id"], handoff.get("return_to"))

    def _event_ctx(self, ctx):
        return ctx

    def _open_tasks(self, session, case_id, exclude=None):
        return [t for t in session.list("task", case_id=case_id, limit=1000)
                if t["state"] in OPEN_TASK and t["id"] != exclude]

    def _note(self, session, ctx, case_id, author, body):
        now = _stamp(self._now())
        session.save({"kind": "case_entry", "schema_version": 1, "id": new_id("case_entry"), "tenant_id": ctx.tenant_id,
                      "revision": 1, "created_at": now, "updated_at": now, "case_id": case_id,
                      "entry_type": "nota_interna", "direction": None, "channel": None, "author": author,
                      "body": clean_text(body)[:8000]})

    def _bump(self, ctx, session, case, event_type, payload, **changes):
        return self.cases.commit_change(ctx, session, case, {**changes}, event_type, payload)

    def _resume_owner(self, ctx, session, case, handoff_event, payload):
        """Back to work for the owner once no subtask is open."""
        changes = {}
        if case["state"] == "aguardando_subtarefa" and not self._open_tasks(session, case["id"]):
            changes["state"] = "em_execucao"
        return self._bump(ctx, session, case, handoff_event, payload, **changes)

    # -- proposing ----------------------------------------------------------------------------------------------------
    def _propose(self, ctx, kind, case_id, to_agent_id, *, reason, summary, objective, expected_result, evidence_ids,
                 due_at, return_to, ttl_seconds, idempotency_key, parent_task_id):
        self._ctx(ctx)
        reason, summary = self._text(reason, 500), self._text(summary)
        objective = self._text(objective, 500) if objective is not None else None
        expected_result = self._text(expected_result, 500) if expected_result is not None else None
        key = idempotency_key or f"{kind}:{new_id('correlation')}"
        now = self._now()
        with self.repository.transaction(ctx.tenant_id) as session:
            known = session.list("handoff", limit=1000)
            for existing in known:
                if existing["idempotency_key"] == key:
                    return existing, False
            case = session.get("case", case_id) if isinstance(case_id, str) else None
            if case is None:
                raise NotFound()
            parent = session.get("task", parent_task_id) if parent_task_id else None
            if parent_task_id and (parent is None or parent["case_id"] != case_id):
                raise NotFound()
            is_owner = case["owner_agent_id"] is not None and ctx.principal_id == case["owner_agent_id"]
            from_agent = case["owner_agent_id"] if ctx.role == "operator" and parent is None else ctx.principal_id
            if ctx.role == "agent":
                assignee = parent is not None and parent["assignee_agent_id"] == ctx.principal_id \
                    and parent["state"] in OPEN_TASK
                if not (is_owner and parent is None) and not (kind == "delegacao" and assignee):
                    raise NotFound()
            if kind == "transferencia" and (parent is not None or ctx.role != "operator" and not is_owner):
                raise NotFound()
            if case["owner_agent_id"] is None:
                raise HandoffRejected("A demanda ainda esta na fila inicial: atribua um responsavel antes.")
            if case["state"] in TERMINAL_CASE:
                raise HandoffRejected("A demanda esta encerrada.")
            source = self._agent(session, from_agent)
            target = self._agent(session, to_agent_id)
            if target["id"] == from_agent:
                raise HandoffRejected("Nao e possivel encaminhar para si mesmo.")
            if ctx.role == "agent" and target["id"] not in source["handoff_target_ids"]:
                raise HandoffRejected("O destino nao esta entre os encaminhamentos permitidos para este agente.")
            if evidence_ids:
                for evidence_id in evidence_ids:
                    evidence = session.get("evidence", evidence_id)
                    if evidence is None or evidence["case_id"] != case_id:
                        raise NotFound()
            handoffs = [h for h in known if h["case_id"] == case_id]
            if kind == "transferencia":
                if any(h["handoff_kind"] == "transferencia" and h["state"] == "proposto" for h in handoffs):
                    raise HandoffRejected("Ja existe uma transferencia aguardando aceite para esta demanda.")
                accepted = [h for h in handoffs if h["handoff_kind"] == "transferencia" and h["state"] == "aceito"]
                if len(accepted) >= MAX_TRANSFERS:
                    raise HandoffRejected("Limite de transferencias da demanda atingido: escale ao operador.")
                if target["id"] == case["owner_agent_id"]:
                    raise HandoffRejected("O destino ja e o responsavel atual.")
                if target["id"] in {h["from_agent_id"] for h in accepted}:
                    raise HandoffRejected("Ciclo de transferencias detectado: o destino ja foi responsavel por esta demanda.")
            task = None
            if kind == "delegacao":
                chain, depth, node = {case["owner_agent_id"], from_agent}, 1, parent
                while node is not None:
                    chain.add(node["assignee_agent_id"])
                    depth += 1
                    node = session.get("task", node["parent_task_id"]) if node["parent_task_id"] else None
                if depth > MAX_DEPTH:
                    raise HandoffRejected("Profundidade maxima de delegacao atingida.")
                if target["id"] in chain:
                    raise HandoffRejected("Ciclo de delegacao detectado: o destino ja participa desta cadeia.")
                if len(self._open_tasks(session, case_id)) >= MAX_OPEN_SUBTASKS:
                    raise HandoffRejected("Limite de subtarefas abertas da demanda atingido.")
            return_agent = return_to or from_agent
            if kind == "delegacao" and return_agent != from_agent:
                self._agent(session, return_agent)
            stamp = _stamp(now)
            if kind == "delegacao":
                task = session.save({
                    "kind": "task", "schema_version": 1, "id": new_id("task"), "tenant_id": ctx.tenant_id, "revision": 1,
                    "created_at": stamp, "updated_at": stamp, "case_id": case_id,
                    "parent_task_id": parent["id"] if parent else None, "assignee_agent_id": target["id"],
                    "state": "pendente", "objective": objective or reason,
                    "expected_result": expected_result or "Resultado resumido com fontes", "due_at": due_at})
            next_revision = case["revision"] + 1  # the proposal itself is a recorded change of the case
            handoff = {
                "kind": "handoff", "schema_version": 1, "id": new_id("handoff"), "tenant_id": ctx.tenant_id, "revision": 1,
                "created_at": stamp, "updated_at": stamp, "case_id": case_id, "task_id": task["id"] if task else None,
                "handoff_kind": kind, "from_agent_id": from_agent, "to_agent_id": target["id"], "reason": reason,
                "context_summary": summary, "sources": [f"evidencia:{e}" for e in (evidence_ids or [])],
                "state": "proposto", "idempotency_key": key, "expected_case_revision": next_revision,
                "return_requested": kind == "delegacao", "expires_at": _stamp(now + timedelta(seconds=ttl_seconds)),
                "objective": objective, "expected_result": expected_result, "evidence_ids": list(evidence_ids or []),
                "return_to": return_agent if kind == "delegacao" else None, "attention": False, "resolution": None}
            try:
                session.save(handoff)
            except IntegrityViolation:
                raise HandoffRejected("Ja existe uma transferencia pendente ou esta chave ja foi usada.") from None
            changes = {}
            if kind == "delegacao" and case["state"] == "em_execucao" and parent is None:
                changes["state"] = "aguardando_subtarefa"
            self._bump(ctx, session, case, "handoff.proposed",
                       {"handoff_id": handoff["id"], "kind": kind, "from": from_agent, "to": target["id"]}, **changes)
            return handoff, True

    def propose_transfer(self, ctx, case_id, to_agent_id, *, reason, summary, evidence_ids=(), objective=None,
                         ttl_seconds=DEFAULT_TTL, idempotency_key=None):
        """The owner proposes handing the case over; nothing changes until the recipient accepts."""
        return self._propose(ctx, "transferencia", case_id, to_agent_id, reason=reason, summary=summary,
                             objective=objective, expected_result=None, evidence_ids=evidence_ids, due_at=None,
                             return_to=None, ttl_seconds=ttl_seconds, idempotency_key=idempotency_key,
                             parent_task_id=None)

    def delegate(self, ctx, case_id, to_agent_id, *, reason, objective, expected_result, summary, evidence_ids=(),
                 due_at=None, return_to=None, ttl_seconds=DEFAULT_TTL, idempotency_key=None, parent_task_id=None):
        """Create a subtask for another agent. The case keeps its owner."""
        return self._propose(ctx, "delegacao", case_id, to_agent_id, reason=reason, summary=summary,
                             objective=objective, expected_result=expected_result, evidence_ids=evidence_ids,
                             due_at=due_at, return_to=return_to, ttl_seconds=ttl_seconds,
                             idempotency_key=idempotency_key, parent_task_id=parent_task_id)

    # -- recipient decisions ----------------------------------------------------------------------------------------
    def _handoff_for(self, ctx, session, handoff_id, role):
        handoff = session.get("handoff", handoff_id) if isinstance(handoff_id, str) else None
        if handoff is None:
            raise NotFound()
        who = {"to": handoff["to_agent_id"], "from": handoff["from_agent_id"]}[role]
        if ctx.role == "agent" and ctx.principal_id != who:
            raise NotFound()
        return handoff

    def _close(self, session, handoff, state, resolution, attention=False):
        return session.save({**handoff, "state": state, "resolution": resolution, "attention": attention,
                             "revision": handoff["revision"] + 1, "updated_at": _stamp(self._now())})

    def _settle_failed(self, ctx, session, handoff, state, text, event_type, attention=None):
        """Refusal, expiry or cancellation: the owner keeps the case and the operator is told."""
        case = session.get("case", handoff["case_id"])
        if handoff["task_id"]:
            task = session.get("task", handoff["task_id"])
            if task["state"] in OPEN_TASK:
                session.save({**task, "state": "cancelada", "revision": task["revision"] + 1,
                              "updated_at": _stamp(self._now())})
        closed = self._close(session, handoff, state, text,
                             attention=state in ("recusado", "expirado") if attention is None else attention)
        self._note(session, ctx, case["id"], ctx.principal_id, f"Encaminhamento {state}: {text}")
        self._resume_owner(ctx, session, case, event_type, {"handoff_id": handoff["id"], "to": handoff["to_agent_id"]})
        return closed

    def accept(self, ctx, handoff_id):
        """The recipient accepts. A transfer swaps the owner atomically, only if the case is unchanged."""
        self._ctx(ctx)
        outcome = None
        with self.repository.transaction(ctx.tenant_id) as session:
            handoff = self._handoff_for(ctx, session, handoff_id, "to")
            if handoff["state"] != "proposto":
                raise HandoffRejected("Este encaminhamento ja foi respondido.")
            case = session.get("case", handoff["case_id"])
            if _parse(handoff["expires_at"]) <= self._now():
                self._settle_failed(ctx, session, handoff, "expirado", "prazo de aceite esgotado", "handoff.expired")
                outcome = "Prazo de aceite esgotado; o responsavel anterior continua."
            elif handoff["handoff_kind"] == "transferencia" and (
                    case["revision"] != handoff["expected_case_revision"]
                    or case["owner_agent_id"] != handoff["from_agent_id"]):
                self._settle_failed(ctx, session, handoff, "cancelado", "a demanda mudou desde a proposta", "handoff.cancelled",
                                    attention=True)
                outcome = "A demanda mudou desde a proposta; peca uma nova proposta."
            elif case["state"] in TERMINAL_CASE:
                self._settle_failed(ctx, session, handoff, "cancelado", "demanda encerrada", "handoff.cancelled")
                outcome = "A demanda foi encerrada."
            else:
                target = self._agent(session, handoff["to_agent_id"])
                if handoff["handoff_kind"] == "transferencia":
                    if not self.access.principal_allowed(session, target["id"], "write", "case", case["id"]):
                        raise HandoffRejected("O destino nao tem acesso a esta demanda; aceitar nao concede acesso.")
                    accepted = self._close(session, handoff, "aceito", "transferencia aceita")
                    self._note(session, ctx, case["id"], ctx.principal_id,
                               f"Responsabilidade transferida: {handoff['reason']}")
                    self._bump(ctx, session, case, "case.transferred",
                               {"handoff_id": handoff["id"], "from": handoff["from_agent_id"], "to": target["id"]},
                               owner_agent_id=target["id"])
                    return accepted
                task = session.get("task", handoff["task_id"])
                session.save({**task, "state": "em_execucao", "revision": task["revision"] + 1,
                              "updated_at": _stamp(self._now())})
                accepted = self._close(session, handoff, "aceito", "delegacao aceita")
                self._bump(ctx, session, case, "handoff.accepted", {"handoff_id": handoff["id"], "to": target["id"]})
                return accepted
        raise HandoffRejected(outcome)

    def decline(self, ctx, handoff_id, reason):
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            handoff = self._handoff_for(ctx, session, handoff_id, "to")
            if handoff["state"] != "proposto":
                raise HandoffRejected("Este encaminhamento ja foi respondido.")
            return self._settle_failed(ctx, session, handoff, "recusado", self._text(reason, 500), "handoff.declined")

    def cancel(self, ctx, handoff_id, reason="cancelado pelo proponente"):
        """The proposer (or the operator) withdraws a handoff that is pending or running."""
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            handoff = self._handoff_for(ctx, session, handoff_id, "from")
            if handoff["state"] not in ("proposto", "aceito") or handoff["handoff_kind"] == "transferencia" \
                    and handoff["state"] == "aceito":
                raise HandoffRejected("Este encaminhamento nao pode mais ser cancelado.")
            return self._settle_failed(ctx, session, handoff, "cancelado", self._text(reason, 500), "handoff.cancelled")

    def expire_due(self, tenant_id, now=None):
        """Backend sweep (scheduler): proposals past their deadline expire and the owner keeps the case."""
        ctx = self.access.tenant_context(self.access.local_operator(), tenant_id)
        limit, expired = now or self._now(), []
        with self.repository.transaction(tenant_id) as session:
            for handoff in session.list("handoff", state="proposto", limit=1000):
                if _parse(handoff["expires_at"]) <= limit:
                    expired.append(self._settle_failed(ctx, session, handoff, "expirado", "prazo de aceite esgotado",
                                                       "handoff.expired"))
        return expired

    # -- subtask work ---------------------------------------------------------------------------------------------------
    def _task(self, ctx, session, task_id):
        task = session.get("task", task_id) if isinstance(task_id, str) else None
        if task is None or (ctx.role == "agent" and ctx.principal_id != task["assignee_agent_id"]):
            raise NotFound()
        handoff = next((h for h in session.list("handoff", task_id=task_id, limit=10)), None)
        return task, handoff

    def complete_task(self, ctx, task_id, summary, evidence=()):
        """The assignee finishes and returns a result. ``evidence`` items follow the same rules as any agent's."""
        self._ctx(ctx)
        text = self._text(summary, 4000)
        with self.repository.transaction(ctx.tenant_id) as session:
            task, handoff = self._task(ctx, session, task_id)
            if task["state"] != "em_execucao" or handoff is None:
                raise HandoffRejected("A subtarefa nao esta em andamento.")
            case = session.get("case", task["case_id"])
            ids = []
            for item in evidence:
                reliability, kind = item.get("reliability"), item.get("evidence_type")
                if ctx.role != "operator" and reliability == "verificada" and (
                        kind not in _TECHNICAL or not item.get("content_hash")):
                    raise CaseRejected("Evidencia verificada exige resultado de ferramenta, recibo ou documento com hash.")
                now = _stamp(self._now())
                saved = session.save({
                    "kind": "evidence", "schema_version": 1, "id": new_id("evidence"), "tenant_id": ctx.tenant_id,
                    "revision": 1, "created_at": now, "updated_at": now, "case_id": case["id"],
                    "source": item["source"], "collected_at": item.get("collected_at") or now,
                    "summary": clean_text(item["summary"]), "content_hash": item.get("content_hash"),
                    "evidence_type": kind, "reliability": reliability, "recorded_by": task["assignee_agent_id"]})
                ids.append(saved["id"])
            session.save({**task, "state": "concluida", "revision": task["revision"] + 1, "updated_at": _stamp(self._now())})
            returned = handoff["return_requested"]
            final = self._close(session, handoff, "devolvido" if returned else "aceito", text)
            self._note(session, ctx, case["id"], task["assignee_agent_id"], f"Resultado da subtarefa: {text}")
            self._resume_owner(ctx, session, case, "case.subtask-returned",
                               {"task_id": task_id, "handoff_id": handoff["id"], "return_to": handoff.get("return_to"),
                                "evidence_ids": ids})
            return final

    def fail_task(self, ctx, task_id, reason):
        """The assignee cannot deliver: the owner keeps the case and the operator is alerted."""
        self._ctx(ctx)
        text = self._text(reason, 500)
        with self.repository.transaction(ctx.tenant_id) as session:
            task, handoff = self._task(ctx, session, task_id)
            if task["state"] not in OPEN_TASK or handoff is None:
                raise HandoffRejected("A subtarefa ja foi encerrada.")
            case = session.get("case", task["case_id"])
            session.save({**task, "state": "falhou", "revision": task["revision"] + 1, "updated_at": _stamp(self._now())})
            final = self._close(session, handoff, "devolvido", f"falhou: {text}", attention=True)
            self._note(session, ctx, case["id"], task["assignee_agent_id"], f"Subtarefa falhou: {text}")
            self._resume_owner(ctx, session, case, "handoff.failed", {"task_id": task_id, "handoff_id": handoff["id"]})
            return final

    # -- reading ---------------------------------------------------------------------------------------------------------
    def view(self, ctx, handoff_id):
        """What a participant may see: the envelope and the evidence it names, never the rest of the case."""
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            handoff = session.get("handoff", handoff_id) if isinstance(handoff_id, str) else None
            if handoff is None or not self._involved(ctx, handoff):
                raise NotFound()
            evidence = [{k: e[k] for k in ("id", "source", "summary", "collected_at", "reliability", "evidence_type")
                         if k in e} for e in (session.get("evidence", i) for i in handoff["evidence_ids"] or []) if e]
            task = session.get("task", handoff["task_id"]) if handoff["task_id"] else None
            return {"handoff": handoff, "task": task, "evidence": evidence}

    def list_handoffs(self, ctx, case_id=None, state=None):
        self._ctx(ctx)
        filters = {k: v for k, v in (("case_id", case_id), ("state", state)) if v}
        with self.repository.transaction(ctx.tenant_id) as session:
            return [h for h in session.list("handoff", limit=1000, **filters) if self._involved(ctx, h)]

    def needing_attention(self, ctx):
        """Refused, expired, cancelled or failed handoffs waiting for the operator."""
        if not isinstance(ctx, AccessContext) or ctx.role != "operator":
            raise PermissionDenied()
        with self.repository.transaction(ctx.tenant_id) as session:
            return [h for h in session.list("handoff", limit=1000) if h.get("attention")]

    def resolve_attention(self, ctx, handoff_id, note):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator":
            raise PermissionDenied()
        with self.repository.transaction(ctx.tenant_id) as session:
            handoff = session.get("handoff", handoff_id) if isinstance(handoff_id, str) else None
            if handoff is None:
                raise NotFound()
            self._note(session, ctx, handoff["case_id"], ctx.principal_id, f"Operador tratou o encaminhamento: {note}")
            return session.save({**handoff, "attention": False, "revision": handoff["revision"] + 1,
                                 "updated_at": _stamp(self._now())})
