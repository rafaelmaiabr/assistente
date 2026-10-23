"""Prepare the remote (signed-in) access. Nothing here opens a port: the listener starts only with the backend.

    python scripts/remote_setup.py status          # shows the address to register at Google and what is missing
    python scripts/remote_setup.py set-secret      # asks for the Google client secret (no echo) and stores it in the Windows vault
    python scripts/remote_setup.py clear-secret    # removes it

The client secret is never printed, written to config.json or kept anywhere but the Windows credential vault.
Settings go in config.json under "remote" (see docs/TEAM-REMOTE.md).
"""
import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import DATA_ROOT, load_config  # noqa: E402
from team.login import SECRET_SLOT, ConfigError, setup_status  # noqa: E402
from team.vault import VaultError, WindowsVault  # noqa: E402


def main(argv=None, vault=None, config=None, secret_reader=getpass.getpass):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "set-secret", "clear-secret"))
    args = parser.parse_args(argv)
    vault = vault or WindowsVault(DATA_ROOT)
    try:
        if args.command == "status":
            print(json.dumps(setup_status(load_config() if config is None else config, vault), ensure_ascii=False, indent=2))
        elif args.command == "set-secret":
            secret = secret_reader("Segredo do cliente OAuth do Google (nao aparece na tela): ").strip()
            if not secret:
                print("Nada foi gravado: o segredo estava vazio.")
                return 1
            vault.put(*SECRET_SLOT, secret)
            print("Segredo gravado no cofre do Windows.")
        else:
            vault.delete(*SECRET_SLOT)
            print("Segredo removido do cofre.")
    except (ConfigError, VaultError, ValueError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
