import json
import logging
import tempfile
import unittest
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.connections import (
    ConnectionRejected, ConnectionService, ConnectorFailed, CredentialError, LauncherRegistry)
from team.ids import new_id
from team.repository import IntegrityViolation, SqliteRepository
from team.samples import bundle, profile_version
from team.schema import ContractError
from team.service import TeamService
from team.vault import LegacyElevenLabsAdapter, MemoryVault, VaultError, WindowsVault

SECRET = "sk-test-SEGREDO-0123456789abcdef"
NEW_SECRET = "sk-test-OUTRO-9876543210fedcba"


class FakeLegacy:
    """Stands in for voice_profiles.WindowsSecrets; exposes get() only, like the adapter."""

    def __init__(self, key="eleven-legacy-key-123"):
        self.key = key

    def get(self):
        return self.key


class ConnectionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = SqliteRepository(root / "team.sqlite3")
        self.repo.migrate()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.vault = MemoryVault()
        self.legacy = FakeLegacy()
        self.launchers = LauncherRegistry(root / "launchers.json", self.access.operator_id)
        self.service = ConnectionService(self.repo, self.access, self.vault, self.launchers,
                                         LegacyElevenLabsAdapter(self.legacy))
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.entry = self.service.approve_mcp_server(
            self.op, catalog_key="erp", entry_version="1.0.0", transport="http",
            url="http://127.0.0.1:9000/mcp", tools=["listar-cobrancas"])
        self.conn = self.service.create_connection(
            self.ca, label="ERP A", provider="erp", connection_kind="mcp", catalog_entry_id=self.entry["id"],
            scopes=["cobrancas.read"])
        self.service.register_connector("erp")

    def _agent_context(self, grant_connection=False):
        from team.samples import bundle as sample_bundle
        agent_doc = dict(next(d for d in sample_bundle() if d["kind"] == "agent_definition"),
                         id=new_id("agent_definition"), tenant_id=self.a["id"], skill_grant_ids=[], handoff_target_ids=[])
        department = self.team.create(self.ca, "department", name="Fin", scope="x", status="ativo")
        agent_doc["department_id"] = department["id"]
        run_doc = dict(next(d for d in sample_bundle() if d["kind"] == "agent_run"), id=new_id("agent_run"),
                       tenant_id=self.a["id"], agent_id=agent_doc["id"], department_id=department["id"],
                       case_id=None, task_id=None, state="em_execucao")
        with self.repo.transaction(self.a["id"]) as session:
            session.save(agent_doc)
            session.save(profile_version(agent_doc, new_id("agent_profile_version")))
            session.save(run_doc)
        if grant_connection:
            self.team.create(self.ca, "access_grant", principal_id=agent_doc["id"], resource_kind="connection",
                             resource_id=self.conn["id"], actions=["read"], granted_by=self.op.principal_id,
                             expires_at=None)
        return self.access.agent_context(self.op, self.a["id"], run_doc["id"])


class CatalogTests(ConnectionCase):
    def test_mcp_connection_takes_transport_and_origin_from_the_approved_catalog(self):
        self.assertEqual((self.conn["transport"], self.conn["endpoint"]), ("http", "http://127.0.0.1:9000/mcp"))
        self.assertEqual(self.conn["scopes"], ["cobrancas.read"])
        self.assertNotIn("credential_ref", self.conn)
        for kwargs in ({"endpoint": "http://evil.example/mcp"}, {"transport": "stdio"}):
            with self.assertRaises(ConnectionRejected):
                self.service.create_connection(self.ca, label="X", provider="erp", connection_kind="mcp",
                                               catalog_entry_id=self.entry["id"], **kwargs)
        with self.assertRaises(ConnectionRejected):  # unknown / not approved
            self.service.create_connection(self.ca, label="X", provider="erp", connection_kind="mcp",
                                           catalog_entry_id=new_id("mcp_catalog_entry"))
        with self.assertRaises(ConnectionRejected):  # non-MCP must not borrow the catalog
            self.service.create_connection(self.ca, label="X", provider="erp", connection_kind="imap",
                                           catalog_entry_id=self.entry["id"])

    def test_inactive_entries_cannot_back_new_connections(self):
        self.service.set_mcp_status(self.op, self.entry["id"], 1, "inativo")
        with self.assertRaises(ConnectionRejected):
            self.service.create_connection(self.ca, label="Y", provider="erp", connection_kind="mcp",
                                           catalog_entry_id=self.entry["id"])
        self.assertEqual(len(self.service.list_mcp_catalog(self.op)), 1)

    def test_stdio_only_accepts_operator_registered_launchers(self):
        for suggestion in ("powershell -c calc", "no-such-launcher", None):
            with self.assertRaises((ConnectionRejected, ContractError)):
                self.service.approve_mcp_server(self.op, catalog_key="local", entry_version="1.0.0",
                                                transport="stdio", launcher=suggestion)
        with self.assertRaises(ConnectionRejected):
            self.launchers.register(self.op, "mail", ["relative.exe"])
        with self.assertRaises(ConnectionRejected):
            self.launchers.register(self.op, "mail", "C:\\tools\\mail.exe --flag")
        self.launchers.register(self.op, "mail", ["C:\\tools\\mail.exe", "--stdio"])
        entry = self.service.approve_mcp_server(self.op, catalog_key="local", entry_version="1.0.0",
                                                transport="stdio", launcher="mail", tools=["ler"])
        self.assertEqual(self.service.stdio_command(entry["id"]), ["C:\\tools\\mail.exe", "--stdio"])
        self.assertNotIn("argv", json.dumps(entry))  # the catalog stores the slug, not the command line
        self.service.set_mcp_status(self.op, entry["id"], 1, "inativo")
        self.assertIsNone(self.service.stdio_command(entry["id"]))

    def test_catalog_requires_the_trusted_operator(self):
        for forged in (self.ca, None, {"principal_id": self.access.operator_id}):
            with self.assertRaises(PermissionDenied):
                self.service.approve_mcp_server(forged, catalog_key="x", entry_version="1.0.0",
                                                transport="http", url="http://127.0.0.1:1/mcp")
        with self.assertRaises(PermissionDenied):
            self.launchers.register(self.ca, "mail", ["C:\\tools\\mail.exe"])
        other = AccessService(self.repo, new_id("user")).local_operator()
        with self.assertRaises(PermissionDenied):
            self.service.approve_mcp_server(other, catalog_key="x", entry_version="1.0.0",
                                            transport="http", url="http://127.0.0.1:1/mcp")
        with self.assertRaises(ConnectionRejected):  # same key and version twice
            self.service.approve_mcp_server(self.op, catalog_key="erp", entry_version="1.0.0",
                                            transport="http", url="http://127.0.0.1:9000/mcp")

    def test_old_config_imports_as_inactive_entries_awaiting_review(self):
        config = {"mcp_servers": [{"url": "http://127.0.0.1:8000/mcp"}, {"url": "http://127.0.0.1:8001/mcp"}]}
        created = self.service.import_legacy_mcp(self.op, config)
        self.assertEqual([c["status"] for c in created], ["inativo", "inativo"])
        self.assertEqual(self.service.import_legacy_mcp(self.op, config), [])  # idempotent
        with self.assertRaises(ConnectionRejected):  # default deny until the operator activates it
            self.service.create_connection(self.ca, label="Legado", provider="legado", connection_kind="mcp",
                                           catalog_entry_id=created[0]["id"])


class CredentialLifecycleTests(ConnectionCase):
    def test_save_replace_revoke_remove_return_only_state(self):
        self.assertEqual(self.service.credential_status(self.ca, self.conn["id"]), {"state": "ausente", "available": False})
        view = self.service.set_credential(self.ca, self.conn["id"], SECRET)
        self.assertEqual(view["credential"]["state"], "ativa")
        self.assertEqual(view["credential"]["store"], "cofre-local")
        with self.repo.transaction(self.a["id"]) as session:
            credential_id = session.get("connection", self.conn["id"])["credential_ref"]
        self.assertEqual(self.vault.get(self.a["id"], credential_id), SECRET)
        status = self.service.credential_status(self.ca, self.conn["id"])
        self.assertTrue(status["available"])
        replaced = self.service.set_credential(self.ca, self.conn["id"], NEW_SECRET)
        self.assertEqual(self.vault.get(self.a["id"], credential_id), NEW_SECRET)
        self.assertEqual(replaced["revision"], view["revision"])  # same credential id, connection untouched
        revoked = self.service.revoke_credential(self.ca, self.conn["id"])
        self.assertEqual(revoked["credential"]["state"], "revogada")
        self.assertIsNone(self.vault.get(self.a["id"], credential_id))  # value wiped
        self.assertFalse(self.service.credential_status(self.ca, self.conn["id"])["available"])
        with self.assertRaises(CredentialError):
            self.service.use_credential(self.ca, self.conn["id"], "erp", lambda s: s)
        self.service.set_credential(self.ca, self.conn["id"], SECRET)  # re-activates
        self.assertTrue(self.service.credential_status(self.ca, self.conn["id"])["available"])
        removed = self.service.remove_credential(self.ca, self.conn["id"])
        self.assertIsNone(removed["credential"])
        self.assertEqual(self.vault.items, {})
        with self.assertRaises(ConnectionRejected):
            self.service.remove_credential(self.ca, self.conn["id"])
        for output in (view, replaced, revoked, removed, status, self.service.list_connections(self.ca)):
            self.assertNotIn("SEGREDO", json.dumps(output))
            self.assertNotIn("OUTRO", json.dumps(output))

    def test_secrets_never_enter_documents_or_the_database(self):
        self.service.set_credential(self.ca, self.conn["id"], SECRET)
        raw = (Path(self.temp.name) / "team.sqlite3").read_bytes()
        self.assertNotIn(b"SEGREDO", raw)

    def test_bad_input_and_unavailable_vault_fail_with_fixed_messages(self):
        for bad in ("", " espaco ", "linha\nquebrada", "x" * 5000, None, 123):
            with self.assertRaises(CredentialError) as caught:
                self.service.set_credential(self.ca, self.conn["id"], bad)
            self.assertNotIn("xxxx", str(caught.exception))
        self.vault.broken = True
        with self.assertRaises(CredentialError) as caught:
            self.service.set_credential(self.ca, self.conn["id"], SECRET)
        self.assertEqual(str(caught.exception), "Cofre de credenciais indisponivel.")
        self.vault.broken = False
        self.assertEqual(self.service.get_connection(self.ca, self.conn["id"])["credential"], None)  # nothing half-saved

    def test_failure_after_vault_write_leaves_no_orphan_secret(self):
        original = self.repo.transaction

        def failing(tenant_id):
            if self.vault.items:  # the secret is already stored: fail the metadata transaction
                raise RuntimeError("falha simulada do banco")
            return original(tenant_id)
        self.repo.transaction = failing
        try:
            with self.assertRaises(CredentialError):
                self.service.set_credential(self.ca, self.conn["id"], SECRET)
        finally:
            self.repo.transaction = original
        self.assertEqual(self.vault.items, {})

    def test_only_the_operator_manages_credentials(self):
        agent = self._agent_context()
        for action in (lambda: self.service.set_credential(agent, self.conn["id"], SECRET),
                       lambda: self.service.revoke_credential(agent, self.conn["id"]),
                       lambda: self.service.remove_credential(agent, self.conn["id"]),
                       lambda: self.service.credential_status(agent, self.conn["id"]),
                       lambda: self.service.create_connection(agent, label="X", provider="erp", connection_kind="outro")):
            with self.assertRaises(PermissionDenied):
                action()


class IsolationAndResolutionTests(ConnectionCase):
    def test_other_company_cannot_see_use_or_change_the_connection(self):
        self.service.set_credential(self.ca, self.conn["id"], SECRET)
        for action in (lambda: self.service.get_connection(self.cb, self.conn["id"]),
                       lambda: self.service.set_credential(self.cb, self.conn["id"], NEW_SECRET),
                       lambda: self.service.revoke_credential(self.cb, self.conn["id"]),
                       lambda: self.service.remove_credential(self.cb, self.conn["id"]),
                       lambda: self.service.update_connection(self.cb, self.conn["id"], 1, label="x"),
                       lambda: self.service.use_credential(self.cb, self.conn["id"], "erp", lambda s: s)):
            with self.assertRaises(NotFound):
                action()
        self.assertEqual(self.service.list_connections(self.cb), [])
        self.assertTrue(all(key[0] == self.a["id"] for key in self.vault.items))  # vault keys are per company

    def test_database_blocks_a_credential_from_another_company(self):
        self.service.set_credential(self.ca, self.conn["id"], SECRET)
        with self.repo.transaction(self.a["id"]) as session:
            credential_id = session.get("connection", self.conn["id"])["credential_ref"]
        foreign = next(d for d in bundle() if d["kind"] == "connection")
        foreign = dict(foreign, id=new_id("connection"), tenant_id=self.b["id"], credential_ref=credential_id,
                       catalog_entry_id=None, transport="http", connection_kind="outro")
        with self.assertRaises(IntegrityViolation):
            with self.repo.transaction(self.b["id"]) as session:
                session.save(foreign)

    def test_secret_is_resolved_only_inside_an_authorized_connector(self):
        self.service.set_credential(self.ca, self.conn["id"], SECRET)
        self.assertEqual(self.service.use_credential(self.ca, self.conn["id"], "erp", lambda s: len(s)), len(SECRET))
        with self.assertRaises(PermissionDenied):  # unregistered connector name
            self.service.use_credential(self.ca, self.conn["id"], "outro", lambda s: s)
        self.service.register_connector("outro")
        with self.assertRaises(PermissionDenied):  # registered, but not this connection's provider
            self.service.use_credential(self.ca, self.conn["id"], "outro", lambda s: s)
        with self.assertRaises(ConnectorFailed) as caught:
            self.service.use_credential(self.ca, self.conn["id"], "erp", lambda s: (_ for _ in ()).throw(RuntimeError(s)))
        self.assertNotIn("SEGREDO", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        with self.assertRaises(ConnectorFailed):  # a response that echoes the credential is blocked
            self.service.use_credential(self.ca, self.conn["id"], "erp", lambda s: {"auth": f"Bearer {s}"})
        revision = self.service.get_connection(self.ca, self.conn["id"])["revision"]
        self.service.update_connection(self.ca, self.conn["id"], revision, status="inativo")
        with self.assertRaises(CredentialError):
            self.service.use_credential(self.ca, self.conn["id"], "erp", lambda s: s)

    def test_agents_need_an_explicit_grant_and_still_never_receive_the_secret(self):
        self.service.set_credential(self.ca, self.conn["id"], SECRET)
        agent = self._agent_context()
        with self.assertRaises(NotFound):
            self.service.use_credential(agent, self.conn["id"], "erp", lambda s: s)
        self.assertEqual(self.service.list_connections(agent), [])
        granted = self._agent_context(grant_connection=True)
        self.assertEqual(self.service.use_credential(granted, self.conn["id"], "erp", lambda s: "ok"), "ok")
        view = self.service.get_connection(granted, self.conn["id"])
        self.assertNotIn("SEGREDO", json.dumps(view))

    def test_no_secret_in_logs_or_error_text(self):
        handler = logging.Handler()
        records = []
        handler.emit = records.append
        logger = logging.getLogger()
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, previous)
        self.service.set_credential(self.ca, self.conn["id"], SECRET)
        try:
            self.service.use_credential(self.ca, self.conn["id"], "erp", lambda s: 1 / 0)
        except ConnectorFailed as error:
            records.append(str(error))
        self.service.revoke_credential(self.ca, self.conn["id"])
        self.assertFalse(any("SEGREDO" in str(getattr(r, "msg", r)) or "SEGREDO" in str(getattr(r, "args", ""))
                             for r in records))

    def test_inline_secret_in_a_connection_document_is_rejected(self):
        with self.assertRaises(ContractError):
            self.service.create_connection(self.ca, label="usa " + SECRET, provider="erp", connection_kind="outro")
        with self.assertRaises(ContractError):
            self.service.create_connection(self.ca, label="X", provider="erp", connection_kind="outro",
                                           endpoint="https://user:senha@host.example/api")


class ElevenLabsReuseTests(ConnectionCase):
    def test_existing_voice_key_is_reused_without_copying_or_breaking_voice(self):
        view = self.service.adopt_elevenlabs(self.ca)
        self.assertEqual((view["provider"], view["credential"]["store"]), ("elevenlabs", "elevenlabs-legado"))
        self.assertEqual(self.vault.items, {})  # nothing was copied
        self.assertEqual(self.service.adopt_elevenlabs(self.ca)["id"], view["id"])  # idempotent
        self.assertTrue(self.service.credential_status(self.ca, view["id"])["available"])
        self.service.register_connector("elevenlabs")
        self.assertEqual(self.service.use_credential(self.ca, view["id"], "elevenlabs", lambda s: s[:3]), "ele")
        with self.assertRaises(ConnectionRejected):  # managed by the voice settings
            self.service.set_credential(self.ca, view["id"], NEW_SECRET)
        self.service.revoke_credential(self.ca, view["id"])
        with self.assertRaises(CredentialError):
            self.service.use_credential(self.ca, view["id"], "elevenlabs", lambda s: s)
        self.assertEqual(self.legacy.get(), "eleven-legacy-key-123")  # voice profiles keep their key
        self.service.remove_credential(self.ca, view["id"])
        self.assertEqual(self.legacy.get(), "eleven-legacy-key-123")

    def test_nothing_to_adopt_without_a_configured_key(self):
        self.legacy.key = None
        with self.assertRaises(ConnectionRejected):
            self.service.adopt_elevenlabs(self.ca)
        bare = ConnectionService(self.repo, self.access, self.vault, self.launchers)
        with self.assertRaises(ConnectionRejected):
            bare.adopt_elevenlabs(self.ca)


class VaultTests(unittest.TestCase):
    def test_windows_vault_names_are_scoped_and_errors_are_sanitized(self):
        vault = WindowsVault(Path("."))
        target = vault._target("ten_a", "crd_b")
        self.assertTrue(target.startswith("Assistente/") and target.endswith("/team/ten_a/crd_b"))
        self.assertNotEqual(vault._target("ten_a", "crd_b"), vault._target("ten_c", "crd_b"))
        try:
            import win32cred  # noqa: F401
        except ImportError:  # CI: the platform module is missing, so the failure path is exercised
            with self.assertRaises(VaultError) as caught:
                vault.put("ten_a", "crd_b", "valor-secreto-123")
            self.assertNotIn("valor-secreto", str(caught.exception))
        else:
            self.skipTest("pywin32 instalado: use scripts/smoke_vault.py para o teste local descartavel")


if __name__ == "__main__":
    unittest.main()
