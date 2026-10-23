"""PostgreSQL for the tests: skipped unless ``ASSISTENTE_TEST_POSTGRES_DSN`` points at a throw-away database.

Every test gets its own schema (dropped afterwards), so nothing leaks between tests and nothing outside it is touched.
Start one locally with ``deploy/postgres/dev.ps1`` (Docker) or use any PostgreSQL 15+ you can discard.
"""
import os
import unittest
import uuid

ENV = "ASSISTENTE_TEST_POSTGRES_DSN"

try:
    import psycopg
except ImportError:  # optional dependency: the tests that need it are skipped
    psycopg = None


def base_dsn():
    return os.environ.get(ENV) or None


def available():
    dsn = base_dsn()
    if not dsn:
        return False
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


requires_postgres = unittest.skipUnless(available(), f"defina {ENV} para um PostgreSQL descartavel (veja deploy/postgres)")


class PostgresSchema:
    """A private schema in the test database; ``dsn`` makes every connection use it."""

    def __init__(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        self.name = "t_" + uuid.uuid4().hex[:16]
        self._base = base_dsn()
        with psycopg.connect(self._base, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {self.name}")
        self.dsn = make_conninfo(self._base, options=f"-c search_path={self.name}")

    def drop(self):
        import psycopg
        from team.pgdb import close_idle
        close_idle(self.dsn)
        with psycopg.connect(self._base, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {self.name} CASCADE")
