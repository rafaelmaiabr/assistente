"""Configure (or remove) the read-only mailbox the chat can read. See docs/PERSONAL-MAIL.md.

    python scripts/mail_setup.py --host imap.gmail.com --user voce@gmail.com --folder INBOX
    python scripts/mail_setup.py --remove

The app password is asked for without echo (or read from MAIL_SETUP_PASSWORD) and goes only to the Windows credential vault.
The connection is tested before anything is kept; nothing is changed in the mailbox.
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from personal_mail import PersonalMail  # noqa: E402
from settings import DATA_ROOT  # noqa: E402
from team.vault import VaultError  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=993)
    parser.add_argument("--user")
    parser.add_argument("--folder", action="append", default=None, help="authorized folder (repeatable; default INBOX)")
    parser.add_argument("--remove", action="store_true", help="forget the account and its password")
    args = parser.parse_args()
    mail = PersonalMail(DATA_ROOT)
    try:
        if args.remove:
            mail.remove()
            print("Caixa removida: arquivo de conta e senha do cofre apagados.")
            return 0
        if not (args.host and args.user):
            parser.error("informe --host e --user")
        password = os.environ.get("MAIL_SETUP_PASSWORD") or getpass.getpass("Senha de aplicativo: ")
        mail.configure(host=args.host, username=args.user, password=password, folders=args.folder or ["INBOX"], port=args.port)
        result = mail.test()
        if "erro" in result:
            mail.remove()
            print(f"Falhou: {result['erro']} A configuração não foi mantida.")
            return 1
        print("Conexão ok. Pastas autorizadas:", [f["name"] for f in result["pastas"]])
        print("Reinicie o servidor web (encerrar-web.cmd e iniciar-web.cmd) para o chat usar a caixa.")
        return 0
    except (ValueError, VaultError) as error:
        print(f"Falhou: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
