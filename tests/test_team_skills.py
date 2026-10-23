import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.connections import ConnectionService, LauncherRegistry
from team.ids import new_id
from team.repository import SqliteRepository
from team.service import TeamService
from team.skills import SkillDenied, SkillRejected, SkillService
from team.vault import MemoryVault

DEMOS = Path(__file__).resolve().parent.parent / "team" / "skills_demo"
PROFILE = dict(name="Financeiro", goals=["Resumir"], personality={"tone": "objetivo", "initiative": "proativo"},
               scope="Cobrancas", out_of_scope="Outros", model_policy_ref="local-primeiro")
MANIFEST = {"manifest_version": 1, "id": "teste.consultar", "version": "1.0.0", "description": "Skill de teste.",
            "instructions": "Consulte e resuma.", "inputs": ["cliente"], "outputs": ["resumo"],
            "required_tools": [{"tool": "erp-demo.listar-cobrancas", "operations": ["read"]}],
            "risks": ["dados_financeiros"], "settings": []}


class SkillCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = SqliteRepository(self.root / "team.sqlite3")
        self.repo.migrate()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.skills = SkillService(self.repo, self.access, self.agents)
        self.connections = ConnectionService(self.repo, self.access, MemoryVault(),
                                             LauncherRegistry(self.root / "launchers.json", self.access.operator_id))
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Fin", scope="x", status="ativo")
        self.agent = self.agents.create_agent(self.ca, department_id=self.dep["id"], **PROFILE)
        self.entry = self.connections.approve_mcp_server(
            self.op, catalog_key="erp-demo", entry_version="1.0.0", transport="http",
            url="http://127.0.0.1:9000/mcp", tools=["listar-cobrancas"])
        self.conn = self.connections.create_connection(
            self.ca, label="ERP demo", provider="erp-demo", connection_kind="mcp", catalog_entry_id=self.entry["id"])

    def package(self, manifest=None, name="pkg", files=None, raw=None):
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        (folder / "skill.json").write_text(raw if raw is not None else json.dumps(manifest or MANIFEST), encoding="utf-8")
        for filename, content in (files or {}).items():
            (folder / filename).write_text(content, encoding="utf-8")
        return folder

    def install(self, manifest=None, name="pkg", **kwargs):
        folder = self.package(manifest, name)
        review = self.skills.review_package(folder)
        return self.skills.install(self.op, folder, review.content_hash, **kwargs)

    def grant(self, skill, **kwargs):
        options = {"agent_id": self.agent["id"], "skill_id": skill["id"], "operations": ["read"],
                   "connection_id": self.conn["id"], **kwargs}
        return self.skills.grant_skill(self.ca, **options)


class PackageTests(SkillCase):
    def test_demo_packages_are_fictitious_and_reviewable(self):
        names = sorted(p.name for p in DEMOS.iterdir())
        self.assertEqual(names, ["financeiro", "secretaria", "suporte"])
        for name in names:
            review = self.skills.review_package(DEMOS / name)
            self.assertTrue(review.fictitious)
            self.assertIn("DEMONSTRACAO", review.description)
            self.assertEqual(review.previous_version, "")
        finance = self.skills.review_package(DEMOS / "financeiro")
        self.assertEqual(finance.permissions, [{"tool": "erp-demo.listar-cobrancas", "operations": ["read"]}])
        self.assertIn("pagamento", finance.risks)
        self.assertEqual(self.skills.review_package(DEMOS / "secretaria").permissions, [])

    def test_review_changes_nothing_and_install_requires_the_reviewed_hash(self):
        folder = self.package()
        review = self.skills.review_package(folder)
        self.assertEqual(self.skills.list_skills(self.op), [])
        with self.assertRaisesRegex(SkillRejected, "difere"):
            self.skills.install(self.op, folder, "0" * 64)
        for forged in (self.ca, None):
            with self.assertRaises(PermissionDenied):
                self.skills.install(forged, folder, review.content_hash)
        skill = self.skills.install(self.op, folder, review.content_hash)
        self.assertEqual((skill["skill_key"], skill["status"], skill["fictitious"]), ("teste.consultar", "ativo", False))
        self.assertEqual(self.skills.install(self.op, folder, review.content_hash)["id"], skill["id"])  # idempotent
        self.assertEqual(len(self.skills.list_skills(self.op)), 1)

    def test_malformed_manifests_are_refused(self):
        bad = {
            "not json": None, "list": [], "missing id": {k: v for k, v in MANIFEST.items() if k != "id"},
            "bad id": {**MANIFEST, "id": "Teste Skill!"}, "bad version": {**MANIFEST, "version": "1.0"},
            "bad manifest_version": {**MANIFEST, "manifest_version": 2},
            "unknown field": {**MANIFEST, "extra": 1}, "no instructions": {k: v for k, v in MANIFEST.items() if k != "instructions"},
            "both instructions": {**MANIFEST, "instructions_file": "a.md"},
            "empty instructions": {**MANIFEST, "instructions": "   "},
            "huge instructions": {**MANIFEST, "instructions": "x" * 9000},
            "unknown risk": {**MANIFEST, "risks": ["destruir_tudo"]},
            "tool without server": {**MANIFEST, "required_tools": [{"tool": "listar", "operations": ["read"]}]},
            "tool bad operation": {**MANIFEST, "required_tools": [{"tool": "a.b", "operations": ["delete"]}]},
            "tool extra key": {**MANIFEST, "required_tools": [{"tool": "a.b", "operations": ["read"], "url": "x"}]},
            "duplicate tool": {**MANIFEST, "required_tools": [{"tool": "a.b", "operations": ["read"]}] * 2},
            "bad inputs": {**MANIFEST, "inputs": ["ok", "NAO OK"]}, "fictitious type": {**MANIFEST, "fictitious": "sim"},
        }
        for label, manifest in bad.items():
            raw = "{nao-json" if manifest is None else json.dumps(manifest)
            with self.assertRaises(SkillRejected, msg=label):
                self.skills.review_package(self.package(raw=raw, name="m" + str(abs(hash(label)))))
        with self.assertRaises(SkillRejected):
            self.skills.review_package(self.package(raw="x" * 70000, name="grande"))
        with self.assertRaises(SkillRejected):
            self.skills.review_package(self.root / "nao-existe")

    def test_packages_with_commands_or_code_are_refused(self):
        for key in ("command", "scripts", "postinstall", "entrypoint", "Hooks"):
            with self.assertRaisesRegex(SkillRejected, "comando"):
                self.skills.review_package(self.package({**MANIFEST, key: "powershell -c calc"}, name="c" + key))
        for filename in ("run.py", "setup.ps1", "go.cmd", "tool.exe", "x.dll", "a.js", "b.sh", "semextensao"):
            with self.assertRaisesRegex(SkillRejected, "nao permitido"):
                self.skills.review_package(self.package(name="f" + filename.replace(".", ""), files={filename: "print(1)"}))
        nested = self.package(name="aninhado")
        (nested / "sub").mkdir()
        with self.assertRaises(SkillRejected):
            self.skills.review_package(nested)
        crowded = self.package(name="cheio", files={f"n{i}.txt": "x" for i in range(25)})
        with self.assertRaisesRegex(SkillRejected, "demais"):
            self.skills.review_package(crowded)

    def test_path_traversal_and_links_are_refused(self):
        (self.root / "segredo.txt").write_text("conteudo fora do pacote", encoding="utf-8")
        for name in ("../segredo.txt", "..\\segredo.txt", "/etc/passwd", "C:\\Windows\\win.ini", "sub/x.md", "x.py", ""):
            manifest = {k: v for k, v in MANIFEST.items() if k != "instructions"} | {"instructions_file": name}
            with self.assertRaises(SkillRejected, msg=name):
                self.skills.review_package(self.package(manifest, name="t" + str(abs(hash(name)))))
        folder = self.package({k: v for k, v in MANIFEST.items() if k != "instructions"} | {"instructions_file": "falta.md"},
                              name="ausente")
        with self.assertRaises(SkillRejected):
            self.skills.review_package(folder)
        link = self.root / "elo"
        try:
            os.symlink(self.root / "segredo.txt", link)
        except (OSError, NotImplementedError):
            self.skipTest("sem permissao para criar links simbolicos neste ambiente")
        manifest = {k: v for k, v in MANIFEST.items() if k != "instructions"} | {"instructions_file": "leitura.md"}
        folder = self.package(manifest, name="comlink")
        os.symlink(self.root / "segredo.txt", folder / "leitura.md")
        with self.assertRaises(SkillRejected):
            self.skills.review_package(folder)

    def test_instructions_file_inside_the_package_is_accepted(self):
        manifest = {k: v for k, v in MANIFEST.items() if k != "instructions"} | {"instructions_file": "instrucoes.md"}
        folder = self.package(manifest, name="comarquivo", files={"instrucoes.md": "Passo a passo seguro."})
        self.assertEqual(self.skills.review_package(folder).definition["instructions"], "Passo a passo seguro.")


class UpgradeTests(SkillCase):
    def test_upgrade_with_new_permissions_needs_confirmation_and_never_widens_grants(self):
        v1 = self.install()
        grant = self.grant(v1)
        wider = copy.deepcopy(MANIFEST)
        wider.update(version="1.1.0", required_tools=[{"tool": "erp-demo.listar-cobrancas", "operations": ["read", "write"]}])
        folder = self.package(wider, name="v11")
        review = self.skills.review_package(folder)
        self.assertEqual(review.previous_version, "1.0.0")
        self.assertEqual(review.added_permissions, [{"tool": "erp-demo.listar-cobrancas", "operation": "write"}])
        with self.assertRaisesRegex(SkillRejected, "permissoes adicionais"):
            self.skills.install(self.op, folder, review.content_hash)
        v11 = self.skills.install(self.op, folder, review.content_hash, accept_new_permissions=True)
        self.assertNotEqual(v11["id"], v1["id"])
        stored = self.skills.list_grants(self.ca)[0]
        self.assertEqual((stored["id"], stored["skill_version"], stored["operations"]), (grant["id"], "1.0.0", ["read"]))
        with self.assertRaises(SkillRejected):  # the new version has no grant for an operation it just added
            self.grant(v11, operations=["admin"])
        self.assertEqual(self.skills.review_package(self.package(MANIFEST, name="v1b")).previous_version, "1.1.0")

    def test_version_rules(self):
        self.install()
        changed = {**MANIFEST, "instructions": "Outro texto"}
        folder = self.package(changed, name="mesmaversao")
        with self.assertRaisesRegex(SkillRejected, "outro conteudo"):
            self.skills.install(self.op, folder, self.skills.review_package(folder).content_hash)
        self.install({**MANIFEST, "version": "1.2.0"}, name="v12")
        older = self.package({**MANIFEST, "version": "1.1.0"}, name="v11antiga")
        with self.assertRaisesRegex(SkillRejected, "maior"):
            self.skills.install(self.op, older, self.skills.review_package(older).content_hash)

    def test_reducing_permissions_needs_no_confirmation(self):
        self.install()
        fewer = {**MANIFEST, "version": "1.1.0", "required_tools": []}
        self.assertEqual(self.skills.review_package(self.package(fewer, name="menos")).added_permissions, [])
        self.install(fewer, name="menos")


class GrantTests(SkillCase):
    def setUp(self):
        super().setUp()
        self.skill = self.install()

    def test_grant_pins_version_agent_connection_and_operations(self):
        grant = self.grant(self.skill, resource="cobrancas")
        self.assertEqual((grant["skill_version"], grant["agent_id"], grant["connection_id"], grant["operations"]),
                         ("1.0.0", self.agent["id"], self.conn["id"], ["read"]))
        with self.repo.transaction(self.a["id"]) as session:
            self.assertEqual([e["event"]["type"] for e in session.pending_outbox()][-1], "config.skill-granted")
        with self.assertRaisesRegex(SkillRejected, "ativa"):
            self.grant(self.skill, resource="cobrancas")

    def test_grant_validation(self):
        for kwargs, error in (
                ({"operations": ["write"]}, SkillRejected), ({"operations": []}, SkillRejected),
                ({"connection_id": None}, SkillRejected), ({"agent_id": new_id("agent_definition")}, NotFound),
                ({"skill_id": new_id("skill_definition")}, NotFound), ({"connection_id": new_id("connection")}, NotFound)):
            with self.assertRaises(error, msg=str(kwargs)):
                self.grant(self.skill, **kwargs)
        other_entry = self.connections.approve_mcp_server(
            self.op, catalog_key="outro-servidor", entry_version="1.0.0", transport="http",
            url="http://127.0.0.1:9100/mcp", tools=["listar-cobrancas"])
        wrong = self.connections.create_connection(self.ca, label="Outro", provider="outro", connection_kind="mcp",
                                                   catalog_entry_id=other_entry["id"])
        with self.assertRaisesRegex(SkillRejected, "ferramenta requerida"):
            self.grant(self.skill, connection_id=wrong["id"])
        plain = self.connections.create_connection(self.ca, label="Plano", provider="p", connection_kind="outro")
        with self.assertRaisesRegex(SkillRejected, "MCP aprovado"):
            self.grant(self.skill, connection_id=plain["id"])

    def test_other_company_resources_and_non_operators_are_refused(self):
        with self.assertRaises(NotFound):
            self.skills.grant_skill(self.cb, agent_id=self.agent["id"], skill_id=self.skill["id"],
                                    operations=["read"], connection_id=self.conn["id"])
        agent_ctx = None
        with self.assertRaises(PermissionDenied):
            self.skills.grant_skill(agent_ctx, agent_id=self.agent["id"], skill_id=self.skill["id"], operations=["read"])
        self.assertEqual(self.skills.list_grants(self.cb), [])

    def test_attach_publishes_a_new_agent_version_with_the_grant(self):
        grant = self.grant(self.skill, attach=True)
        current = self.agents.get_agent(self.ca, self.agent["id"])
        self.assertEqual((current["agent_version"], current["skill_grant_ids"]), (2, [grant["id"]]))
        self.assertEqual(self.agents.get_version(self.ca, self.agent["id"], 1)["definition"]["skill_grant_ids"], [])


class UseTests(SkillCase):
    def setUp(self):
        super().setUp()
        self.skill = self.install()
        self.grant_doc = self.grant(self.skill, attach=True)

    def use(self, operation="read", **kwargs):
        return self.skills.authorize_use(self.ca, self.agent["id"], "teste.consultar", operation,
                                         connection_id=self.conn["id"], **kwargs)

    def deny(self, **kwargs):
        with self.assertRaises(SkillDenied) as caught:
            self.skills.authorize_use(self.ca, kwargs.pop("agent", self.agent["id"]), kwargs.pop("key", "teste.consultar"),
                                      kwargs.pop("operation", "read"), **kwargs)
        return str(caught.exception)

    def test_granted_use_is_allowed_and_everything_else_is_denied_alike(self):
        self.assertEqual(self.use()["id"], self.grant_doc["id"])
        messages = {self.deny(connection_id=self.conn["id"], operation="write"),
                    self.deny(connection_id=self.conn["id"], key="outra.skill"),
                    self.deny(connection_id=new_id("connection")),
                    self.deny(connection_id=self.conn["id"], agent=new_id("agent_definition")),
                    self.deny(connection_id=self.conn["id"], operation="send")}
        self.assertEqual(messages, {"skill sem concessao valida"})
        with self.assertRaises(PermissionDenied):
            self.skills.authorize_use(None, self.agent["id"], "teste.consultar", "read")

    def test_skill_without_a_grant_is_refused(self):
        other = self.install({**MANIFEST, "id": "teste.sem-concessao"}, name="outra")
        self.assertIn("sem concessao", self.deny(key="teste.sem-concessao", connection_id=self.conn["id"]))
        self.assertEqual(other["status"], "ativo")

    def test_revoking_the_grant_blocks_new_calls_at_once_even_for_pinned_versions(self):
        self.skills.revoke_grant(self.ca, self.grant_doc["id"])
        self.assertIn("sem concessao", self.deny(connection_id=self.conn["id"]))
        self.assertIn("sem concessao", self.deny(connection_id=self.conn["id"], agent_version=2))
        self.assertTrue(self.skills.revoke_grant(self.ca, self.grant_doc["id"])["revoked_at"])  # idempotent

    def test_deactivating_skill_connection_agent_or_expiry_blocks_use(self):
        self.skills.set_skill_status(self.op, self.skill["id"], "inativo")
        self.assertIn("sem concessao", self.deny(connection_id=self.conn["id"]))
        self.skills.set_skill_status(self.op, self.skill["id"], "ativo")
        self.assertTrue(self.use())
        revision = self.connections.get_connection(self.ca, self.conn["id"])["revision"]
        self.connections.update_connection(self.ca, self.conn["id"], revision, status="inativo")
        self.assertIn("sem concessao", self.deny(connection_id=self.conn["id"]))
        with self.assertRaises(SkillRejected):
            self.grant(self.skill, resource="novo")  # cannot grant over an inactive connection

    def test_expired_grants_do_not_authorize(self):
        second = self.install({**MANIFEST, "id": "teste.expira"}, name="expira")
        expired = self.skills.grant_skill(self.ca, agent_id=self.agent["id"], skill_id=second["id"], operations=["read"],
                                          connection_id=self.conn["id"], expires_at="2020-01-01T00:00:00Z", attach=True)
        self.assertEqual(expired["expires_at"], "2020-01-01T00:00:00Z")
        self.assertIn("sem concessao", self.deny(key="teste.expira", connection_id=self.conn["id"]))

    def test_resource_scope_and_pinned_agent_version(self):
        scoped = self.install({**MANIFEST, "id": "teste.recurso"}, name="recurso")
        self.skills.grant_skill(self.ca, agent_id=self.agent["id"], skill_id=scoped["id"], operations=["read"],
                                connection_id=self.conn["id"], resource="cobrancas", attach=True)
        self.assertTrue(self.skills.authorize_use(self.ca, self.agent["id"], "teste.recurso", "read",
                                                  connection_id=self.conn["id"], resource="cobrancas"))
        self.assertIn("sem concessao", self.deny(key="teste.recurso", connection_id=self.conn["id"], resource="outro"))
        self.assertIn("sem concessao", self.deny(key="teste.recurso", connection_id=self.conn["id"]))
        # version 1 of the agent predates every grant: a run pinned to it cannot use the skill
        self.assertIn("sem concessao", self.deny(connection_id=self.conn["id"], agent_version=1))
        self.assertTrue(self.skills.authorize_use(self.ca, self.agent["id"], "teste.consultar", "read",
                                                  connection_id=self.conn["id"], agent_version=2))

    def test_other_company_cannot_use_the_grant(self):
        with self.assertRaises(SkillDenied):
            self.skills.authorize_use(self.cb, self.agent["id"], "teste.consultar", "read", connection_id=self.conn["id"])


if __name__ == "__main__":
    unittest.main()
