"""Runs the existing service-level tests on PostgreSQL by swapping the repository class (see scripts/run_tests_postgres.py).

``SqliteRepository(path)`` becomes a ``PostgresRepository`` on a private schema; the same path always maps to the same schema, so a
test that "restarts" by opening the path again sees its data. Schemas are dropped when the process ends.
"""
import atexit
from pathlib import Path

from team.pgdb import PostgresRepository
from tests.pg_support import PostgresSchema

_SCHEMAS = {}


def _cleanup():
    for schema in _SCHEMAS.values():
        schema.drop()


atexit.register(_cleanup)


class ShimRepository(PostgresRepository):
    def __init__(self, path, **options):
        key = str(Path(path))
        if key not in _SCHEMAS:
            _SCHEMAS[key] = PostgresSchema()
        super().__init__(_SCHEMAS[key].dsn, **options)
        self.path = Path(path)


def install():
    import team.repository
    team.repository.SqliteRepository = ShimRepository
