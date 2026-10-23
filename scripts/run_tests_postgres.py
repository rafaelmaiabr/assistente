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
}


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
    suite = unittest.TestSuite(t for t in _tests(loaded) if t.id() not in SQLITE_ONLY)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
