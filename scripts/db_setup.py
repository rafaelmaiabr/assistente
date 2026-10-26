"""Choose and prepare the team database. SQLite is the default; PostgreSQL is opt-in.

    python scripts/db_setup.py status       # which backend config.json asks for and whether the connection is stored (never shows it)
    python scripts/db_setup.py set-dsn      # asks for the PostgreSQL connection string (no echo) and stores it in the Windows vault
    python scripts/db_setup.py clear-dsn    # removes it from the vault

The connection string carries the password: it is never printed, written to config.json or kept anywhere but the vault (servers
without the Windows vault can use the ASSISTENTE_DATABASE_DSN environment variable instead). The backend itself is chosen in
config.json under "database" (see docs/TEAM-POSTGRES.md). Moving existing data is `scripts/pg_migrate.py`.
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import DATA_ROOT, load_config  # noqa: E402
from team import database  # noqa: E402
from team.vault import VaultError, WindowsVault  # noqa: E402


def status(config, vault, environ=None):
    chosen = database.settings(config)
    in_vault = bool(database.find_dsn(vault, environ={}))
    in_env = bool((environ if environ is not None else os.environ).get(database.DSN_ENV))
    return {"backend": chosen["backend"], "max_connections": chosen["max_connections"], "connection_in_vault": in_vault,
            "connection_in_environment": in_env,
            "ready": chosen["backend"] == "sqlite" or in_vault or in_env}


def main(argv=None, vault=None, config=None, secret_reader=getpass.getpass, environ=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "set-dsn", "clear-dsn"))
    args = parser.parse_args(argv)
    vault = vault or WindowsVault(DATA_ROOT)
    try:
        if args.command == "status":
            print(json.dumps(status(load_config() if config is None else config, vault, environ), ensure_ascii=False, indent=2))
        elif args.command == "set-dsn":
            dsn = secret_reader("Conexao do PostgreSQL, ex.: postgresql://usuario:senha@servidor:5432/banco (nao aparece na tela): ").strip()
            if not dsn.lower().startswith(("postgresql://", "postgres://")):
                print("Nada foi gravado: informe uma URL que comece com postgresql://", file=sys.stderr)
                return 1
            vault.put(*database.DSN_SLOT, dsn)
            print("Conexao gravada no cofre do Windows. Para usar, defina database.backend = \"postgres\" no config.json.")
        else:
            vault.delete(*database.DSN_SLOT)
            print("Conexao removida do cofre.")
    except (database.DatabaseConfigError, VaultError, ValueError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
