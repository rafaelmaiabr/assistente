"""Run the existing team tests on PostgreSQL instead of SQLite.

    set ASSISTENTE_TEST_POSTGRES_DSN=postgresql://usuario:senha@127.0.0.1:55432/banco_de_teste
    python scripts/run_tests_postgres.py                       # the modules that must pass on PostgreSQL
    python scripts/run_tests_postgres.py tests.test_team_cases # or the ones you name

The tests construct ``SqliteRepository(path)``; here that name is replaced by a PostgreSQL repository on a private schema (dropped at
the end). Modules that use SQLite-only SQL of their own (issue #89) are not in the default list. Use a throw-away database.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Modules whose code and tests are backend-neutral (repository API and the queue). Grow this list as #89 ports the rest.
DEFAULT = [
    "tests.test_team_contracts", "tests.test_team_access", "tests.test_team_agents", "tests.test_team_connections", "tests.test_team_skills",
    "tests.test_team_cases", "tests.test_team_handoffs", "tests.test_team_approvals", "tests.test_team_triage", "tests.test_team_gaps",
    "tests.test_team_queue", "tests.test_team_outbound", "tests.test_team_gateway", "tests.test_team_runtime",
    # ported by #89
    "tests.test_team_identity", "tests.test_team_knowledge", "tests.test_team_ops", "tests.test_team_scheduler", "tests.test_team_remote",
    "tests.test_team_remote_scope", "tests.test_team_scope", "tests.test_team_oauth", "tests.test_team_oauth_microsoft", "tests.test_team_smtp",
    "tests.test_team_imap", "tests.test_team_email_ingest", "tests.test_team_finance", "tests.test_team_renewal", "tests.test_team_evals",
    "tests.test_team_diagnostics", "tests.test_team_onboarding", "tests.test_team_repository", "tests.test_team_database_pg",
]


# Tests that open the SQLite file itself (SQLite functions, file bytes, SQLite error text). Their PostgreSQL counterparts are
# tests/test_team_repository_pg.py (GuardBehaviourTests): append-only records, immutable versions, same-company credentials,
# one sending message / one active job per case and the unique keys.
SQLITE_ONLY = {
    "tests.test_team_agents.CrudAndVersionTests.test_database_keeps_published_versions_immutable",
    "tests.test_team_connections.CredentialLifecycleTests.test_secrets_never_enter_documents_or_the_database",
    "tests.test_team_cases.HistoryTests.test_history_and_evidence_are_append_only",
    "tests.test_team_outbound.ReliabilityTests.test_the_database_allows_one_sending_message_per_case_and_unique_keys",
    "tests.test_team_triage.VerifiableDecisionTests.test_decision_record_is_complete_hashed_and_append_only",
    # Restoring in place replaces the SQLite file; PostgreSQL never overwrites the database in use (tests/test_team_backup_pg.py).
    "tests.test_team_ops.BackupTests.test_restore_brings_back_state_and_reconciles_jobs_and_messages_without_resending",
    "tests.test_team_ops.BackupTests.test_messages_that_were_sending_become_unknown_for_an_operator_decision",
    # They read the SQLite file or turn its foreign keys off (PRAGMA).
    "tests.test_team_oauth.FlowTests.test_authorization_code_with_pkce_connects_the_mailbox_and_keeps_tokens_out_of_documents",
    "tests.test_team_oauth_microsoft.DeviceFlowTests.test_the_device_code_flow_connects_the_mailbox_for_the_allowed_directory",
    "tests.test_team_smtp.SendTests.test_replies_carry_threading_headers_from_the_incoming_email",
}
# Whole classes about SQLite file migrations and the on-disk upgrade/doctor path; PostgreSQL has tests/test_team_pgmigrate.py and
# tests/test_team_database_pg.py (doctor).
SQLITE_ONLY_CLASSES = (
    "tests.test_team_repository.MigrationTests.", "tests.test_team_repository.PersistenceSwitchTests.",
    "tests.test_team_onboarding.DoctorTests.", "tests.test_team_onboarding.UpgradeAndRestartTests.",
)


# Tests written around Windows paths: they say nothing about the database and cannot pass on Linux (the CI job for PostgreSQL).
WINDOWS_ONLY = {"tests.test_team_connections.CatalogTests.test_stdio_only_accepts_operator_registered_launchers"}


def _tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _tests(item)
        else:
            yield item


def main(argv=None):
    from tests import pg_support
    if not pg_support.available():
        print(f"Defina {pg_support.ENV} para um PostgreSQL descartavel e acessivel.", file=sys.stderr)
        return 2
    from tests import pg_shim
    pg_shim.install()
    names = list(argv if argv is not None else sys.argv[1:]) or DEFAULT
    loaded = unittest.defaultTestLoader.loadTestsFromNames(names)
    suite = unittest.TestSuite(t for t in _tests(loaded) if t.id() not in SQLITE_ONLY and not t.id().startswith(SQLITE_ONLY_CLASSES) and not (sys.platform != 'win32' and t.id() in WINDOWS_ONLY))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
