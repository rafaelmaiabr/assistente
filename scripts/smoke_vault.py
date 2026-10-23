"""Disposable local check of the Windows vault used by team connections.

Run with the project's interpreter on a Windows machine where pywin32 is installed:
    .venv/Scripts/python.exe scripts/smoke_vault.py
It writes one random secret under a throwaway install scope and tenant, reads it back, replaces it,
deletes it and confirms it is gone. The secret is never printed and nothing is left behind.
"""
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from team.ids import new_id  # noqa: E402
from team.vault import VaultError, WindowsVault  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as scope:  # the temp path makes the credential scope unique
        vault, tenant, credential = WindowsVault(scope), new_id("tenant"), new_id("credential")
        first, second = "smoke-" + secrets.token_hex(16), "smoke-" + secrets.token_hex(16)
        try:
            vault.put(tenant, credential, first)
            assert vault.get(tenant, credential) == first, "leitura apos salvar"
            vault.put(tenant, credential, second)
            assert vault.get(tenant, credential) == second, "leitura apos substituir"
            assert vault.get(new_id("tenant"), credential) is None, "isolamento por empresa"
        except (VaultError, AssertionError) as error:
            print("FALHA:", error)
            return 1
        finally:
            try:
                vault.delete(tenant, credential)
            except VaultError:
                print("AVISO: remova manualmente a credencial Assistente/*/team/" + tenant)
        assert vault.get(tenant, credential) is None, "remocao"
    print("OK: cofre do Windows funcionando (salvar, ler, substituir, isolar, remover).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
