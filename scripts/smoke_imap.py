"""Validate the read-only IMAP client against a REAL mailbox (manual; the automated suite uses a fake server).

    python scripts/smoke_imap.py --host imap.exemplo.com --user voce@exemplo.com --folder INBOX

The password is asked for without echo (or read from IMAP_SMOKE_PASSWORD); it is never printed or stored.
Only the sender, subject and date of the newest message are shown. Nothing is changed in the mailbox:
the client only uses EXAMINE and BODY.PEEK, so the message stays unread.
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from team.imap_client import ImapConfig, ImapError, ReadOnlyImap  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=993)
    parser.add_argument("--user", required=True)
    parser.add_argument("--folder", action="append", default=None, help="authorized folder (repeatable; default INBOX)")
    args = parser.parse_args()
    folders = tuple(args.folder or ["INBOX"])
    password = os.environ.get("IMAP_SMOKE_PASSWORD") or getpass.getpass("Senha de aplicativo: ")
    try:
        with ReadOnlyImap(ImapConfig(host=args.host, port=args.port, username=args.user, folders=folders), password) as imap:
            print("TLS e login: ok")
            print("Pastas autorizadas:", imap.list_folders())
            page = imap.list_messages(folders[0], limit=1)
            print(f"UIDVALIDITY {page['uidvalidity']}; mensagens listadas: {len(page['messages'])}")
            if page["messages"]:
                top = page["messages"][0]
                message = imap.read_message(folders[0], top["uid"], page["uidvalidity"])
                print("Mais recente:", message["date"], "|", message["from"], "|", message["subject"])
                print(f"Texto: {len(message['text'])} caracteres; anexos: {len(message['attachments'])}; truncado: {message['truncated']}")
            print("Comandos enviados:", sorted(set(imap.sent)))
    except ImapError as error:
        print(f"Falhou ({error.code}): {error.message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
