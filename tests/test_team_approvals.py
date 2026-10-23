import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.approvals import (
    POLICY_VERSION, ApprovalInvalid, ApprovalRejected, ApprovalService, normalize_request, requires_approval)
from team.cases import CaseRejected, CaseService
from team.control import OperatorControl
from team.gateway import ApprovalRequired, Gateway, InvalidArguments, ToolCancelled, ToolDenied, ToolTimeout
from team.handoffs import HandoffService
from team.ids import new_id
from team.jobs import JobQueue
from team.repository import RevisionConflict, SqliteRepository
from team.schema import content_hash
from team.service import TeamService
from tests.team_fixtures import MANIFEST, GatewayCase

PROFILE = dict(name="Financeiro", goals=["Cobrar"], personality={"tone": "claro", "initiative": "proativo"},
               scope="Cobrancas", out_of_scope="Outros", model_policy_ref="local-primeiro")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class ApprovalBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "team.sqlite3"
        self.repo = SqliteRepository(self.path)
        self.repo.migrate()
        self.clock = Clock()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases, clock=self.clock)
        self.approvals = ApprovalService(self.repo, self.access, self.cases, clock=self.clock)
        self.queue = JobQueue(self.repo, self.access)
        self.control = OperatorControl(self.repo, self.access, self.cases, self.approvals, self.handoffs, self.queue)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Financeiro", scope="x", status="ativo")
        self.agent = self.agents.create_agent(self.ca, department_id=self.dep["id"], **PROFILE)
        self.other = self.agents.create_agent(self.ca, department_id=self.dep["id"], **{**PROFILE, "name": "Outro"})
        self.team.create(self.ca, "access_grant", principal_id=self.agent["id"], resource_kind="case", resource_id=None,
                         actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        self.case = self.working_case()

    def working_case(self, owner=None):
        case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Cobrar cliente", customer_ref="c1")
        case = self.cases.transition(self.ca, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], (owner or self.agent)["id"])
        return self.cases.transition(self.ca, case["id"], case["revision"], "em_execucao")

    def agent_ctx(self, agent=None):
        agent = agent or self.agent
        run = self.agents.start_run(self.ca, agent["id"], budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 60})
        return self.access.agent_context(self.op, self.a["id"], run["id"])

    def request(self, ctx=None, case=None, **kwargs):
        options = dict(action="enviar_email", resource="con_x", recipients=["Cliente@Exemplo.com "], amount_cents=15000,
                       currency="BRL", params={"assunto": "Cobranca de dezembro"})
        options.update(kwargs)
        approval, _ = self.approvals.request(ctx or self.agent_ctx(), (case or self.case)["id"], **options)
        return approval

    def payload(self, approval):
        return approval["normalized_action"]

    def get_case(self, case=None):
        return self.cases.get_case(self.ca, (case or self.case)["id"])

    def events(self, case=None):
        with self.repo.transaction(self.a["id"]) as session:
            return [e for _, e in session.events((case or self.case)["id"])]


class RequestAndDecisionTests(ApprovalBase):
    def test_request_records_the_normalized_action_and_waits_for_the_operator(self):
        approval = self.request()
        self.assertEqual((approval["state"], approval["action"], approval["policy_ref"]), ("pendente", "enviar_email", POLICY_VERSION))
        self.assertEqual((approval["amount_cents"], approval["currency"], approval["recipient"]),
                         (15000, "brl", {"to": ["cliente@exemplo.com"]}))
        self.assertEqual(approval["payload_hash"], content_hash(approval["normalized_action"]))
        self.assertEqual((approval["requested_by"], approval["single_use"], approval["decided_by"]),
                         (self.agent["id"], True, None))
        self.assertEqual(self.get_case()["state"], "aguardando_aprovacao")
        self.assertEqual(self.events()[-1]["type"], "approval.requested")

    def test_the_hash_depends_on_every_field_and_on_nothing_else(self):
        base = dict(resource="con_x", recipients=["a@x.com"], amount_cents=100, currency="brl", params={"a": 1, "b": 2})
        same = normalize_request("t", "c", "pagamento", **{**base, "recipients": [" A@X.com"], "params": {"b": 2, "a": 1}})
        self.assertEqual(content_hash(normalize_request("t", "c", "pagamento", **base)), content_hash(same))
        variants = [dict(base, recipients=["b@x.com"]), dict(base, amount_cents=101), dict(base, currency="usd"),
                    dict(base, resource="con_y"), dict(base, params={"a": 1, "b": 3})]
        digests = {content_hash(normalize_request("t", "c", "pagamento", **v)) for v in variants}
        digests |= {content_hash(normalize_request("t", "c", "outra", **base)),
                    content_hash(normalize_request("t", "c2", "pagamento", **base)),
                    content_hash(normalize_request("t2", "c", "pagamento", **base)),
                    content_hash(normalize_request("t", "c", "pagamento", **base))}
        self.assertEqual(len(digests), 9)

    def test_requests_are_idempotent_and_validated(self):
        ctx = self.agent_ctx()
        first, created = self.approvals.request(ctx, self.case["id"], "enviar_email", recipients=["a@x.com"])
        again, created_again = self.approvals.request(ctx, self.case["id"], "enviar_email", recipients=["A@x.com"])
        self.assertEqual((created, created_again, again["id"]), (True, False, first["id"]))
        for bad in (dict(action="consultar"), dict(action="Enviar Email!"), dict(amount_cents=-1, currency="brl"),
                    dict(amount_cents=10), dict(currency="brl"), dict(amount_cents=10, currency="real"),
                    dict(params={"token": "abc"}), dict(params={"x": "y" * 5000}), dict(ttl_seconds=5),
                    dict(ttl_seconds=10 ** 9), dict(params=["lista"])):
            with self.assertRaises(ApprovalRejected, msg=str(bad)):
                self.approvals.request(ctx, self.case["id"], **{"action": "enviar_email", **bad})
        self.assertFalse(requires_approval("consultar"))
        self.assertTrue(all(requires_approval(a) for a in ("renovar_dominio", "emitir_cobranca", "enviar_email", "desconhecida")))

    def test_only_the_trusted_operator_decides_and_only_what_was_shown(self):
        approval = self.request()
        agent_ctx = self.agent_ctx()
        stranger = AccessService(self.repo, new_id("user"))
        foreign = stranger.tenant_context(stranger.local_operator(), self.a["id"])
        for ctx in (agent_ctx, foreign, {"tenant_id": self.a["id"], "role": "operator"}):
            for action in (lambda c: self.approvals.approve(c, approval["id"], approval["payload_hash"]),
                           lambda c: self.approvals.deny(c, approval["id"], "nao"),
                           lambda c: self.approvals.revoke(c, approval["id"], "x")):
                with self.assertRaises(PermissionDenied):
                    action(ctx)
        with self.assertRaisesRegex(ApprovalRejected, "nao corresponde"):
            self.approvals.approve(self.ca, approval["id"], "0" * 64)
        approved = self.approvals.approve(self.ca, approval["id"], approval["payload_hash"], comment="Confirmado por telefone")
        self.assertEqual((approved["state"], approved["decided_by"], approved["reason"]),
                         ("aprovada", self.op.principal_id, "Confirmado por telefone"))
        self.assertEqual(self.get_case()["state"], "em_execucao")  # back to work once nothing is pending
        with self.assertRaisesRegex(ApprovalRejected, "ja foi decidida"):
            self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])

    def test_denial_and_expiry_end_the_request_and_resume_the_case(self):
        denied = self.approvals.deny(self.ca, self.request()["id"], "Valor nao confere <think>x</think>")
        self.assertEqual((denied["state"], denied["decided_by"]), ("negada", self.op.principal_id))
        self.assertEqual(self.get_case()["state"], "em_execucao")
        soon = self.request(action="renovar_dominio", ttl_seconds=120)
        self.assertEqual(self.get_case()["state"], "aguardando_aprovacao")
        self.clock.now += timedelta(seconds=121)
        with self.assertRaisesRegex(ApprovalRejected, "expirou"):
            self.approvals.approve(self.ca, soon["id"], soon["payload_hash"])
        self.assertEqual(self.approvals.get(self.ca, soon["id"])["state"], "expirada")
        self.assertEqual(self.get_case()["state"], "em_execucao")
        later = self.request(action="pagamento", ttl_seconds=120)
        self.approvals.approve(self.ca, later["id"], later["payload_hash"])
        self.clock.now += timedelta(seconds=121)
        self.assertEqual([a["state"] for a in self.approvals.expire_due(self.a["id"])], ["expirada"])

    def test_agents_cannot_request_for_cases_they_do_not_own(self):
        with self.assertRaises(NotFound):
            self.approvals.request(self.agent_ctx(self.other), self.case["id"], "enviar_email")
        with self.assertRaises(NotFound):
            self.approvals.request(self.cb, self.case["id"], "enviar_email")
        closed = self.cases.transition(self.ca, self.case["id"], self.get_case()["revision"], "cancelada", reason="x")
        with self.assertRaisesRegex(ApprovalRejected, "encerrada"):
            self.approvals.request(self.ca, closed["id"], "enviar_email")


class ConsumptionTests(ApprovalBase):
    def approved(self, **kwargs):
        approval = self.request(**kwargs)
        return self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])

    def test_exact_payload_works_once_and_replay_is_refused(self):
        approval = self.approved()
        ticket = self.approvals.consume(self.ca, approval["id"], self.payload(approval))
        self.assertEqual((ticket["approval_id"], ticket["payload_hash"]), (approval["id"], approval["payload_hash"]))
        stored = self.approvals.get(self.ca, approval["id"])
        self.assertEqual(stored["state"], "consumida")
        self.assertTrue(stored["consumed_at"])
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, approval["id"], self.payload(approval))

    def test_any_change_of_recipient_amount_or_parameters_is_refused_and_keeps_the_approval(self):
        approval = self.approved()
        exact = self.payload(approval)
        changes = [{"recipients": ["outro@exemplo.com"]}, {"amount_cents": 15001}, {"currency": "usd"},
                   {"resource": "con_y"}, {"action": "pagamento"}, {"params": {"assunto": "Outro assunto"}},
                   {"case_id": new_id("case")}]
        for change in changes:
            with self.assertRaises(ApprovalInvalid, msg=str(change)):
                self.approvals.consume(self.ca, approval["id"], {**exact, **change})
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, approval["id"], {})
        self.assertEqual(self.approvals.get(self.ca, approval["id"])["state"], "aprovada")
        self.approvals.consume(self.ca, approval["id"], exact)  # the exact payload still works, once

    def test_unapproved_denied_revoked_expired_and_foreign_approvals_never_authorize(self):
        pending = self.request()
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, pending["id"], self.payload(pending))
        self.approvals.deny(self.ca, pending["id"], "nao")
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, pending["id"], self.payload(pending))
        revoked = self.approved(action="pagamento")
        self.approvals.revoke(self.ca, revoked["id"], "mudei de ideia")
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, revoked["id"], self.payload(revoked))
        old = self.approved(action="renovar_dominio", ttl_seconds=60)
        self.clock.now += timedelta(seconds=61)
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, old["id"], self.payload(old))
        fresh = self.approved(action="emitir_cobranca")
        for ctx in (self.cb, ):
            with self.assertRaises(ApprovalInvalid):
                self.approvals.consume(ctx, fresh["id"], self.payload(fresh))
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, new_id("approval_request"), self.payload(fresh))
        with self.assertRaises(PermissionDenied):
            self.approvals.consume({"tenant_id": self.a["id"]}, fresh["id"], self.payload(fresh))
        self.assertEqual({str(e) for e in self._errors(fresh)}, {"aprovacao invalida para esta execucao"})

    def _errors(self, approval):
        errors = []
        for payload in ({}, {"action": "x"}, self.payload(approval) | {"amount_cents": 1}):
            try:
                self.approvals.consume(self.ca, new_id("approval_request"), payload)
            except ApprovalInvalid as error:
                errors.append(error)
        return errors

    def test_double_click_has_one_winner(self):
        approval = self.approved()
        results, barrier = [], threading.Barrier(4)

        def click():
            barrier.wait()
            try:
                self.approvals.consume(self.ca, approval["id"], self.payload(approval))
                results.append("ok")
            except ApprovalInvalid:
                results.append("invalido")

        threads = [threading.Thread(target=click) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(sorted(results), ["invalido"] * 3 + ["ok"])

    def test_race_between_consumption_and_revocation_never_yields_both(self):
        for attempt in range(8):
            case = self.working_case()
            request = self.approvals.request(self.ca, case["id"], "enviar_email", params={"n": attempt})[0]
            approval = self.approvals.approve(self.ca, request["id"], request["payload_hash"])
            outcome, barrier = {}, threading.Barrier(2)

            def consume():
                barrier.wait()
                try:
                    self.approvals.consume(self.ca, approval["id"], self.payload(approval))
                    outcome["consume"] = True
                except ApprovalInvalid:
                    outcome["consume"] = False

            def revoke():
                barrier.wait()
                try:
                    self.approvals.revoke(self.ca, approval["id"], "corrida")
                    outcome["revoke"] = True
                except ApprovalRejected:
                    outcome["revoke"] = False

            threads = [threading.Thread(target=consume), threading.Thread(target=revoke)]
            [t.start() for t in threads]
            [t.join(30) for t in threads]
            self.assertNotEqual(outcome["consume"], outcome["revoke"], outcome)  # exactly one of them wins
            self.assertEqual(self.approvals.get(self.ca, approval["id"])["state"],
                             "consumida" if outcome["consume"] else "cancelada")

    def test_a_changed_owner_pause_or_operator_control_voids_unspent_approvals(self):
        approval = self.approved()
        self.team.create(self.ca, "access_grant", principal_id=self.other["id"], resource_kind="case", resource_id=None,
                         actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        current = self.agents.get_agent(self.ca, self.agent["id"])
        self.agents.update_agent(self.ca, self.agent["id"], current["revision"], handoff_target_ids=[self.other["id"]])
        proposal, _ = self.handoffs.propose_transfer(self.agent_ctx(), self.case["id"], self.other["id"],
                                                     reason="Passar adiante", summary="Resumo")
        self.handoffs.accept(self.agent_ctx(self.other), proposal["id"])
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, approval["id"], self.payload(approval))
        paused = self.approved(action="pagamento", case=self.working_case())
        case = self.get_case(self.cases.get_case(self.ca, paused["case_id"]))
        self.cases.transition(self.ca, case["id"], case["revision"], "pausada", reason="aguardar")
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, paused["id"], self.payload(paused))

    def test_effects_are_recorded_and_unknown_ones_wait_for_reconciliation(self):
        approval = self.approved()
        self.approvals.consume(self.ca, approval["id"], self.payload(approval))
        recorded = self.approvals.record_effect(self.ca, approval["id"], "desconhecido", "tempo esgotado depois do envio")
        self.assertEqual(recorded["effect_outcome"], "desconhecido")
        self.assertEqual([a["id"] for a in self.approvals.needing_reconciliation(self.ca)], [approval["id"]])
        with self.assertRaises(PermissionDenied):
            self.approvals.needing_reconciliation(self.agent_ctx())
        with self.assertRaises(PermissionDenied):
            self.approvals.resolve_effect(self.agent_ctx(), approval["id"], "confirmado", "x")
        resolved = self.approvals.resolve_effect(self.ca, approval["id"], "confirmado", "Provedor mostra o email enviado")
        self.assertEqual(resolved["effect_outcome"], "confirmado")
        self.assertEqual(self.approvals.needing_reconciliation(self.ca), [])
        with self.assertRaises(ApprovalRejected):
            self.approvals.record_effect(self.ca, approval["id"], "falhou")  # one verdict only
        unspent = self.approved(action="pagamento")
        with self.assertRaises(ApprovalRejected):
            self.approvals.record_effect(self.ca, unspent["id"], "confirmado")  # nothing ran

    def test_other_companies_and_agents_do_not_see_approvals(self):
        approval = self.request()
        for action in (lambda: self.approvals.get(self.cb, approval["id"]), lambda: self.approvals.get(self.agent_ctx(self.other), approval["id"])):
            with self.assertRaises(NotFound):
                action()
        self.assertEqual(self.approvals.list_approvals(self.cb), [])
        self.assertEqual(self.approvals.list_approvals(self.agent_ctx(self.other)), [])
        self.assertEqual([a["id"] for a in self.approvals.list_approvals(self.agent_ctx())], [approval["id"]])


class OperatorControlTests(ApprovalBase):
    def test_pause_stops_agents_without_taking_control(self):
        case = self.get_case()
        paused = self.control.pause(self.ca, case["id"], case["revision"], "Aguardar contato com o cliente")
        self.assertEqual(paused["state"], "pausada")
        with self.assertRaises(PermissionDenied):
            self.control.pause(self.agent_ctx(), case["id"], paused["revision"], "x")
        with self.assertRaises(RevisionConflict):
            self.control.pause(self.ca, case["id"], case["revision"], "revisao velha")

    def test_taking_over_withdraws_agent_work_and_blocks_the_agents(self):
        agent_ctx = self.agent_ctx()
        self.team.create(self.ca, "access_grant", principal_id=self.other["id"], resource_kind="case", resource_id=None,
                         actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        pending = self.request(ctx=agent_ctx)
        approved = self.approvals.approve(self.ca, self.request(ctx=agent_ctx, action="pagamento")["id"],
                                          self.request(ctx=agent_ctx, action="pagamento")["payload_hash"])
        current = self.agents.get_agent(self.ca, self.agent["id"])
        self.agents.update_agent(self.ca, self.agent["id"], current["revision"], handoff_target_ids=[self.other["id"]])
        agent_ctx = self.agent_ctx()
        handoff, _ = self.handoffs.delegate(agent_ctx, self.case["id"], self.other["id"], reason="r", objective="o",
                                            expected_result="e", summary="s")
        job, _ = self.queue.enqueue(self.a["id"], "agent_run", {}, idempotency_key="j1", case_id=self.case["id"])
        case = self.get_case()
        taken = self.control.take_over(self.ca, case["id"], case["revision"], "Vou falar com o cliente pessoalmente")
        self.assertEqual((taken["state"], taken["operator_control"]), ("pausada", True))
        self.assertEqual(self.approvals.get(self.ca, pending["id"])["state"], "cancelada")
        self.assertEqual(self.approvals.get(self.ca, approved["id"])["state"], "cancelada")
        self.assertEqual(self.handoffs.list_handoffs(self.ca)[0]["state"], "cancelado")
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "pausado")
        with self.assertRaises(ApprovalInvalid):
            self.approvals.consume(self.ca, approved["id"], self.payload(approved))
        for blocked in (lambda: self.cases.transition(agent_ctx, case["id"], taken["revision"], "em_execucao"),
                        lambda: self.cases.add_message(agent_ctx, case["id"], "saida", "email", "Resposta do agente"),
                        lambda: self.approvals.request(agent_ctx, case["id"], "enviar_email", recipients=["z@x.com"])):
            with self.assertRaises((PermissionDenied, NotFound)):
                blocked()
        self.cases.add_message(self.ca, case["id"], "saida", "email", "Resposta do operador")  # the operator may

    def test_returning_the_case_restores_the_owner_and_releases_jobs(self):
        job, _ = self.queue.enqueue(self.a["id"], "agent_run", {}, idempotency_key="j2", case_id=self.case["id"])
        case = self.get_case()
        taken = self.control.take_over(self.ca, case["id"], case["revision"], "Assumi")
        with self.assertRaises(RevisionConflict):
            self.control.return_to_team(self.ca, case["id"], taken["revision"] + 5, "x")
        returned = self.control.return_to_team(self.ca, case["id"], taken["revision"], "Cliente atendido, pode seguir")
        self.assertEqual((returned["state"], returned["operator_control"], returned["owner_agent_id"]),
                         ("em_execucao", False, self.agent["id"]))
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "pendente")
        self.cases.add_message(self.agent_ctx(), case["id"], "saida", "email", "O agente volta a falar")
        with self.assertRaisesRegex(CaseRejected, "nao esta sob controle"):
            self.control.return_to_team(self.ca, case["id"], returned["revision"], "de novo")

    def test_rejecting_cancels_the_case_and_everything_in_flight(self):
        request = self.request()
        job, _ = self.queue.enqueue(self.a["id"], "agent_run", {}, idempotency_key="j3", case_id=self.case["id"])
        case = self.get_case()
        rejected = self.control.reject(self.ca, case["id"], case["revision"], "Pedido improcedente")
        self.assertEqual(rejected["state"], "cancelada")
        self.assertEqual(self.approvals.get(self.ca, request["id"])["state"], "cancelada")
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "cancelado")
        with self.assertRaisesRegex(CaseRejected, "encerrada"):
            self.control.take_over(self.ca, case["id"], rejected["revision"], "tarde")
        with self.assertRaises(NotFound):
            self.control.pause(self.cb, case["id"], 1, "x")

    def test_effects_already_sent_are_reconciled_not_undone(self):
        approval = self.request()
        approval = self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])
        self.approvals.consume(self.ca, approval["id"], self.payload(approval))
        self.approvals.record_effect(self.ca, approval["id"], "desconhecido", "sem resposta do provedor")
        case = self.get_case()
        self.control.take_over(self.ca, case["id"], case["revision"], "Verificar com o provedor")
        self.assertEqual(self.approvals.get(self.ca, approval["id"])["state"], "consumida")  # history is kept
        self.assertEqual(len(self.approvals.needing_reconciliation(self.ca)), 1)
        self.assertFalse(hasattr(self.approvals, "undo") or hasattr(self.approvals, "refund"))


# -- the tool gateway only runs writes that carry a matching, single-use approval -----------------------------------
WRITE_MANIFEST = {**MANIFEST, "id": "teste.cobrar", "required_tools": [
    {"tool": "pay-demo.enviar-cobranca", "operations": ["write"]}]}
WRITE_SPEC = {"operation": "write", "timeout_seconds": 5, "recipient_argument": "destinatario",
              "amount_argument": "valor", "currency_argument": "moeda", "arguments": {
                  "cliente": {"type": "string", "required": True, "max_length": 40},
                  "destinatario": {"type": "string", "required": True, "max_length": 80},
                  "valor": {"type": "integer", "required": True, "minimum": 1},
                  "moeda": {"type": "string", "required": True, "enum": ["brl", "usd"]}}}
CALL = {"cliente": "Acme", "destinatario": "financeiro@acme.example", "valor": 15000, "moeda": "brl"}


class GatewayApprovalTests(GatewayCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.cases = CaseService(self.repo, self.access)
        self.approvals = ApprovalService(self.repo, self.access, self.cases, clock=self.clock)
        self.gateway = Gateway(self.repo, self.access, self.skills, self.connections, {"http": self.transport},
                               approvals=self.approvals)
        self.transport.remote["enviar-cobranca"] = {"read_only": False}
        entry = self.connections.approve_mcp_server(
            self.op, catalog_key="pay-demo", entry_version="1.0.0", transport="http", url="http://127.0.0.1:9100/mcp",
            tools=["enviar-cobranca"], tool_specs={"enviar-cobranca": WRITE_SPEC})
        self.connections.register_connector("pay-demo")
        conn = self.connections.create_connection(self.ca, label="Pay", provider="pay-demo", connection_kind="mcp",
                                                  catalog_entry_id=entry["id"])
        folder = Path(self.temp.name) / "payskill"
        folder.mkdir()
        (folder / "skill.json").write_text(json.dumps(WRITE_MANIFEST), encoding="utf-8")
        skill = self.skills.install(self.op, folder, self.skills.review_package(folder).content_hash)
        self.skills.grant_skill(self.ca, agent_id=self.agent["id"], skill_id=skill["id"], operations=["write"],
                                connection_id=conn["id"], attach=True)
        self.dep = self.team.create(self.ca, "department", name="Fin2", scope="x", status="ativo")
        case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Cobrar", customer_ref="c1")
        case = self.cases.transition(self.ca, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], self.agent["id"])
        self.case = self.cases.transition(self.ca, case["id"], case["revision"], "em_execucao")
        self.alias = "pay-demo.enviar-cobranca"

    def execution(self, with_case=True):
        run = self.agents.start_run(self.ca, self.agent["id"], case_id=self.case["id"] if with_case else None,
                                    budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 60})
        return self.gateway.open_execution(self.access.agent_context(self.op, self.a["id"], run["id"]), run["id"])

    def approve_pending(self, **expected):
        pending = self.approvals.list_approvals(self.ca, state="pendente")
        self.assertEqual(len(pending), 1)
        for key, value in expected.items():
            self.assertEqual(pending[0][key], value)
        return self.approvals.approve(self.ca, pending[0]["id"], pending[0]["payload_hash"])

    async def test_a_write_waits_for_approval_then_runs_exactly_once(self):
        execution = self.execution()
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, CALL)
        self.assertEqual(self.transport.calls, [])
        with self.assertRaises(ApprovalRequired):  # the same call again does not duplicate the request
            await self.gateway.call_tool(execution, self.alias, CALL)
        approval = self.approve_pending(action="ferramenta:teste.cobrar:enviar-cobranca", amount_cents=15000, currency="brl")
        self.assertEqual(approval["recipient"], {"to": ["financeiro@acme.example"]})
        result = await self.gateway.call_tool(execution, self.alias, CALL)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(result.data)
        self.assertEqual(self.approvals.get(self.ca, approval["id"])["state"], "consumida")
        self.assertEqual(self.approvals.get(self.ca, approval["id"])["effect_outcome"], "confirmado")
        with self.assertRaises(ApprovalRequired):  # replay: a new approval is needed
            await self.gateway.call_tool(execution, self.alias, CALL)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_changing_recipient_or_amount_needs_a_new_approval(self):
        execution = self.execution()
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, CALL)
        self.approve_pending()
        for change in ({"destinatario": "outro@acme.example"}, {"valor": 15001}, {"moeda": "usd"}, {"cliente": "Outra"}):
            with self.assertRaises(ApprovalRequired, msg=str(change)):
                await self.gateway.call_tool(execution, self.alias, {**CALL, **change})
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(len(self.approvals.list_approvals(self.ca, state="pendente")), 4)
        with self.assertRaises(InvalidArguments):
            await self.gateway.call_tool(execution, self.alias, {**CALL, "valor": 0})

    async def test_revoking_or_expiring_the_approval_blocks_the_call(self):
        execution = self.execution()
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, CALL)
        approval = self.approve_pending()
        self.approvals.revoke(self.ca, approval["id"], "mudei de ideia")
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, CALL)
        approval = self.approve_pending()
        self.clock.now += timedelta(days=2)
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, CALL)
        self.assertEqual(self.transport.calls, [])

    async def test_without_an_approval_service_or_a_case_writes_stay_blocked(self):
        bare = Gateway(self.repo, self.access, self.skills, self.connections, {"http": self.transport})
        run = self.agents.start_run(self.ca, self.agent["id"], case_id=self.case["id"],
                                    budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 60})
        bare_execution = bare.open_execution(self.access.agent_context(self.op, self.a["id"], run["id"]), run["id"])
        with self.assertRaises(ToolDenied):
            await bare.call_tool(bare_execution, self.alias, CALL)
        with self.assertRaises(ToolDenied):
            await self.gateway.call_tool(self.execution(with_case=False), self.alias, CALL)
        self.assertEqual(self.transport.calls, [])

    async def test_reads_do_not_need_approval(self):
        execution = self.execution()
        names = [t["name"] for t in self.gateway.list_tools(execution)]
        self.assertEqual(names, ["erp-demo.listar-cobrancas", self.alias])
        result = await self.gateway.call_tool(execution, "erp-demo.listar-cobrancas", {"cliente": "Acme"})
        self.assertTrue(result.data)
        self.assertEqual(self.approvals.list_approvals(self.ca), [])

    async def test_a_call_that_may_have_gone_out_is_marked_for_reconciliation(self):
        execution = self.execution()
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, CALL)
        approval = self.approve_pending()
        self.transport.delay = 5
        slow = Gateway(self.repo, self.access, self.skills, self.connections, {"http": self.transport}, approvals=self.approvals)
        run = self.agents.start_run(self.ca, self.agent["id"], case_id=self.case["id"],
                                    budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 60})
        short = slow.open_execution(self.access.agent_context(self.op, self.a["id"], run["id"]), run["id"], deadline_seconds=0.3)
        with self.assertRaises(ToolTimeout):
            await slow.call_tool(short, self.alias, CALL)
        stored = self.approvals.get(self.ca, approval["id"])
        self.assertEqual((stored["state"], stored["effect_outcome"]), ("consumida", "desconhecido"))
        self.assertEqual([a["id"] for a in self.approvals.needing_reconciliation(self.ca)], [approval["id"]])
        self.assertTrue(self.transport.cancelled)

    async def test_a_call_that_never_left_is_recorded_as_failed_and_cancellation_as_unknown(self):
        execution = self.execution()
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, CALL)
        approval = self.approve_pending()
        del self.transport.remote["enviar-cobranca"]  # the server no longer knows the tool: nothing was sent
        with self.assertRaises(Exception):
            await self.gateway.call_tool(execution, self.alias, CALL)
        self.assertEqual(self.approvals.get(self.ca, approval["id"])["effect_outcome"], "falhou")
        self.assertEqual(self.transport.calls, [])
        self.transport.remote["enviar-cobranca"] = {"read_only": False}
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, CALL)
        second = self.approve_pending()
        self.transport.delay = 5
        task = asyncio.ensure_future(self.gateway.call_tool(execution, self.alias, CALL))
        await asyncio.sleep(0.3)
        execution.cancel()
        with self.assertRaises(ToolCancelled):
            await task
        self.assertEqual(self.approvals.get(self.ca, second["id"])["effect_outcome"], "desconhecido")


if __name__ == "__main__":
    unittest.main()
