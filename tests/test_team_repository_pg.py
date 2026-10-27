import copy
import unittest

from team.db import MigrationError, discover
from team.ids import new_id
from team.repository import IntegrityViolation
from team.samples import IDS
from team.pgdb import DatabaseSaturated, DatabaseUnavailable, PostgresRepository, translate_sql
from tests.pg_support import PostgresSchema, psycopg, requires_postgres
from tests.test_team_repository import RepositoryContract, SampleLoader, sample


class PostgresCase(SampleLoader, unittest.TestCase):
    def setUp(self):
        self.schema = PostgresSchema()
        self.addCleanup(self.schema.drop)
        self.repo = PostgresRepository(self.schema.dsn)
        self.repo.migrate()

    def restart(self):
        return PostgresRepository(self.schema.dsn)


@requires_postgres
class PostgresRepositoryTests(RepositoryContract, PostgresCase):
    """The very same contract tests as SQLite: round trip, revisions, isolation, atomic state+event+outbox, rollback, commands."""


class SqlTranslationTests(unittest.TestCase):
    def test_placeholders_and_percent_signs(self):
        self.assertEqual(translate_sql("SELECT a FROM t WHERE x = ? AND y LIKE 'a%b?' AND z = ?"), "SELECT a FROM t WHERE x = %s AND y LIKE 'a%%b?' AND z = %s")
        self.assertEqual(translate_sql("SELECT 100 % 7"), "SELECT 100 %% 7")
        self.assertEqual(translate_sql('SELECT "a?b" FROM t WHERE c = ?'), 'SELECT "a?b" FROM t WHERE c = %s')


class WithoutServerTests(unittest.TestCase):
    def test_a_missing_or_unreachable_database_is_a_clear_error_that_never_shows_the_connection_string(self):
        with self.assertRaises(DatabaseUnavailable):
            PostgresRepository("")
        secret = "postgresql://usuario:senha-secreta-123@127.0.0.1:1/banco"
        try:
            PostgresRepository(secret).migrate()
        except DatabaseUnavailable as error:
            self.assertNotIn("senha-secreta-123", str(error) + repr(error))
            self.assertNotIn("senha-secreta-123", repr(PostgresRepository(secret)))
        else:
            self.fail("should not connect")
        self.assertFalse(PostgresRepository(secret).ping())


@requires_postgres
class SchemaParityTests(PostgresCase):
    """The PostgreSQL schema is the SQLite one, translated: same tables, columns, explicit indexes and guard triggers."""
    FTS = ("knowledge_index",)           # SQLite's FTS5 virtual table (and its shadow tables)
    PG_ONLY_TABLES = {"knowledge_index"}  # ...is an ordinary table with a tsvector column on PostgreSQL (team/knowledge.py)
    PG_ONLY_INDEXES = {"knowledge_index_doc", "knowledge_index_tsv"}
    EXTRA_COLUMNS = {("jobs", "seq")}    # PostgreSQL has no rowid: the queue orders ties by this column

    def sqlite_schema(self):
        import tempfile
        from pathlib import Path
        from team.db import connect, migrate
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paridade.sqlite3"
            migrate(path)
            conn = connect(path)
            try:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                tables = [t for t in tables if not t.startswith(self.FTS)]
                columns = {(t, r[1]): bool(r[3]) or bool(r[5]) for t in tables for r in conn.execute(f"PRAGMA table_info({t})")}
                indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")}
                triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            finally:
                conn.close()
        return set(tables), columns, indexes, triggers

    def postgres_schema(self):
        with self.repo._transaction() as conn:
            tables = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'")}
            columns = {(r[0], r[1]): r[2] == "NO" for r in conn.execute(
                "SELECT table_name, column_name, is_nullable FROM information_schema.columns WHERE table_schema = current_schema()")}
            indexes = {r[0] for r in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = c.oid)")}
            triggers = {r[0] for r in conn.execute("SELECT DISTINCT trigger_name FROM information_schema.triggers WHERE trigger_schema = current_schema()")}
        return tables, columns, indexes, triggers

    def test_tables_columns_and_nullability_match(self):
        s_tables, s_columns, _, _ = self.sqlite_schema()
        p_tables, p_columns, _, _ = self.postgres_schema()
        self.assertEqual(s_tables, p_tables - self.PG_ONLY_TABLES)
        p_columns = {k: v for k, v in p_columns.items() if k[0] not in self.PG_ONLY_TABLES}
        self.assertEqual({k for k in p_columns if k not in s_columns}, self.EXTRA_COLUMNS)
        self.assertEqual({k for k in s_columns if k not in p_columns}, set())
        different = {k for k, required in s_columns.items() if p_columns[k] != required}
        self.assertEqual(different, set(), "nullability differs")

    def test_explicit_indexes_and_guard_triggers_match(self):
        _, _, s_indexes, s_triggers = self.sqlite_schema()
        _, _, p_indexes, p_triggers = self.postgres_schema()
        self.assertEqual(s_indexes, p_indexes - self.PG_ONLY_INDEXES)
        self.assertEqual(s_triggers, p_triggers)

    def test_the_committed_postgres_migrations_are_exactly_the_translation(self):
        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(subprocess.run([sys.executable, str(root / "scripts" / "translate_migrations_pg.py"), "--check"], capture_output=True, text=True).returncode, 0)
        sqlite_names = [(v, n) for v, n, *_ in discover()]
        from team.pgdb import MIGRATIONS_PG_DIR
        self.assertEqual([(v, n) for v, n, *_ in discover(MIGRATIONS_PG_DIR)], sqlite_names)


@requires_postgres
class GuardBehaviourTests(PostgresCase):
    """The database-level guards of the SQLite migrations, behaving the same on PostgreSQL (these checks are SQLite-only in the
    shared suites because they run SQLite statements directly)."""

    def setUp(self):
        super().setUp()
        self.load_sample()

    def refused(self, sql, params=()):
        import psycopg
        with self.assertRaises(psycopg.errors.IntegrityError):
            with self.repo._transaction() as conn:
                conn.execute(sql, params)

    def test_history_evidence_and_triage_records_are_append_only(self):
        for table in ("case_entries", "evidences", "triage_decisions"):
            self.refused(f"UPDATE {table} SET created_at = '2030-01-01T00:00:00Z'")
            self.refused(f"DELETE FROM {table}")

    def test_published_agent_versions_are_immutable_but_can_be_disabled(self):
        self.refused("UPDATE agent_profile_versions SET doc = jsonb_set(doc, '{definition,scope}', '\"x\"')")
        self.refused("UPDATE agent_profile_versions SET content_hash = 'outro'")
        self.refused("DELETE FROM agent_profile_versions")
        with self.repo._transaction() as conn:  # the only allowed change: the status
            self.assertEqual(conn.execute("UPDATE agent_profile_versions SET status = 'inativo'").rowcount, 2)

    def sample_of(self, kind):
        return sample(kind)[0]

    def test_a_run_needs_an_existing_active_version(self):
        with self.repo._transaction() as conn:
            conn.execute("UPDATE agent_profile_versions SET status = 'inativo'")
        run = dict(self.sample_of("agent_run"), id=new_id("agent_run"))
        with self.assertRaises(IntegrityViolation):
            with self.repo.transaction(IDS["tenant"]) as session:
                session.save(run)

    def test_a_credential_must_belong_to_the_same_company(self):
        other = dict(self.sample_of("tenant"), id=new_id("tenant"), name="Outra")
        self.repo.create_tenant(other)
        credential = new_id("credential")
        with self.assertRaises(psycopg.errors.IntegrityError):
            with self.repo._transaction() as conn:
                conn.execute("INSERT INTO credentials (id, tenant_id, revision, created_at, updated_at, state, doc) VALUES (?, ?, 1, 'a', 'a', 'ativa', '{}')",
                             (credential, other["id"]))
                conn.execute("UPDATE connections SET credential_ref = ? WHERE id = ?", (credential, self.sample_of("connection")["id"]))

    def test_only_one_message_may_be_sending_per_case(self):
        twin = copy.deepcopy(self.sample_of("outbound_message"))
        twin.update(id=new_id("outbound_message"), dedup_key="outra-chave", provider_id=None, state="enviando")
        with self.repo._transaction() as conn:
            conn.execute("UPDATE outbound_messages SET state = 'enviando'")
        with self.assertRaises(IntegrityViolation):
            with self.repo.transaction(IDS["tenant"]) as session:
                session.save(twin)

    def test_the_queue_keeps_one_job_per_idempotency_key_and_one_active_job_per_case(self):
        insert = ("INSERT INTO jobs (id, tenant_id, kind, case_id, idempotency_key, state, priority, attempts, max_attempts, available_at_ms, payload, checkpoint, created_ms, updated_ms) "
                  "VALUES (?, ?, 'k', ?, ?, ?, 0, 0, 3, 0, '{}', '{}', 0, 0)")
        with self.repo._transaction() as conn:
            conn.execute(insert, (new_id("job"), IDS["tenant"], None, "chave-1", "pendente"))
            conn.execute(insert, (new_id("job"), IDS["tenant"], IDS["case"], "chave-2", "em_execucao"))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.repo._transaction() as conn:
                conn.execute(insert, (new_id("job"), IDS["tenant"], None, "chave-1", "pendente"))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.repo._transaction() as conn:
                conn.execute(insert, (new_id("job"), IDS["tenant"], IDS["case"], "chave-3", "em_execucao"))
