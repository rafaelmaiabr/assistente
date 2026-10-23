import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.cases import CaseRejected, CaseService
from team.handoffs import MAX_DEPTH, MAX_OPEN_SUBTASKS, MAX_TRANSFERS, HandoffRejected, HandoffService
from team.ids import new_id
from team.repository import SqliteRepository
from team.schema import ContractError
from team.service import TeamService

PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")
BUDGET = {"max_steps": 4, "token_budget": 100, "deadline_seconds": 60}


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class HandoffCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "team.sqlite3"
        self.repo = SqliteRepository(self.path)
        self.repo.migrate()
        self.clock = FakeClock()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases, clock=self.clock)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Atendimento", scope="x", status="ativo")
        self.secretaria = self.agent("Secretaria")
        self.suporte = self.agent("Suporte")
        self.financeiro = self.agent("Financeiro")
        self.allow(self.secretaria, self.suporte, self.financeiro)
        self.allow(self.suporte, self.financeiro, self.secretaria)
        self.allow(self.financeiro, self.suporte)
        self.grant_case_access(self.secretaria)  # owners need their own access; Financeiro gets none by default

    # -- builders -----------------------------------------------------------------------------------------------------
    def agent(self, name):
        return self.agents.create_agent(self.ca, department_id=self.dep["id"], name=name, **PROFILE)

    def allow(self, source, *targets):
        current = self.agents.get_agent(self.ca, source["id"])
        self.agents.update_agent(self.ca, source["id"], current["revision"], handoff_target_ids=[t["id"] for t in targets])

    def ctx(self, agent):
        run = self.agents.start_run(self.ca, agent["id"], budget=BUDGET)
        return self.access.agent_context(self.op, self.a["id"], run["id"])

    def grant_case_access(self, agent):
        self.team.create(self.ca, "access_grant", principal_id=agent["id"], resource_kind="case", resource_id=None,
                         actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)

    def open_case(self, owner=None, state="em_execucao"):
        case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Site fora do ar", origin="email")
        case = self.cases.transition(self.ca, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], (owner or self.secretaria)["id"])
        if state == "em_execucao":
            case = self.cases.transition(self.ca, case["id"], case["revision"], "em_execucao")
        return case

    def evidence(self, case, **kwargs):
        options = dict(source="consulta DNS", summary="Sem registro A", evidence_type="fato", reliability="declarada")
        return self.cases.add_evidence(self.ca, case["id"], **{**options, **kwargs})

    def case(self, case):
        return self.cases.get_case(self.ca, case["id"])

    def events(self, case):
        with self.repo.transaction(self.a["id"]) as session:
            return [e for _, e in session.events(case["id"])]

    def propose(self, ctx, case, target, **kwargs):
        options = dict(reason="Cliente com problema tecnico", summary="Site fora; DNS sem registro A.")
        handoff, _ = self.handoffs.propose_transfer(ctx, case["id"], target["id"], **{**options, **kwargs})
        return handoff

    def delegate(self, ctx, case, target, **kwargs):
        options = dict(reason="Analisar renovacao", objective="Verificar vencimento do dominio",
                       expected_result="Data de vencimento e valor", summary="Dominio possivelmente vencido.")
        handoff, _ = self.handoffs.delegate(ctx, case["id"], target["id"], **{**options, **kwargs})
        return handoff


class RequiredScenarioTests(HandoffCase):
    def test_secretaria_to_suporte_to_financeiro_and_back_to_suporte(self):
        case = self.open_case(state="triagem")
        fact = self.evidence(case)
        sec, sup, fin = self.ctx(self.secretaria), self.ctx(self.suporte), self.ctx(self.financeiro)
        # Secretaria hands the case to Suporte: a proposal, then an atomic swap on acceptance
        self.grant_case_access(self.suporte)
        proposal = self.propose(sec, case, self.suporte, evidence_ids=[fact["id"]])
        self.assertEqual(proposal["state"], "proposto")
        self.assertEqual(self.case(case)["owner_agent_id"], self.secretaria["id"])  # nothing changes before acceptance
        self.handoffs.accept(sup, proposal["id"])
        case_now = self.case(case)
        self.assertEqual(case_now["owner_agent_id"], self.suporte["id"])
        self.assertEqual(self.events(case)[-1]["type"], "case.transferred")
        # the former owner can no longer act on the case or speak to the customer
        with self.assertRaises(PermissionDenied):
            self.cases.transition(sec, case["id"], case_now["revision"], "pausada", reason="x")
        with self.assertRaises(PermissionDenied):
            self.cases.add_message(sec, case["id"], "saida", "email", "Resposta indevida")
        with self.assertRaises(NotFound):
            self.propose(sec, case, self.suporte)
        # Suporte works, then delegates the financial check to Financeiro; ownership stays with Suporte
        case_now = self.cases.transition(sup, case["id"], case_now["revision"], "em_execucao")
        delegation = self.delegate(sup, case, self.financeiro, evidence_ids=[fact["id"]], due_at="2026-06-02T12:00:00Z")
        after = self.case(case)
        self.assertEqual((after["owner_agent_id"], after["state"]), (self.suporte["id"], "aguardando_subtarefa"))
        self.assertEqual(delegation["return_to"], self.suporte["id"])
        task = self.handoffs.view(fin, delegation["id"])["task"]
        self.assertEqual((task["state"], task["assignee_agent_id"]), ("pendente", self.financeiro["id"]))
        self.handoffs.accept(fin, delegation["id"])
        self.assertEqual(self.case(case)["owner_agent_id"], self.suporte["id"])
        # Financeiro finishes and returns the result to Suporte
        result = self.handoffs.complete_task(fin, task["id"], "Dominio vence em 2026-12-01; renovacao custa R$ 60,00.",
                                             evidence=[{"source": "ERP", "summary": "Cobranca aberta", "evidence_type": "documento",
                                                        "reliability": "verificada", "content_hash": "e" * 64}])
        self.assertEqual(result["state"], "devolvido")
        back = self.case(case)
        self.assertEqual((back["owner_agent_id"], back["state"]), (self.suporte["id"], "em_execucao"))
        types = [e["type"] for e in self.events(case)]
        self.assertIn("case.subtask-returned", types)
        timeline = " ".join(item["item"].get("body", "") for item in self.cases.timeline(self.ca, case["id"]))
        self.assertIn("Resultado da subtarefa", timeline)
        with self.assertRaises(NotFound):  # the assignee never became owner or got case access
            self.cases.get_case(fin, case["id"])
        with self.assertRaises(NotFound):  # no grant on the case at all: it does not even exist for Financeiro
            self.cases.add_message(fin, case["id"], "saida", "email", "Financeiro falando com o cliente")

    def test_secretaria_to_financeiro_direct_needs_the_recipients_own_access(self):
        case = self.open_case()
        sec, fin = self.ctx(self.secretaria), self.ctx(self.financeiro)
        proposal = self.propose(sec, case, self.financeiro, reason="Questao de cobranca", summary="Cliente pergunta sobre boleto.")
        with self.assertRaisesRegex(HandoffRejected, "nao tem acesso"):
            self.handoffs.accept(fin, proposal["id"])  # accepting does not grant access
        self.assertEqual(self.handoffs.list_handoffs(self.ca, state="proposto")[0]["id"], proposal["id"])
        self.grant_case_access(self.financeiro)
        self.handoffs.accept(fin, proposal["id"])
        self.assertEqual(self.case(case)["owner_agent_id"], self.financeiro["id"])
        self.cases.add_message(fin, case["id"], "saida", "email", "Segue o boleto atualizado.")  # the new owner speaks
        with self.assertRaises(PermissionDenied):
            self.cases.add_message(sec, case["id"], "saida", "email", "Segunda voz")

    def test_concurrent_acceptances_have_a_single_winner(self):
        case = self.open_case()
        self.grant_case_access(self.suporte)
        proposal = self.propose(self.ctx(self.secretaria), case, self.suporte)
        wins, losses, barrier = [], [], threading.Barrier(2)
        contexts = [self.ctx(self.suporte), self.ctx(self.suporte)]

        def attempt(ctx):
            barrier.wait()
            try:
                wins.append(self.handoffs.accept(ctx, proposal["id"]))
            except HandoffRejected:
                losses.append(1)

        threads = [threading.Thread(target=attempt, args=(c,)) for c in contexts]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual((len(wins), len(losses)), (1, 1))
        revisions = [e["revision"] for e in self.events(case)]
        self.assertEqual(revisions, sorted(set(revisions)))  # one transfer event, no gaps or repeats
        self.assertEqual(self.case(case)["owner_agent_id"], self.suporte["id"])


class FailureTests(HandoffCase):
    def test_refusal_keeps_the_owner_and_alerts_the_operator(self):
        case = self.open_case()
        sec, fin = self.ctx(self.secretaria), self.ctx(self.financeiro)
        delegation = self.delegate(self.ctx(self.secretaria), case, self.financeiro)
        self.assertEqual(self.case(case)["state"], "aguardando_subtarefa")
        declined = self.handoffs.decline(fin, delegation["id"], "Fora do meu escopo <think>talvez</think>")
        self.assertEqual((declined["state"], declined["attention"], declined["resolution"]), ("recusado", True, "Fora do meu escopo"))
        back = self.case(case)
        self.assertEqual((back["owner_agent_id"], back["state"]), (self.secretaria["id"], "em_execucao"))
        task = self.handoffs.view(self.ca, delegation["id"])["task"]
        self.assertEqual(task["state"], "cancelada")
        self.assertEqual([h["id"] for h in self.handoffs.needing_attention(self.ca)], [delegation["id"]])
        with self.assertRaises(PermissionDenied):
            self.handoffs.needing_attention(sec)
        self.handoffs.resolve_attention(self.ca, delegation["id"], "Vou reencaminhar manualmente")
        self.assertEqual(self.handoffs.needing_attention(self.ca), [])
        with self.assertRaisesRegex(HandoffRejected, "ja foi respondido"):
            self.handoffs.accept(fin, delegation["id"])

    def test_timeout_expires_the_proposal_and_keeps_the_owner(self):
        case = self.open_case()
        self.grant_case_access(self.suporte)
        transfer = self.propose(self.ctx(self.secretaria), case, self.suporte, ttl_seconds=600)
        delegation = self.delegate(self.ctx(self.secretaria), case, self.financeiro, ttl_seconds=600)
        self.assertEqual(self.handoffs.expire_due(self.a["id"]), [])  # still within the deadline
        self.clock.now += timedelta(seconds=601)
        expired = self.handoffs.expire_due(self.a["id"])
        self.assertEqual({h["id"] for h in expired}, {transfer["id"], delegation["id"]})
        self.assertEqual({h["state"] for h in expired}, {"expirado"})
        after = self.case(case)
        self.assertEqual((after["owner_agent_id"], after["state"]), (self.secretaria["id"], "em_execucao"))
        self.assertEqual(len(self.handoffs.needing_attention(self.ca)), 2)

    def test_accepting_after_the_deadline_is_refused_and_recorded(self):
        case = self.open_case()
        self.grant_case_access(self.suporte)
        transfer = self.propose(self.ctx(self.secretaria), case, self.suporte, ttl_seconds=60)
        self.clock.now += timedelta(seconds=61)
        with self.assertRaisesRegex(HandoffRejected, "Prazo de aceite"):
            self.handoffs.accept(self.ctx(self.suporte), transfer["id"])
        self.assertEqual(self.handoffs.list_handoffs(self.ca)[0]["state"], "expirado")
        self.assertEqual(self.case(case)["owner_agent_id"], self.secretaria["id"])

    def test_a_transfer_is_void_if_the_case_changed_after_the_proposal(self):
        case = self.open_case()
        self.grant_case_access(self.suporte)
        transfer = self.propose(self.ctx(self.secretaria), case, self.suporte)
        moved = self.cases.transition(self.ca, case["id"], self.case(case)["revision"], "aguardando_cliente")
        with self.assertRaisesRegex(HandoffRejected, "mudou desde a proposta"):
            self.handoffs.accept(self.ctx(self.suporte), transfer["id"])
        stored = self.handoffs.list_handoffs(self.ca)[0]
        self.assertEqual((stored["state"], stored["attention"]), ("cancelado", True))
        self.assertEqual(self.case(case)["owner_agent_id"], self.secretaria["id"])
        self.assertTrue(moved)

    def test_delegation_survives_the_owner_progressing_the_case(self):
        case = self.open_case()
        delegation = self.delegate(self.ctx(self.secretaria), case, self.financeiro)
        self.cases.add_note(self.ca, case["id"], "Nota do operador")
        self.handoffs.accept(self.ctx(self.financeiro), delegation["id"])  # revision of the case is not required here
        self.assertEqual(self.handoffs.list_handoffs(self.ca)[0]["state"], "aceito")

    def test_a_failed_subtask_returns_the_problem_to_the_owner_and_the_operator(self):
        case = self.open_case()
        delegation = self.delegate(self.ctx(self.secretaria), case, self.financeiro)
        fin = self.ctx(self.financeiro)
        self.handoffs.accept(fin, delegation["id"])
        task = self.handoffs.view(fin, delegation["id"])["task"]
        failed = self.handoffs.fail_task(fin, task["id"], "ERP indisponivel")
        self.assertEqual((failed["state"], failed["attention"]), ("devolvido", True))
        self.assertEqual(self.case(case)["state"], "em_execucao")
        with self.assertRaises(HandoffRejected):
            self.handoffs.complete_task(fin, task["id"], "tarde demais")

    def test_proposer_can_withdraw_but_a_finished_transfer_cannot_be_undone(self):
        case = self.open_case()
        self.grant_case_access(self.suporte)
        first = self.propose(self.ctx(self.secretaria), case, self.suporte)
        withdrawn = self.handoffs.cancel(self.ctx(self.secretaria), first["id"])
        self.assertEqual(withdrawn["state"], "cancelado")
        second = self.propose(self.ctx(self.secretaria), case, self.suporte)
        self.handoffs.accept(self.ctx(self.suporte), second["id"])
        with self.assertRaisesRegex(HandoffRejected, "nao pode mais ser cancelado"):
            self.handoffs.cancel(self.ctx(self.secretaria), second["id"])


class LimitTests(HandoffCase):
    def test_cycles_and_self_targets_are_rejected(self):
        case = self.open_case(owner=self.suporte)
        sup, fin = self.ctx(self.suporte), self.ctx(self.financeiro)
        with self.assertRaisesRegex(HandoffRejected, "si mesmo"):
            self.delegate(sup, case, self.suporte)
        delegation = self.delegate(sup, case, self.financeiro)
        self.handoffs.accept(fin, delegation["id"])
        task = self.handoffs.view(fin, delegation["id"])["task"]
        with self.assertRaisesRegex(HandoffRejected, "Ciclo de delegacao"):
            self.handoffs.delegate(fin, case["id"], self.suporte["id"], reason="Voltar", objective="Devolver",
                                   expected_result="x", summary="Suporte ja e dono", parent_task_id=task["id"])
        # transfer ping-pong: Suporte -> Financeiro accepted, then Financeiro -> Suporte is a cycle
        self.grant_case_access(self.financeiro)
        self.handoffs.complete_task(fin, task["id"], "feito")
        transfer = self.propose(sup, case, self.financeiro)
        self.handoffs.accept(fin, transfer["id"])
        fin2 = self.ctx(self.financeiro)
        with self.assertRaisesRegex(HandoffRejected, "Ciclo de transferencias"):
            self.propose(fin2, case, self.suporte)
        with self.assertRaisesRegex(HandoffRejected, "si mesmo"):  # the operator cannot hand the case to its owner
            self.handoffs.propose_transfer(self.ca, case["id"], self.financeiro["id"], reason="x", summary="y")

    def test_delegation_depth_and_open_subtask_limits(self):
        extra = [self.agent(f"Extra {i}") for i in range(MAX_DEPTH + 1)]
        chain = [self.suporte, self.financeiro, *extra]
        for source, target in zip(chain, chain[1:]):
            self.allow(source, target)
        case = self.open_case(owner=self.suporte)
        parent, last = None, None
        for depth, (source, target) in enumerate(zip(chain, chain[1:]), start=1):
            ctx = self.ctx(source)
            if depth > MAX_DEPTH:
                with self.assertRaisesRegex(HandoffRejected, "Profundidade"):
                    self.handoffs.delegate(ctx, case["id"], target["id"], reason="r", objective="o", expected_result="e",
                                           summary="s", parent_task_id=parent)
                break
            handoff = self.delegate(ctx, case, target, parent_task_id=parent)
            self.handoffs.accept(self.ctx(target), handoff["id"])
            parent = handoff["task_id"]
            last = handoff
        self.assertTrue(last)

    def test_open_subtasks_are_capped(self):
        others = [self.agent(f"Apoio {i}") for i in range(MAX_OPEN_SUBTASKS + 1)]
        self.allow(self.suporte, *others)
        case = self.open_case(owner=self.suporte)
        sup = self.ctx(self.suporte)
        for other in others[:MAX_OPEN_SUBTASKS]:
            self.delegate(sup, case, other)
        with self.assertRaisesRegex(HandoffRejected, "subtarefas abertas"):
            self.delegate(sup, case, others[-1])

    def test_transfer_hops_are_limited(self):
        agents = [self.agent(f"No {i}") for i in range(MAX_TRANSFERS + 2)]
        for source, target in zip(agents, agents[1:]):
            self.allow(source, target)
            self.grant_case_access(target)
        case = self.open_case(owner=agents[0])
        for source, target in zip(agents[:MAX_TRANSFERS], agents[1:MAX_TRANSFERS + 1]):
            proposal = self.propose(self.ctx(source), case, target)
            self.handoffs.accept(self.ctx(target), proposal["id"])
        with self.assertRaisesRegex(HandoffRejected, "Limite de transferencias"):
            self.propose(self.ctx(agents[MAX_TRANSFERS]), case, agents[MAX_TRANSFERS + 1])

    def test_targets_status_and_state_preconditions(self):
        case = self.open_case()
        sec = self.ctx(self.secretaria)
        stranger = self.agent("Fora da lista")
        with self.assertRaisesRegex(HandoffRejected, "permitidos"):
            self.propose(sec, case, stranger)
        self.propose(self.ca, case, stranger, reason="Operador decide", summary="Excecao manual.")  # operator may route anywhere
        current = self.agents.get_agent(self.ca, self.financeiro["id"])
        self.agents.update_agent(self.ca, self.financeiro["id"], current["revision"], status="inativo")
        with self.assertRaisesRegex(HandoffRejected, "desativado"):
            self.delegate(sec, case, self.financeiro)
        queue_case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Sem dono")
        with self.assertRaisesRegex(HandoffRejected, "fila inicial"):
            self.propose(self.ca, queue_case, self.suporte)
        done = self.open_case()
        self.cases.add_evidence(self.ca, done["id"], source="s", summary="ok", evidence_type="recibo",
                                reliability="verificada", content_hash="a" * 64)
        self.cases.transition(self.ca, done["id"], self.case(done)["revision"], "resolvida")
        with self.assertRaisesRegex(HandoffRejected, "encerrada"):
            self.propose(self.ca, done, self.suporte)

    def test_one_pending_transfer_per_case_and_idempotent_proposals(self):
        case = self.open_case()
        sec = self.ctx(self.secretaria)
        first, created = self.handoffs.propose_transfer(sec, case["id"], self.suporte["id"], reason="r", summary="s",
                                                        idempotency_key="msg-77")
        again, created_again = self.handoffs.propose_transfer(sec, case["id"], self.suporte["id"], reason="r", summary="s",
                                                              idempotency_key="msg-77")
        self.assertEqual((created, created_again, again["id"]), (True, False, first["id"]))
        with self.assertRaisesRegex(HandoffRejected, "aguardando aceite"):
            self.propose(sec, case, self.financeiro)


class IsolationTests(HandoffCase):
    def test_envelope_exposes_only_what_was_shared_and_nobody_gets_extra_access(self):
        case = self.open_case()
        shared = self.evidence(case, summary="Fato compartilhado")
        self.evidence(case, summary="Fato interno que NAO foi compartilhado")
        self.cases.add_note(self.ca, case["id"], "Nota interna reservada")
        delegation = self.delegate(self.ctx(self.secretaria), case, self.financeiro, evidence_ids=[shared["id"]])
        fin = self.ctx(self.financeiro)
        view = self.handoffs.view(fin, delegation["id"])
        self.assertEqual([e["summary"] for e in view["evidence"]], ["Fato compartilhado"])
        flat = str(view)
        self.assertNotIn("NAO foi compartilhado", flat)
        self.assertNotIn("reservada", flat)
        for blocked in (lambda: self.cases.get_case(fin, case["id"]), lambda: self.cases.timeline(fin, case["id"]),
                        lambda: self.handoffs.view(self.ctx(self.suporte), delegation["id"])):
            with self.assertRaises(NotFound):
                blocked()

    def test_secrets_and_reasoning_do_not_enter_the_envelope(self):
        case = self.open_case()
        sec = self.ctx(self.secretaria)
        with self.assertRaises(ContractError):
            self.propose(sec, case, self.suporte, summary="Use a chave sk-abcdefghijklmnopqrstuvwx para entrar")
        cleaned = self.propose(sec, case, self.suporte, reason="<think>plano</think>Motivo claro", summary="Resumo")
        self.assertEqual(cleaned["reason"], "Motivo claro")
        with self.assertRaises(CaseRejected):
            self.propose(sec, case, self.financeiro, reason="<think>so raciocinio</think>", summary="x")

    def test_other_company_cannot_touch_handoffs_or_use_foreign_agents(self):
        case = self.open_case()
        proposal = self.propose(self.ctx(self.secretaria), case, self.suporte)
        dep_b = self.team.create(self.cb, "department", name="B", scope="x", status="ativo")
        foreign = self.agents.create_agent(self.cb, department_id=dep_b["id"], name="Agente B", **PROFILE)
        for action in (lambda: self.handoffs.view(self.cb, proposal["id"]),
                       lambda: self.handoffs.accept(self.cb, proposal["id"]),
                       lambda: self.handoffs.decline(self.cb, proposal["id"], "x"),
                       lambda: self.handoffs.propose_transfer(self.ca, case["id"], foreign["id"], reason="r", summary="s")):
            with self.assertRaises(NotFound):
                action()
        self.assertEqual(self.handoffs.list_handoffs(self.cb), [])
        self.assertEqual(self.handoffs.expire_due(self.b["id"]), [])

    def test_only_participants_see_a_handoff(self):
        case = self.open_case()
        proposal = self.propose(self.ctx(self.secretaria), case, self.suporte)
        self.assertEqual(self.handoffs.view(self.ctx(self.suporte), proposal["id"])["handoff"]["id"], proposal["id"])
        outsider = self.ctx(self.financeiro)
        with self.assertRaises(NotFound):
            self.handoffs.view(outsider, proposal["id"])
        with self.assertRaises(NotFound):
            self.handoffs.accept(outsider, proposal["id"])
        self.assertEqual(self.handoffs.list_handoffs(outsider), [])
        with self.assertRaises(PermissionDenied):
            self.handoffs.view({"tenant_id": self.a["id"]}, proposal["id"])


if __name__ == "__main__":
    unittest.main()
