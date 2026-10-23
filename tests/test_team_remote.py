import socket
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from aiohttp import ClientSession, CookieJar, web

from team import persistence
from team.app import TeamApp
from team.login import SECRET_SLOT, ConfigError, GoogleLogin, RateLimiter, RemoteConfig
from team.remote import POLICY, build_app
from team.vault import MemoryVault
from tests.google_login_fake import FakeGoogleLogin

CLIENT_ID = "cliente-login.apps.googleusercontent.com"
CONFIG = {"model": "qwen2.5:7b", "ollama_url": "http://127.0.0.1:11435/v1", "max_concurrent_generations": 1}
PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Browser:
    """One person's browser: its own cookie jar, and the CSRF token the interface would read from /auth/session."""

    def __init__(self, test):
        self.test, self.csrf = test, None
        self.client = ClientSession(cookie_jar=CookieJar(unsafe=True))

    async def close(self):
        await self.client.close()

    async def raw(self, method, path, **kwargs):
        async with self.client.request(method, self.test.base + path, allow_redirects=False, **kwargs) as response:
            text = await response.text()
            return response.status, dict(response.headers), text

    async def sign_in(self, email, sub=None, **overrides):
        """The whole Google round trip. Returns the final redirect target."""
        status, headers, _ = await self.raw("GET", "/auth/google/start")
        assert status == 302, (status, headers)
        query = self.test.google.authorize(headers["Location"], sub=sub or "sub-" + email, email=email, **overrides)
        status, headers, _ = await self.raw("GET", "/auth/google/callback?" + urllib.parse.urlencode(query))
        assert status == 303, status
        await self.refresh()
        return headers["Location"]

    async def refresh(self):
        status, _, text = await self.raw("GET", "/auth/session")
        import json
        data = json.loads(text)
        self.csrf = data.get("csrf")
        return status, data

    async def call(self, method, path, body=None, *, csrf=True, origin=True, headers=None):
        import json
        send = {"Origin": self.test.origin} if origin else {}
        if csrf and self.csrf and method not in ("GET", "HEAD"):
            send["X-CSRF-Token"] = self.csrf
        send.update(headers or {})
        kwargs = {"headers": send}
        if body is not None:
            kwargs["json"] = body
        status, _, text = await self.raw(method, path, **kwargs)
        return status, (json.loads(text) if text.startswith(("{", "[")) else text)


class RemoteCase(unittest.IsolatedAsyncioTestCase):
    limiters = {}
    allowed_domains = []

    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        persistence.enable(root)
        self.vault = MemoryVault()
        self.vault.put(*SECRET_SLOT, "segredo-do-login-123")
        self.team_app = TeamApp(root, CONFIG, vault=self.vault, legacy_elevenlabs=None, voice_profiles=lambda: [])
        self.s = self.team_app.services()
        self.port = free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.base = self.origin
        self.config = RemoteConfig.from_config({"remote": {"enabled": True, "port": self.port, "public_origin": self.origin,
                                                           "google_client_id": CLIENT_ID, "allowed_domains": self.allowed_domains}})
        self.google = FakeGoogleLogin(CLIENT_ID, redirect_uri=self.config.redirect_uri)
        self.login = GoogleLogin(self.config, self.vault, http=self.google)
        self.app = build_app(self.config, self.team_app, self.login, **self.limiters)
        self.extend(self.app)
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        self.browsers = []
        op = self.s.operator
        self.a, self.b = self.s.team.create_company(op, "Empresa A"), self.s.team.create_company(op, "Empresa B")
        self.owner_a = self.s.access.tenant_context(op, self.a["id"])
        self.owner_b = self.s.access.tenant_context(op, self.b["id"])
        self.dep_a = self.s.team.create(self.owner_a, "department", name="Suporte", scope="x", status="ativo")
        self.dep_b = self.s.team.create(self.owner_b, "department", name="Suporte", scope="x", status="ativo")
        self.agent_a = self.s.agents.create_agent(self.owner_a, department_id=self.dep_a["id"], name="Suporte A", **PROFILE)
        self.agent_b = self.s.agents.create_agent(self.owner_b, department_id=self.dep_b["id"], name="Suporte B", **PROFILE)

    def extend(self, app):
        """Hook for a test to register routes before the router freezes."""

    async def asyncTearDown(self):
        for browser in self.browsers:
            await browser.close()
        await self.runner.cleanup()

    def t(self, company, tail=""):
        return f"/api/team/companies/{company['id']}{tail}"

    async def person(self, email, role, company=None, signed_in=True):
        company = company or self.a
        ctx = self.s.access.tenant_context(self.s.operator, company["id"])
        self.s.identity.invite(ctx, company["id"], email, role)
        browser = Browser(self)
        self.browsers.append(browser)
        if signed_in:
            await browser.sign_in(email)
        return browser

    def working_case(self, owner_ctx, department, agent, objective="Site fora do ar"):
        case = self.s.cases.open_case(owner_ctx, department_id=department["id"], objective=objective, customer_ref="c1")
        case = self.s.cases.transition(owner_ctx, case["id"], case["revision"], "triagem")
        case = self.s.cases.assign_owner(owner_ctx, case["id"], case["revision"], agent["id"])
        return self.s.cases.transition(owner_ctx, case["id"], case["revision"], "em_execucao")


class SignInFlowTests(RemoteCase):
    async def test_a_registered_person_signs_in_and_sees_only_their_companies(self):
        ana = await self.person("ana@empresa.invalid", "admin", signed_in=False)
        status, data = await ana.refresh()
        self.assertEqual((status, data["authenticated"], data["login_url"]), (401, False, "/auth/google/start"))
        self.assertEqual(await ana.sign_in("ana@empresa.invalid"), "/")
        status, data = await ana.refresh()
        self.assertEqual((status, data["user"]["email"], data["companies"]), (200, "ana@empresa.invalid", [
            {"id": self.a["id"], "name": "Empresa A", "role": "admin"}]))
        status, state = await ana.call("GET", "/api/team/state")
        self.assertEqual((status, state["mode"], [c["id"] for c in state["companies"]], state["voices"]), (200, "remote", [self.a["id"]], []))
        cookie = next(iter(ana.client.cookie_jar))
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie.key, "assistente_session")  # the __Host- prefix is used when the public origin is https

    async def test_the_secret_stays_in_the_token_request_and_the_url_carries_none(self):
        ana = await self.person("ana@empresa.invalid", "admin")
        url, fields = self.google.calls[-1]
        self.assertEqual(fields["client_secret"], "segredo-do-login-123")
        self.assertEqual(fields["grant_type"], "authorization_code")
        status, headers, text = await ana.raw("GET", "/auth/google/start")
        self.assertNotIn("segredo", headers["Location"] + text)

    async def test_someone_who_was_not_registered_is_refused_without_a_session(self):
        stranger = Browser(self)
        self.browsers.append(stranger)
        self.assertEqual(await stranger.sign_in("intruso@fora.invalid"), "/?login_error=conta_nao_autorizada")
        self.assertEqual((await stranger.refresh())[0], 401)
        self.assertEqual((await stranger.call("GET", "/api/team/state"))[0], 401)
        self.assertIn(("login_negado", "nao_cadastrado"), [(e["kind"], e["detail"]) for e in self.s.identity.trail(self.s.operator)])

    async def test_the_callback_needs_the_same_browser_and_a_fresh_one_time_state(self):
        victim = await self.person("ana@empresa.invalid", "admin", signed_in=False)
        attacker = Browser(self)
        self.browsers.append(attacker)
        _, headers, _ = await attacker.raw("GET", "/auth/google/start")
        query = self.google.authorize(headers["Location"], sub="sub-ana@empresa.invalid", email="ana@empresa.invalid")
        link = "/auth/google/callback?" + urllib.parse.urlencode(query)
        status, headers, _ = await victim.raw("GET", link)           # a crafted link opened in another browser (login CSRF)
        self.assertEqual((status, headers["Location"]), (303, "/?login_error=estado_invalido"))
        self.assertEqual((await victim.refresh())[0], 401)
        status, headers, _ = await attacker.raw("GET", link)         # the attempt burned the state: it fails closed, for everybody
        self.assertEqual((status, headers["Location"]), (303, "/?login_error=estado_invalido"))
        self.assertEqual(await attacker.sign_in("ana@empresa.invalid"), "/")   # a fresh sign-in in the same browser works

    async def test_a_state_that_is_unknown_expired_or_a_cancelled_login_is_refused(self):
        ana = await self.person("ana@empresa.invalid", "admin", signed_in=False)
        status, headers, _ = await ana.raw("GET", "/auth/google/callback?code=abc&state=inventado")
        self.assertEqual(headers["Location"], "/?login_error=estado_invalido")
        _, headers, _ = await ana.raw("GET", "/auth/google/start")
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)["state"][0]
        status, headers, _ = await ana.raw("GET", "/auth/google/callback?" + urllib.parse.urlencode({"error": "access_denied", "state": state}))
        self.assertEqual(headers["Location"], "/?login_error=cancelado")
        _, headers, _ = await ana.raw("GET", "/auth/google/start")
        query = self.google.authorize(headers["Location"], sub="s", email="ana@empresa.invalid")
        for flow in self.login.pending.values():
            flow["expires"] = 0
        status, headers, _ = await ana.raw("GET", "/auth/google/callback?" + urllib.parse.urlencode(query))
        self.assertEqual(headers["Location"], "/?login_error=estado_invalido")

    async def test_the_identity_token_is_checked(self):
        await self.person("ana@empresa.invalid", "admin", signed_in=False)
        cases = {"identidade_invalida": [{"aud": "outro-cliente"}, {"iss": "https://evil.invalid"}, {"nonce": "outro"}, {"exp": 1}, {"aud": ["outro", "mais-um"]}],
                 "email_nao_verificado": [{"email_verified": False}, {"email_verified": None}]}
        for code, variants in cases.items():
            for override in variants:
                browser = Browser(self)
                self.browsers.append(browser)
                target = await browser.sign_in("ana@empresa.invalid", **override)
                self.assertEqual(target, f"/?login_error={code}", override)
                self.assertEqual((await browser.refresh())[0], 401)

    async def test_the_provider_failing_or_refusing_the_code_ends_in_a_short_code(self):
        ana = await self.person("ana@empresa.invalid", "admin", signed_in=False)
        self.google.unavailable = True
        self.assertEqual(await ana.sign_in("ana@empresa.invalid"), "/?login_error=provedor_indisponivel")
        self.google.unavailable, self.google.status = False, 400
        self.assertEqual(await ana.sign_in("ana@empresa.invalid"), "/?login_error=troca_recusada")

    async def test_without_a_configured_secret_nobody_is_sent_to_google(self):
        self.vault.delete(*SECRET_SLOT)
        browser = Browser(self)
        self.browsers.append(browser)
        status, headers, _ = await browser.raw("GET", "/auth/google/start")
        self.assertEqual((status, headers["Location"]), (303, "/?login_error=nao_configurado"))

    async def test_the_login_endpoints_are_rate_limited(self):
        browser = Browser(self)
        self.browsers.append(browser)
        seen = [(await browser.raw("GET", "/auth/google/start"))[1].get("Location", "") for _ in range(25)]
        self.assertTrue(any(loc.endswith("login_error=limite") for loc in seen))
        self.assertTrue(seen[0].startswith("https://accounts.google.com/"))


class DomainRestrictionTests(RemoteCase):
    allowed_domains = ["empresa.invalid"]

    async def test_only_the_configured_workspace_domain_may_sign_in(self):
        await self.person("ana@empresa.invalid", "admin", signed_in=False)
        for hd, expected in (("empresa.invalid", "/"), ("outra.invalid", "/?login_error=dominio_nao_permitido"), (None, "/?login_error=dominio_nao_permitido")):
            browser = Browser(self)
            self.browsers.append(browser)
            self.assertEqual(await browser.sign_in("ana@empresa.invalid", hd=hd), expected)


class GuardTests(RemoteCase):
    async def test_the_request_must_come_to_the_public_address_from_a_signed_in_person_in_the_same_origin(self):
        ana = await self.person("ana@empresa.invalid", "admin")
        path = self.t(self.a, "/cases")
        self.assertEqual((await ana.call("GET", path))[0], 200)
        stranger = Browser(self)
        self.browsers.append(stranger)
        status, data = await stranger.call("GET", path)
        self.assertEqual((status, data["code"], data["login_url"]), (401, "unauthenticated", "/auth/google/start"))
        self.assertEqual((await ana.call("GET", path, origin=False))[0], 403)                        # no Origin, no same-origin proof
        self.assertEqual((await ana.call("GET", path, origin=False, headers={"Sec-Fetch-Site": "same-origin"}))[0], 200)
        self.assertEqual((await ana.call("GET", path, origin=False, headers={"Sec-Fetch-Site": "cross-site"}))[0], 403)
        self.assertEqual((await ana.call("GET", path, headers={"Origin": "https://evil.invalid"}))[0], 403)
        status, _, _ = await ana.raw("GET", path, headers={"Host": "evil.invalid", "Origin": self.origin})
        self.assertEqual(status, 403)                                                                 # DNS rebinding / wrong name

    async def test_changes_need_the_csrf_token_json_and_the_exact_origin(self):
        ana = await self.person("ana@empresa.invalid", "admin")
        body = {"department_id": self.dep_a["id"], "objective": "Nova demanda"}
        path = self.t(self.a, "/cases")
        self.assertEqual((await ana.call("POST", path, body, csrf=False))[0], 403)
        status, data = await ana.call("POST", path, body, headers={"X-CSRF-Token": "x" * 64})
        self.assertEqual((status, data["code"]), (403, "csrf"))
        self.assertEqual((await ana.call("POST", path, body, origin=False))[0], 403)
        status, _, _ = await ana.raw("POST", path, data="x=1", headers={"Origin": self.origin, "X-CSRF-Token": ana.csrf,
                                                                          "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        status, case = await ana.call("POST", path, body)
        self.assertEqual((status, case["objective"]), (201, "Nova demanda"))
        other = await self.person("bia@empresa.invalid", "admin")
        self.assertEqual((await other.call("POST", path, body, headers={"X-CSRF-Token": ana.csrf}))[0], 403)  # one person's token is not another's

    async def test_security_headers_and_no_cors(self):
        ana = await self.person("ana@empresa.invalid", "admin")
        status, headers, _ = await ana.raw("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual((headers["Cache-Control"], headers["X-Content-Type-Options"], headers["Referrer-Policy"]), ("no-store", "nosniff", "no-referrer"))
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, _ = await ana.raw("OPTIONS", self.t(self.a, "/cases"), headers={"Origin": "https://evil.invalid"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, _ = await ana.raw("GET", "/auth/google/start")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])   # redirects get them too

    async def test_nothing_of_the_local_application_exists_on_the_remote_listener(self):
        ana = await self.person("ana@empresa.invalid", "admin")
        for method, path in (("GET", "/ws"), ("GET", "/api/voices"), ("POST", "/api/voices/settings"), ("GET", "/api/persistence"),
                             ("POST", "/api/persistence/disable"), ("GET", "/microphone-worklet.js")):
            self.assertEqual((await ana.raw(method, path, headers={"Origin": self.origin}))[0], 404, path)

    async def test_requests_are_rate_limited_per_session(self):
        state = self.app[__import__("team.remote", fromlist=["STATE"]).STATE]
        state.api_limiter, state.write_limiter = RateLimiter(5, 60), RateLimiter(2, 60)
        ana, bia = await self.person("ana@empresa.invalid", "admin"), await self.person("bia@empresa.invalid", "admin")
        path = self.t(self.a, "/cases")
        statuses = [(await ana.call("GET", path))[0] for _ in range(7)]
        self.assertEqual(statuses[:5], [200] * 5)
        self.assertEqual(statuses[5:], [429, 429])
        self.assertEqual((await bia.call("GET", path))[0], 200)                                       # another session is not affected
        body = {"department_id": self.dep_a["id"], "objective": "x"}
        self.assertEqual([(await bia.call("POST", path, body))[0] for _ in range(3)], [201, 201, 429])

    async def test_ending_a_session_logs_out_and_needs_the_token(self):
        ana = await self.person("ana@empresa.invalid", "admin")
        status, _, _ = await ana.raw("POST", "/auth/logout", json={}, headers={"Origin": self.origin})
        self.assertEqual(status, 403)                                                                   # no CSRF token
        status, data = await ana.call("POST", "/auth/logout", {})
        self.assertEqual((status, data), (200, {"ok": True}))
        self.assertEqual((await ana.call("GET", self.t(self.a, "/cases")))[0], 401)


class PolicyTests(RemoteCase):
    def extend(self, app):
        async def unlisted(request):
            return web.json_response({"leak": True})
        app.router.add_get("/api/team/novidade", unlisted)

    async def test_every_route_of_the_remote_application_has_a_rule_and_every_rule_a_route(self):
        registered = {f"{route.method} {route.resource.canonical}" for route in self.app.router.routes()
                      if route.resource is not None and route.resource.canonical.startswith("/api/team") and route.method != "HEAD"}
        registered.discard("GET /api/team/novidade")  # registered on purpose by this class to prove default-deny
        self.assertEqual(registered - set(POLICY), set(), "route without a rule (it would answer 403 to everybody)")
        self.assertEqual(set(POLICY) - registered, set(), "rule for a route that does not exist")

    async def test_an_unlisted_route_and_the_local_only_actions_are_denied_to_everyone(self):
        ana = await self.person("ana@empresa.invalid", "admin")
        self.assertEqual((await ana.call("GET", "/api/team/novidade"))[0], 403)
        for method, path, body in (("POST", "/api/team/companies", {"name": "Outra"}),
                                   ("PATCH", self.t(self.a), {"expected_revision": 1, "name": "Renomeada"}),
                                   ("POST", self.t(self.a, "/skills/install"), {"demo": "suporte", "approved_hash": "0" * 64}),
                                   ("PATCH", self.t(self.a, "/skills/skl_x"), {"status": "inativo"}),
                                   ("POST", self.t(self.a, "/mcp-catalog"), {}),
                                   ("PATCH", self.t(self.a, "/mcp-catalog/mcp_x"), {})):
            self.assertEqual((await ana.call(method, path, body))[0], 403, path)
        self.assertEqual(sorted(c["name"] for c in self.s.team.list_companies(self.s.operator)), ["Empresa A", "Empresa B"])

    async def test_files_exports_search_streams_and_tools_have_no_remote_route(self):
        ana = await self.person("ana@empresa.invalid", "admin")
        for method, tail in (("GET", "/export"), ("GET", "/search?q=segredo"), ("GET", "/files/relatorio.csv"), ("GET", "/knowledge"), ("GET", "/backup"),
                             ("POST", "/backup"), ("GET", "/audit"), ("POST", "/tools/call"), ("GET", "/stream"), ("GET", "/memory"), ("POST", "/finance/import")):
            status, _ = await ana.call(method, self.t(self.a, tail), {} if method == "POST" else None)
            self.assertIn(status, (403, 404, 405), tail)
        for path in ("/ws", "/api/team/ws", "/api/team/stream"):
            self.assertIn((await ana.raw("GET", path, headers={"Origin": self.origin}))[0], (403, 404))

    async def test_each_role_reaches_exactly_what_it_should(self):
        people = {role: await self.person(f"{role}@empresa.invalid", role) for role in ("leitor", "atendente", "aprovador", "admin")}
        case = self.working_case(self.owner_a, self.dep_a, self.agent_a)
        approval, _ = self.s.approvals.request(self.owner_a, case["id"], action="enviar_email", resource="con_x", recipients=["c@x.invalid"],
                                               params={"assunto": "Oi"})
        new_case = {"department_id": self.dep_a["id"], "objective": "Pedido"}
        checks = [("GET", self.t(self.a, "/cases"), None, "leitor"), ("POST", self.t(self.a, "/cases"), new_case, "atendente"),
                  ("POST", self.t(self.a, f"/cases/{case['id']}/pause"), {"expected_revision": 999, "reason": "x"}, "atendente"),
                  ("POST", self.t(self.a, f"/approvals/{approval['id']}/deny"), {"reason": "nao"}, "aprovador"),
                  ("GET", self.t(self.a, "/connections"), None, "admin"), ("GET", self.t(self.a, "/members"), None, "admin"),
                  ("POST", self.t(self.a, "/departments"), {"name": "Novo", "scope": "x"}, "admin")]
        order = ["leitor", "atendente", "aprovador", "admin"]
        for method, path, body, minimum in checks:
            for role, browser in people.items():
                status, _ = await browser.call(method, path, body)
                allowed = order.index(role) >= order.index(minimum)
                if allowed:
                    self.assertNotIn(status, (401, 403), (method, path, role))
                else:
                    self.assertEqual(status, 403, (method, path, role))
        self.assertEqual(self.s.approvals.get(self.owner_a, approval["id"])["state"], "negada")   # the aprovador's call really went through

    async def test_a_reader_cannot_change_anything_even_where_the_route_would_let_them_in(self):
        reader = await self.person("leitor@empresa.invalid", "leitor")
        self.assertEqual((await reader.call("GET", self.t(self.a, "/cases")))[0], 200)
        self.assertEqual((await reader.call("POST", self.t(self.a, "/cases"), {"department_id": self.dep_a["id"], "objective": "x"}))[0], 403)
        self.assertEqual(self.s.cases.list_cases(self.owner_a), [])


class IsolationTests(RemoteCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ana = await self.person("ana@a.invalid", "admin", self.a)
        self.bia = await self.person("bia@b.invalid", "admin", self.b)
        self.case_a = self.working_case(self.owner_a, self.dep_a, self.agent_a, "Demanda secreta da A")
        self.case_b = self.working_case(self.owner_b, self.dep_b, self.agent_b, "Demanda secreta da B")
        self.approval_b, _ = self.s.approvals.request(self.owner_b, self.case_b["id"], action="enviar_email", resource="con_x",
                                                      recipients=["c@x.invalid"], params={"assunto": "Oi"})
        self.gap_b = self.s.gaps.report(self.owner_b, self.case_b["id"], "Consultar dominio na Cloudflare")

    async def test_two_people_two_companies_by_id_list_cursor_search_and_replay(self):
        status, own = await self.ana.call("GET", self.t(self.a, "/cases"))
        self.assertEqual([c["id"] for c in own["cases"]], [self.case_a["id"]])
        # reading another company's data through the URL the person is allowed to use never works: ids are scoped by company
        for path in (f"/cases/{self.case_b['id']}", "/cases?state=em_execucao"):
            status, data = await self.ana.call("GET", self.t(self.a, path))
            self.assertNotIn("Demanda secreta da B", str(data))
        self.assertEqual((await self.ana.call("GET", self.t(self.a, f"/cases/{self.case_b['id']}")))[0], 404)
        # ... and by naming the other company in the URL
        for tail in ("/cases", f"/cases/{self.case_b['id']}", "/events?after=0", "/attention", "/agents", "/members", "/gaps", "/connections"):
            status, data = await self.ana.call("GET", self.t(self.b, tail))
            self.assertEqual(status, 404, tail)
            self.assertEqual(data, {"error": "Recurso nao encontrado."})
        status, data = await self.ana.call("GET", "/api/team/companies/ten_" + "0" * 32 + "/cases")
        self.assertEqual((status, data), (404, {"error": "Recurso nao encontrado."}))   # indistinguishable from a company that does not exist
        # replay of an event cursor from one company in the other
        status, feed = await self.bia.call("GET", self.t(self.b, "/events?after=0"))
        self.assertEqual(status, 200)
        status, replay = await self.ana.call("GET", self.t(self.a, f"/events?after={feed['cursor']}&case_id={self.case_b['id']}"))
        self.assertEqual(status, 404)

    async def test_actions_on_another_companys_records_are_refused(self):
        a_path = self.t(self.a, f"/approvals/{self.approval_b['id']}/approve")
        status, _ = await self.ana.call("POST", a_path, {"shown_hash": self.approval_b["payload_hash"]})
        self.assertEqual(status, 404)
        self.assertEqual((await self.ana.call("POST", self.t(self.b, f"/approvals/{self.approval_b['id']}/approve"),
                                              {"shown_hash": self.approval_b["payload_hash"]}))[0], 404)
        self.assertEqual(self.s.approvals.get(self.owner_b, self.approval_b["id"])["state"], "pendente")
        status, _ = await self.ana.call("POST", self.t(self.a, f"/gaps/{self.gap_b['id']}/decide"),
                                        {"expected_revision": self.gap_b["revision"], "decision": "nao_atender", "note": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(self.s.gaps.get(self.owner_b, self.gap_b["id"])["state"], "aberta")
        self.assertEqual((await self.ana.call("POST", self.t(self.a, f"/cases/{self.case_b['id']}/take-over"),
                                              {"expected_revision": self.case_b["revision"], "reason": "x"}))[0], 404)
        self.assertFalse(self.s.cases.get_case(self.owner_b, self.case_b["id"]).get("operator_control"))
        status, _ = await self.ana.call("POST", self.t(self.a, "/cases"), {"department_id": self.dep_b["id"], "objective": "Cruzado"})
        self.assertEqual(status, 404)                                                                  # a department of the other company

    async def test_the_body_cannot_name_another_company(self):
        status, data = await self.ana.call("POST", self.t(self.a, "/cases"), {"department_id": self.dep_a["id"], "objective": "x", "tenant_id": self.b["id"]})
        self.assertEqual(status, 400)
        self.assertEqual([c["objective"] for c in self.s.cases.list_cases(self.owner_b)], ["Demanda secreta da B"])

    async def test_a_person_in_two_companies_gets_each_role_where_it_applies(self):
        self.s.identity.invite(self.owner_b, self.b["id"], "ana@a.invalid", "leitor")
        status, state = await self.ana.call("GET", "/api/team/state")
        self.assertEqual({c["id"]: c["role"] for c in state["companies"]}, {self.a["id"]: "admin", self.b["id"]: "leitor"})
        self.assertEqual((await self.ana.call("GET", self.t(self.b, "/cases")))[0], 200)
        self.assertEqual((await self.ana.call("GET", self.t(self.b, "/members")))[0], 403)             # admin in A, only a reader in B
        self.assertEqual((await self.ana.call("POST", self.t(self.b, "/cases"), {"department_id": self.dep_b["id"], "objective": "x"}))[0], 403)

    async def test_members_are_visible_to_their_own_company_only(self):
        status, data = await self.ana.call("GET", self.t(self.a, "/members"))
        self.assertEqual([m["email"] for m in data["members"]], ["ana@a.invalid"])
        status, data = await self.ana.call("POST", self.t(self.a, "/members"), {"email": "novo@a.invalid", "role": "leitor", "name": "Novo"})
        self.assertEqual((status, data["role"], data["signed_in_before"]), (201, "leitor", False))
        status, _ = await self.ana.call("POST", self.t(self.b, "/members"), {"email": "novo@a.invalid", "role": "admin"})
        self.assertEqual(status, 404)
        status, data = await self.bia.call("GET", self.t(self.b, "/members"))
        self.assertEqual([m["email"] for m in data["members"]], ["bia@b.invalid"])


class RevocationTests(RemoteCase):
    async def test_revoked_sessions_changed_roles_and_removed_members_take_effect_on_the_next_request(self):
        admin = await self.person("admin@empresa.invalid", "admin")
        clerk = await self.person("clerk@empresa.invalid", "aprovador")
        path = self.t(self.a, "/cases")
        new_case = {"department_id": self.dep_a["id"], "objective": "x"}
        self.assertEqual((await clerk.call("POST", path, new_case))[0], 201)
        user = self.s.identity.members(self.owner_a, self.a["id"])
        clerk_id = next(m["user_id"] for m in user if m["email"] == "clerk@empresa.invalid")
        status, data = await admin.call("PATCH", self.t(self.a, f"/members/{clerk_id}"), {"role": "leitor"})
        self.assertEqual((status, data["role"]), (200, "leitor"))
        self.assertEqual((await clerk.call("POST", path, new_case))[0], 403)                            # role lowered: effective at once
        self.assertEqual((await clerk.call("GET", path))[0], 200)
        status, data = await admin.call("PATCH", self.t(self.a, f"/members/{clerk_id}"), {"status": "inativo"})
        self.assertEqual((status, data["status"]), (200, "inativo"))
        self.assertEqual((await clerk.call("GET", path))[0], 404)                                        # no longer a member
        await admin.call("PATCH", self.t(self.a, f"/members/{clerk_id}"), {"status": "ativo"})
        self.assertEqual((await clerk.call("GET", path))[0], 200)
        self.s.identity.revoke_sessions(self.s.operator, clerk_id)
        self.assertEqual((await clerk.call("GET", path))[0], 401)                                        # revoked: immediate
        self.assertEqual((await clerk.refresh())[0], 401)

    async def test_an_admin_cannot_lock_themselves_out_through_the_api(self):
        admin = await self.person("admin@empresa.invalid", "admin")
        me = self.s.identity.members(self.owner_a, self.a["id"])[0]["user_id"]
        for body in ({"role": "leitor"}, {"status": "inativo"}):
            status, data = await admin.call("PATCH", self.t(self.a, f"/members/{me}"), body)
            self.assertEqual(status, 400, body)
        status, data = await admin.call("PATCH", self.t(self.a, f"/members/{me}"), {})
        self.assertEqual(status, 400)
        status, data = await admin.call("PATCH", self.t(self.a, "/members/usr_" + "0" * 32), {"role": "leitor"})
        self.assertEqual(status, 404)

    async def test_deactivating_the_person_globally_by_the_owner_ends_their_session(self):
        ana = await self.person("ana@empresa.invalid", "admin")
        user = self.s.identity.members(self.owner_a, self.a["id"])[0]["user_id"]
        self.s.identity.set_user_status(self.s.operator, user, "inativo")
        self.assertEqual((await ana.call("GET", self.t(self.a, "/cases")))[0], 401)


class ConfigTests(unittest.TestCase):
    def test_remote_is_off_by_default_and_never_opens_anything_by_itself(self):
        self.assertFalse(RemoteConfig.from_config({}).enabled)
        self.assertFalse(RemoteConfig.from_config({"remote": {"enabled": False, "bind": "0.0.0.0"}}).enabled)
        self.assertFalse(RemoteConfig.from_config({"remote": {"public_origin": "http://x"}}).enabled)   # enabled must be exactly true

    def test_a_valid_configuration(self):
        config = RemoteConfig.from_config({"remote": {"enabled": True, "public_origin": "https://Pc.Tailnet.TS.net", "google_client_id": " id ",
                                                      "allowed_domains": ["Empresa.com"]}})
        self.assertEqual((config.bind, config.port, config.public_origin, config.host, config.secure), ("127.0.0.1", 8766, "https://pc.tailnet.ts.net",
                                                                                                        "pc.tailnet.ts.net", True))
        self.assertEqual((config.client_id, config.allowed_domains, config.redirect_uri),
                         ("id", ("empresa.com",), "https://pc.tailnet.ts.net/auth/google/callback"))

    def test_unsafe_configurations_are_refused(self):
        base = {"enabled": True, "public_origin": "https://pc.example.invalid", "google_client_id": "id"}
        bad = [{"bind": "0.0.0.0"}, {"bind": "192.168.0.10"}, {"bind": "::"}, {"tls_cert": "a.pem"}, {"public_origin": "http://pc.example.invalid"},
               {"public_origin": "https://pc.example.invalid/caminho"}, {"public_origin": "https://u:p@pc.example.invalid"},
               {"public_origin": "ftp://x"}, {"public_origin": ""}, {"google_client_id": ""}, {"port": 80}, {"port": True}, {"idle_minutes": 1},
               {"session_hours": 9999}, {"allowed_domains": "empresa.com"}, {"campo_novo": 1}]
        for override in bad:
            with self.assertRaises(ConfigError, msg=str(override)):
                RemoteConfig.from_config({"remote": {**base, **override}})
        RemoteConfig.from_config({"remote": {**base, "bind": "0.0.0.0", "tls_cert": "c.pem", "tls_key": "k.pem"}})   # public bind only with TLS
        RemoteConfig.from_config({"remote": {**base, "public_origin": "http://localhost:8766"}})                      # http only for localhost
        with self.assertRaises(ConfigError):
            RemoteConfig.from_config({"remote": "sim"})

    def test_direct_tls_is_off_unless_a_certificate_is_configured(self):
        from team.remote import ssl_context
        self.assertIsNone(ssl_context(RemoteConfig.from_config({"remote": {"enabled": True, "public_origin": "https://pc.example.invalid",
                                                                            "google_client_id": "id"}})))

    def test_the_rate_limiter_is_a_sliding_window(self):
        now = [0.0]
        limiter = RateLimiter(2, 10, clock=lambda: now[0])
        self.assertEqual([limiter.allow("a"), limiter.allow("a"), limiter.allow("a"), limiter.allow("b")], [True, True, False, True])
        now[0] = 10.5
        self.assertTrue(limiter.allow("a"))


if __name__ == "__main__":
    unittest.main()
