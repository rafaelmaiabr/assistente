"""Move the team's data between SQLite and PostgreSQL, in either direction, and prove it arrived intact.

SQLite is the default and stays the way back: ``to_postgres`` copies a SQLite database into an empty, migrated PostgreSQL database
and ``to_sqlite`` copies it back into a new SQLite file (the rollback path). Neither direction changes the source. Both:

* refuse a target that already has data (nothing is merged or overwritten);
* copy inside one transaction, so a failure leaves the target as it was;
* switch off the target's guard triggers *during the copy only* (history is copied, not re-created, and a run may point at a
  version that was disabled later); they are back before the transaction ends;
* compare, per table, the number of rows and a fingerprint of the content (JSON is compared as data, not as text) and fail loudly
  on any difference;
* rebuild the full-text index afterwards: it is derived from the documents, so it is not copied (``reindex``).
"""
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from .db import connect, migrate
from .pgdb import PostgresRepository
from .repository import SqliteRepository

JSON_COLUMNS = {"doc", "payload", "checkpoint", "result", "budget"}
SKIP = {"schema_migrations", "sqlite_sequence"}
SKIP_PREFIX = ("knowledge_index",)
IDENTITY = {"events": "seq", "outbox": "id", "tool_decisions": "id", "erasure_log": "id", "auth_events": "seq", "jobs": "seq"}
BATCH = 500


class MigrationMismatch(RuntimeError):
    """The copy did not reproduce the source exactly; the target was rolled back."""


class TargetNotEmpty(RuntimeError):
    pass


def _reference_connection():
    """A throw-away migrated SQLite database: the reference for table names, columns and dependencies."""
    folder = tempfile.mkdtemp(prefix="assistente-ordem-")
    path = Path(folder) / "ref.sqlite3"
    migrate(path)
    return connect(path), path


def table_plan():
    """``[(table, [columns])]`` in an order that satisfies foreign keys (parents first; self-references by creation order)."""
    conn, path = _reference_connection()
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        tables = [t for t in tables if t not in SKIP and not t.startswith(SKIP_PREFIX)]
        parents = {t: {r[2] for r in conn.execute(f"PRAGMA foreign_key_list({t})") if r[2] != t and r[2] in tables} for t in tables}
        columns = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in tables}
    finally:
        conn.close()
        try:
            path.unlink()
            path.parent.rmdir()
        except OSError:
            pass
    ordered, remaining = [], dict(parents)
    while remaining:
        ready = sorted(t for t, deps in remaining.items() if deps <= set(ordered))
        if not ready:
            raise RuntimeError("dependencia circular entre tabelas: " + ", ".join(sorted(remaining)))
        for table in ready:
            ordered.append(table)
            del remaining[table]
    return [(t, columns[t]) for t in ordered]


def _sort_key(table, columns):
    """A stable, parent-before-child order for rows: creation time where there is one, then the key."""
    for name in ("created_at", "created_ms", "occurred_at", "issued_at", "granted_at"):
        if name in columns:
            return f"{name}, " + ("id" if "id" in columns else columns[0])
    return ", ".join(columns[:2]) if len(columns) > 1 else columns[0]


def _canonical(column, value):
    if value is None:
        return None
    if column in JSON_COLUMNS and isinstance(value, (str, bytes)):
        return json.dumps(json.loads(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return value


def fingerprint(conn, table, columns):
    """``(rows, sha256)`` of the table's content, independent of row order and of JSON formatting."""
    key = ", ".join(columns)
    digest, count = hashlib.sha256(), 0
    rows = sorted((tuple(_canonical(c, v) for c, v in zip(columns, row)) for row in conn.execute(f"SELECT {key} FROM {table}").fetchall()),
                  key=lambda r: json.dumps(r, default=str))
    for row in rows:
        digest.update(json.dumps(row, default=str, ensure_ascii=False).encode("utf-8"))
        count += 1
    return count, digest.hexdigest()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _require_empty(conn, plan):
    filled = [t for t, _ in plan if _count(conn, t) > 0]
    if filled:
        raise TargetNotEmpty("O destino ja tem dados (" + ", ".join(filled[:5]) + "); use um banco novo, recem-migrado e vazio.")


def _copy(source, target, plan, placeholder):
    copied = {}
    for table, columns in plan:
        names = ", ".join(columns)
        marks = ", ".join(placeholder for _ in columns)
        rows = source.execute(f"SELECT {names} FROM {table} ORDER BY {_sort_key(table, columns)}").fetchall()
        for start in range(0, len(rows), BATCH):
            target.executemany(f"INSERT INTO {table} ({names}) VALUES ({marks})", rows[start:start + BATCH])
        copied[table] = len(rows)
    return copied


def _verify(source, target, plan):
    for table, columns in plan:
        a, b = fingerprint(source, table, columns), fingerprint(target, table, columns)
        if a != b:
            raise MigrationMismatch(f"A tabela {table} nao ficou igual no destino (origem {a[0]} linhas, destino {b[0]}).")
    return {table: _count(source, table) for table, _ in plan}


def _reindex(repository):
    """Rebuild the knowledge search index of every company in a freshly copied database."""
    from .knowledge import KnowledgeService
    service = KnowledgeService(repository, None)
    return sum(service.reindex(tenant["id"]) for tenant in repository.list_global("tenant", limit=1000))


def to_postgres(sqlite_path, repository: PostgresRepository):
    """Copy a SQLite database into an empty, migrated PostgreSQL database. Returns ``{table: rows}``; raises on any difference."""
    if not Path(sqlite_path).exists():
        raise FileNotFoundError("Banco SQLite de origem nao encontrado.")
    repository.migrate()
    plan = table_plan()
    source = connect(sqlite_path)
    try:
        with repository._transaction() as target:
            _require_empty(target, plan)
            tables = [t for t, _ in plan]
            for table in tables:
                target.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            _copy(source, target, plan, "?")
            for table, column in IDENTITY.items():  # the next generated number continues after the copied rows
                target.execute(f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), COALESCE((SELECT MAX({column}) FROM {table}), 0) + 1, false)")
            report = _verify(source, target, plan)
            for table in tables:  # back on before the transaction ends; on any failure the whole transaction (triggers included) rolls back
                target.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        _reindex(repository)
        return report
    finally:
        source.close()


def to_sqlite(repository: PostgresRepository, sqlite_path):
    """The way back: copy a PostgreSQL database into a NEW SQLite file (migrated, empty before the copy)."""
    path = Path(sqlite_path)
    if path.exists():
        raise TargetNotEmpty("O arquivo SQLite de destino ja existe; escolha um novo.")
    migrate(path)
    plan = table_plan()
    target = connect(path)
    report = None
    try:
        target.execute("BEGIN IMMEDIATE")
        triggers = target.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'").fetchall()
        for name, _ in triggers:
            target.execute(f"DROP TRIGGER {name}")
        try:
            with repository._transaction() as source:
                source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")  # one snapshot for every table, even while the app writes
                _require_empty(target, plan)
                _copy(source, target, plan, "?")
                report = _verify(source, target, plan)
            for _, sql in triggers:
                target.execute(sql)
            check = target.execute("PRAGMA foreign_key_check").fetchall()
            if check:
                raise MigrationMismatch("Chaves estrangeiras quebradas no SQLite copiado: " + str(check[:3]))
            target.execute("COMMIT")
        except BaseException:
            if target.in_transaction:
                target.execute("ROLLBACK")
            raise
    except BaseException:
        target.close()
        try:
            path.unlink()  # a half-made file must not look like a real database
        except OSError:
            pass
        raise
    finally:
        try:
            target.close()
        except sqlite3.Error:
            pass
    _reindex(SqliteRepository(path))
    return report


def verify(sqlite_path, repository: PostgresRepository):
    """Compare an existing SQLite database with a PostgreSQL one (counts and fingerprints). Writes nothing."""
    plan = table_plan()
    source = connect(sqlite_path)
    try:
        with repository._transaction() as target:
            return _verify(source, target, plan)
    finally:
        source.close()
