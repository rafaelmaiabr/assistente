"""Read-only access to the person's own mailbox for the chat: account file, vault password and three bounded actions.

The account (server, port, user, authorized folders) lives in ``mail-account.json`` inside the data folder, which Git ignores.
The app password lives only in the Windows credential vault. The model never sees either: it only calls the three actions
below, and everything they return is marked as untrusted data. Nothing here sends, deletes, moves or marks a message as read.
"""
import json
import os
from pathlib import Path

from team.imap_client import ImapConfig, ImapError, ReadOnlyImap
from team.vault import SecretVault, VaultError, WindowsVault

FILE = "mail-account.json"
TENANT, SLOT = "pessoal", "email-imap"
MAX_ITEMS = 10
MAX_TEXT = 6000


class MailNotConfigured(Exception):
    pass


class PersonalMail:
    def __init__(self, root, vault: SecretVault = None, ssl_context=None):
        self.path = Path(root) / FILE
        self.vault = vault or WindowsVault(root)
        self.ssl_context = ssl_context

    # -- account ------------------------------------------------------------------------------------------------------------
    def configure(self, *, host, username, password, folders=("INBOX",), port=993, tls=True):
        config = ImapConfig(host=host, port=port, username=username, folders=tuple(folders), tls=tls)
        try:
            config.validate()
        except ImapError as error:
            raise ValueError(error.message) from None
        self.vault.put(TENANT, SLOT, password)
        data = {"host": host, "port": port, "username": username, "folders": list(folders), "tls": tls}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def remove(self):
        self.vault.delete(TENANT, SLOT)
        self.path.unlink(missing_ok=True)

    def configured(self) -> bool:
        return self._config() is not None

    def folders(self):
        config = self._config()
        return list(config.folders) if config else []

    def _config(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return ImapConfig(host=raw["host"], port=raw["port"], username=raw["username"], folders=tuple(raw["folders"]),
                              tls=raw.get("tls", True))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _open(self):
        config = self._config()
        if config is None:
            raise MailNotConfigured()
        try:
            password = self.vault.get(TENANT, SLOT)
        except VaultError:
            password = None
        if not password:
            raise MailNotConfigured()
        return config, ReadOnlyImap(config, password, self.ssl_context)

    # -- actions (blocking; the chat runs them in a thread) -----------------------------------------------------------------
    def _run(self, action):
        try:
            _, imap = self._open()
            with imap:
                return action(imap)
        except MailNotConfigured:
            return {"erro": "Nenhuma caixa de email configurada. Rode scripts/mail_setup.py para configurar."}
        except ImapError as error:
            return {"erro": error.message}

    def _folder(self, folder):
        config = self._config()
        return folder or (config.folders[0] if config else "INBOX")

    @staticmethod
    def _summary(page, limit):
        return {"dados_nao_confiaveis": True, "pasta": page["folder"], "uidvalidity": page["uidvalidity"],
                "mensagens": [{"uid": m["uid"], "de": m["from"], "assunto": m["subject"], "data": m["date"]}
                              for m in page["messages"][:limit]],
                "ha_mais": page["next_before_uid"] is not None}

    def list_recent(self, folder=None, limit=5, since=None):
        limit = max(1, min(int(limit), MAX_ITEMS))
        return self._run(lambda imap: self._summary(imap.list_messages(self._folder(folder), limit=limit, since=since), limit))

    def search(self, folder=None, text=None, sender=None, subject=None, since=None, limit=5):
        limit = max(1, min(int(limit), MAX_ITEMS))
        return self._run(lambda imap: self._summary(
            imap.search(self._folder(folder), text=text, sender=sender, subject=subject, since=since, limit=limit), limit))

    def read(self, uid, uidvalidity, folder=None):
        def action(imap):
            message = imap.read_message(self._folder(folder), int(uid), int(uidvalidity))
            return {"dados_nao_confiaveis": True, "de": message["from"], "para": message["to"], "assunto": message["subject"],
                    "data": message["date"], "texto": message["text"][:MAX_TEXT], "anexos": message["attachments"][:10],
                    "truncado": message["truncated"] or len(message["text"]) > MAX_TEXT}
        return self._run(action)

    def test(self):
        """The connection check the setup script shows: folders reachable, no message content."""
        return self._run(lambda imap: {"ok": True, "pastas": imap.list_folders()})
