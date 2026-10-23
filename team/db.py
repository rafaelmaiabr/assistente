"""SQLite connections and numbered, checksummed, transactional migrations.

A failed migration never deletes or resets the database: each migration runs in one transaction
(DDL included), and an existing database is copied to a ``.bak`` file before pending migrations run.
"""
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
BUSY_TIMEOUT_MS = 5000


class MigrationError(RuntimeError):
    """Actionable migration diagnostic; the database file is left as it was."""


def connect(path) -> sqlite3.Connection:
    """Connection in autocommit mode (transactions are explicit) with foreign keys enforced."""
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def discover(directory: Path = MIGRATIONS_DIR) -> list:
    """Return ``[(version, name, sql, checksum)]``; versions must be 1..N without gaps."""
    found = []
    for file in sorted(Path(directory).glob("*.sql")):
        match = _FILE.match(file.name)
        if not match:
            raise MigrationError(f"Nome de migracao invalido: {file.name} (use NNNN_nome.sql).")
        sql = file.read_text(encoding="utf-8")
        found.append((int(match.group(1)), match.group(2), sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()))
    if [item[0] for item in found] != list(range(1, len(found) + 1)):
        raise MigrationError("As migracoes devem ser numeradas sem lacunas a partir de 0001.")
    return found


def _applied(conn) -> dict:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
    return {row[0]: (row[1], row[2]) for row in conn.execute("SELECT version, name, checksum FROM schema_migrations")}


def _backup(conn, path: Path, version: int) -> Path:
    target = path.with_name(f"{path.name}.pre-migracao-{version:04d}.bak")
    copy = sqlite3.connect(str(target))
    try:
        conn.backup(copy)
    finally:
        copy.close()
    return target


def migrate(path, directory: Path = MIGRATIONS_DIR) -> list:
    """Apply pending migrations; return the versions applied (empty when up to date)."""
    path = Path(path)
    migrations = discover(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = connect(path)
    except sqlite3.Error as error:
        raise MigrationError(f"Nao foi possivel abrir o banco {path.name}: {error}. O arquivo nao foi alterado.") from error
    try:
        applied = _applied(conn)
        for version, (name, checksum) in applied.items():
            known = next((m for m in migrations if m[0] == version), None)
            if known is None:
                raise MigrationError(
                    f"O banco tem a migracao {version:04d}_{name}, desconhecida desta versao do aplicativo. "
                    "Atualize o aplicativo; o arquivo nao foi alterado.")
            if known[3] != checksum:
                raise MigrationError(
                    f"A migracao {version:04d}_{name} foi alterada depois de aplicada. O arquivo nao foi alterado.")
        pending = [m for m in migrations if m[0] not in applied]
        if pending and applied:
            _backup(conn, path, max(applied))
        done = []
        for version, name, sql, checksum in pending:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            script = (f"BEGIN IMMEDIATE;\n{sql}\nINSERT INTO schema_migrations (version, name, checksum, applied_at) "
                      f"VALUES ({version}, '{name}', '{checksum}', '{stamp}');\nCOMMIT;")
            try:
                conn.executescript(script)
            except sqlite3.Error as error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                if version in _applied(conn):  # another process applied it first
                    continue
                raise MigrationError(
                    f"Falha na migracao {version:04d}_{name}: {error}. Nenhuma alteracao dela foi mantida; "
                    f"o banco {path.name} foi preservado" + (" e existe copia .bak anterior." if applied else ".")) from error
            done.append(version)
        return done
    except sqlite3.Error as error:
        raise MigrationError(f"Banco {path.name} ilegivel ou ocupado: {error}. O arquivo nao foi alterado.") from error
    finally:
        conn.close()
