"""Configure (or remove) the read-only mailbox the chat can read. See docs/PERSONAL-MAIL.md.

    python scripts/mail_setup.py --host imap.gmail.com --user voce@gmail.com --folder INBOX
    python scripts/mail_setup.py --host imap.gmail.com --user voce@gmail.com --read-only
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
    parser.add_argument("--smtp-host", help="outgoing server (default: the IMAP host with imap. replaced by smtp.)")
    parser.add_argument("--smtp-port", type=int, default=587)
    parser.add_argument("--smtp-security", choices=("starttls", "ssl"), default="starttls")
    parser.add_argument("--from-address", help="sender address (default: the user)")
    parser.add_argument("--from-name")
    parser.add_argument("--read-only", action="store_true", help="read mail only; replies stay disabled")
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
        smtp = None if args.read_only else {"host": args.smtp_host or args.host.replace("imap.", "smtp.", 1), "port": args.smtp_port,
                                            "security": args.smtp_security, "from_address": args.from_address or args.user,
                                            "from_name": args.from_name}
        mail.configure(host=args.host, username=args.user, password=password, folders=args.folder or ["INBOX"], port=args.port, smtp=smtp)
        result = mail.test()
        if "erro" in result:
            mail.remove()
            print(f"Falhou: {result['erro']} A configuração não foi mantida.")
            return 1
        if smtp:
            sent = mail.test_smtp()
            if "erro" in sent:
                mail.remove()
                print(f"Falhou: {sent['erro']} A configuração não foi mantida (use --read-only para só ler).")
                return 1
            print("Envio configurado: o assistente só prepara rascunhos; você aprova cada um na tela.")
        print("Conexão ok. Pastas autorizadas:", [f["name"] for f in result["pastas"]])
        print("Reinicie o servidor web (encerrar-web.cmd e iniciar-web.cmd) para o chat usar a caixa.")
        return 0
    except (ValueError, VaultError) as error:
        print(f"Falhou: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
