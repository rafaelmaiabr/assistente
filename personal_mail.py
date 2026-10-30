"""Read-only access to the person's own mailbox for the chat: account file, vault password and three bounded actions.

The account (server, port, user, authorized folders) lives in ``mail-account.json`` inside the data folder, which Git ignores.
The app password lives only in the Windows credential vault. The model never sees either: it only calls the three actions
below, and everything they return is marked as untrusted data. Nothing here sends, deletes, moves or marks a message as read.
"""
import json
import os
import re
import smtplib
import ssl
from pathlib import Path

from team.connections import ConnectionRejected
from mail_mover import ImapMover
from team.imap_client import ImapConfig, ImapError, ReadOnlyImap
from team.smtp_transport import SmtpConfig
from team.vault import SecretVault, VaultError, WindowsVault

FILE = "mail-account.json"
TENANT, SLOT = "pessoal", "email-imap"
MAX_ITEMS = 10
MAX_TEXT = 6000


# Known providers by address domain: (IMAP host, SMTP host). Anything else is guessed as imap.<domain>/smtp.<domain>; the form
# lets the person correct both. Several providers disable app-password login by default: the test says so instead of guessing.
PRESETS = {
    "gmail.com": ("imap.gmail.com", "smtp.gmail.com"), "googlemail.com": ("imap.gmail.com", "smtp.gmail.com"),
    "outlook.com": ("outlook.office365.com", "smtp.office365.com"), "hotmail.com": ("outlook.office365.com", "smtp.office365.com"),
    "live.com": ("outlook.office365.com", "smtp.office365.com"), "yahoo.com": ("imap.mail.yahoo.com", "smtp.mail.yahoo.com"),
    "yahoo.com.br": ("imap.mail.yahoo.com", "smtp.mail.yahoo.com"), "icloud.com": ("imap.mail.me.com", "smtp.mail.me.com"),
    "me.com": ("imap.mail.me.com", "smtp.mail.me.com"), "zoho.com": ("imap.zoho.com", "smtp.zoho.com"),
}
_EMAIL = re.compile(r"^[^@\s,;<>]+@([^@\s,;<>]+\.[^@\s,;<>]+)$")


def guess_servers(address):
    """``(imap_host, smtp_host)`` for an address, or None when it is not an address."""
    match = _EMAIL.match(address or "")
    if not match:
        return None
    domain = match.group(1).lower()
    return PRESETS.get(domain, (f"imap.{domain}", f"smtp.{domain}"))


class MailNotConfigured(Exception):
    pass


class PersonalMail:
    def __init__(self, root, vault: SecretVault = None, ssl_context=None):
        self.path = Path(root) / FILE
        self.vault = vault or WindowsVault(root)
        self.ssl_context = ssl_context

    # -- account ------------------------------------------------------------------------------------------------------------
    def configure(self, *, host, username, password, folders=("INBOX",), port=993, tls=True, smtp=None):
        """``smtp`` (optional) is ``{host, port, security, from_address, from_name}``; the app password is the same one."""
        config = ImapConfig(host=host, port=port, username=username, folders=tuple(folders), tls=tls)
        try:
            config.validate()
        except ImapError as error:
            raise ValueError(error.message) from None
        data = {"host": host, "port": port, "username": username, "folders": list(folders), "tls": tls}
        if smtp:
            data["smtp"] = {key: smtp.get(key) for key in ("host", "port", "security", "from_address", "from_name")}
            self._smtp_from(data, username).validate()
        self.vault.put(TENANT, SLOT, password)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def setup(self, address, password, *, imap_host=None, smtp_host=None, read_only=False, ssl_context=None, imap_port=993, imap_tls=True,
              smtp_port=587, smtp_security="starttls", smtp_factory=None):
        """Configure from the form: guess the servers, keep nothing unless both connections work, and leave the old account intact on failure.

        Returns ``{"ok": True, ...}`` or ``{"erro": ...}``; neither ever contains the password.
        """
        servers = guess_servers(address)
        if servers is None or not isinstance(password, str) or not password.strip() or len(password) > 200:
            return {"erro": "Informe um endereço de email válido e a senha de aplicativo."}
        imap, smtp = (imap_host or servers[0]).strip(), (smtp_host or servers[1]).strip()
        before_file = self.path.read_bytes() if self.path.exists() else None
        before_secret = self.secret()

        def restore():
            if before_file is None:
                self.path.unlink(missing_ok=True)
            else:
                self.path.write_bytes(before_file)
            try:
                self.vault.put(TENANT, SLOT, before_secret) if before_secret else self.vault.delete(TENANT, SLOT)
            except VaultError:
                pass
        try:
            self.configure(host=imap, username=address, password=password.strip(), port=imap_port, tls=imap_tls, smtp=None if read_only else {
                "host": smtp, "port": smtp_port, "security": smtp_security, "from_address": address, "from_name": None})
        except (ValueError, VaultError) as error:
            restore()
            return {"erro": str(error) if isinstance(error, ValueError) else "Não foi possível guardar a senha no cofre do Windows."}
        reading = self.test_imap(ssl_context)
        if "erro" in reading:
            restore()
            return {"erro": reading["erro"] + " Nada foi guardado."}
        if not read_only:
            sending = self.test_smtp(ssl_context, smtp_factory)
            if "erro" in sending:
                restore()
                return {"erro": sending["erro"] + " Nada foi guardado."}
        return {"ok": True, "leitura": True, "envio": not read_only, "servidor": imap, "usuario": address}

    def remove(self):
        self.vault.delete(TENANT, SLOT)
        self.path.unlink(missing_ok=True)

    @staticmethod
    def _smtp_from(data, username):
        raw = data.get("smtp") or {}
        return SmtpConfig(host=raw.get("host") or "", username=username, from_address=raw.get("from_address") or username,
                          port=raw.get("port") or 587, security=raw.get("security") or "starttls", from_name=raw.get("from_name"))

    def smtp_config(self):
        """The outbound account, or None when only reading was configured."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not data.get("smtp"):
                return None
            config = self._smtp_from(data, data["username"])
            config.validate()
            return config
        except (OSError, ValueError, KeyError, TypeError, ConnectionRejected):
            return None

    def secret(self):
        """The app password (the vault is the only place it exists), or None."""
        try:
            return self.vault.get(TENANT, SLOT) or None
        except VaultError:
            return None

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

    def open_mover(self):
        """The write side, for moving approved messages to Trash or Spam (#140). A mailbox set up as read-only never moves anything."""
        config, password = self._config(), self.secret()
        if config is None or not password:
            raise ImapError("sem_caixa", "Nenhuma caixa de email configurada.")
        if self.smtp_config() is None:
            raise ImapError("somente_leitura", "Esta caixa foi configurada como somente leitura; ela não move mensagens.")
        return ImapMover(config, password, self.ssl_context)

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

    def original(self, uid, uidvalidity, folder=None):
        """The parsed message a reply is about (for the outbox, never shown to the model as is), or ``{"erro": ...}``."""
        return self._run(lambda imap: imap.read_message(self._folder(folder), int(uid), int(uidvalidity)))

    def test_smtp(self, ssl_context=None, factory=None):
        """Connect, negotiate TLS and log in with the app password, then quit. No message is sent."""
        config, secret = self.smtp_config(), self.secret()
        if config is None or not secret:
            return {"erro": "O envio não está configurado."}
        context = ssl_context or ssl.create_default_context()
        try:
            if config.security == "ssl":
                server = (factory or smtplib.SMTP_SSL)(config.host, config.port, timeout=config.timeout, context=context)
            else:
                server = (factory or smtplib.SMTP)(config.host, config.port, timeout=config.timeout)
            try:
                server.ehlo()
                if config.security == "starttls":
                    server.starttls(context=context)
                    server.ehlo()
                server.login(config.username, secret)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            return {"ok": True}
        except smtplib.SMTPAuthenticationError:
            return {"erro": "O servidor de saída recusou o usuário ou a senha (use senha de aplicativo)."}
        except ssl.SSLError:
            return {"erro": "Falha de TLS ou certificado não aceito no servidor de saída."}
        except (smtplib.SMTPException, OSError):
            return {"erro": "Não foi possível conectar ao servidor de saída (confira servidor, porta e segurança)."}

    def test(self):
        """The connection check the setup script shows: folders reachable, no message content."""
        return self._run(lambda imap: {"ok": True, "pastas": imap.list_folders()})

    def test_imap(self, ssl_context=None):
        original, self.ssl_context = self.ssl_context, ssl_context or self.ssl_context
        try:
            result = self.test()
        finally:
            self.ssl_context = original
        return result
