import tempfile
import unittest
from pathlib import Path
from unittest import mock

from team import database, doctor
from team.pgdb import PostgresRepository
from team.vault import MemoryVault
from tests.pg_support import PostgresSchema, requires_postgres

CONFIG = {"database": {"backend": "postgres"}}


class SettingsTests(unittest.TestCase):
    def test_sqlite_is_the_default_and_the_connection_string_never_goes_in_the_config(self):
        self.assertEqual(database.settings({})["backend"], "sqlite")
        self.assertIsNone(database.postgres_repository({}))
        for key in ("dsn", "password", "host"):
            with self.assertRaises(database.DatabaseConfigError) as raised:
                database.settings({"database": {"backend": "postgres", key: "segredo-123"}})
            self.assertNotIn("segredo-123", str(raised.exception))
        with self.assertRaises(database.DatabaseConfigError):
            database.settings({"database": {"backend": "mysql"}})

    def test_the_connection_string_comes_from_the_vault_before_the_environment(self):
        vault = MemoryVault()
        vault.put(*database.DSN_SLOT, "postgresql://cofre")
        self.assertEqual(database.find_dsn(vault, {database.DSN_ENV: "postgresql://ambiente"}), "postgresql://cofre")
        self.assertEqual(database.find_dsn(MemoryVault(), {database.DSN_ENV: "postgresql://ambiente"}), "postgresql://ambiente")
        self.assertIsNone(database.find_dsn(MemoryVault(), {}))


@requires_postgres
class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.schema = PostgresSchema()
        self.addCleanup(self.schema.drop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = MemoryVault()
        self.vault.put(*database.DSN_SLOT, self.schema.dsn)
        self.addCleanup(database.forget)

    def banco(self):
        return doctor.check_database(Path(self.temp.name), CONFIG, self.vault)

    def test_reports_pending_then_ok_with_server_version_and_never_the_dsn(self):
        pending = self.banco()
        self.assertEqual(pending["estado"], "aviso")
        PostgresRepository(self.schema.dsn).migrate()
        ok = self.banco()
        self.assertEqual(ok["estado"], "ok")
        self.assertIn("PostgreSQL", ok["detalhe"] if "detalhe" in ok else str(ok))
        self.assertNotIn(self.schema.dsn, str(ok))

    def test_an_unknown_migration_is_an_error_that_changes_nothing(self):
        repo = PostgresRepository(self.schema.dsn)
        repo.migrate()
        with repo._transaction() as conn:
            conn.execute("INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (999, 'futura', 'x', 'agora')")
        self.assertEqual(self.banco()["estado"], "erro")

    def test_a_missing_connection_is_an_error_with_an_action(self):
        result = doctor.check_database(Path(self.temp.name), CONFIG, MemoryVault())
        self.assertEqual(result["estado"], "erro")
        self.assertTrue(result.get("acao"))


if __name__ == "__main__":
    unittest.main()
