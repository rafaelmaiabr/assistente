from tests.test_team_remote import PROFILE, Browser, RemoteCase


class ScopeOverHttpTests(RemoteCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        s, owner = self.s, self.owner_a
        self.fin = s.team.create(owner, "department", name="Financeiro", scope="x", status="ativo")
        self.agent_fin = s.agents.create_agent(owner, department_id=self.fin["id"], name="Financeiro A", **PROFILE)
        self.case_sup = self.working_case(owner, self.dep_a, self.agent_a, "Site fora do ar")
        self.case_fin = self.working_case(owner, self.fin, self.agent_fin, "Boleto vencido")
        self.approval_sup, _ = s.approvals.request(owner, self.case_sup["id"], action="enviar_email", resource="con_x",
                                                   recipients=["c@x.invalid"], params={"assunto": "Site"})
        self.approval_fin, _ = s.approvals.request(owner, self.case_fin["id"], action="enviar_email", resource="con_x",
                                                   recipients=["c@x.invalid"], params={"assunto": "Boleto"})

    async def scoped(self, email, role, departments):
        ctx = self.s.access.tenant_context(self.s.operator, self.a["id"])
        self.s.identity.invite(ctx, self.a["id"], email, role, departments=departments)
        browser = Browser(self)
        self.browsers.append(browser)
        await browser.sign_in(email)
        return browser

    async def test_a_limited_member_reaches_only_their_departments_through_every_route(self):
        support = await self.scoped("sup@empresa.invalid", "atendente", [self.dep_a["id"]])
        status, data = await support.call("GET", self.t(self.a, "/cases"))
        self.assertEqual([c["id"] for c in data["cases"]], [self.case_sup["id"]])
        self.assertEqual((await support.call("GET", self.t(self.a, f"/cases/{self.case_sup['id']}")))[0], 200)
        status, data = await support.call("GET", self.t(self.a, f"/cases/{self.case_fin['id']}"))
        self.assertEqual((status, data), (404, {"error": "Recurso nao encontrado."}))
        status, data = await support.call("GET", self.t(self.a, "/departments"))
        self.assertEqual([d["name"] for d in data], ["Suporte"])
        status, data = await support.call("GET", self.t(self.a, "/agents"))
        self.assertEqual([a["name"] for a in data], ["Suporte A"])
        status, feed = await support.call("GET", self.t(self.a, "/events?after=0"))
        self.assertEqual({e["case_id"] for e in feed["events"]}, {self.case_sup["id"]})
        self.assertEqual((await support.call("GET", self.t(self.a, f"/events?after=0&case_id={self.case_fin['id']}")))[0], 404)
        status, inbox = await support.call("GET", self.t(self.a, "/attention"))
        self.assertEqual([a["case_id"] for a in inbox["pending_approvals"]], [self.case_sup["id"]])
        self.assertNotIn(self.approval_fin["id"], str(inbox))

    async def test_actions_on_other_departments_are_refused_as_not_found(self):
        support = await self.scoped("sup@empresa.invalid", "atendente", [self.dep_a["id"]])
        fresh = self.s.cases.open_case(self.owner_a, department_id=self.dep_a["id"], objective="Sem dono")
        checks = [("POST", f"/cases/{self.case_fin['id']}/take-over", {"expected_revision": self.case_fin["revision"], "reason": "x"}),
                  ("POST", f"/cases/{self.case_fin['id']}/pause", {"expected_revision": self.case_fin["revision"], "reason": "x"}),
                  ("POST", "/cases", {"department_id": self.fin["id"], "objective": "Cruzado"}),
                  ("POST", f"/cases/{fresh['id']}/assign", {"expected_revision": fresh["revision"], "agent_id": self.agent_fin["id"]}),
                  ("POST", "/gaps", {"case_id": self.case_fin["id"], "requested": "Consultar dominio na Cloudflare"})]
        for method, tail, body in checks:
            status, data = await support.call(method, self.t(self.a, tail), body)
            self.assertEqual(status, 404, tail)
        self.assertFalse(self.s.cases.get_case(self.owner_a, self.case_fin["id"]).get("operator_control"))
        self.assertEqual(self.s.gaps.list_gaps(self.owner_a), [])
        status, taken = await support.call("POST", self.t(self.a, f"/cases/{self.case_sup['id']}/take-over"),
                                           {"expected_revision": self.case_sup["revision"], "reason": "meu departamento"})
        self.assertIn(status, (200, 409))                                                       # its own department works as usual

    async def test_an_attendant_registers_a_gap_on_their_own_cases_but_does_not_list_or_decide(self):
        support = await self.scoped("sup@empresa.invalid", "atendente", [self.dep_a["id"]])
        status, gap = await support.call("POST", self.t(self.a, "/gaps"), {"case_id": self.case_sup["id"], "requested": "Consultar dominio na Cloudflare"})
        self.assertEqual((status, gap["state"]), (201, "aberta"))
        self.assertEqual((await support.call("GET", self.t(self.a, "/gaps")))[0], 403)
        self.assertEqual((await support.call("POST", self.t(self.a, f"/gaps/{gap['id']}/decide"),
                                             {"expected_revision": gap["revision"], "decision": "criar_skill", "note": "x"}))[0], 403)

    async def test_an_approver_decides_only_the_approvals_of_their_departments(self):
        finance = await self.scoped("fin@empresa.invalid", "aprovador", [self.fin["id"]])
        status, _ = await finance.call("POST", self.t(self.a, f"/approvals/{self.approval_sup['id']}/deny"), {"reason": "nao"})
        self.assertEqual(status, 404)
        self.assertEqual(self.s.approvals.get(self.owner_a, self.approval_sup["id"])["state"], "pendente")
        status, _ = await finance.call("POST", self.t(self.a, f"/approvals/{self.approval_fin['id']}/deny"), {"reason": "nao"})
        self.assertEqual(status, 200)
        self.assertEqual(self.s.approvals.get(self.owner_a, self.approval_fin["id"])["state"], "negada")

    async def test_an_administrator_manages_the_limits_and_they_apply_on_the_next_request(self):
        admin = await self.person("admin@empresa.invalid", "admin")
        reader = await self.person("leitor@empresa.invalid", "leitor")
        status, data = await reader.call("GET", self.t(self.a, "/cases"))
        self.assertEqual(len(data["cases"]), 2)                                                  # unlimited until told otherwise
        user = next(m["user_id"] for m in self.s.identity.members(self.owner_a, self.a["id"]) if m["email"] == "leitor@empresa.invalid")
        status, member = await admin.call("PATCH", self.t(self.a, f"/members/{user}"), {"departments": [self.fin["id"]]})
        self.assertEqual((status, member["departments"]), (200, [self.fin["id"]]))
        status, data = await reader.call("GET", self.t(self.a, "/cases"))
        self.assertEqual([c["id"] for c in data["cases"]], [self.case_fin["id"]])
        status, member = await admin.call("PATCH", self.t(self.a, f"/members/{user}"), {"departments": []})
        self.assertEqual(member["departments"], [])
        self.assertEqual(len((await reader.call("GET", self.t(self.a, "/cases")))[1]["cases"]), 2)
        status, data = await admin.call("PATCH", self.t(self.a, f"/members/{user}"), {"departments": ["dep_" + "0" * 32]})
        self.assertEqual(status, 400)
        status, data = await admin.call("POST", self.t(self.a, "/members"), {"email": "novo@empresa.invalid", "role": "admin", "departments": [self.fin["id"]]})
        self.assertEqual(status, 400)
        status, data = await admin.call("POST", self.t(self.a, "/members"), {"email": "novo@empresa.invalid", "role": "atendente", "departments": [self.fin["id"]]})
        self.assertEqual((status, data["departments"]), (201, [self.fin["id"]]))

    async def test_a_limited_member_of_one_company_is_unlimited_nowhere_else(self):
        other = self.s.team.create(self.owner_b, "department", name="Outro", scope="x", status="ativo")
        self.s.identity.invite(self.owner_b, self.b["id"], "sup@empresa.invalid", "leitor", departments=[other["id"]])
        support = await self.scoped("sup@empresa.invalid", "atendente", [self.dep_a["id"]])
        status, data = await support.call("GET", self.t(self.b, "/cases"))
        self.assertEqual((status, data["cases"]), (200, []))
        self.assertEqual((await support.call("GET", self.t(self.b, "/departments")))[1][0]["id"], other["id"])


if __name__ == "__main__":
    import unittest
    unittest.main()
