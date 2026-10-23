import copy
import tempfile
import unittest
from pathlib import Path

from team import persistence
from team.access import AccessContext, AccessService, NotFound, OperatorContext, PermissionDenied
from team.ids import new_id
from team.repository import RevisionConflict, SqliteRepository
from team.samples import IDS, bundle
from team.service import Conflict, TeamService


def one(kind):
    return copy.deepcopy(next(d for d in bundle() if d["kind"] == kind))


class AccessCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SqliteRepository(Path(self.temp.name) / "team.sqlite3")
        self.repo.migrate()
        self.access = AccessService(self.repo, new_id("user"))
        self.service = TeamService(self.repo, self.access)
        self.op = self.access.local_operator()
        # Two companies with the same kinds of resources; each id is only valid inside its owner.
        self.a = self.service.create_company(self.op, "Empresa A")
        self.b = self.service.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep_a = self.service.create(self.ca, "department", name="Financeiro A", scope="Cobrancas", status="ativo")
        self.dep_b = self.service.create(self.cb, "department", name="Financeiro B", scope="Cobrancas", status="ativo")
        self.contact_a = self.service.create(self.ca, "contact", display_name="Cliente A", status="ativo")
        self.contact_b = self.service.create(self.cb, "contact", display_name="Cliente B", status="ativo")
        self.address_b = self.service.create(
            self.cb, "contact_address", contact_id=self.contact_b["id"], channel="email",
            address="b@exemplo.invalid", link_state="vinculado")

    def same_failure(self, action, foreign, missing):
        """Foreign ids must fail exactly like ids that do not exist (type and message)."""
        errors = []
        for value in (foreign, missing):
            with self.assertRaises(NotFound) as caught:
                action(value)
            errors.append((type(caught.exception), str(caught.exception)))
        self.assertEqual(errors[0], errors[1])


class ContextTests(AccessCase):
    def test_contexts_cannot_be_forged(self):
        with self.assertRaises(PermissionDenied):
            AccessContext(self.a["id"], new_id("user"), "operator", None)
        with self.assertRaises(PermissionDenied):
            AccessContext(self.a["id"], new_id("user"), "operator", object())
        with self.assertRaises(PermissionDenied):
            OperatorContext(new_id("user"), "token")
        with self.assertRaises(PermissionDenied):  # a plain dict proposed by an LLM is not a context
            self.service.list({"tenant_id": self.a["id"], "role": "operator"}, "department")
        other = AccessService(self.repo, new_id("user"))
        with self.assertRaises(PermissionDenied):
            self.access.tenant_context(other.local_operator(), self.a["id"])
        with self.assertRaises(NotFound):
            self.access.tenant_context(self.op, new_id("tenant"))

    def test_tenant_and_role_fields_are_not_accepted_as_input(self):
        for field in ("tenant_id", "id", "revision", "kind"):
            with self.assertRaisesRegex(ValueError, "reservados"):
                self.service.create(self.ca, "contact", display_name="X", status="ativo", **{field: self.b["id"]})
        created = self.service.create(self.ca, "contact", display_name="Y", status="ativo")
        self.assertEqual(created["tenant_id"], self.a["id"])


class CrudTests(AccessCase):
    def test_company_and_department_crud(self):
        self.assertEqual({c["name"] for c in self.service.list_companies(self.op)}, {"Empresa A", "Empresa B"})
        renamed = self.service.update_company(self.op, self.a["id"], 1, name="Empresa A Ltda")
        self.assertEqual(renamed["revision"], 2)
        with self.assertRaises(RevisionConflict):
            self.service.update_company(self.op, self.a["id"], 1, name="Velho")
        with self.assertRaises(ValueError):
            self.service.update_company(self.op, self.a["id"], 2, id=self.b["id"])
        updated = self.service.update(self.ca, "department", self.dep_a["id"], 1, scope="Cobrancas e recebimentos")
        self.assertEqual(self.service.get(self.ca, "department", self.dep_a["id"])["scope"], updated["scope"])
        with self.assertRaises(RevisionConflict):
            self.service.update(self.ca, "department", self.dep_a["id"], 1, name="Velho")
        with self.assertRaises(ValueError):
            self.service.update(self.ca, "department", self.dep_a["id"], 2, tenant_id=self.b["id"])
        self.assertEqual(self.service.deactivate(self.ca, "department", self.dep_a["id"], 2)["status"], "inativo")
        with self.assertRaises(ValueError):
            self.service.delete(self.ca, "department", self.dep_a["id"])  # entities are deactivated, not deleted

    def test_contact_identity_is_separate_from_sender_address(self):
        observed = self.service.observe_address(self.ca, "email", "Cliente@Exemplo.invalid")
        self.assertEqual(observed["link_state"], "observado")
        self.assertIsNone(observed["contact_id"])
        self.assertEqual(self.service.observe_address(self.ca, "email", "cliente@exemplo.invalid")["id"],
                         observed["id"])  # same address, case-insensitive: one record
        self.assertIsNone(self.service.find_contact_by_address(self.ca, "email", "cliente@exemplo.invalid"))
        linked = self.service.link_address(self.ca, observed["id"], self.contact_a["id"], 1)
        self.assertEqual(linked["link_state"], "vinculado")
        found = self.service.find_contact_by_address(self.ca, "email", "CLIENTE@exemplo.invalid")
        self.assertEqual(found["id"], self.contact_a["id"])
        self.service.unlink_address(self.ca, observed["id"], 2)
        self.assertIsNone(self.service.find_contact_by_address(self.ca, "email", "cliente@exemplo.invalid"))

    def test_links_and_duplicates(self):
        link = self.service.create(self.ca, "contact_link", contact_id=self.contact_a["id"],
                                   department_id=self.dep_a["id"], relation="cliente")
        self.assertEqual(self.service.update(self.ca, "contact_link", link["id"], 1, relation="fornecedor")["relation"],
                         "fornecedor")
        with self.assertRaises(Conflict):
            self.service.create(self.ca, "contact_link", contact_id=self.contact_a["id"],
                                department_id=self.dep_a["id"], relation="interno")
        self.service.delete(self.ca, "contact_link", link["id"])
        self.assertEqual(self.service.list(self.ca, "contact_link"), [])
        self.service.create(self.ca, "contact_address", contact_id=None, channel="email",
                            address="x@exemplo.invalid", link_state="observado")
        with self.assertRaises(Conflict):
            self.service.create(self.ca, "contact_address", contact_id=None, channel="email",
                                address="X@exemplo.invalid", link_state="observado")

    def test_invalid_documents_are_rejected_by_the_contract(self):
        from team.schema import ContractError
        with self.assertRaises(ContractError):  # linked address without a contact
            self.service.create(self.ca, "contact_address", contact_id=None, channel="email",
                                address="a@exemplo.invalid", link_state="vinculado")
        with self.assertRaises(ContractError):
            self.service.create(self.ca, "contact_address", contact_id=None, channel="email",
                                address="sem-arroba", link_state="observado")


class IsolationTests(AccessCase):
    def test_foreign_ids_fail_like_missing_ids_for_every_operation(self):
        missing = new_id("department")
        self.same_failure(lambda i: self.service.get(self.ca, "department", i), self.dep_b["id"], missing)
        self.same_failure(lambda i: self.service.update(self.ca, "department", i, 1, name="Invadido"),
                          self.dep_b["id"], missing)
        self.same_failure(lambda i: self.service.deactivate(self.ca, "department", i, 1), self.dep_b["id"], missing)
        self.same_failure(lambda i: self.service.delete(self.ca, "contact_address", i),
                          self.address_b["id"], new_id("contact_address"))
        self.same_failure(lambda i: self.service.create(
            self.ca, "contact_link", contact_id=self.contact_a["id"], department_id=i, relation="cliente"),
            self.dep_b["id"], missing)
        self.same_failure(lambda i: self.service.create(
            self.ca, "contact_address", contact_id=i, channel="email", address="n@exemplo.invalid",
            link_state="vinculado"), self.contact_b["id"], new_id("contact"))
        mine = self.service.observe_address(self.ca, "email", "mine@exemplo.invalid")
        self.same_failure(lambda i: self.service.link_address(self.ca, mine["id"], i, 1),
                          self.contact_b["id"], new_id("contact"))
        self.same_failure(lambda i: self.service.link_address(self.ca, i, self.contact_a["id"], 1),
                          self.address_b["id"], new_id("contact_address"))
        self.assertEqual(self.service.get(self.cb, "department", self.dep_b["id"])["name"], "Financeiro B")  # untouched

    def test_list_search_and_export_only_show_own_company(self):
        self.assertEqual([d["id"] for d in self.service.list(self.ca, "department")], [self.dep_a["id"]])
        self.assertEqual(self.service.list(self.ca, "contact_address"), [])
        self.assertIsNone(self.service.find_contact_by_address(self.ca, "email", "b@exemplo.invalid"))
        self.assertEqual(self.service.find_contact_by_address(self.cb, "email", "b@exemplo.invalid")["id"],
                         self.contact_b["id"])
        exported = self.service.export(self.ca)
        ids = {d["id"] for d in exported["documents"]}
        self.assertIn(self.a["id"], ids)
        self.assertTrue({self.b["id"], self.dep_b["id"], self.contact_b["id"], self.address_b["id"]}.isdisjoint(ids))
        self.assertTrue(all(d.get("tenant_id", d["id"]) == self.a["id"] for d in exported["documents"]))

    def test_events_follow_case_visibility(self):
        case_b = self.seed_case(self.b["id"], self.dep_b["id"])
        missing = new_id("case")
        self.same_failure(lambda i: self.service.events(self.ca, i), case_b, missing)
        own = self.seed_case(self.a["id"], self.dep_a["id"])
        self.assertEqual(len(self.service.events(self.ca, own)), 1)
        self.assertEqual([e["case_id"] for _, e in self.service.events(self.ca)], [own])
        self.assertEqual([e["case_id"] for _, e in self.service.events(self.cb)], [case_b])

    def seed_case(self, tenant_id, department_id):
        case = dict(one("case"), id=new_id("case"), tenant_id=tenant_id, department_id=department_id,
                    owner_agent_id=None)
        event = dict(one("event"), id=new_id("event"), tenant_id=tenant_id, case_id=case["id"], revision=1,
                     causation_id=None)
        with self.repo.transaction(tenant_id) as session:
            session.save(case, event=event)
        return case["id"]


class AgentPolicyTests(AccessCase):
    def agent_ctx(self, state="em_execucao"):
        agent = dict(one("agent_definition"), id=new_id("agent_definition"), tenant_id=self.a["id"],
                     department_id=self.dep_a["id"], skill_grant_ids=[], handoff_target_ids=[])
        run = dict(one("agent_run"), id=new_id("agent_run"), tenant_id=self.a["id"], agent_id=agent["id"],
                   department_id=self.dep_a["id"], case_id=None, task_id=None, state=state)
        with self.repo.transaction(self.a["id"]) as session:
            session.save(agent)
            session.save(run)
        return self.access.agent_context(self.op, self.a["id"], run["id"]), agent, run

    def grant(self, agent, kind, actions, resource_id=None, **extra):
        return self.service.create(self.ca, "access_grant", principal_id=agent["id"], resource_kind=kind,
                                   resource_id=resource_id, actions=actions, granted_by=self.op.principal_id,
                                   expires_at=extra.get("expires_at"))

    def test_default_deny_until_explicit_grant(self):
        ctx, agent, _ = self.agent_ctx()
        self.assertEqual(self.service.list(ctx, "department"), [])
        self.same_failure(lambda i: self.service.get(ctx, "department", i), self.dep_a["id"], new_id("department"))
        with self.assertRaises(PermissionDenied):
            self.service.create(ctx, "contact", display_name="X", status="ativo")
        with self.assertRaises(PermissionDenied):
            self.service.events(ctx)
        self.assertEqual(self.service.export(ctx)["documents"], [])
        self.grant(agent, "department", ["read"], self.dep_a["id"])
        self.assertEqual([d["id"] for d in self.service.list(ctx, "department")], [self.dep_a["id"]])
        with self.assertRaises(NotFound):  # read is not write
            self.service.update(ctx, "department", self.dep_a["id"], 1, name="Z")
        self.assertEqual(self.service.export(ctx)["documents"], [])  # read is not export

    def test_grants_are_scoped_by_kind_action_resource_and_expiry(self):
        ctx, agent, _ = self.agent_ctx()
        other_dep = self.service.create(self.ca, "department", name="Suporte A", scope="Tecnico", status="ativo")
        self.grant(agent, "department", ["read", "export"], self.dep_a["id"])
        self.assertEqual(len(self.service.list(ctx, "department")), 1)
        self.assertEqual([d["id"] for d in self.service.export(ctx)["documents"]], [self.dep_a["id"]])
        self.grant(agent, "contact", ["read"], expires_at="2020-01-01T00:00:00Z")
        self.assertEqual(self.service.list(ctx, "contact"), [])  # expired
        self.grant(agent, "contact", ["read", "write", "relate"])
        self.assertEqual(len(self.service.list(ctx, "contact")), 1)
        created = self.service.create(ctx, "contact", display_name="Criado pelo agente", status="ativo")
        self.assertEqual(created["tenant_id"], self.a["id"])
        self.grant(agent, "contact_link", ["write"])
        with self.assertRaises(NotFound):  # relate on a department the agent has no relate grant for
            self.service.create(ctx, "contact_link", contact_id=created["id"], department_id=other_dep["id"],
                                relation="cliente")

    def test_agents_cannot_manage_grants_or_cross_companies(self):
        ctx, agent, _ = self.agent_ctx()
        with self.assertRaises(PermissionDenied):
            self.service.create(ctx, "access_grant", principal_id=agent["id"], resource_kind="department",
                                resource_id=None, actions=["read"], granted_by=agent["id"], expires_at=None)
        self.grant(agent, "department", ["read"])
        self.assertEqual(self.service.list(ctx, "access_grant"), [])
        self.assertEqual(self.service.list(ctx, "department")[0]["tenant_id"], self.a["id"])
        self.same_failure(lambda i: self.service.get(ctx, "department", i), self.dep_b["id"], new_id("department"))

    def test_finished_or_foreign_runs_do_not_yield_contexts(self):
        _, _, run = self.agent_ctx()
        with self.assertRaises(NotFound):
            self.access.agent_context(self.op, self.b["id"], run["id"])  # the run belongs to company A
        with self.assertRaises(PermissionDenied):
            self.agent_ctx("concluida")


class LocalPersistenceTests(unittest.TestCase):
    def test_team_service_uses_one_stable_operator(self):
        with tempfile.TemporaryDirectory() as root:
            persistence.enable(root)
            first, second = persistence.operator_id(root), persistence.operator_id(root)
            self.assertEqual(first, second)
            service = persistence.team_service(root)
            op = service.access.local_operator()
            company = service.create_company(op, "Local")
            self.assertEqual(persistence.team_service(root).list_companies(
                persistence.team_service(root).access.local_operator())[0]["id"], company["id"])


if __name__ == "__main__":
    unittest.main()
