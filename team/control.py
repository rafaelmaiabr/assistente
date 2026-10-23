"""Operator control of a case: pause, take over, reject and give it back to the team.

Taking over stops the agents on that case: pending handoffs and approvals are withdrawn, queued and
running jobs of the case are paused (running model and tool calls stop at the next heartbeat), and
agents lose the right to act until the operator returns the case. Approvals already consumed cannot
be undone from here; their effects are reconciled through ``ApprovalService``.
"""
from .access import AccessContext, NotFound, PermissionDenied
from .cases import TERMINAL_STATES, CaseRejected, clean_text
from .enums import can_transition
from .repository import RevisionConflict


class OperatorControl:
    def __init__(self, repository, access, cases, approvals, handoffs, queue=None):
        self.repository, self.access, self.cases = repository, access, cases
        self.approvals, self.handoffs, self.queue = approvals, handoffs, queue

    def _operator(self, ctx):
        if not self.access.is_operator(ctx, "atendente"):
            raise PermissionDenied("apenas o operador confiavel ou quem atende pode controlar a demanda")

    def _load(self, ctx, case_id, expected_revision):
        with self.repository.transaction(ctx.tenant_id) as session:
            case = session.get("case", case_id) if isinstance(case_id, str) else None
        if case is None:
            raise NotFound()
        if case["revision"] != expected_revision:
            raise RevisionConflict("revisao desatualizada")
        if case["state"] in TERMINAL_STATES:
            raise CaseRejected("A demanda esta encerrada.")
        return case

    def _withdraw_agent_work(self, ctx, case_id):
        """Cancel what agents had in flight for the case: open handoffs, pending/approved approvals, live jobs."""
        with self.repository.transaction(ctx.tenant_id) as session:
            open_tasks = {t["id"] for t in session.list("task", case_id=case_id, limit=1000)
                          if t["state"] in ("pendente", "em_execucao", "aguardando")}
        for handoff in self.handoffs.list_handoffs(ctx, case_id=case_id):
            # only work still in flight: a pending proposal, or a delegation whose subtask is open.
            # A finished transfer or a returned delegation is history and cannot (and need not) be cancelled.
            in_flight = handoff["state"] == "proposto" or (
                handoff["state"] == "aceito" and handoff["handoff_kind"] == "delegacao" and handoff.get("task_id") in open_tasks)
            if in_flight:
                self.handoffs.cancel(ctx, handoff["id"], "operador assumiu a demanda")
        for approval in self.approvals.list_approvals(ctx, case_id=case_id):
            if approval["state"] in ("pendente", "aprovada"):
                self.approvals.revoke(ctx, approval["id"], "operador assumiu a demanda")

    def _fresh(self, ctx, case_id):
        with self.repository.transaction(ctx.tenant_id) as session:
            return session.get("case", case_id)

    def pause(self, ctx, case_id, expected_revision, reason):
        """Stop the case without taking control: agents may not advance it while it is paused."""
        self._operator(ctx)
        self._load(ctx, case_id, expected_revision)
        return self.cases.transition(ctx, case_id, expected_revision, "pausada", reason=reason)

    def take_over(self, ctx, case_id, expected_revision, reason):
        """The operator assumes the case; agents are stopped and can no longer act on it."""
        self._operator(ctx)
        text = clean_text(reason)
        self._load(ctx, case_id, expected_revision)
        self._withdraw_agent_work(ctx, case_id)
        if self.queue is not None:
            self.queue.pause_case(ctx.tenant_id, case_id)
        with self.repository.transaction(ctx.tenant_id) as session:
            case = session.get("case", case_id)
            changes = {"operator_control": True, "owner_version": (case.get("owner_version") or 0) + 1}
            if case["state"] != "pausada" and can_transition(case["state"], "pausada"):
                changes["state"] = "pausada"
            taken = self.cases.commit_change(ctx, session, case, changes, "case.operator-took-over", {"reason": text})
            self.cases.commit_note(ctx, session, case_id, f"O operador assumiu a demanda: {text}")
            return taken

    def reject(self, ctx, case_id, expected_revision, reason):
        """Reject the case: everything in flight is withdrawn and the case is cancelled with the reason."""
        self._operator(ctx)
        text = clean_text(reason)
        self._load(ctx, case_id, expected_revision)
        self._withdraw_agent_work(ctx, case_id)
        if self.queue is not None:
            self.queue.cancel_case(ctx.tenant_id, case_id)
        fresh = self._fresh(ctx, case_id)
        return self.cases.transition(ctx, case_id, fresh["revision"], "cancelada", reason=text)

    def return_to_team(self, ctx, case_id, expected_revision, reason):
        """Give the case back: the owner keeps it and work resumes (queued jobs are released)."""
        self._operator(ctx)
        text = clean_text(reason)
        case = self._load(ctx, case_id, expected_revision)
        if not case.get("operator_control"):
            raise CaseRejected("A demanda nao esta sob controle do operador.")
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("case", case_id)
            changes = {"operator_control": False, "owner_version": (current.get("owner_version") or 0) + 1}
            if current["state"] == "pausada" and current["owner_agent_id"] is not None:
                changes["state"] = "em_execucao"
            elif current["state"] == "pausada":
                changes["state"] = "triagem"
            returned = self.cases.commit_change(ctx, session, current, changes, "case.returned-to-team", {"reason": text})
            self.cases.commit_note(ctx, session, case_id, f"O operador devolveu a demanda a equipe: {text}")
        if self.queue is not None:
            self.queue.resume_case(ctx.tenant_id, case_id)
        return returned
