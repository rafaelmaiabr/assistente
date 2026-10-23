import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.approvals import ApprovalService
from team.cases import CaseService
from team.connections import ConnectionService, LauncherRegistry
from team.diagnostics import Diagnostics, DnsFailure, HttpReply, TlsFailure
from team.finance import FinanceService
from team.handoffs import HandoffService
from team.ids import new_id
from team.renewal import ACTION, NOTICE_MANUAL, NOTICE_REGISTRAR, FlowRejected, RenewalFlow
from team.repository import SqliteRepository
from team.service import TeamService
from team.vault import MemoryVault
from tests.test_team_diagnostics import PUBLIC, FakeNetwork, rdap_body

PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")
BUDGET = {"max_steps": 4, "token_budget": 100, "deadline_seconds": 60}
DOMAIN = "exemplo.com.br"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def stamp(self, **delta):
        return (self.now + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


class RenewalCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SqliteRepository(Path(self.temp.name) / "team.sqlite3")
        self.repo.migrate()
        self.clock = Clock()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases, clock=self.clock)
        self.approvals = ApprovalService(self.repo, self.access, self.cases, clock=self.clock)
        self.finance = FinanceService(self.repo, self.access, clock=self.clock)
        self.net = FakeNetwork()
        self.net.rdap[DOMAIN] = rdap_body(expires="2026-05-01T00:00:00Z")  # expired in the registry
        self.flow = RenewalFlow(self.repo, self.access, self.cases, self.handoffs, self.approvals,
                                lambda tenant: Diagnostics(self.net, lambda host: host.endswith(DOMAIN), self.clock), self.finance, clock=self.clock)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Atendimento", scope="x", status="ativo")
        self.suporte = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Suporte", **PROFILE)
        self.fin = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Financeiro", **PROFILE)
        self.other = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Outro", **PROFILE)
        for source, target in ((self.suporte, self.fin), (self.fin, self.suporte)):
            current = self.agents.get_agent(self.ca, source["id"])
            self.agents.update_agent(self.ca, source["id"], current["revision"], handoff_target_ids=[target["id"]])
        for agent in (self.suporte, self.fin):  # Financeiro owns the case after a billing hand-over
            self.team.create(self.ca, "access_grant", principal_id=agent["id"], resource_kind="case", resource_id=None,
                             actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Site fora do ar", origin="email")
        case = self.cases.transition(self.ca, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], self.suporte["id"])
        self.case = self.cases.transition(self.ca, case["id"], case["revision"], "em_execucao")
        self.sup_ctx, self.fin_ctx = self.ctx(self.suporte), self.ctx(self.fin)

    def ctx(self, agent):
        run = self.agents.start_run(self.ca, agent["id"], budget=BUDGET)
        return self.access.agent_context(self.op, self.a["id"], run["id"])

    def healthy(self):
        self.net.dns[DOMAIN] = [PUBLIC]
        self.net.web[(DOMAIN, "https", "/")] = HttpReply(200, {}, b"", {"not_after": "2027-01-01T00:00:00Z"})
        self.net.rdap[DOMAIN] = rdap_body(expires="2027-06-15T00:00:00Z")

    def start(self):
        """Support diagnoses and delegates; Financeiro accepts. Returns (diagnosis, handoff)."""
        diagnosis = self.flow.diagnose(self.sup_ctx, self.case["id"], DOMAIN)
        handoff = self.flow.open_financial_subtask(self.sup_ctx, self.case["id"], DOMAIN, self.fin["id"], diagnosis_evidence_id=diagnosis["evidencia"])
        self.handoffs.accept(self.fin_ctx, handoff["id"])
        with self.repo.transaction(self.a["id"]) as session:
            handoff = session.get("handoff", handoff["id"])
        return diagnosis, handoff

    def propose(self, **kwargs):
        options = dict(counterparty="Registro Exemplo", amount_cents=8990, currency="BRL")
        return self.flow.propose(self.fin_ctx, self.case["id"], DOMAIN, **{**options, **kwargs})

    def approved(self):
        self.start()
        proposal = self.propose()
        approval = self.approvals.get(self.ca, proposal["aprovacao"])
        self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])
        return approval

    def renew(self, approval, **kwargs):
        options = dict(reference="REG-2026-77", amount_cents=8990, currency="BRL", paid_at=self.clock.stamp(), registrar="Registro Exemplo")
        return self.flow.record_manual_renewal(self.ca, self.case["id"], approval["id"], **{**options, **kwargs})

    def task_id(self):
        with self.repo.transaction(self.a["id"]) as session:
            return next(t["id"] for t in session.list("task", case_id=self.case["id"], limit=10) if t["assignee_agent_id"] == self.fin["id"])

    def state(self):
        return self.cases.get_case(self.ca, self.case["id"])["state"]


class FullFlowTests(RenewalCase):
    def test_support_to_financeiro_approval_manual_renewal_propagation_and_return(self):
        diagnosis, handoff = self.start()
        self.assertEqual((diagnosis["categoria"], diagnosis["certeza"], diagnosis["renovacao_em_questao"]), ("dominio_expirado", "confirmado", True))
        self.assertEqual((handoff["state"], handoff["return_to"]), ("aceito", self.suporte["id"]))
        self.assertEqual(self.state(), "aguardando_subtarefa")
        proposal = self.propose()
        self.assertEqual(proposal["acao"], "aprovacao_solicitada")
        self.assertIn("AINDA NAO foi feita", proposal["mensagem"])
        approval = self.approvals.get(self.ca, proposal["aprovacao"])
        self.assertEqual((approval["action"], approval["requested_by"], approval["amount_cents"], approval["currency"]), (ACTION, self.fin["id"], 8990, "brl"))
        status = self.flow.status(self.ca, self.case["id"])
        self.assertEqual((status["etapa"], status["execucao"]), ("aprovacao_pendente", "manual"))
        self.assertFalse(status["pode_encerrar"])
        self.assertEqual(status["aviso"], NOTICE_MANUAL)
        self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])
        self.assertEqual(self.flow.status(self.ca, self.case["id"])["etapa"], "aprovada_aguardando_execucao_manual")
        # a person renews outside the system; the operator records it
        receipt = self.renew(approval)
        self.assertTrue(receipt["evidencia"])
        done = self.approvals.get(self.ca, approval["id"])
        self.assertEqual((done["state"], done["effect_outcome"]), ("consumida", "confirmado"))
        self.assertEqual(self.flow.status(self.ca, self.case["id"])["etapa"], "renovacao_confirmada_aguardando_verificacao")
        # the registry says valid but DNS still fails right after the renewal: propagation, not a persistent failure
        self.net.rdap[DOMAIN] = rdap_body(expires="2027-06-15T00:00:00Z")
        first = self.flow.reverify(self.ca, self.case["id"], DOMAIN)
        self.assertEqual((first["estado"], first["categoria"]), ("em_propagacao", "dns_sem_resposta_dominio_vigente"))
        self.assertIn("propagacao", first["detalhe"])
        self.assertFalse(self.flow.status(self.ca, self.case["id"])["pode_encerrar"])
        self.clock.now += timedelta(hours=25)
        self.assertEqual(self.flow.reverify(self.ca, self.case["id"], DOMAIN)["estado"], "falha_persistente")
        self.assertIn("Investigar", self.flow.status(self.ca, self.case["id"])["proximo_passo"])
        with self.assertRaises(FlowRejected):
            self.flow.close_if_verified(self.ca, self.case["id"], "tentativa")
        self.healthy()
        ok = self.flow.reverify(self.ca, self.case["id"], DOMAIN)
        self.assertEqual((ok["estado"], ok["expira_em"]), ("resolvido_tecnicamente", "2027-06-15T00:00:00Z"))
        # Financeiro returns the result to Support, which keeps the case and validates
        self.flow.return_to_support(self.fin_ctx, self.task_id(), "Renovacao confirmada por uma pessoa (REG-2026-77); aguardando validacao tecnica.")
        self.assertEqual(self.state(), "em_execucao")
        status = self.flow.status(self.ca, self.case["id"])
        self.assertTrue(status["pode_encerrar"], status)
        closed = self.flow.close_if_verified(self.ca, self.case["id"], "Site restabelecido apos renovacao confirmada")
        self.assertEqual(closed["state"], "resolvida")

    def test_denied_approval_never_renews_or_resolves(self):
        self.start()
        approval = self.approvals.get(self.ca, self.propose()["aprovacao"])
        self.approvals.deny(self.ca, approval["id"], "Cliente nao quer renovar")
        status = self.flow.status(self.ca, self.case["id"])
        self.assertEqual(status["etapa"], "aprovacao_negada")
        self.assertIn("aprovacao negada", status["bloqueios_para_encerrar"])
        with self.assertRaises(FlowRejected):
            self.renew(approval)
        with self.assertRaises(FlowRejected):
            self.flow.close_if_verified(self.ca, self.case["id"], "x")
        self.assertNotEqual(self.state(), "resolvida")

    def test_an_expired_approval_is_a_timeout_not_a_resolution(self):
        self.start()
        approval = self.approvals.get(self.ca, self.propose()["aprovacao"])
        self.clock.now += timedelta(days=2)
        self.approvals.expire_due(self.a["id"])
        self.assertEqual(self.flow.status(self.ca, self.case["id"])["etapa"], "aprovacao_expirada")
        with self.assertRaises(FlowRejected):
            self.renew(approval)
        with self.assertRaises(FlowRejected):
            self.flow.close_if_verified(self.ca, self.case["id"], "x")

    def test_renewal_confirmed_but_the_site_is_still_down_for_a_non_renewal_reason(self):
        approval = self.approved()
        self.renew(approval)
        self.net.dns[DOMAIN] = [PUBLIC]
        self.net.web[(DOMAIN, "https", "/")] = HttpReply(503, {}, b"", {"not_after": "2027-01-01T00:00:00Z"})
        self.net.rdap[DOMAIN] = rdap_body(expires="2027-06-15T00:00:00Z")
        result = self.flow.reverify(self.ca, self.case["id"], DOMAIN)
        self.assertEqual((result["estado"], result["categoria"]), ("falha_persistente", "erro_do_servidor"))  # HTTP is not propagation
        with self.assertRaises(FlowRejected):
            self.flow.close_if_verified(self.ca, self.case["id"], "x")
        self.net.web[(DOMAIN, "https", "/")] = TlsFailure("vencido")
        self.assertEqual(self.flow.reverify(self.ca, self.case["id"], DOMAIN)["categoria"], "certificado_vencido")

    def test_a_verification_older_than_the_confirmation_does_not_count(self):
        approval = self.approved()
        self.healthy()
        before = self.flow.reverify(self.ca, self.case["id"], DOMAIN)  # healthy, but nobody confirmed a renewal yet
        self.assertEqual(before["estado"], "sem_confirmacao_de_renovacao")
        self.clock.now += timedelta(minutes=10)
        self.renew(approval)
        status = self.flow.status(self.ca, self.case["id"])
        self.assertEqual(status["etapa"], "renovacao_confirmada_aguardando_verificacao")
        self.assertFalse(status["pode_encerrar"])
        self.clock.now += timedelta(minutes=1)
        self.assertEqual(self.flow.reverify(self.ca, self.case["id"], DOMAIN)["estado"], "resolvido_tecnicamente")
        self.assertTrue(self.flow.status(self.ca, self.case["id"])["pode_encerrar"] or self.state() == "aguardando_subtarefa")

    def test_when_only_billing_is_left_financeiro_takes_over_after_accepting(self):
        approval = self.approved()
        self.renew(approval)
        self.healthy()
        self.flow.reverify(self.ca, self.case["id"], DOMAIN)
        self.flow.return_to_support(self.fin_ctx, self.task_id(), "Renovacao confirmada por uma pessoa; resta apenas a cobranca ao cliente.")
        handoff = self.flow.hand_over_billing(self.sup_ctx, self.case["id"], self.fin["id"], "Site normalizado; resta cobrar o cliente.")
        self.assertEqual((handoff["handoff_kind"], handoff["state"]), ("transferencia", "proposto"))
        self.assertEqual(self.cases.get_case(self.ca, self.case["id"])["owner_agent_id"], self.suporte["id"])  # not yet
        self.handoffs.accept(self.fin_ctx, handoff["id"])
        self.assertEqual(self.cases.get_case(self.ca, self.case["id"])["owner_agent_id"], self.fin["id"])


class GuardTests(RenewalCase):
    def test_finance_blockers_turn_the_proposal_into_questions_without_an_approval(self):
        csv = ("tipo,contraparte,beneficiario,pagador,documento,valor,moeda,origem,data_da_fonte\n"
               "renovacao,Registro Exemplo,Registro Exemplo,Empresa A,REN-1,89.90,BRL,erp,2026-05-01\n"
               "renovacao,Registro Exemplo,Registro Exemplo,Empresa A,REN-1,99.90,BRL,email,2026-05-02\n")
        self.finance.import_csv(self.ca, csv, "teste")
        self.flow.finance.clock = self.clock
        self.start()
        # renovacao rows alone do not block; two payable invoices with the same number do
        self.finance.import_csv(self.ca, "tipo,contraparte,beneficiario,pagador,documento,valor,moeda,origem,data_da_fonte\n"
                                "fatura_fornecedor,Registro Exemplo,Registro Exemplo,Empresa A,FAT-9,89.90,BRL,erp,2026-05-01\n"
                                "fatura_fornecedor,Registro Exemplo,Registro Exemplo,Empresa A,FAT-9,95.00,BRL,email,2026-05-02\n", "teste2")
        result = self.propose(document="FAT-9")
        self.assertEqual((result["acao"], result["aprovacao"], result["bloqueios"]), ("pedir_informacoes", None, ["fatura_duplicada"]))
        self.assertIn("Nenhuma renovacao foi feita", result["mensagem"])
        with self.repo.transaction(self.a["id"]) as session:
            self.assertEqual(session.list("approval_request", limit=10), [])

    def test_the_subtask_needs_a_real_renewal_diagnosis_of_the_same_domain(self):
        self.healthy()
        healthy = self.flow.diagnose(self.sup_ctx, self.case["id"], DOMAIN)
        self.assertFalse(healthy["renovacao_em_questao"])
        with self.assertRaisesRegex(FlowRejected, "nao aponta problema de registro"):
            self.flow.open_financial_subtask(self.sup_ctx, self.case["id"], DOMAIN, self.fin["id"], diagnosis_evidence_id=healthy["evidencia"])
        with self.assertRaisesRegex(FlowRejected, "Registre o diagnostico"):
            self.flow.open_financial_subtask(self.sup_ctx, self.case["id"], DOMAIN, self.fin["id"], diagnosis_evidence_id="evd_" + "0" * 32)
        other = self.net.dns.pop(DOMAIN)
        expired = self.flow.diagnose(self.sup_ctx, self.case["id"], DOMAIN)
        self.assertTrue(self.net.dns.update({DOMAIN: other}) is None and expired["categoria"])
        with self.assertRaisesRegex(FlowRejected, "outro dominio"):
            self.flow.open_financial_subtask(self.sup_ctx, self.case["id"], "www." + DOMAIN, self.fin["id"], diagnosis_evidence_id=expired["evidencia"])
        with self.assertRaises(FlowRejected):
            self.flow.diagnose(self.sup_ctx, self.case["id"], "outro.com")  # not an authorized asset
        with self.assertRaises(FlowRejected):
            self.flow.diagnose(self.sup_ctx, self.case["id"], "127.0.0.1")

    def test_only_the_owner_or_the_assignee_may_ask_and_only_the_operator_records(self):
        with self.assertRaises(NotFound):
            self.approvals.request(self.fin_ctx, self.case["id"], ACTION, resource="dominio:x.com", amount_cents=1, currency="brl")  # no subtask yet
        approval = self.approved()
        outsider = self.ctx(self.other)
        with self.assertRaises(NotFound):
            self.approvals.request(outsider, self.case["id"], ACTION, resource="dominio:y.com", amount_cents=1, currency="brl")
        for agent_ctx in (self.sup_ctx, self.fin_ctx, outsider):
            with self.assertRaises(PermissionDenied):
                self.flow.record_manual_renewal(agent_ctx, self.case["id"], approval["id"], reference="R", amount_cents=8990, currency="BRL", paid_at=self.clock.stamp())
        with self.assertRaises(PermissionDenied):
            self.approvals.approve(self.fin_ctx, approval["id"], approval["payload_hash"])  # asking never means deciding

    def test_the_confirmation_must_match_what_was_approved(self):
        approval = self.approved()
        for kwargs, text in (({"amount_cents": 9990}, "diferem"), ({"currency": "USD"}, "diferem"), ({"paid_at": self.clock.stamp(days=2)}, "futuro"),
                             ({"paid_at": "ontem"}, "Data invalida"), ({"reference": "   "}, "")):
            with self.assertRaises(Exception, msg=str(kwargs)) as raised:
                self.renew(approval, **kwargs)
            self.assertIn(text, str(raised.exception))
        self.assertEqual(self.approvals.get(self.ca, approval["id"])["state"], "aprovada")  # nothing was consumed
        self.renew(approval)
        with self.assertRaises(FlowRejected):
            self.renew(approval)  # single use
        with self.assertRaises(NotFound):
            self.flow.record_manual_renewal(self.ca, self.case["id"], "apv_" + "0" * 32, reference="R", amount_cents=1, currency="BRL", paid_at=self.clock.stamp())

    def test_nothing_claims_a_purchase_and_the_registrar_notice_stays_manual(self):
        approval = self.approved()
        self.assertIn(self.flow.status(self.ca, self.case["id"])["aviso"], (NOTICE_MANUAL,))
        self.assertIn("nao comprou nem renovou", NOTICE_MANUAL)
        ConnectionService(self.repo, self.access, MemoryVault(), LauncherRegistry(Path(self.temp.name) / "l.json", self.access.operator_id)) \
            .create_connection(self.ca, label="Registrador", provider="registrador", connection_kind="outro")
        status = self.flow.status(self.ca, self.case["id"])
        self.assertEqual((status["aviso"], status["execucao"]), (NOTICE_REGISTRAR, "manual"))
        for claim in ("Dominio renovado e pago", "Pagamento efetuado, dominio comprado", "A renovacao foi quitada"):
            with self.assertRaisesRegex(FlowRejected, "sem confirmacao"):
                self.flow.return_to_support(self.fin_ctx, self.task_id(), claim)
        self.flow.return_to_support(self.fin_ctx, self.task_id(), "Aprovada; a renovacao ainda nao foi confirmada por uma pessoa.")
        self.assertTrue(approval)

    def test_other_companies_cannot_see_or_drive_the_flow(self):
        diagnosis, _ = self.start()
        with self.assertRaises(NotFound):
            self.flow.status(self.cb, self.case["id"])
        with self.assertRaises(NotFound):
            self.flow.reverify(self.cb, self.case["id"], DOMAIN)
        with self.assertRaises(PermissionDenied):
            self.flow.status("nao-e-contexto", self.case["id"])
