import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from team import persistence
from team.db import MigrationError, connect, discover, migrate
from team.ids import new_id
from team.repository import (
    TABLES, IntegrityViolation, RepositoryError, RevisionConflict, SqliteRepository, TenantMismatch, indexed_columns)
from team.samples import IDS, NOW, bundle

ORDER = ["tenant", "skill_definition", "mcp_catalog_entry", "department", "credential", "connection",
         "model_policy", "capability", "agent_definition", "agent_profile_version", "skill_grant", "case", "task", "agent_run", "approval_request", "handoff", "evidence", "case_entry", "triage_decision", "outbound_message", "knowledge_document", "memory_item", "monitored_asset", "financial_record", "email_message", "mailbox_cursor", "schedule", "notification", "oauth_app", "oauth_grant", "capability_gap",
         "contact", "contact_address", "contact_link", "access_grant"]


def sample(kind):
    return [copy.deepcopy(d) for d in bundle() if d["kind"] == kind]


def event(case_id, revision, tenant=IDS["tenant"]):
    item = copy.deepcopy(sample("event")[0])
    item.update(id=new_id("event"), case_id=case_id, revision=revision, tenant_id=tenant)
    return item


class SampleLoader:
    """Loads the sample company into ``self.repo``; shared by every backend's tests."""

    def load_sample(self):
        self.repo.create_tenant(sample("tenant")[0])
        self.repo.save_global(sample("skill_definition")[0])
        self.repo.save_global(sample("mcp_catalog_entry")[0])
        with self.repo.transaction(IDS["tenant"]) as session:
            for kind in ORDER[3:]:
                for doc in sample(kind):
                    session.save(doc)


class RepoCase(SampleLoader, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "team-data" / "team.sqlite3"
        self.repo = SqliteRepository(self.path)
        self.repo.migrate()


class MigrationTests(RepoCase):
    def test_fresh_install_then_idempotent(self):
        self.assertEqual(migrate(self.path), [])
        conn = connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({*TABLES.values(), "events", "outbox", "commands", "schema_migrations"} <= names)

    def test_indexed_columns_exist_in_every_table(self):
        conn = connect(self.path)
        self.addCleanup(conn.close)
        for kind, table in TABLES.items():
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            self.assertTrue(set(indexed_columns(kind)) <= columns, kind)

    def test_failed_migration_preserves_database_and_leaves_backup(self):
        folder = Path(self.temp.name) / "migrations"
        folder.mkdir()
        for item in discover():
            (folder / f"{item[0]:04d}_{item[1]}.sql").write_text(item[2], encoding="utf-8")
        last = len(discover())
        broken = f"{last + 1:04d}_quebrada"
        (folder / f"{broken}.sql").write_text("CREATE TABLE parcial (x); INSERT INTO inexistente VALUES (1);", encoding="utf-8")
        self.load_sample()
        with self.assertRaisesRegex(MigrationError, f"{broken}.*preservado"):
            migrate(self.path, folder)
        conn = connect(self.path)
        self.addCleanup(conn.close)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("parcial", tables)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], last)
        self.assertTrue(self.path.with_name(self.path.name + f".pre-migracao-{last:04d}.bak").exists())

    def test_upgrade_from_previous_version_keeps_data_and_backup(self):
        old = Path(self.temp.name) / "old"
        old.mkdir()
        for item in discover()[:1]:
            (old / f"{item[0]:04d}_{item[1]}.sql").write_text(item[2], encoding="utf-8")
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        self.assertEqual(migrate(legacy, old), [1])
        conn = connect(legacy)
        conn.execute("INSERT INTO tenants VALUES ('ten_x', 1, 'a', 'a', '{}')")
        conn.close()
        self.assertEqual(migrate(legacy), list(range(2, len(discover()) + 1)))
        conn = connect(legacy)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)
        self.assertTrue(legacy.with_name("legacy.sqlite3.pre-migracao-0001.bak").exists())

    def test_corrupt_file_is_not_modified_or_replaced(self):
        broken = Path(self.temp.name) / "ruim.sqlite3"
        broken.write_bytes(b"isto nao e um banco sqlite" * 50)
        before = broken.read_bytes()
        with self.assertRaisesRegex(MigrationError, "nao foi alterado"):
            migrate(broken)
        self.assertEqual(broken.read_bytes(), before)

    def test_tampered_or_newer_database_is_refused(self):
        conn = connect(self.path)
        self.addCleanup(conn.close)
        original = conn.execute("SELECT checksum FROM schema_migrations WHERE version = 1").fetchone()[0]
        conn.execute("UPDATE schema_migrations SET checksum = 'x' WHERE version = 1")
        with self.assertRaisesRegex(MigrationError, "alterada"):
            migrate(self.path)
        conn.execute("UPDATE schema_migrations SET checksum = ? WHERE version = 1", (original,))
        conn.execute("INSERT INTO schema_migrations VALUES (99, 'futura', 'x', 'x')")
        with self.assertRaisesRegex(MigrationError, "desconhecida"):
            migrate(self.path)

    def test_migration_numbering_must_be_contiguous(self):
        folder = Path(self.temp.name) / "gap"
        folder.mkdir()
        (folder / "0002_pulo.sql").write_text("SELECT 1;", encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "lacunas"):
            discover(folder)


class RepositoryContract:
    """The repository contracts. Every backend runs these same tests (SQLite here, PostgreSQL in test_team_repository_pg.py)."""

    def test_roundtrip_survives_restart_and_lists_by_index(self):
        self.load_sample()
        reopened = self.restart()
        with reopened.transaction(IDS["tenant"]) as session:
            self.assertEqual(session.get("case", IDS["case"]), sample("case")[0])
            self.assertEqual(len(session.list("agent_definition")), 2)
            self.assertEqual([t["id"] for t in session.list("task", case_id=IDS["case"])], [IDS["task"]])
            with self.assertRaises(RepositoryError):
                session.list("task", objective="x")  # not an indexed column
        self.assertEqual(reopened.get_global("tenant", IDS["tenant"])["name"], "Empresa Exemplo")

    def test_revision_control(self):
        self.load_sample()
        case = sample("case")[0]
        with self.repo.transaction(IDS["tenant"]) as session:
            with self.assertRaises(RevisionConflict):
                session.save(case)  # revision 1 again
            case.update(revision=2, state="aguardando_cliente")
            session.save(case)
            case["revision"] = 4
            with self.assertRaises(RevisionConflict):
                session.save(case)
        with self.repo.transaction(IDS["tenant"]) as session:
            self.assertEqual(session.get("case", IDS["case"])["state"], "aguardando_cliente")
            self.assertEqual(len(session.list("case", state="aguardando_cliente")), 1)

    def test_tenant_context_is_required_and_isolates_data(self):
        self.load_sample()
        other = dict(sample("tenant")[0], id=new_id("tenant"), name="Outra")
        self.repo.create_tenant(other)
        for bad in (None, "", new_id("tenant")):
            with self.assertRaises(TenantMismatch):
                self.repo.transaction(bad).__enter__()
        with self.repo.transaction(other["id"]) as session:
            self.assertIsNone(session.get("case", IDS["case"]))
            self.assertEqual(session.list("case"), [])
            with self.assertRaises(TenantMismatch):
                session.save(sample("department")[0])  # document of another tenant
            with self.assertRaises(IntegrityViolation):  # same-tenant FK blocks borrowed parents
                foreign = dict(sample("agent_definition")[0], id=new_id("agent_definition"), tenant_id=other["id"])
                session.save(foreign)

    def test_state_event_and_outbox_commit_together_or_not_at_all(self):
        self.load_sample()
        case = dict(sample("case")[0], revision=2, state="pausada")
        with self.repo.transaction(IDS["tenant"]) as session:
            session.save(case, event=event(IDS["case"], 1))
        with self.repo.transaction(IDS["tenant"]) as session:
            pending = session.pending_outbox()
            self.assertEqual(len(pending), 1)
            self.assertEqual(session.get("case", IDS["case"])["state"], "pausada")
        failing = dict(case, revision=3, state="em_execucao")
        with self.assertRaises(RevisionConflict):
            with self.repo.transaction(IDS["tenant"]) as session:
                session.save(failing, event=event(IDS["case"], 1))  # duplicate event revision
        with self.repo.transaction(IDS["tenant"]) as session:
            self.assertEqual(session.get("case", IDS["case"])["state"], "pausada")  # rolled back
            self.assertEqual(len(session.pending_outbox()), 1)
            session.mark_dispatched([pending[0]["id"]])
        with self.repo.transaction(IDS["tenant"]) as session:
            self.assertEqual(session.pending_outbox(), [])
            self.assertEqual(len(session.events(IDS["case"])), 1)

    def test_exception_rolls_back_every_write(self):
        self.load_sample()
        with self.assertRaises(ZeroDivisionError):
            with self.repo.transaction(IDS["tenant"]) as session:
                session.save(dict(sample("case")[0], revision=2, state="falhou"))
                1 / 0
        with self.repo.transaction(IDS["tenant"]) as session:
            self.assertEqual(session.get("case", IDS["case"])["state"], "em_execucao")

    def test_command_idempotency(self):
        self.load_sample()
        with self.repo.transaction(IDS["tenant"]) as session:
            first = sample("command")[0]
            self.assertTrue(session.register_command(first))
            self.assertFalse(session.register_command(dict(first, id=new_id("command"))))  # same key

    def test_events_require_existing_case(self):
        self.load_sample()
        with self.repo.transaction(IDS["tenant"]) as session:
            with self.assertRaises(IntegrityViolation):
                session.record_event(event(new_id("case"), 1))

    def test_two_concurrent_writers_lose_no_event(self):
        self.load_sample()
        errors, per_writer = [], 15

        def writer():
            try:
                for _ in range(per_writer):
                    while True:
                        try:
                            with self.repo.transaction(IDS["tenant"]) as session:
                                revision = len(session.events(IDS["case"], limit=1000)) + 1
                                session.record_event(event(IDS["case"], revision))
                            break
                        except RevisionConflict:
                            continue
            except Exception as error:  # surfaced below
                errors.append(error)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(errors, [])
        with self.repo.transaction(IDS["tenant"]) as session:
            stored = session.events(IDS["case"], limit=1000)
            self.assertEqual([e["revision"] for _, e in stored], list(range(1, 2 * per_writer + 1)))
            self.assertEqual(len(session.pending_outbox(limit=1000)), 2 * per_writer)


class RepositoryTests(RepositoryContract, RepoCase):
    def restart(self):
        return SqliteRepository(self.path)


class PersistenceSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_default_off_and_enable_disable_keep_database(self):
        status = persistence.status(self.root)
        self.assertFalse(status["enabled"])
        self.assertFalse((self.root / "team-data").exists())  # nothing is created until opted in
        status = persistence.enable(self.root)
        self.assertTrue(status["enabled"])
        self.assertIn("não são importadas", status["notice"])
        database = persistence.data_dir(self.root) / persistence.DATABASE
        self.assertTrue(database.exists())
        self.assertFalse(persistence.disable(self.root)["enabled"])
        self.assertTrue(database.exists())  # disabling never deletes data

    def test_failed_migration_keeps_switch_off_with_diagnostic(self):
        folder = persistence.data_dir(self.root)
        folder.mkdir()
        garbage = folder / persistence.DATABASE
        garbage.write_bytes(b"lixo" * 100)
        status = persistence.enable(self.root)
        self.assertFalse(status["enabled"])
        self.assertIn("nao foi alterado", status["error"])
        self.assertEqual(garbage.read_bytes(), b"lixo" * 100)
        self.assertFalse(persistence.status(self.root)["enabled"])

    def test_corrupt_flag_means_disabled(self):
        folder = persistence.data_dir(self.root)
        folder.mkdir()
        (folder / persistence.FLAG).write_text("{", encoding="utf-8")
        self.assertFalse(persistence.status(self.root)["enabled"])


if __name__ == "__main__":
    unittest.main()
