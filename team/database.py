"""Which database the team uses: SQLite (the default, nothing to configure) or PostgreSQL (opt-in).

``config.json`` only says *which* backend and how many connections it may hold::

    "database": {"backend": "postgres", "max_connections": 8}

The connection string carries a password, so it never goes in ``config.json``: it is read from the credential vault (slot
``DSN_SLOT``, written by ``scripts/db_setup.py set-dsn``) or, for servers without the Windows vault, from the environment variable
``ASSISTENTE_DATABASE_DSN``. A ``dsn``/``password`` key in the config is refused with a message that names the key, not its value.
Nothing here prints, logs or returns the connection string.
"""
import hashlib
import os
import threading

DSN_SLOT = ("plataforma", "banco-postgres")  # (tenant, credential) in the vault; not a company
DSN_ENV = "ASSISTENTE_DATABASE_DSN"
BACKENDS = ("sqlite", "postgres")
_ALLOWED = {"backend", "max_connections", "acquire_timeout"}
_FORBIDDEN = ("dsn", "url", "password", "senha", "connection_string", "host", "user", "usuario")


class DatabaseConfigError(ValueError):
    """The database settings are invalid or incomplete; the message is safe to show."""


def settings(config):
    """Validated ``{backend, max_connections, acquire_timeout}`` from the ``database`` section (SQLite when it is absent)."""
    raw = (config or {}).get("database")
    if raw is None:
        return {"backend": "sqlite", "max_connections": 8, "acquire_timeout": 5.0}
    if not isinstance(raw, dict):
        raise DatabaseConfigError("A secao `database` do config.json deve ser um objeto.")
    for key in raw:
        if str(key).lower() in _FORBIDDEN:
            raise DatabaseConfigError(f"A chave `{key}` nao pode ficar no config.json: a conexao com senha vai no cofre (scripts/db_setup.py set-dsn).")
    unknown = sorted(set(raw) - _ALLOWED)
    if unknown:
        raise DatabaseConfigError("Chave(s) desconhecida(s) em `database`: " + ", ".join(map(str, unknown)) + ".")
    backend = raw.get("backend", "sqlite")
    if backend not in BACKENDS:
        raise DatabaseConfigError("`database.backend` deve ser `sqlite` ou `postgres`.")
    connections, timeout = raw.get("max_connections", 8), raw.get("acquire_timeout", 5.0)
    if isinstance(connections, bool) or not isinstance(connections, int) or not 1 <= connections <= 64:
        raise DatabaseConfigError("`database.max_connections` deve ser um inteiro de 1 a 64.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 120:
        raise DatabaseConfigError("`database.acquire_timeout` deve ser um numero de segundos entre 0,1 e 120.")
    return {"backend": backend, "max_connections": connections, "acquire_timeout": float(timeout)}


def find_dsn(vault=None, environ=None):
    """The connection string from the vault, else the environment; ``None`` when neither has it. Never logged."""
    if vault is not None:
        try:
            value = vault.get(*DSN_SLOT)
        except Exception:
            value = None
        if value:
            return value
    return (environ if environ is not None else os.environ).get(DSN_ENV) or None


_CACHE = {}
_LOCK = threading.Lock()


def postgres_repository(config, vault=None, environ=None):
    """The (process-wide, pooled) PostgreSQL repository for this configuration, or ``None`` when the backend is SQLite."""
    chosen = settings(config)
    if chosen["backend"] != "postgres":
        return None
    dsn = find_dsn(vault, environ)
    if not dsn:
        raise DatabaseConfigError("Falta a conexao do PostgreSQL: rode `python scripts/db_setup.py set-dsn` (cofre) ou defina " + DSN_ENV + ".")
    from .pgdb import PostgresRepository
    key = (hashlib.sha256(dsn.encode("utf-8")).hexdigest(), chosen["max_connections"], chosen["acquire_timeout"])
    with _LOCK:
        if key not in _CACHE:
            _CACHE[key] = PostgresRepository(dsn, max_connections=chosen["max_connections"], acquire_timeout=chosen["acquire_timeout"])
        return _CACHE[key]


def forget():
    """Drop the cached repositories and their idle connections (tests, and shutdown)."""
    with _LOCK:
        for repository in _CACHE.values():
            repository.close()
        _CACHE.clear()
