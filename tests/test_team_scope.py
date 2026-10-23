import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team import overview
from team.access import AccessContext, AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.approvals import ApprovalService
from team.cases import CaseService
from team.connections import ConnectionService, LauncherRegistry
from team.handoffs import HandoffService
from team.identity import _TOKEN as IDENTITY_TOKEN, Identity, IdentityRejected, IdentityService
from team.ids import new_id
from team.repository import SqliteRepository
from team.service import TeamService
from team.vault import MemoryVault

PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class ScopeBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SqliteRepository(Path(self.temp.name) / "team.sqlite3")
        self.repo.migrate()
        self.clock = Clock()
        self.access = AccessService(self.repo, new_id("user"))
        self.identity = IdentityService(self.repo, self.access, clock=self.clock)
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases, clock=self.clock)
        self.approvals = ApprovalService(self.repo, self.access, self.cases, clock=self.clock)
        self.connections = ConnectionService(self.repo, self.access, MemoryVault(), LauncherRegistry(Path(self.temp.name) / "l.json", self.access.operator_id), None)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.owner = self.access.tenant_context(self.op, self.a["id"])
        self.owner_b = self.access.tenant_context(self.op, self.b["id"])
        self.sup = self.team.create(self.owner, "department", name="Suporte", scope="x", status="ativo")
        self.fin = self.team.create(self.owner, "department", name="Financeiro", scope="x", status="ativo")
        self.dep_b = self.team.create(self.owner_b, "department", name="Suporte", scope="x", status="ativo")
        self.agent_sup = self.agents.create_agent(self.owner, department_id=self.sup["id"], name="Suporte", **PROFILE)
        self.agent_fin = self.agents.create_agent(self.owner, department_id=self.fin["id"], name="Financeiro", **PROFILE)
        self.case_sup = self.working_case(self.sup, self.agent_sup, "Site fora do ar")
        self.case_fin = self.working_case(self.fin, self.agent_fin, "Boleto vencido")

    def working_case(self, department, agent, objective):
        case = self.cases.open_case(self.owner, department_id=department["id"], objective=objective, customer_ref="c1")
        case = self.cases.transition(self.owner, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(self.owner, case["id"], case["revision"], agent["id"])
        return self.cases.transition(self.owner, case["id"], case["revision"], "em_execucao")

    def member(self, email, role, departments=None, tenant=None):
        tenant = tenant or self.a
        manager = self.access.tenant_context(self.op, tenant["id"])
        self.identity.invite(manager, tenant["id"], email, role, departments=departments)
        token, _ = self.identity.sign_in("sub-" + email, email)
        return self.identity.context(self.identity.resolve(token), tenant["id"])

    def request_approval(self, case):
        approval, _ = self.approvals.request(self.owner, case["id"], action="enviar_email", resource="con_x", recipients=["c@x.invalid"],
                                             params={"assunto": "Oi"})
        return approval

    def services(self):
        return type("S", (), {"repo": self.repo, "access": self.access})()


class DepartmentScopeTests(ScopeBase):
    def test_a_limited_member_sees_only_their_departments_in_every_listing(self):
        support = self.member("sup@empresa.invalid", "atendente", [self.sup["id"]])
        self.assertEqual(support.departments, frozenset({self.sup["id"]}))
        self.assertEqual([c["id"] for c in self.cases.list_cases(support)], [self.case_sup["id"]])
        self.assertEqual([d["id"] for d in self.team.list(support, "department")], [self.sup["id"]])
        self.assertEqual([a["id"] for a in self.agents.list_agents(support)], [self.agent_sup["id"]])
        self.assertEqual([c["id"] for c in overview.list_cases(self.services(), support)["cases"]], [self.case_sup["id"]])
        self.assertEqual(len(overview.list_cases(self.services(), self.owner)["cases"]), 2)

    def test_ids_of_other_departments_answer_not_found_for_reading_and_writing(self):
        support = self.member("sup@empresa.invalid", "atendente", [self.sup["id"]])
        with self.assertRaises(NotFound):
            self.cases.get_case(support, self.case_fin["id"])
        with self.assertRaises(NotFound):
            overview.snapshot(self.services(), support, self.case_fin["id"])
        with self.assertRaises(NotFound):
            self.cases.add_note(support, self.case_fin["id"], "intruso")
        with self.assertRaises(NotFound):
            self.cases.open_case(support, department_id=self.fin["id"], objective="Em outro departamento")
        self.assertEqual(self.cases.open_case(support, department_id=self.sup["id"], objective="No meu")["department_id"], self.sup["id"])
        fresh = self.cases.open_case(support, department_id=self.sup["id"], objective="Sem dono")
        with self.assertRaises(NotFound):
            self.cases.assign_owner(support, fresh["id"], fresh["revision"], self.agent_fin["id"])   # an agent of another department
        self.assertEqual(self.cases.assign_owner(support, fresh["id"], fresh["revision"], self.agent_sup["id"])["owner_agent_id"], self.agent_sup["id"])
        self.assertEqual(len(self.cases.timeline(self.owner, self.case_fin["id"])), len(self.cases.timeline(self.owner, self.case_fin["id"])))

    def test_events_the_inbox_and_cursors_hide_other_departments(self):
        support = self.member("sup@empresa.invalid", "atendente", [self.sup["id"]])
        self.request_approval(self.case_sup)
        approval_fin = self.request_approval(self.case_fin)
        feed = overview.events_after(self.services(), support, 0)
        self.assertTrue(feed["events"])
        self.assertEqual({e["case_id"] for e in feed["events"]}, {self.case_sup["id"]})
        full = overview.events_after(self.services(), self.owner, 0)
        self.assertEqual({e["case_id"] for e in full["events"]} - {None}, {self.case_sup["id"], self.case_fin["id"]})   # None: company-wide events
        self.assertEqual(feed["cursor"], full["cursor"])                       # paging moves past hidden events, so it never stalls
        with self.assertRaises(NotFound):
            overview.events_after(self.services(), support, 0, case_id=self.case_fin["id"])
        inbox = overview.attention(self.services(), support)
        self.assertEqual([a["case_id"] for a in inbox["pending_approvals"]], [self.case_sup["id"]])
        self.assertNotIn(approval_fin["id"], str(inbox))
        self.assertEqual(len(overview.attention(self.services(), self.owner)["pending_approvals"]), 2)

    def test_a_member_in_several_departments_and_one_without_limit(self):
        both = self.member("dois@empresa.invalid", "aprovador", [self.sup["id"], self.fin["id"]])
        everything = self.member("tudo@empresa.invalid", "atendente")
        self.assertEqual(len(self.cases.list_cases(both)), 2)
        self.assertIsNone(everything.departments)
        self.assertEqual(len(self.cases.list_cases(everything)), 2)

    def test_the_limit_is_part_of_the_context_and_reads_the_stored_value_every_time(self):
        support = self.member("sup@empresa.invalid", "atendente", [self.sup["id"]])
        user = self.identity.members(self.owner, self.a["id"])[0]["user_id"]
        token = self.identity.sign_in("sub-sup@empresa.invalid", "sup@empresa.invalid")[0]
        identity = self.identity.resolve(token)
        self.assertEqual(identity.scopes, {self.a["id"]: (self.sup["id"],)})
        self.identity.set_departments(self.owner, self.a["id"], user, [self.fin["id"]])
        self.assertEqual(self.identity.context(identity, self.a["id"]).departments, frozenset({self.fin["id"]}))
        self.identity.set_departments(self.owner, self.a["id"], user, [])
        self.assertIsNone(self.identity.context(identity, self.a["id"]).departments)
        self.assertEqual(self.identity.members(self.owner, self.a["id"])[0]["departments"], [])

    def test_only_valid_departments_of_this_company_and_never_for_an_administrator(self):
        with self.assertRaises(IdentityRejected):
            self.member("x@empresa.invalid", "atendente", [self.dep_b["id"]])          # a department of another company
        with self.assertRaises(IdentityRejected):
            self.member("y@empresa.invalid", "atendente", ["dep_" + "0" * 32])
        with self.assertRaises(IdentityRejected):
            self.member("z@empresa.invalid", "admin", [self.sup["id"]])
        with self.assertRaises(IdentityRejected):
            self.identity.invite(self.owner, self.a["id"], "w@empresa.invalid", "leitor", departments="dep")
        ana = self.member("ana@empresa.invalid", "leitor", [self.sup["id"]])
        with self.assertRaises(IdentityRejected):
            self.identity.set_departments(self.owner, self.a["id"], ana.principal_id, ["dep_" + "1" * 32])
        self.identity.set_role(self.owner, self.a["id"], ana.principal_id, "admin")      # promoting drops the limit
        self.assertEqual(self.identity.members(self.owner, self.a["id"])[0]["departments"], [])
        with self.assertRaises(IdentityRejected):
            self.identity.set_departments(self.owner, self.a["id"], ana.principal_id, [self.sup["id"]])
        identity = Identity(ana.principal_id, "a@b.invalid", None, "ses_x", {self.a["id"]: "admin"}, IDENTITY_TOKEN)
        with self.assertRaises(PermissionDenied):
            self.access.member_context(identity, self.a["id"], "admin", [self.sup["id"]])      # an administrator is never limited
        with self.assertRaises(PermissionDenied):
            AccessContext(self.a["id"], "usr_" + "1" * 32, "operator", object(), "atendente", frozenset({"x"}))

    def test_only_administrators_change_the_limits(self):
        support = self.member("sup@empresa.invalid", "atendente", [self.sup["id"]])
        for call in (lambda: self.identity.set_departments(support, self.a["id"], support.principal_id, []),
                     lambda: self.identity.invite(support, self.a["id"], "a@b.invalid", "leitor")):
            with self.assertRaises(PermissionDenied):
                call()


class ConfigurationIsAdministratorOnlyTests(ScopeBase):
    def test_roles_below_admin_cannot_change_configuration_even_through_the_services(self):
        for role in ("leitor", "atendente", "aprovador"):
            who = self.member(f"{role}@empresa.invalid", role)
            with self.assertRaises(PermissionDenied):
                self.team.create(who, "department", name="Novo", scope="x", status="ativo")
            with self.assertRaises(PermissionDenied):
                self.agents.create_agent(who, department_id=self.sup["id"], name="Outro", **PROFILE)
            with self.assertRaises(PermissionDenied):
                self.connections.create_connection(who, label="x", provider="y", connection_kind="http")
        admin = self.member("admin@empresa.invalid", "admin")
        self.assertEqual(self.team.create(admin, "department", name="Novo", scope="x", status="ativo")["name"], "Novo")
        self.assertEqual(self.agents.create_agent(admin, department_id=self.sup["id"], name="Outro", **PROFILE)["name"], "Outro")


class ServiceIdentityTests(ScopeBase):
    def test_a_worker_has_its_own_identity_that_is_neither_a_person_nor_the_local_owner(self):
        worker = self.access.service_context(self.a["id"], "worker")
        expiry = self.access.service_context(self.a["id"], "expiracao")
        self.assertTrue(worker.principal_id.startswith("sys_"))
        self.assertNotEqual(worker.principal_id, expiry.principal_id)
        self.assertNotEqual(worker.principal_id, self.access.operator_id)
        self.assertEqual(worker.principal_id, self.access.service_context(self.a["id"], "worker").principal_id)   # stable
        self.assertEqual((worker.role, worker.member_role, worker.tenant_id), ("operator", "servico", self.a["id"]))
        with self.assertRaises(PermissionDenied):
            self.access.service_context(self.a["id"], "dono")
        with self.assertRaises(NotFound):
            self.access.service_context("ten_" + "0" * 32, "worker")

    def test_a_worker_can_work_cases_but_never_approve_administer_or_manage_members(self):
        worker = self.access.service_context(self.a["id"], "worker")
        case = self.cases.open_case(worker, department_id=self.sup["id"], objective="Trabalho do worker")
        self.assertEqual(case["origin"], "operador")
        self.assertIsNotNone(self.agents.start_run(worker, self.agent_sup["id"], budget={"max_steps": 2, "token_budget": 10, "deadline_seconds": 5}))
        approval = self.request_approval(self.case_sup)
        for forbidden in (lambda: self.approvals.approve(worker, approval["id"], approval["payload_hash"]),
                          lambda: self.approvals.deny(worker, approval["id"], "nao"),
                          lambda: self.identity.invite(worker, self.a["id"], "x@y.invalid", "leitor"),
                          lambda: self.team.create(worker, "department", name="N", scope="x", status="ativo"),
                          lambda: self.agents.create_agent(worker, department_id=self.sup["id"], name="N", **PROFILE),
                          lambda: self.team.create(worker, "access_grant", principal_id=self.agent_sup["id"], resource_kind="case",
                                                   resource_id=None, actions=["read"], granted_by=worker.principal_id, expires_at=None)):
            with self.assertRaises(PermissionDenied):
                forbidden()
        self.assertEqual(self.approvals.get(self.owner, approval["id"])["state"], "pendente")

    def test_what_the_background_sweeps_do_is_recorded_under_their_identity(self):
        approval = self.request_approval(self.case_sup)
        self.clock.now += timedelta(days=3)
        expired = self.approvals.expire_due(self.a["id"])
        self.assertEqual([a["id"] for a in expired], [approval["id"]])
        actors = {e["actor_id"] for _, e in self.repo_events(self.case_sup["id"]) if e["type"] == "approval.expired"}
        self.assertEqual(actors, {self.access.service_context(self.a["id"], "expiracao").principal_id})
        self.assertNotIn(self.access.operator_id, actors)

    def repo_events(self, case_id):
        with self.repo.transaction(self.a["id"]) as session:
            return session.events(case_id, 0, 1000)

    def test_handoff_expiry_is_recorded_under_the_service_identity_too(self):
        proposal = self.handoffs.propose_transfer(self.owner, self.case_sup["id"], self.agent_fin["id"], reason="x", summary="y", idempotency_key="k1")
        handoff = proposal[0] if isinstance(proposal, tuple) else proposal
        self.clock.now += timedelta(days=30)
        expired = self.handoffs.expire_due(self.a["id"])
        self.assertEqual([h["id"] for h in expired], [handoff["id"]])
        actors = {e["actor_id"] for _, e in self.repo_events(self.case_sup["id"]) if e["type"] == "handoff.expired"}
        self.assertEqual(actors, {self.access.service_context(self.a["id"], "expiracao").principal_id})


if __name__ == "__main__":
    unittest.main()
