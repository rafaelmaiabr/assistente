"""Outbound email: a real SMTP transport (TLS, authenticated), a simulated one for tests/demos, and account setup.

* IMAP never sends: only a connection of kind ``smtp`` can be an outbound account, and it needs an SMTP server that the
  operator configured explicitly. Without that configuration the router uses the **simulated** transport, whose
  results are labelled ``simulated`` and write nothing to the network.
* TLS is mandatory and verified (``starttls`` or ``ssl``); plain SMTP is accepted only for a loopback test server.
* The dialog is done step by step so the outcome is classified honestly: anything refused **before** ``DATA`` (connect,
  TLS, login, sender, recipient) is a definitive "not sent" (``ProviderRejected``); a verdict of ``250`` after ``DATA`` is
  "accepted by the server"; a timeout or disconnect **after** ``DATA`` started raises ``SmtpOutcomeUnknown`` (the gateway
  records it as unknown and never retries on its own). Acceptance is not delivery, and certainly not reading.
* Our own ``Message-ID`` (chosen when the draft is created and part of the approved payload) is sent, so a human can find
  the message in the Sent folder and the operator can reconcile an unknown outcome.
"""
import asyncio
import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from .access import OperatorContext, PermissionDenied
from .connections import ConnectionRejected
from .outbound import OutboundTransport, ProviderRejected

_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
SECURITY = ("starttls", "ssl", "plain")


class SmtpOutcomeUnknown(Exception):
    """The server may or may not have accepted the message (the connection broke after DATA started)."""


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    username: str
    from_address: str
    port: int = 587
    security: str = "starttls"
    from_name: str | None = None
    timeout: float = 20.0
    ca_file: str | None = None

    def validate(self):
        if not (isinstance(self.host, str) and _HOST.match(self.host)):
            raise ConnectionRejected("Servidor SMTP invalido.")
        if not (isinstance(self.port, int) and not isinstance(self.port, bool) and 1 <= self.port <= 65535):
            raise ConnectionRejected("Porta SMTP invalida.")
        if self.security not in SECURITY:
            raise ConnectionRejected("Seguranca deve ser starttls ou ssl.")
        if self.security == "plain" and self.host not in _LOOPBACK:
            raise ConnectionRejected("A conexao SMTP precisa usar TLS validado.")
        if not (isinstance(self.username, str) and 0 < len(self.username) <= 320 and not re.search(r"[\r\n\x00]", self.username)):
            raise ConnectionRejected("Usuario SMTP invalido.")
        if not (isinstance(self.from_address, str) and _ADDRESS.match(self.from_address)):
            raise ConnectionRejected("Endereco de remetente invalido.")


class SmtpAccountRegistry:
    """``connection_id -> SMTP settings`` chosen by the operator; never model input and never holding the password."""

    def __init__(self, path, operator_id):
        self.path, self.operator_id = path, operator_id

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))["accounts"]
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, KeyError):
            return {}

    def register(self, operator, tenant_id, connection_id, config: SmtpConfig):
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.operator_id:
            raise PermissionDenied()
        config.validate()
        accounts = self._load()
        accounts[connection_id] = {"tenant_id": tenant_id, "host": config.host, "port": config.port, "username": config.username,
                                   "from_address": config.from_address, "from_name": config.from_name, "security": config.security,
                                   "timeout": config.timeout, "ca_file": config.ca_file}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"accounts": accounts}, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def get(self, tenant_id, connection_id):
        raw = self._load().get(connection_id)
        if not raw or raw.get("tenant_id") != tenant_id:
            return None
        return SmtpConfig(host=raw["host"], port=raw["port"], username=raw["username"], from_address=raw["from_address"],
                          security=raw["security"], from_name=raw.get("from_name"), timeout=raw["timeout"], ca_file=raw.get("ca_file"))

    def message_id_domain(self, tenant_id, connection_id):
        config = self.get(tenant_id, connection_id)
        return config.from_address.rsplit("@", 1)[-1].lower() if config else "assistente.invalid"


def build_email(config, message, attachments):
    """The RFC 5322 message that will be sent. ``attachments`` is a list of ``(metadata, bytes)``."""
    msg = EmailMessage()
    msg["From"] = formataddr((config.from_name or "", config.from_address)) if config.from_name else config.from_address
    msg["To"] = ", ".join(message["to"])
    if message.get("cc"):
        msg["Cc"] = ", ".join(message["cc"])  # Bcc never appears in the headers
    msg["Subject"] = message.get("subject") or "(sem assunto)"
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = message["message_id"]
    if message.get("in_reply_to"):
        msg["In-Reply-To"] = message["in_reply_to"]
    if message.get("references"):
        msg["References"] = " ".join(message["references"])
    msg["X-Assistente-Origem"] = "resposta"  # lets our own inbox ingestion recognise and ignore loops (T21)
    msg.set_content(message["body"])
    for meta, data in attachments:
        maintype, subtype = meta["content_type"].split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=meta["filename"])
    return msg


class SmtpTransport(OutboundTransport):
    def __init__(self, registry, attachments=None, ssl_context=None, factory=None):
        self.registry, self.attachments, self.ssl_context, self.factory = registry, attachments, ssl_context, factory

    async def send(self, account, message, *, dedup_key, secret):
        config = self.registry.get(account["tenant_id"], account["id"])
        if config is None:
            raise ProviderRejected("conta_smtp_nao_configurada")
        if not secret:
            raise ProviderRejected("sem_credencial")
        files = []
        for meta in message.get("attachments") or []:
            if self.attachments is None:
                raise ProviderRejected("anexos_indisponiveis")
            try:
                files.append((meta, self.attachments.read(account["tenant_id"], message["case_id"], meta)))
            except ValueError as error:
                raise ProviderRejected("anexo_invalido") from error
        raw = build_email(config, message, files).as_bytes()
        recipients = [*message["to"], *(message.get("cc") or []), *(message.get("bcc") or [])]
        return await asyncio.to_thread(self._dialog, config, secret, raw, recipients, message["message_id"])

    def _dialog(self, config, secret, raw, recipients, message_id):
        context = self.ssl_context or ssl.create_default_context(cafile=config.ca_file)
        context.check_hostname, context.verify_mode = True, ssl.CERT_REQUIRED
        try:
            if config.security == "ssl":
                server = (self.factory or smtplib.SMTP_SSL)(config.host, config.port, timeout=config.timeout, context=context)
            else:
                server = (self.factory or smtplib.SMTP)(config.host, config.port, timeout=config.timeout)
        except ssl.SSLCertVerificationError:
            raise ProviderRejected("tls_invalido") from None
        except (smtplib.SMTPException, OSError):
            raise ProviderRejected("sem_conexao") from None
        try:
            try:
                server.ehlo()
                if config.security == "starttls":
                    server.starttls(context=context)
                    server.ehlo()
                server.login(config.username, secret)
            except smtplib.SMTPAuthenticationError:
                raise ProviderRejected("autenticacao_recusada") from None
            except (ssl.SSLError, smtplib.SMTPNotSupportedError):
                raise ProviderRejected("tls_invalido") from None
            except (smtplib.SMTPException, OSError):
                raise ProviderRejected("falha_antes_do_envio") from None
            try:
                code, _ = server.mail(config.from_address)
                if code != 250:
                    raise ProviderRejected("remetente_recusado")
                for recipient in recipients:
                    code, _ = server.rcpt(recipient)
                    if code not in (250, 251):
                        raise ProviderRejected("destinatario_recusado")  # nothing was sent: the whole message is abandoned
            except ProviderRejected:
                try:
                    server.rset()
                except Exception:
                    pass
                raise
            except (smtplib.SMTPException, OSError):
                raise ProviderRejected("falha_antes_do_envio") from None
            # --- point of no return: from here a missing answer means "unknown", never "not sent" ---
            try:
                code, reply = server.data(raw)
            except smtplib.SMTPDataError:
                raise ProviderRejected("dados_recusados") from None  # the server answered with an error: not accepted
            except Exception:
                raise SmtpOutcomeUnknown("sem_resposta_apos_dados") from None
            if code != 250:  # smtplib returns the final verdict of DATA instead of raising: anything but 250 means "not accepted"
                raise ProviderRejected("dados_recusados")
            text = reply.decode("utf-8", "replace") if isinstance(reply, bytes) else str(reply)
            return {"provider_id": message_id, "provider_result": f"{code} {text}"[:200]}
        finally:
            try:
                server.quit()
            except Exception:
                pass

    async def lookup(self, account, dedup_key):
        return None  # SMTP cannot tell; check the Sent folder for our Message-ID


class FakeEmailTransport(OutboundTransport):
    """Simulated sending. Nothing leaves the computer; every result says so."""

    def __init__(self):
        self.sent = []

    async def send(self, account, message, *, dedup_key, secret):
        self.sent.append({**message, "dedup_key": dedup_key, "account": account["id"]})
        return {"provider_id": message.get("message_id") or f"simulado:{dedup_key}", "provider_result": "envio simulado; nada saiu do computador",
                "simulated": True}


class RoutingEmailTransport(OutboundTransport):
    """Real SMTP only for connections the operator configured; everything else is simulated."""

    def __init__(self, registry, real: OutboundTransport, fake: OutboundTransport | None = None):
        self.registry, self.real, self.fake = registry, real, fake or FakeEmailTransport()

    def _pick(self, account):
        return self.real if self.registry.get(account["tenant_id"], account["id"]) else self.fake

    async def send(self, account, message, *, dedup_key, secret):
        return await self._pick(account).send(account, message, dedup_key=dedup_key, secret=secret)

    async def lookup(self, account, dedup_key):
        return await self._pick(account).lookup(account, dedup_key)


class SmtpAccounts:
    """Operator-facing setup for an outbound account (a connection of kind ``smtp``) and a connection test."""

    def __init__(self, connections, registry, ssl_context=None, factory=None):
        self.connections, self.registry, self.ssl_context, self.factory = connections, registry, ssl_context, factory
        connections.register_connector("smtp")

    def _connection(self, ctx, connection_id):
        from .access import NotFound
        connection = self.connections.get_connection(ctx, connection_id)
        if connection["connection_kind"] != "smtp" or connection["provider"] != "smtp":
            raise NotFound()
        return connection

    def configure(self, operator, ctx, connection_id, *, host, username, from_address, port=587, security="starttls", from_name=None,
                  timeout=20.0, ca_file=None):
        self._connection(ctx, connection_id)
        self.registry.register(operator, ctx.tenant_id, connection_id,
                               SmtpConfig(host=host, port=port, username=username, from_address=from_address, security=security,
                                          from_name=from_name, timeout=timeout, ca_file=ca_file))

    def test(self, ctx, connection_id):
        """Connect, negotiate TLS and log in with the vaulted password, then quit. No message is sent."""
        self._connection(ctx, connection_id)
        config = self.registry.get(ctx.tenant_id, connection_id)
        if config is None:
            return {"ok": False, "erro": "conta_nao_configurada", "mensagem": "Informe servidor, usuario e remetente."}
        transport = SmtpTransport(self.registry, ssl_context=self.ssl_context, factory=self.factory)

        def probe(secret):
            context = transport.ssl_context or ssl.create_default_context(cafile=config.ca_file)
            try:
                if config.security == "ssl":
                    server = (self.factory or smtplib.SMTP_SSL)(config.host, config.port, timeout=config.timeout, context=context)
                else:
                    server = (self.factory or smtplib.SMTP)(config.host, config.port, timeout=config.timeout)
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
                return {"ok": False, "erro": "autenticacao_recusada", "mensagem": "O servidor recusou o usuario ou a senha (use senha de aplicativo, se o provedor exigir)."}
            except ssl.SSLError:
                return {"ok": False, "erro": "tls_invalido", "mensagem": "Falha de TLS ou certificado nao aceito (confira host, porta e seguranca)."}
            except (smtplib.SMTPException, OSError):
                return {"ok": False, "erro": "sem_conexao", "mensagem": "Nao foi possivel conectar ao servidor SMTP (confira host, porta e rede)."}

        return self.connections.use_credential(ctx, connection_id, "smtp", probe)
