import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.approvals import ApprovalService
from team.cases import CaseService
from team.control import OperatorControl
from team.gaps import GapService
from team.handoffs import HandoffService
from team.identity import Identity, IdentityRejected, IdentityService, LoginDenied, hash_token, normalize_email
from team.ids import new_id
from team.knowledge import KnowledgeService
from team.repository import SqliteRepository
from team.service import TeamService
from tests.dbtools import SqlTools

PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class IdentityBase(SqlTools, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "team.sqlite3"
        self.repo = SqliteRepository(self.path)
        self.repo.migrate()
        self.clock = Clock()
        self.access = AccessService(self.repo, new_id("user"))
        self.identity = IdentityService(self.repo, self.access, clock=self.clock)
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases)
        self.approvals = ApprovalService(self.repo, self.access, self.cases)
        self.control = OperatorControl(self.repo, self.access, self.cases, self.approvals, self.handoffs)
        self.knowledge = KnowledgeService(self.repo, self.access)
        self.gaps = GapService(self.repo, self.access, self.cases, self.knowledge)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.owner_a = self.access.tenant_context(self.op, self.a["id"])
        self.owner_b = self.access.tenant_context(self.op, self.b["id"])
        self.dep_a = self.team.create(self.owner_a, "department", name="Suporte", scope="x", status="ativo")
        self.agent_a = self.agents.create_agent(self.owner_a, department_id=self.dep_a["id"], name="Suporte", **PROFILE)

    def member(self, email, role, tenant=None, sub=None):
        """Register a person, sign them in and return their request-time context for the company."""
        tenant = tenant or self.a
        ctx = self.access.tenant_context(self.op, tenant["id"])
        self.identity.invite(ctx, tenant["id"], email, role)
        token, _ = self.identity.sign_in(sub or "sub-" + email, email, email.split("@")[0])
        return token, self.identity.context(self.identity.resolve(token), tenant["id"])

    def events(self):
        return [(e["kind"], e["detail"]) for e in reversed(self.identity.trail(self.op))]


class SignInTests(IdentityBase):
    def test_only_a_registered_person_can_sign_in_and_the_google_account_is_bound_at_the_first_time(self):
        with self.assertRaises(LoginDenied) as refused:
            self.identity.sign_in("sub-1", "ana@empresa.invalid")
        self.assertEqual(refused.exception.reason, "nao_cadastrado")
        self.identity.invite(self.owner_a, self.a["id"], " Ana@Empresa.INVALID ", "atendente", "Ana")
        token, session = self.identity.sign_in("sub-1", "ana@empresa.invalid", "Ana Maria")
        identity = self.identity.resolve(token)
        self.assertEqual((identity.email, identity.memberships, identity.session_id), ("ana@empresa.invalid", {self.a["id"]: "atendente"}, session))
        with self.assertRaises(LoginDenied) as other:  # same email, another Google account: not the same person
            self.identity.sign_in("sub-2", "ana@empresa.invalid")
        self.assertEqual(other.exception.reason, "conta_google_diferente")
        self.assertIsNotNone(self.identity.sign_in("sub-1", "mudou@empresa.invalid")[0])  # the bound account still works
        self.assertIn(("login_negado", "nao_cadastrado"), self.events())

    def test_a_person_without_an_active_company_or_deactivated_cannot_sign_in(self):
        self.identity.invite(self.owner_a, self.a["id"], "bia@empresa.invalid", "leitor")
        member = self.identity.members(self.owner_a, self.a["id"])[0]
        self.identity.set_member_status(self.owner_a, self.a["id"], member["user_id"], "inativo")
        with self.assertRaises(LoginDenied) as refused:
            self.identity.sign_in("sub-b", "bia@empresa.invalid")
        self.assertEqual(refused.exception.reason, "sem_empresa_ativa")
        self.identity.set_member_status(self.owner_a, self.a["id"], member["user_id"], "ativo")
        self.identity.sign_in("sub-b", "bia@empresa.invalid")
        self.identity.set_user_status(self.op, member["user_id"], "inativo")
        with self.assertRaises(LoginDenied) as off:
            self.identity.sign_in("sub-b", "bia@empresa.invalid")
        self.assertEqual(off.exception.reason, "usuario_inativo")
        with self.assertRaises(IdentityRejected):
            self.identity.invite(self.owner_a, self.a["id"], "bia@empresa.invalid", "leitor")

    def test_malformed_identities_are_refused(self):
        for sub, email in (("", "a@b.invalid"), (None, "a@b.invalid"), ("x" * 65, "a@b.invalid"), ("s", "sem-arroba"), ("s", "a b@c.invalid")):
            with self.assertRaises(LoginDenied):
                self.identity.sign_in(sub, email)
        for bad in ("", "x", "a@b", "a@b.c", "a,b@c.invalid", "<a>@c.invalid", "a@" + "b" * 300 + ".invalid"):
            with self.assertRaises(IdentityRejected):
                normalize_email(bad)


class SessionTests(IdentityBase):
    def test_only_the_hash_of_the_token_is_stored(self):
        token, session = self.member("ana@empresa.invalid", "admin")[0], None
        rows = self.rows("SELECT token_hash FROM auth_sessions")
        events = self.rows("SELECT detail FROM auth_events")
        self.assertEqual([r[0] for r in rows], [hash_token(token)])
        self.assertNotIn(token, str(rows) + str(events))

    def test_idle_and_absolute_expiry_and_sliding_idle_window(self):
        token, _ = self.member("ana@empresa.invalid", "admin")
        self.clock.advance(minutes=400)
        self.assertIsNotNone(self.identity.resolve(token))   # activity extends the idle window
        self.clock.advance(minutes=400)
        self.assertIsNotNone(self.identity.resolve(token))
        self.clock.advance(minutes=481)
        self.assertIsNone(self.identity.resolve(token))      # idle for more than 8 hours
        token2, _ = self.identity.sign_in("sub-ana@empresa.invalid", "ana@empresa.invalid")
        for _ in range(23):                                   # active every 7 hours for 161 hours: still valid
            self.clock.advance(hours=7)
            self.assertIsNotNone(self.identity.resolve(token2))
        self.clock.advance(hours=6)                           # 167 hours since sign-in
        self.assertIsNotNone(self.identity.resolve(token2))
        self.clock.advance(hours=2)                           # past the 7-day absolute limit, despite constant activity
        self.assertIsNone(self.identity.resolve(token2))

    def test_sign_out_and_revocation_take_effect_on_the_next_lookup(self):
        token, _ = self.member("ana@empresa.invalid", "admin")
        self.identity.sign_out(token)
        self.assertIsNone(self.identity.resolve(token))
        other, _ = self.identity.sign_in("sub-ana@empresa.invalid", "ana@empresa.invalid")
        user = self.identity.resolve(other).user_id
        self.assertEqual(self.identity.revoke_sessions(self.op, user), 1)
        self.assertIsNone(self.identity.resolve(other))
        self.assertEqual(len(self.identity.sessions(self.op, user)), 2)
        for junk in (None, "", "curto", "x" * 300, 5):
            self.assertIsNone(self.identity.resolve(junk))

    def test_deactivating_a_person_ends_every_session_at_once(self):
        token, ctx = self.member("ana@empresa.invalid", "admin")
        self.identity.set_user_status(self.op, ctx.principal_id, "inativo")
        self.assertIsNone(self.identity.resolve(token))

    def test_only_the_local_owner_revokes_sessions_or_disables_people(self):
        token, ctx = self.member("ana@empresa.invalid", "admin")
        for call in (lambda: self.identity.revoke_sessions(ctx, ctx.principal_id), lambda: self.identity.set_user_status(ctx, ctx.principal_id, "inativo"),
                     lambda: self.identity.trail(ctx), lambda: self.identity.sessions(ctx, ctx.principal_id)):
            with self.assertRaises(PermissionDenied):
                call()
        self.assertIsNotNone(self.identity.resolve(token))

    def test_the_trail_has_ids_and_reasons_only(self):
        token, ctx = self.member("ana@empresa.invalid", "admin")
        self.identity.sign_out(token)
        trail = self.identity.trail(self.op)
        self.assertEqual([e["kind"] for e in reversed(trail)], ["membro_convidado", "login", "logout"])
        self.assertNotIn("ana@empresa.invalid", str(trail))
        self.assertNotIn(token, str(trail))


class MemberTests(IdentityBase):
    def test_roles_are_per_company_and_the_context_comes_from_the_stored_role(self):
        token, ctx = self.member("ana@empresa.invalid", "atendente")
        identity = self.identity.resolve(token)
        self.assertEqual((ctx.role, ctx.member_role, ctx.principal_id, ctx.tenant_id), ("operator", "atendente", identity.user_id, self.a["id"]))
        with self.assertRaises(NotFound):
            self.identity.context(identity, self.b["id"])      # not a member there: the same answer as a missing company
        with self.assertRaises(NotFound):
            self.identity.context(identity, "ten_" + "0" * 32)
        self.identity.set_role(self.owner_a, self.a["id"], identity.user_id, "leitor")
        self.assertEqual(self.identity.context(identity, self.a["id"]).member_role, "leitor")  # the old identity object does not matter
        self.identity.set_member_status(self.owner_a, self.a["id"], identity.user_id, "inativo")
        with self.assertRaises(NotFound):
            self.identity.context(identity, self.a["id"])

    def test_a_context_cannot_be_forged(self):
        token, ctx = self.member("ana@empresa.invalid", "leitor")
        identity = self.identity.resolve(token)
        with self.assertRaises(PermissionDenied):
            Identity(identity.user_id, "x@y.invalid", None, identity.session_id, {self.a["id"]: "admin"}, object())
        with self.assertRaises(PermissionDenied):
            self.access.member_context(identity, self.a["id"], "admin")   # the identity says leitor
        with self.assertRaises(PermissionDenied):
            self.access.member_context(object(), self.a["id"], "leitor")
        self.assertFalse(self.access.is_operator(ctx, "atendente"))

    def test_the_roles_open_the_matching_services_and_nothing_more(self):
        _, reader = self.member("leitor@empresa.invalid", "leitor")
        _, clerk = self.member("atendente@empresa.invalid", "atendente")
        _, approver = self.member("aprovador@empresa.invalid", "aprovador")
        _, admin = self.member("admin@empresa.invalid", "admin")
        with self.assertRaises(PermissionDenied):
            self.cases.open_case(reader, department_id=self.dep_a["id"], objective="Nao deveria")
        self.assertEqual(self.cases.list_cases(reader), [])
        case = self.cases.open_case(clerk, department_id=self.dep_a["id"], objective="Site fora do ar")
        self.assertEqual(len(self.cases.list_cases(reader)), 1)
        case = self.cases.transition(clerk, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(clerk, case["id"], case["revision"], self.agent_a["id"])
        case = self.cases.transition(clerk, case["id"], case["revision"], "em_execucao")
        approval, _ = self.approvals.request(self.owner_a, case["id"], action="enviar_email", resource="con_x", recipients=["c@x.invalid"],
                                             params={"assunto": "Oi"})
        for who in (reader, clerk):
            with self.assertRaises(PermissionDenied):
                self.approvals.approve(who, approval["id"], approval["payload_hash"])
        self.approvals.deny(approver, approval["id"], "nao")                    # decided by a person: recorded under their own id
        self.assertEqual(self.approvals.get(approver, approval["id"])["decided_by"], approver.principal_id)
        with self.assertRaises(PermissionDenied):
            self.control.take_over(reader, case["id"], case["revision"], "x")
        self.control.pause(clerk, case["id"], self.cases.get_case(clerk, case["id"])["revision"], "revisando")
        gap = self.gaps.report(self.owner_a, case["id"], "Consultar dominio na Cloudflare")
        for who in (reader, clerk, approver):
            with self.assertRaises(PermissionDenied):
                self.gaps.decide(who, gap["id"], gap["revision"], "criar_skill", "ok")
        self.assertEqual(self.gaps.decide(admin, gap["id"], gap["revision"], "criar_skill", "ok")["decided_by"], admin.principal_id)

    def test_nothing_that_is_global_or_local_only_is_reachable_with_a_member_context(self):
        _, admin = self.member("admin@empresa.invalid", "admin")
        with self.assertRaises(PermissionDenied):
            self.team.create_company(admin, "Nova empresa")                  # these take the local owner, not a context
        for owner_only in (lambda: self.identity.set_user_status(admin, admin.principal_id, "inativo"),
                           lambda: self.access.tenant_context(admin, self.b["id"])):
            with self.assertRaises(PermissionDenied):
                owner_only()


class ManagementTests(IdentityBase):
    def test_an_admin_manages_only_their_own_company_and_the_owner_any(self):
        _, admin_a = self.member("admin-a@empresa.invalid", "admin")
        _, admin_b = self.member("admin-b@empresa.invalid", "admin", tenant=self.b)
        self.identity.invite(admin_a, self.a["id"], "novo@empresa.invalid", "leitor")
        self.assertEqual({m["email"] for m in self.identity.members(admin_a, self.a["id"])}, {"admin-a@empresa.invalid", "novo@empresa.invalid"})
        for call in (lambda: self.identity.members(admin_a, self.b["id"]), lambda: self.identity.invite(admin_a, self.b["id"], "x@y.invalid", "leitor"),
                     lambda: self.identity.members(admin_b, self.a["id"])):
            with self.assertRaises(PermissionDenied):
                call()
        self.assertEqual({m["email"] for m in self.identity.members(admin_b, self.b["id"])}, {"admin-b@empresa.invalid"})
        self.assertEqual(len(self.identity.members(self.owner_b, self.b["id"])), 1)

    def test_lower_roles_cannot_manage_members(self):
        for role in ("leitor", "atendente", "aprovador"):
            _, ctx = self.member(f"{role}@empresa.invalid", role)
            with self.assertRaises(PermissionDenied):
                self.identity.invite(ctx, self.a["id"], "x@y.invalid", "leitor")
            with self.assertRaises(PermissionDenied):
                self.identity.members(ctx, self.a["id"])

    def test_nobody_locks_a_company_out_of_its_administration(self):
        _, first = self.member("um@empresa.invalid", "admin")
        _, second = self.member("dois@empresa.invalid", "admin")
        with self.assertRaises(IdentityRejected):
            self.identity.set_role(first, self.a["id"], first.principal_id, "leitor")             # not by yourself
        with self.assertRaises(IdentityRejected):
            self.identity.set_member_status(first, self.a["id"], first.principal_id, "inativo")
        self.identity.set_role(first, self.a["id"], second.principal_id, "atendente")              # another admin may
        with self.assertRaises(IdentityRejected):
            self.identity.set_member_status(self.owner_a, self.a["id"], first.principal_id, "inativo")  # never the last one
        with self.assertRaises(IdentityRejected):
            self.identity.set_role(self.owner_a, self.a["id"], first.principal_id, "leitor")
        self.identity.set_role(self.owner_a, self.a["id"], second.principal_id, "admin")
        self.identity.set_member_status(self.owner_a, self.a["id"], first.principal_id, "inativo")
        with self.assertRaises(NotFound):
            self.identity.set_role(self.owner_a, self.a["id"], "usr_" + "0" * 32, "leitor")
        with self.assertRaises(IdentityRejected):
            self.identity.set_role(self.owner_a, self.a["id"], second.principal_id, "dono")

    def test_a_person_in_two_companies_has_a_role_in_each(self):
        self.identity.invite(self.owner_a, self.a["id"], "ana@empresa.invalid", "admin")
        self.identity.invite(self.owner_b, self.b["id"], "ana@empresa.invalid", "leitor")
        token, _ = self.identity.sign_in("sub-ana", "ana@empresa.invalid")
        identity = self.identity.resolve(token)
        self.assertEqual(identity.memberships, {self.a["id"]: "admin", self.b["id"]: "leitor"})
        self.assertEqual(self.identity.context(identity, self.b["id"]).member_role, "leitor")
        self.identity.set_member_status(self.owner_b, self.b["id"], identity.user_id, "inativo")
        self.assertEqual(self.identity.resolve(token).memberships, {self.a["id"]: "admin"})


if __name__ == "__main__":
    unittest.main()
