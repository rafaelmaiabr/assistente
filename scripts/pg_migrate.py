"""Operate the PostgreSQL backend: schema, copy of the data in either direction, and checks. SQLite stays the default.

    set ASSISTENTE_PG_DSN=postgresql://usuario:senha@127.0.0.1:5432/assistente      (the connection string lives in the environment,
                                                                                    never on the command line or in a file of the repo)
    python scripts/pg_migrate.py status                       # server version, migrations applied / pending
    python scripts/pg_migrate.py migrate                      # apply pending PostgreSQL migrations (each in a transaction)
    python scripts/pg_migrate.py to-postgres --sqlite team-data/team.sqlite3   # copy SQLite -> an EMPTY, migrated PostgreSQL
    python scripts/pg_migrate.py verify --sqlite team-data/team.sqlite3        # compare the two (counts and fingerprints), writes nothing
    python scripts/pg_migrate.py to-sqlite --sqlite volta.sqlite3              # the way back: PostgreSQL -> a NEW SQLite file

The source is never changed. Make a backup anyway before the first copy (docs/TEAM-OPS.md) and see docs/TEAM-POSTGRES.md.
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from team.db import MigrationError, discover  # noqa: E402
from team.pgdb import DatabaseUnavailable, MIGRATIONS_PG_DIR, PostgresRepository  # noqa: E402
from team.pgmigrate import MigrationMismatch, TargetNotEmpty, to_postgres, to_sqlite, verify  # noqa: E402


def repository(env):
    dsn = os.environ.get(env)
    if not dsn:
        raise DatabaseUnavailable(f"Defina a variavel de ambiente {env} com a conexao do PostgreSQL.")
    return PostgresRepository(dsn)


def default_sqlite():
    from settings import DATA_ROOT
    from team import persistence
    return persistence.data_dir(DATA_ROOT) / persistence.DATABASE


def status(repo):
    with repo._transaction() as conn:
        version = conn.execute("SHOW server_version").fetchone()[0]
        try:
            applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        except Exception:
            applied = set()
    wanted = {v for v, *_ in discover(MIGRATIONS_PG_DIR)}
    print(f"PostgreSQL {version}")
    print(f"Migracoes aplicadas: {len(applied & wanted)} de {len(wanted)}" + (f"; pendentes: {sorted(wanted - applied)}" if wanted - applied else " (em dia)"))
    if applied - wanted:
        print(f"ATENCAO: o banco tem migracoes desconhecidas desta versao ({sorted(applied - wanted)}); atualize o aplicativo.")
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "migrate", "to-postgres", "to-sqlite", "verify"))
    parser.add_argument("--dsn-env", default="ASSISTENTE_PG_DSN", help="name of the environment variable that holds the connection string")
    parser.add_argument("--sqlite", type=Path, help="SQLite file (source for to-postgres/verify, NEW target for to-sqlite)")
    args = parser.parse_args(argv)
    try:
        repo = repository(args.dsn_env)
        if args.command == "status":
            return status(repo)
        if args.command == "migrate":
            done = repo.migrate()
            print("Migracoes aplicadas:", done or "nenhuma (em dia)")
            return 0
        sqlite = args.sqlite or (default_sqlite() if args.command != "to-sqlite" else None)
        if sqlite is None:
            print("Informe --sqlite com o arquivo NOVO de destino.", file=sys.stderr)
            return 2
        report = {"to-postgres": lambda: to_postgres(sqlite, repo), "to-sqlite": lambda: to_sqlite(repo, sqlite), "verify": lambda: verify(sqlite, repo)}[args.command]()
        print(f"{args.command}: {sum(report.values())} linhas em {len(report)} tabelas, conferidas (contagem e conteudo).")
        return 0
    except (DatabaseUnavailable, MigrationError, MigrationMismatch, TargetNotEmpty, FileNotFoundError) as error:
        print("Erro:", re.sub(r"postgres(?:ql)?://\S+", "***", str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
