import asyncio
import tempfile
import unittest
from pathlib import Path

from team import overview
from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.approvals import ApprovalService
from team.cases import CaseService
from team.control import OperatorControl
from team.gaps import GapRejected, GapService, terms, topic_of
from team.handoffs import HandoffService
from team.ids import new_id
from team.knowledge import KnowledgeService
from team.repository import RevisionConflict, SqliteRepository
from team.service import TeamService
from team.triage import TriageService

PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")
REQUESTED = "Consultar o registro de um dominio na Cloudflare"


class NoModel:
    """A classifier that never finds anything, so a triage outcome can only come from the declined memory."""

    async def classify(self, message, catalog):
        return {"capabilities": [], "question": None, "discarded": []}


class GapBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SqliteRepository(Path(self.temp.name) / "team.sqlite3")
        self.repo.migrate()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases)
        self.knowledge = KnowledgeService(self.repo, self.access)
        self.control = OperatorControl(self.repo, self.access, self.cases, ApprovalService(self.repo, self.access, self.cases), self.handoffs)
        self.gaps = GapService(self.repo, self.access, self.cases, self.knowledge)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Suporte", scope="x", status="ativo")
        self.agent = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Suporte", **PROFILE)
        self.other = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Outro", **PROFILE)
        for agent in (self.agent, self.other):
            self.team.create(self.ca, "access_grant", principal_id=agent["id"], resource_kind="case", resource_id=None,
                             actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)

    def working_case(self, owner=None, objective="Site do cliente nao abre"):
        case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective=objective, customer_ref="c1")
        case = self.cases.transition(self.ca, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], (owner or self.agent)["id"])
        return self.cases.transition(self.ca, case["id"], case["revision"], "em_execucao")

    def agent_ctx(self, agent=None):
        run = self.agents.start_run((self.ca), (agent or self.agent)["id"],
                                    budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 60})
        return self.access.agent_context(self.op, self.a["id"], run["id"])

    def report(self, case=None, ctx=None, requested=REQUESTED, **kwargs):
        return self.gaps.report(ctx or self.agent_ctx(), (case or self.working_case())["id"], requested, **kwargs)

    def case(self, case):
        return self.cases.get_case(self.ca, case["id"])

    def notes(self, case):
        return [i["item"]["body"] for i in self.cases.timeline(self.ca, case["id"]) if i["tipo"] == "entrada"]


class ReportTests(GapBase):
    def test_the_owner_declares_the_gap_and_the_case_stops(self):
        case = self.working_case()
        gap = self.report(case, tried=["skill diagnostico-dominio: nao cobre a Cloudflare"], gap_type="ferramenta")
        self.assertEqual((gap["state"], gap["occurrences"], gap["gap_type"]), ("aberta", 1, "ferramenta"))
        self.assertEqual(gap["related_case_ids"], [case["id"]])
        self.assertEqual(self.case(case)["state"], "pausada")
        self.assertTrue(any("Lacuna de capacidade" in note for note in self.notes(case)))
        events = [i["item"] for i in self.cases.timeline(self.ca, case["id"]) if i["tipo"] == "evento"]
        self.assertTrue(any(e["payload"].get("gap_id") == gap["id"] for e in events))

    def test_it_shows_in_the_cases_center_and_the_operator_inbox(self):
        case = self.working_case()
        gap = self.report(case)
        services = type("S", (), {"repo": self.repo, "access": self.access})()
        summary = overview.snapshot(services, self.ca, case["id"])
        self.assertEqual(summary["case"]["open_gaps"], 1)
        self.assertEqual(summary["case"]["phase"], "aguardando_humano")
        self.assertEqual([g["id"] for g in summary["gaps"]], [gap["id"]])
        inbox = overview.attention(services, self.ca)["gaps"]
        self.assertEqual([(g["id"], g["occurrences"]) for g in inbox], [(gap["id"], 1)])

    def test_only_the_current_owner_or_the_operator_may_declare_it(self):
        case = self.working_case()
        with self.assertRaises(PermissionDenied):
            self.gaps.report(self.agent_ctx(self.other), case["id"], REQUESTED)
        self.assertEqual(self.case(case)["state"], "em_execucao")
        self.assertEqual(self.report(case, ctx=self.ca)["state"], "aberta")  # the operator may

    def test_a_closed_case_or_another_company_cannot_report(self):
        case = self.working_case()
        closed = self.cases.transition(self.ca, case["id"], case["revision"], "cancelada", reason="desistiu")
        with self.assertRaises(GapRejected):
            self.gaps.report(self.ca, closed["id"], REQUESTED)
        open_case = self.working_case()
        with self.assertRaises(NotFound):
            self.gaps.report(self.cb, open_case["id"], REQUESTED)

    def test_the_same_subject_groups_into_one_pending_decision(self):
        first, second = self.working_case(), self.working_case()
        gap = self.report(first, topic="dominio cloudflare")
        again = self.report(second, requested="Cliente tem dominio na Cloudflare e o site caiu", topic="Cloudflare dominio")
        self.assertEqual(again["id"], gap["id"])
        self.assertEqual((again["occurrences"], again["related_case_ids"]), (2, [first["id"], second["id"]]))
        self.assertEqual({self.case(first)["state"], self.case(second)["state"]}, {"pausada"})
        self.assertEqual(len(self.gaps.list_gaps(self.ca)), 1)
        self.assertEqual(self.report(first, topic="dominio cloudflare")["occurrences"], 2)  # the same case again changes nothing
        other = self.report(self.working_case(), requested="Emitir nota fiscal de servico", topic="nota fiscal")
        self.assertNotEqual(other["id"], gap["id"])
        self.assertEqual(self.gaps.list_gaps(self.ca)[0]["id"], gap["id"])  # the most repeated need comes first

    def test_a_topic_is_a_stable_key_of_significant_terms(self):
        self.assertEqual(topic_of("Consultar o registro de um dominio na Cloudflare"), "cloudflare-consultar-dominio-registro")
        self.assertEqual(topic_of("x", topic="Domínio Cloudflare"), "cloudflare-dominio")
        self.assertEqual(terms("O site da Ação não abre"), ["site", "acao", "abre"])
        with self.assertRaises(GapRejected):
            topic_of("o a de")

    def test_input_is_cleaned_limited_and_never_holds_a_secret(self):
        case = self.working_case()
        with self.assertRaises(GapRejected):
            self.gaps.report(self.ca, case["id"], "   ")
        with self.assertRaises(GapRejected):
            self.gaps.report(self.ca, case["id"], REQUESTED, tried=["x"] * 11)
        with self.assertRaises(GapRejected):
            self.gaps.report(self.ca, case["id"], "usar a chave sk-test-0123456789abcdef0123 no registrador")
        with self.assertRaises(GapRejected):
            self.gaps.report(self.ca, case["id"], REQUESTED, gap_type="outro")
        self.assertEqual(self.case(case)["state"], "em_execucao")
        thought = self.report(case, requested="<think>vou inventar</think>Consultar dominio na Cloudflare")
        self.assertNotIn("inventar", thought["requested"])

    def test_a_case_still_in_the_initial_queue_keeps_its_state_but_shows_the_gap(self):
        case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Dominio fora do ar")
        self.gaps.report(self.ca, case["id"], REQUESTED)
        self.assertEqual(self.case(case)["state"], "recebida")  # no owner yet: it already waits for a person
        services = type("S", (), {"repo": self.repo, "access": self.access})()
        self.assertEqual(overview.snapshot(services, self.ca, case["id"])["case"]["phase"], "aguardando_humano")


class DecisionTests(GapBase):
    def open_gap(self, topic="dominio cloudflare"):
        first, second = self.working_case(), self.working_case()
        self.report(first, topic=topic)
        gap = self.report(second, topic=topic)
        return gap, first, second

    def test_only_the_trusted_operator_decides_with_the_expected_revision_and_a_reason(self):
        gap, *_ = self.open_gap()
        with self.assertRaises(PermissionDenied):
            self.gaps.decide(self.agent_ctx(), gap["id"], gap["revision"], "criar_skill", "ok")
        with self.assertRaises(RevisionConflict):
            self.gaps.decide(self.ca, gap["id"], gap["revision"] - 1, "criar_skill", "ok")
        with self.assertRaises(GapRejected):
            self.gaps.decide(self.ca, gap["id"], gap["revision"], "criar_skill", "  ")
        with self.assertRaises(GapRejected):
            self.gaps.decide(self.ca, gap["id"], gap["revision"], "inventar", "ok")
        with self.assertRaises(NotFound):
            self.gaps.decide(self.cb, gap["id"], gap["revision"], "criar_skill", "ok")
        self.assertEqual(self.gaps.get(self.ca, gap["id"])["state"], "aberta")

    def test_write_a_skill_or_build_a_tool_keeps_the_cases_waiting_with_the_reference(self):
        for decision in ("criar_skill", "criar_ferramenta"):
            gap, first, second = self.open_gap(topic=f"assunto {decision}")
            done = self.gaps.decide(self.ca, gap["id"], gap["revision"], decision, "Vale a pena", follow_up="issue 99")
            self.assertEqual((done["state"], done["decision"], done["follow_up"]), ("decidida", decision, "issue 99"))
            self.assertEqual(done["decided_by"], self.op.principal_id)
            self.assertEqual([self.case(first)["state"], self.case(second)["state"]], ["pausada", "pausada"])
            self.assertTrue(any("issue 99" in note for note in self.notes(first)))
            with self.assertRaises(GapRejected):
                self.gaps.decide(self.ca, gap["id"], done["revision"], "nao_atender", "mudei de ideia")

    def test_declining_cancels_the_waiting_cases_and_keeps_a_confirmed_memory(self):
        gap, first, second = self.open_gap()
        done = self.gaps.decide(self.ca, gap["id"], gap["revision"], "nao_atender", "Nao trabalhamos com Cloudflare")
        self.assertEqual([self.case(first)["state"], self.case(second)["state"]], ["cancelada", "cancelada"])
        events = [i["item"] for i in self.cases.timeline(self.ca, first["id"]) if i["tipo"] == "evento"]
        self.assertTrue(any("Fora do escopo" in (e["payload"].get("reason") or "") for e in events))
        memory = [m for m in self.knowledge.list_memory(self.ca) if m["id"] == done["memory_id"]][0]
        self.assertEqual((memory["state"], memory["source"], memory["scope"]), ("confirmada", f"lacuna:{gap['id']}", "empresa"))
        self.assertIn("cloudflare-dominio", memory["text"])

    def test_a_stale_decision_leaves_no_memory_behind(self):
        gap, *_ = self.open_gap()
        self.gaps.decide(self.ca, gap["id"], gap["revision"], "criar_skill", "ok")
        before = len(self.knowledge.list_memory(self.ca))
        other, *_ = self.open_gap(topic="outro assunto")
        with self.assertRaises(RevisionConflict):
            self.gaps.decide(self.ca, other["id"], other["revision"] - 1, "nao_atender", "nao")
        self.assertEqual(len(self.knowledge.list_memory(self.ca)), before)

    def test_fulfilling_resumes_only_what_is_still_paused_and_under_the_team(self):
        gap, first, second = self.open_gap()
        with self.assertRaises(GapRejected):
            self.gaps.fulfill(self.ca, gap["id"], gap["revision"], "ja existe")  # not decided yet
        done = self.gaps.decide(self.ca, gap["id"], gap["revision"], "criar_skill", "ok")
        taken = self.case(second)  # the operator takes the second case over while it waits
        self.control.take_over(self.ca, taken["id"], taken["revision"], "assumo este")
        fulfilled = self.gaps.fulfill(self.ca, gap["id"], done["revision"], "Skill publicada")
        self.assertEqual(fulfilled["state"], "atendida")
        self.assertIsNotNone(fulfilled["fulfilled_at"])
        self.assertEqual(self.case(first)["state"], "em_execucao")
        self.assertEqual(self.case(second)["state"], "pausada")  # the person in control decides about that one
        with self.assertRaises(GapRejected):
            self.gaps.fulfill(self.ca, gap["id"], fulfilled["revision"], "de novo")

    def test_a_declined_gap_cannot_be_marked_as_fulfilled(self):
        gap, *_ = self.open_gap()
        done = self.gaps.decide(self.ca, gap["id"], gap["revision"], "nao_atender", "nao")
        with self.assertRaises(GapRejected):
            self.gaps.fulfill(self.ca, gap["id"], done["revision"], "x")

    def test_gaps_of_one_company_are_invisible_to_another(self):
        gap, *_ = self.open_gap()
        self.assertEqual(self.gaps.list_gaps(self.cb), [])
        with self.assertRaises(NotFound):
            self.gaps.get(self.cb, gap["id"])
        with self.assertRaises(PermissionDenied):
            self.gaps.list_gaps(self.agent_ctx())


class TriageMemoryTests(GapBase):
    def setUp(self):
        super().setUp()
        self.triage = TriageService(self.repo, self.access, self.cases, self.handoffs, NoModel())
        self.secretaria = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Secretaria", **PROFILE)
        gap, *_ = [self.report(self.working_case(), topic="dominio cloudflare")]
        self.declined = self.gaps.decide(self.ca, gap["id"], gap["revision"], "nao_atender", "Fora do nosso servico")

    def go(self, text):
        case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Atendimento", customer_ref="c9")
        return asyncio.run(self.triage.triage(self.ca, case["id"], text, secretaria_agent_id=self.secretaria["id"]))

    def test_a_request_already_declined_goes_straight_to_a_person_with_the_reason(self):
        decision = self.go("Meu dominio esta na Cloudflare e o site caiu")
        self.assertEqual((decision["decision"], decision["method"], decision["capabilities"]), ("fila_humana", "regra", []))
        reason = decision["reasons"][0]
        self.assertEqual((reason["tipo"], reason["lacuna"], reason["memoria"]), ("fora_do_escopo", self.declined["id"], self.declined["memory_id"]))

    def test_other_requests_are_triaged_as_usual(self):
        decision = self.go("Quero a segunda via do boleto")
        self.assertEqual(decision["method"], "llm")
        self.assertNotEqual(decision["reasons"][0]["tipo"], "fora_do_escopo")

    def test_forgetting_the_memory_stops_the_automatic_refusal(self):
        self.knowledge.forget(self.ca, self.declined["memory_id"])
        self.assertEqual(self.go("Meu dominio esta na Cloudflare")["method"], "llm")

    def test_another_company_is_not_affected(self):
        dep = self.team.create(self.cb, "department", name="Suporte", scope="x", status="ativo")
        secretaria = self.agents.create_agent(self.cb, department_id=dep["id"], name="Secretaria", **PROFILE)
        case = self.cases.open_case(self.cb, department_id=dep["id"], objective="Atendimento", customer_ref="c9")
        decision = asyncio.run(self.triage.triage(self.cb, case["id"], "dominio na Cloudflare", secretaria_agent_id=secretaria["id"]))
        self.assertEqual(decision["method"], "llm")


if __name__ == "__main__":
    unittest.main()
