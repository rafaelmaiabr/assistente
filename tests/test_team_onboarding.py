import asyncio
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import ClientSession, web

from team import doctor, persistence
from team.access import NotFound, PermissionDenied
from team.app import TeamApp
from team.db import MIGRATIONS_DIR, discover, migrate
from team.onboarding import OnboardingRejected, load_templates, validate_template
from team.remote import POLICY
from team.vault import MemoryVault
from team.web import TEAM
from web_server import ALLOWED_HOSTS, create_app

CONFIG = {"model": "qwen2.5:7b", "ollama_url": "http://127.0.0.1:11435/v1", "max_concurrent_generations": 1}


def boot(folder):
    persistence.enable(Path(folder))
    app = TeamApp(Path(folder), CONFIG, vault=MemoryVault(), legacy_elevenlabs=None, voice_profiles=lambda: [])
    return app, app.services()


class OnboardingBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app, self.s = boot(self.temp.name)
        self.op = self.s.operator

    def preview(self, template="atendimento-basico", name="Empresa Modelo"):
        return self.s.onboarding.preview(self.op, template, name)


class TemplateTests(unittest.TestCase):
    def test_shipped_templates_are_valid_versioned_and_hold_no_account_secret_or_address(self):
        found = load_templates()
        self.assertTrue({"atendimento-basico", "somente-suporte"} <= set(found))
        for template in found.values():
            self.assertIsInstance(template["template_version"], int)
            text = json.dumps(template)
            for forbidden in ("http", "@", "password", "senha", "token", "smtp", "imap", "connection", "credential"):
                self.assertNotIn(forbidden, text.lower(), template["template_id"])

    def test_a_template_with_dangling_references_unknown_fields_or_remote_models_is_refused(self):
        base = json.loads(json.dumps(load_templates()["atendimento-basico"]))

        def broken(mutate):
            data = json.loads(json.dumps(base))
            mutate(data)
            with self.assertRaises(OnboardingRejected):
                validate_template(data)
        broken(lambda d: d["agents"][1].update(department="inexistente"))
        broken(lambda d: d["agents"][1].update(capabilities=["nao-definida"]))
        broken(lambda d: d["agents"][1].update(model_policy_ref="gpt-externo"))
        broken(lambda d: d["handoffs"].append({"from": "suporte", "to": "fantasma"}))
        broken(lambda d: d["handoffs"].append({"from": "suporte", "to": "suporte"}))
        broken(lambda d: d["departments"].append(dict(d["departments"][0])))
        broken(lambda d: d.update(conexoes=[{"host": "x"}]))
        broken(lambda d: d["agents"][0].update(extra="x"))
        broken(lambda d: d["departments"][0].update(scope="Escreva para contato@exemplo.com"))
        broken(lambda d: d["departments"][0].update(scope="Use sk-abcdefghijklmnopqrstuvwxyz"))
        broken(lambda d: d.update(case_access=["fantasma"]))
        broken(lambda d: d["agents"][2].update(out_of_scope="Pagar sem aprovacao quando o cliente pedir"))
        broken(lambda d: d.pop("agents"))


class PreviewAndApplyTests(OnboardingBase):
    def test_the_preview_lists_everything_that_would_be_created_and_writes_nothing(self):
        before = self.s.team.list_companies(self.op)
        plan = self.preview()
        self.assertEqual(self.s.team.list_companies(self.op), before)
        self.assertEqual([d["name"] for d in plan["departments"]], ["Secretaria", "Suporte", "Financeiro"])
        self.assertEqual([a["name"] for a in plan["agents"]], ["Secretaria", "Suporte", "Financeiro"])
        self.assertEqual({c["name"] for c in plan["capabilities"]}, {"suporte", "financeiro"})
        self.assertTrue(any(h["de"] == "Suporte" and h["para"] == "Financeiro" for h in plan["handoffs"]))
        self.assertEqual({g["agent"] for g in plan["case_access"]}, {"Secretaria", "Suporte", "Financeiro"})
        self.assertIn("Nenhuma conta, conexão, credencial, skill ou endereço é criado ou conectado.", plan["nao_inclui"])
        self.assertEqual(self.preview()["plan_hash"], plan["plan_hash"])                         # same input, same hash
        self.assertNotEqual(self.preview(name="Outra")["plan_hash"], plan["plan_hash"])
        self.assertNotEqual(self.preview("somente-suporte")["plan_hash"], plan["plan_hash"])

    def test_applying_creates_exactly_the_reviewed_team(self):
        plan = self.preview()
        done = self.s.onboarding.apply(self.op, "atendimento-basico", "Empresa Modelo", plan["plan_hash"])
        self.assertEqual((done["departments"], done["agents"], done["capabilities"]), (3, 3, 2))
        ctx = self.s.access.tenant_context(self.op, done["company"]["id"])
        agents = {a["name"]: a for a in self.s.agents.list_agents(ctx)}
        self.assertEqual(set(agents), {"Secretaria", "Suporte", "Financeiro"})
        self.assertEqual(agents["Suporte"]["capabilities"], ["suporte"])
        self.assertEqual(sorted(agents["Suporte"]["handoff_target_ids"]), sorted([agents["Financeiro"]["id"]]))
        self.assertEqual(sorted(agents["Secretaria"]["handoff_target_ids"]), sorted([agents["Suporte"]["id"], agents["Financeiro"]["id"]]))
        self.assertEqual(sorted(c["name"] for c in self.s.team.list(ctx, "department")), ["Financeiro", "Secretaria", "Suporte"])
        grants = self.s.team.list(ctx, "access_grant")
        self.assertEqual({g["principal_id"] for g in grants}, {a["id"] for a in agents.values()})
        self.assertTrue(all(g["actions"] == ["read", "write"] and g["resource_kind"] == "case" for g in grants))
        # nothing real was connected
        self.assertEqual(self.s.connections.list_connections(ctx), [])
        self.assertEqual(self.s.skills.list_grants(ctx), [])
        self.assertEqual(self.s.skills.list_skills(self.op), [])
        self.assertEqual(self.s.outbound.list_messages(ctx), [])

    def test_the_new_team_really_works_triage_routes_by_the_template_capabilities(self):
        done = self.s.onboarding.apply(self.op, "atendimento-basico", "Empresa Modelo", self.preview()["plan_hash"])
        ctx = self.s.access.tenant_context(self.op, done["company"]["id"])
        agents = {a["name"]: a for a in self.s.agents.list_agents(ctx)}
        department = next(d for d in self.s.team.list(ctx, "department") if d["name"] == "Secretaria")
        case = self.s.cases.open_case(ctx, department_id=department["id"], objective="Atendimento", customer_ref="c1")
        decision = asyncio.run(self.s.triage.triage(ctx, case["id"], "O meu site esta fora do ar desde ontem", secretaria_agent_id=agents["Secretaria"]["id"]))
        self.assertEqual((decision["decision"], decision["capabilities"]), ("rotear", ["suporte"]))
        handoff = self.s.handoffs.list_handoffs(ctx, case_id=case["id"])[0]
        self.assertEqual(handoff["to_agent_id"], agents["Suporte"]["id"])

    def test_the_confirmation_must_match_the_preview_and_the_name_must_be_new(self):
        plan = self.preview()
        for bad in (None, "", "0" * 64, self.preview("somente-suporte")["plan_hash"]):
            with self.assertRaises(OnboardingRejected):
                self.s.onboarding.apply(self.op, "atendimento-basico", "Empresa Modelo", bad)
        with self.assertRaises(OnboardingRejected):
            self.s.onboarding.apply(self.op, "atendimento-basico", "Outro Nome", plan["plan_hash"])    # name changed after the preview
        self.assertEqual(self.s.team.list_companies(self.op), [])
        self.s.onboarding.apply(self.op, "atendimento-basico", "Empresa Modelo", plan["plan_hash"])
        with self.assertRaisesRegex(OnboardingRejected, "Ja existe"):
            self.preview(name="  empresa modelo ")
        with self.assertRaises(NotFound):
            self.preview("inexistente")
        for name in ("", "   ", None, "x" * 200):
            with self.assertRaises(OnboardingRejected):
                self.preview(name=name)

    def test_a_failure_in_the_middle_leaves_the_company_deactivated_and_says_so(self):
        plan = self.preview()
        original = self.s.agents.create_agent
        calls = []

        def flaky(*args, **kwargs):
            calls.append(kwargs["name"])
            if len(calls) == 2:
                raise RuntimeError("falha simulada")
            return original(*args, **kwargs)
        with mock.patch.object(self.s.agents, "create_agent", flaky):
            with self.assertRaisesRegex(OnboardingRejected, "DESATIVADA"):
                self.s.onboarding.apply(self.op, "atendimento-basico", "Empresa Modelo", plan["plan_hash"])
        company = next(c for c in self.s.team.list_companies(self.op) if c["name"] == "Empresa Modelo")
        self.assertEqual(company["status"], "inativo")

    def test_only_the_local_owner_configures_by_template(self):
        done = self.s.onboarding.apply(self.op, "somente-suporte", "Empresa Um", self.preview("somente-suporte", "Empresa Um")["plan_hash"])
        ctx = self.s.access.tenant_context(self.op, done["company"]["id"])
        for call in (lambda: self.s.onboarding.preview(ctx, "somente-suporte", "X"), lambda: self.s.onboarding.list_templates(ctx),
                     lambda: self.s.onboarding.apply(object(), "somente-suporte", "X", "0" * 64)):
            with self.assertRaises(PermissionDenied):
                call()
        self.assertEqual([t["id"] for t in self.s.onboarding.list_templates(self.op)], ["atendimento-basico", "somente-suporte"])

    def test_no_route_for_these_actions_exists_on_the_remote_listener(self):
        for key in ("GET /api/team/onboarding/templates", "POST /api/team/onboarding/preview", "POST /api/team/onboarding/apply", "GET /api/team/doctor"):
            self.assertEqual(POLICY[key], "local")


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        team_app = TeamApp(Path(self.folder.name), CONFIG, vault=MemoryVault(), legacy_elevenlabs=None, voice_profiles=lambda: [])
        self.app = create_app(object, CONFIG, object(), data_root=Path(self.folder.name), team_app=team_app)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.host = f"127.0.0.1:{self.runner.addresses[0][1]}"
        self.base = f"http://{self.host}"
        ALLOWED_HOSTS.add(self.host)
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        ALLOWED_HOSTS.discard(self.host)
        self.folder.cleanup()

    async def call(self, method, path, body=None):
        kwargs = {"headers": {"Origin": self.base}}
        if body is not None:
            kwargs["json"] = body
        async with self.client.request(method, self.base + path, **kwargs) as response:
            text = await response.text()
            return response.status, (json.loads(text) if text.startswith(("{", "[")) else text)

    async def test_the_setup_assistant_through_the_api_and_the_doctor_without_persistence(self):
        status, report = await self.call("GET", "/api/team/doctor")                 # works before persistence is on
        self.assertEqual(status, 200)
        by_id = {i["id"]: i for i in report["itens"]}
        self.assertEqual(by_id["persistencia"]["estado"], "aviso")
        self.assertIn("Ative", by_id["persistencia"]["acao"])
        self.assertEqual((await self.call("GET", "/api/team/onboarding/templates"))[0], 409)   # needs persistence
        await self.call("POST", "/api/persistence/enable", {})
        status, data = await self.call("GET", "/api/team/onboarding/templates")
        self.assertEqual((status, [t["id"] for t in data["templates"]]), (200, ["atendimento-basico", "somente-suporte"]))
        body = {"template_id": "somente-suporte", "company_name": "Empresa via API"}
        status, plan = await self.call("POST", "/api/team/onboarding/preview", body)
        self.assertEqual((status, [a["name"] for a in plan["agents"]]), (200, ["Suporte"]))
        status, data = await self.call("POST", "/api/team/onboarding/apply", {**body, "plan_hash": "0" * 64})
        self.assertEqual(status, 400)
        self.assertIn("mudou", data["error"])
        status, data = await self.call("POST", "/api/team/onboarding/apply", {**body, "plan_hash": plan["plan_hash"]})
        self.assertEqual((status, data["agents"]), (201, 1))
        status, state = await self.call("GET", "/api/team/state")
        self.assertEqual([c["name"] for c in state["companies"]], ["Empresa via API"])
        status, data = await self.call("POST", "/api/team/onboarding/preview", {**body, "extra": 1})
        self.assertEqual(status, 400)
        status, data = await self.call("POST", "/api/team/onboarding/preview", body)
        self.assertEqual(status, 400)                                                  # the name is taken now


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def report(self, **kwargs):
        kwargs.setdefault("probe", lambda url: True)
        result = doctor.run(self.root, {**CONFIG}, vault=kwargs.pop("vault", MemoryVault()), **kwargs)
        return result, {i["id"]: i for i in result["itens"]}

    def test_a_fresh_install_is_fine_and_every_problem_comes_with_an_action(self):
        result, by_id = self.report()
        self.assertEqual(by_id["banco"]["estado"], "ok")
        for entry in result["itens"]:
            self.assertTrue(entry["estado"] == "ok" or entry["acao"] is None or entry["acao"], entry)
            if entry["estado"] != "ok":
                self.assertTrue(entry["acao"], entry)
        self.assertTrue(result["somente_leitura"])

    def test_it_reports_pending_migrations_and_how_they_are_handled(self):
        persistence.enable(self.root)
        db = persistence.data_dir(self.root) / persistence.DATABASE
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM schema_migrations WHERE version = (SELECT MAX(version) FROM schema_migrations)")
        conn.commit()
        conn.close()
        _, by_id = self.report()
        self.assertEqual(by_id["banco"]["estado"], "aviso")
        self.assertIn("pendente", by_id["banco"]["detalhe"])
        self.assertIn("pre-migracao", by_id["banco"]["acao"])

    def test_a_database_from_a_newer_version_is_an_error_that_changes_nothing(self):
        persistence.enable(self.root)
        db = persistence.data_dir(self.root) / persistence.DATABASE
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (999, 'futura', 'x', '2099-01-01T00:00:00Z')")
        conn.commit()
        conn.close()
        before = db.read_bytes()
        result, by_id = self.report()
        self.assertFalse(result["ok"])
        self.assertEqual(by_id["banco"]["estado"], "erro")
        self.assertIn("atualize o aplicativo", by_id["banco"]["acao"])
        self.assertEqual(db.read_bytes(), before)

    def test_an_altered_migration_and_a_corrupt_file_are_errors(self):
        persistence.enable(self.root)
        db = persistence.data_dir(self.root) / persistence.DATABASE
        conn = sqlite3.connect(db)
        conn.execute("UPDATE schema_migrations SET checksum = 'adulterado' WHERE version = 1")
        conn.commit()
        conn.close()
        self.assertIn("alterada", self.report()[1]["banco"]["detalhe"])
        db.write_bytes(b"isto nao e um banco sqlite" * 200)
        _, by_id = self.report()
        self.assertEqual(by_id["banco"]["estado"], "erro")
        self.assertIn("backup", by_id["banco"]["acao"])

    def test_backups_vault_model_server_and_remote_access_are_reported(self):
        persistence.enable(self.root)
        self.assertEqual(self.report()[1]["backup"]["estado"], "aviso")
        manifest = persistence.data_dir(self.root) / "backups" / "2026-06-01" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
        self.assertEqual(self.report()[1]["backup"]["estado"], "ok")
        broken = MemoryVault()
        broken.broken = True
        self.assertEqual(self.report(vault=broken)[1]["cofre"]["estado"], "erro")
        _, by_id = self.report(probe=lambda url: False)
        self.assertEqual(by_id["modelo"]["estado"], "aviso")
        self.assertIn("Ollama", by_id["modelo"]["acao"])
        remote = {**CONFIG, "remote": {"enabled": True, "public_origin": "https://pc.example.invalid", "google_client_id": "id"}}
        entry = {i["id"]: i for i in doctor.run(self.root, remote, vault=MemoryVault(), probe=lambda url: True)["itens"]}["acesso_remoto"]
        self.assertEqual(entry["estado"], "aviso")
        self.assertIn("set-secret", entry["acao"])
        bad = {**CONFIG, "remote": {"enabled": True, "bind": "0.0.0.0", "public_origin": "https://x.example.invalid", "google_client_id": "id"}}
        self.assertEqual({i["id"]: i for i in doctor.run(self.root, bad, probe=lambda url: True)["itens"]}["acesso_remoto"]["estado"], "erro")

    def test_it_never_prints_a_secret(self):
        vault = MemoryVault()
        vault.put("plataforma", "google-login", "segredo-que-nao-pode-aparecer")
        remote = {**CONFIG, "remote": {"enabled": True, "public_origin": "https://pc.example.invalid", "google_client_id": "id"}}
        text = json.dumps(doctor.run(self.root, remote, vault=vault, probe=lambda url: True))
        self.assertNotIn("segredo-que-nao-pode-aparecer", text)


class UpgradeAndRestartTests(unittest.TestCase):
    """Fixtures of earlier versions: a database at an older schema upgrades in place and keeps everything."""

    def older_database(self, folder, keep):
        subset = Path(folder) / "migracoes-antigas"
        subset.mkdir()
        for number, name, sql, checksum in discover(MIGRATIONS_DIR):
            if number <= keep:
                shutil.copyfile(MIGRATIONS_DIR / f"{number:04d}_{name}.sql", subset / f"{number:04d}_{name}.sql")
        path = persistence.data_dir(folder) / persistence.DATABASE
        migrate(path, subset)
        return path

    def test_upgrading_from_an_earlier_schema_keeps_data_settings_and_credential_references(self):
        with tempfile.TemporaryDirectory() as folder:
            latest = max(number for number, *_ in discover(MIGRATIONS_DIR))
            path = self.older_database(folder, latest - 5)
            persistence.data_dir(folder).mkdir(parents=True, exist_ok=True)
            (Path(folder) / "config.json").write_text(json.dumps({"model": "meu-modelo"}), encoding="utf-8")
            from team.access import AccessService
            from team.repository import SqliteRepository
            from team.service import TeamService
            repo = SqliteRepository(path)
            access = AccessService(repo, persistence.operator_id(folder))
            company = TeamService(repo, access).create_company(access.local_operator(), "Empresa Antiga")
            persistence._write(folder, True)
            config_before = (Path(folder) / "config.json").read_bytes()
            flag_before = (persistence.data_dir(folder) / persistence.FLAG).read_bytes()
            before = doctor.run(folder, CONFIG, vault=MemoryVault(), probe=lambda url: True)
            self.assertEqual({i["id"]: i for i in before["itens"]}["banco"]["estado"], "aviso")        # pending, and says so
            app = TeamApp(Path(folder), CONFIG, vault=MemoryVault(), legacy_elevenlabs=None, voice_profiles=lambda: [])
            services = app.services()                                                                   # the upgrade happens here
            self.assertEqual([c["name"] for c in services.team.list_companies(services.operator)], ["Empresa Antiga"])
            self.assertEqual(services.team.list_companies(services.operator)[0]["id"], company["id"])
            self.assertEqual((Path(folder) / "config.json").read_bytes(), config_before)
            self.assertEqual((persistence.data_dir(folder) / persistence.FLAG).read_bytes(), flag_before)
            self.assertTrue(list(persistence.data_dir(folder).glob("*.pre-migracao-*.bak")), "a copy of the old database must be kept")
            after = doctor.run(folder, CONFIG, vault=MemoryVault(), probe=lambda url: True)
            self.assertEqual({i["id"]: i for i in after["itens"]}["banco"]["estado"], "ok")
            self.assertIn(f"({latest} migracoes aplicadas)", {i["id"]: i for i in after["itens"]}["banco"]["detalhe"])

    def test_a_restart_with_a_pending_case_keeps_it_and_the_operator_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            app, s = boot(folder)
            done = s.onboarding.apply(s.operator, "somente-suporte", "Empresa", s.onboarding.preview(s.operator, "somente-suporte", "Empresa")["plan_hash"])
            ctx = s.access.tenant_context(s.operator, done["company"]["id"])
            department = s.team.list(ctx, "department")[0]
            case = s.cases.open_case(ctx, department_id=department["id"], objective="Pedido pendente", customer_ref="c1")
            operator_before = s.operator.principal_id
            restarted = TeamApp(Path(folder), CONFIG, vault=MemoryVault(), legacy_elevenlabs=None, voice_profiles=lambda: []).services()
            self.assertEqual(restarted.operator.principal_id, operator_before)
            ctx2 = restarted.access.tenant_context(restarted.operator, done["company"]["id"])
            again = restarted.cases.get_case(ctx2, case["id"])
            self.assertEqual((again["state"], again["objective"]), ("recebida", "Pedido pendente"))
            self.assertEqual(len(restarted.agents.list_agents(ctx2)), 1)

    def test_turning_persistence_off_and_on_never_deletes_the_team(self):
        with tempfile.TemporaryDirectory() as folder:
            app, s = boot(folder)
            s.onboarding.apply(s.operator, "somente-suporte", "Empresa", s.onboarding.preview(s.operator, "somente-suporte", "Empresa")["plan_hash"])
            persistence.disable(Path(folder))
            self.assertIsNone(TeamApp(Path(folder), CONFIG, vault=MemoryVault(), legacy_elevenlabs=None, voice_profiles=lambda: []).services())
            persistence.enable(Path(folder))
            back = TeamApp(Path(folder), CONFIG, vault=MemoryVault(), legacy_elevenlabs=None, voice_profiles=lambda: []).services()
            self.assertEqual([c["name"] for c in back.team.list_companies(back.operator)], ["Empresa"])


if __name__ == "__main__":
    unittest.main()
