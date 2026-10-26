"""PostgreSQL backend for the repository contracts, the outbox and the durable queue.

The domain code asks the repository for sessions and writes documents; the SQL it runs is the same on both backends, so this
module only adapts what really differs:

* ``PgConnection`` gives a psycopg connection the small ``sqlite3`` surface the shared SQL expects (``?`` placeholders,
  ``execute``/``executemany``, cursors with ``fetchone``/``fetchall``/``rowcount``);
* JSON columns are ``JSONB`` in the database but travel as *text*, so documents come out of ``json.loads`` exactly as before;
* migrations are the SQLite ones translated by ``scripts/translate_migrations_pg.py`` (``team/migrations_pg``), applied one per
  transaction under an advisory lock, with the same numbering, checksum and "database newer than the program" refusals;
* a bounded number of connections (``max_connections``) is the backpressure of the database: when they are all busy a caller
  waits ``acquire_timeout`` seconds and then gets ``DatabaseSaturated``, instead of piling up connections;
* the connection string is a secret: it is taken from the argument (the caller reads it from the vault or an environment
  variable) and never printed, logged or put in an error message.

SQLite remains the default backend. psycopg is imported only when this backend is used (``requirements-postgres.txt``).
"""
import collections
import hashlib
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .db import MigrationError, discover
from .repository import SqliteRepository, SqliteSession

MIGRATIONS_PG_DIR = Path(__file__).resolve().parent / "migrations_pg"
_URL = re.compile(r"postgres(?:ql)?://\S+")
ADVISORY_LOCK = 727274  # arbitrary, fixed: serialises concurrent migrators of the same database


class DatabaseSaturated(RuntimeError):
    """Every connection is in use and none was freed in time: callers should back off and retry."""


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached. The message never carries the connection string."""


def _driver():
    try:
        import psycopg
        return psycopg
    except ImportError as error:
        raise DatabaseUnavailable("O driver do PostgreSQL (psycopg) nao esta instalado: pip install -r requirements-postgres.txt.") from error


def translate_sql(sql):
    """``?`` placeholders -> ``%s``; literal ``%`` doubled; quoted text left alone."""
    out, quote = [], None
    for ch in sql:
        if quote:
            out.append("%%" if ch == "%" else ch)
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


class PgCursor:
    """What the shared SQL expects of a cursor."""

    def __init__(self, cursor):
        self._cursor, self.lastrowid = cursor, None

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone() if self._cursor.description else None

    def fetchall(self):
        return self._cursor.fetchall() if self._cursor.description else []

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection:
    """A psycopg connection wearing the ``sqlite3`` surface the shared SQL uses."""

    def __init__(self, raw):
        self.raw = raw

    @property
    def in_transaction(self):
        return self.raw.info.transaction_status != 0  # IDLE

    def execute(self, sql, params=()):
        cursor = self.raw.cursor()
        cursor.execute(translate_sql(sql), tuple(params))
        return PgCursor(cursor)

    def executemany(self, sql, rows):
        cursor = self.raw.cursor()
        cursor.executemany(translate_sql(sql), [tuple(row) for row in rows])
        return PgCursor(cursor)


# Idle connections are kept for reuse (opening one costs more than a small transaction). The cap is global, so any number of
# repositories (one per test, one per process) can never hold more than IDLE_CAP idle connections between them.
IDLE_CAP = 16
_IDLE = collections.OrderedDict()
_IDLE_LOCK = threading.Lock()


def _take(dsn):
    with _IDLE_LOCK:
        for key in reversed(list(_IDLE)):
            if _IDLE[key][0] == dsn:
                raw = _IDLE.pop(key)[1]
                if not raw.closed and raw.info.transaction_status == 0:
                    return raw
                try:
                    raw.close()
                except Exception:
                    pass
    return None


def _give(dsn, raw):
    with _IDLE_LOCK:
        _IDLE[id(raw)] = (dsn, raw)
        while len(_IDLE) > IDLE_CAP:
            _, (_, oldest) = _IDLE.popitem(last=False)
            try:
                oldest.close()
            except Exception:
                pass


def close_idle(dsn=None):
    """Close the idle connections of one database (or all of them). Used by tests and on shutdown."""
    with _IDLE_LOCK:
        for key in [k for k, (d, _) in _IDLE.items() if dsn is None or d == dsn]:
            _, raw = _IDLE.pop(key)
            try:
                raw.close()
            except Exception:
                pass


def _checksum(sql):
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def open_raw(dsn, connect_timeout=5):
    psycopg = _driver()
    try:
        raw = psycopg.connect(dsn, connect_timeout=connect_timeout, autocommit=False, prepare_threshold=None)  # no server-side prepared statements: safe behind connection poolers
    except psycopg.OperationalError as error:
        raise DatabaseUnavailable("Nao foi possivel conectar ao PostgreSQL (confira o servidor, a rede e as credenciais).") from error
    from psycopg.types.string import TextLoader  # JSON arrives as text: the same documents as with SQLite
    raw.adapters.register_loader("jsonb", TextLoader)
    raw.adapters.register_loader("json", TextLoader)
    return raw


def migrate_pg(dsn, directory=MIGRATIONS_PG_DIR, source=None):
    """Apply pending PostgreSQL migrations; returns the versions applied. Each runs in its own transaction."""
    psycopg = _driver()
    migrations = discover(directory)
    reference = discover(source) if source is not None else None
    if reference is not None and [(m[0], m[1]) for m in migrations] != [(m[0], m[1]) for m in reference]:
        raise MigrationError("As migracoes do PostgreSQL nao correspondem as do SQLite; gere-as de novo com scripts/translate_migrations_pg.py.")
    raw = open_raw(dsn)
    done = []
    try:
        raw.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK,))
        raw.commit()
        raw.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version BIGINT PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        raw.commit()
        applied = {v: (n, c) for v, n, c in raw.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()}
        raw.commit()
        for version, (name, checksum) in applied.items():
            known = next((m for m in migrations if m[0] == version), None)
            if known is None:
                raise MigrationError(f"O banco tem a migracao {version:04d}_{name}, desconhecida desta versao do aplicativo. Atualize o aplicativo; nada foi alterado.")
            if known[3] != checksum:
                raise MigrationError(f"A migracao {version:04d}_{name} foi alterada depois de aplicada. Nada foi alterado.")
        for version, name, sql, checksum in migrations:
            if version in applied:
                continue
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                raw.execute(sql)
                raw.execute("INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (%s, %s, %s, %s)", (version, name, checksum, stamp))
                raw.commit()
            except psycopg.Error as error:
                raw.rollback()
                detail = _URL.sub("***", str(error))[:200]
                raise MigrationError(f"Falha na migracao {version:04d}_{name}: {detail}. Nenhuma alteracao dela foi mantida.") from error
            done.append(version)
        return done
    finally:
        try:
            raw.rollback()
            raw.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
            raw.commit()
        except Exception:
            pass
        raw.close()


def inspect(repository, directory=MIGRATIONS_PG_DIR):
    """Read-only state of a PostgreSQL database for the installation check: server version, applied/pending/unknown/changed
    migrations. Raises ``DatabaseUnavailable`` (never carrying the connection string) when it cannot be reached."""
    available = {number: (name, checksum) for number, name, _, checksum in discover(directory)}
    with repository._transaction() as conn:
        version = conn.execute("SHOW server_version").fetchone()[0]
        has_table = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is not None
        applied = {v: (n, c) for v, n, c in conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()} if has_table else {}
    return {"server_version": version, "applied": sorted(applied), "pending": sorted(set(available) - set(applied)),
            "future": sorted(set(applied) - set(available)),
            "changed": sorted(n for n, (_, checksum) in applied.items() if n in available and available[n][1] != checksum)}


class PostgresSession(SqliteSession):
    def __init__(self, conn, tenant_id):
        super().__init__(conn, tenant_id)
        self.INTEGRITY = (_driver().errors.IntegrityError,)

    @staticmethod
    def _is_unique(error):
        return getattr(error, "sqlstate", None) == "23505"

    def _insert_event(self, event):
        from .repository import _dump
        row = self.conn.execute(
            "INSERT INTO events (id, tenant_id, case_id, revision, occurred_at, doc) VALUES (?, ?, ?, ?, ?, ?) RETURNING seq",
            (event["id"], self.tenant_id, event["case_id"], event["revision"], event["occurred_at"], _dump(event))).fetchone()
        return row[0]

    def _insert_command(self, command):
        from .repository import _dump
        return self.conn.execute(
            "INSERT INTO commands (id, tenant_id, case_id, idempotency_key, issued_at, doc) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (command["id"], self.tenant_id, command["case_id"], command["idempotency_key"], command["issued_at"], _dump(command))).rowcount == 1


class PostgresRepository(SqliteRepository):
    """The repository over PostgreSQL. ``dsn`` is a secret: it is not kept in any message or representation."""

    dialect = "postgres"
    row_order = "seq"
    session_class = PostgresSession

    def __init__(self, dsn, max_connections=8, acquire_timeout=5.0):
        if not isinstance(dsn, str) or not dsn.strip():
            raise DatabaseUnavailable("Informe a conexao do PostgreSQL.")
        self._dsn = dsn
        self._slots = threading.BoundedSemaphore(max_connections)
        self.max_connections, self.acquire_timeout = max_connections, acquire_timeout
        self.path = None

    @property
    def integrity_errors(self):
        return (_driver().errors.IntegrityError,)

    def __repr__(self):
        return f"PostgresRepository(max_connections={self.max_connections})"

    def migrate(self):
        return migrate_pg(self._dsn, source=None)

    @contextmanager
    def _transaction(self):
        if not self._slots.acquire(timeout=self.acquire_timeout):
            raise DatabaseSaturated("O banco esta com todas as conexoes ocupadas; tente de novo em instantes.")
        raw, healthy = None, True
        try:
            raw = _take(self._dsn) or open_raw(self._dsn)
            yield PgConnection(raw)
            raw.commit()
        except BaseException:
            if raw is not None:
                try:
                    raw.rollback()
                except Exception:
                    healthy = False
            raise
        finally:
            if raw is not None:
                if healthy and not raw.closed and raw.info.transaction_status == 0:
                    _give(self._dsn, raw)
                else:
                    try:
                        raw.close()
                    except Exception:
                        pass
            self._slots.release()

    @contextmanager
    def reader(self):
        with self._transaction() as conn:  # pooled and read-only in practice: PostgreSQL readers never block writers
            yield conn

    def close(self):
        """Close the connections kept for reuse. The repository stays usable (it reconnects on demand)."""
        close_idle(self._dsn)

    def _connect(self):
        raise NotImplementedError("use _transaction(): conexoes do PostgreSQL sao sempre transacionais")

    def get_global(self, kind, doc_id):
        from .repository import GLOBAL_KINDS, TABLES, RepositoryError
        import json
        if kind not in GLOBAL_KINDS:
            raise RepositoryError(f"tipo nao e global: {kind}")
        with self._transaction() as conn:
            row = conn.execute(f"SELECT doc FROM {TABLES[kind]} WHERE id = ?", (doc_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_global(self, kind, limit=100):
        from .repository import GLOBAL_KINDS, MAX_LIST, TABLES, RepositoryError
        import json
        if kind not in GLOBAL_KINDS:
            raise RepositoryError(f"tipo nao e global: {kind}")
        with self._transaction() as conn:
            rows = conn.execute(f"SELECT doc FROM {TABLES[kind]} ORDER BY created_at, id LIMIT ?", (min(max(int(limit), 1), MAX_LIST),)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def ping(self):
        """True when the database answers; used by the installation check. Never raises."""
        try:
            with self._transaction() as conn:
                return conn.execute("SELECT 1").fetchone()[0] == 1
        except Exception:
            return False
