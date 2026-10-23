"""Read-only IMAP access with a safe MIME reader (standard library only).

* TLS is mandatory and verified (certificate and host name); plain IMAP is allowed only for a loopback
  test server.
* Only a short list of commands can be sent: LOGIN, LIST, EXAMINE (read-only), UID SEARCH and UID FETCH
  with ``BODY.PEEK``. Nothing that stores flags, moves, deletes, appends or marks a message as read
  exists in this module, and the wrapper refuses any other command or fetch item.
* Folders come from an allowlist decided by the backend. Messages are addressed by
  ``(folder, UIDVALIDITY, UID)``; a changed UIDVALIDITY or an unknown UID is an error, never a guess.
* Message text is *untrusted data*: bounded, decoded safely, HTML reduced to text without loading any
  resource, attachments listed but never opened, nesting and part counts limited.
"""
import html
import imaplib
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr

MAX_PAGE = 50
MAX_BYTES = 256 * 1024
MAX_TEXT_CHARS = 20_000
MAX_DEPTH = 6
MAX_PARTS = 50
MAX_FOLDERS = 20
HEADER_FIELDS = ("FROM", "TO", "CC", "SUBJECT", "DATE", "MESSAGE-ID", "IN-REPLY-TO", "REFERENCES")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_FETCH_TOKEN = re.compile(r"BODY\.PEEK\[[A-Z0-9 .()-]*\](?:<\d+\.\d+>)?|RFC822\.SIZE|UID")
_BAD = re.compile(r"[\r\n\x00]")


class ImapError(Exception):
    """Actionable failure with a stable ``code``; the message never includes credentials or message content."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class ImapConfig:
    host: str
    username: str
    folders: tuple
    port: int = 993
    tls: bool = True
    timeout: float = 15.0
    max_bytes: int = MAX_BYTES
    ca_file: str | None = None  # an extra trusted CA (private servers); verification stays on

    def validate(self):
        if not (isinstance(self.host, str) and _HOST.match(self.host)):
            raise ImapError("config_invalida", "Servidor IMAP invalido.")
        if not (isinstance(self.port, int) and not isinstance(self.port, bool) and 1 <= self.port <= 65535):
            raise ImapError("config_invalida", "Porta IMAP invalida.")
        if not self.tls and self.host not in _LOOPBACK:
            raise ImapError("tls_obrigatorio", "A conexao IMAP precisa usar TLS validado.")
        if not (isinstance(self.username, str) and self.username and not _BAD.search(self.username) and len(self.username) <= 320):
            raise ImapError("config_invalida", "Usuario IMAP invalido.")
        if not (self.folders and len(self.folders) <= MAX_FOLDERS and all(
                isinstance(f, str) and f and len(f) <= 200 and not _BAD.search(f) for f in self.folders)):
            raise ImapError("config_invalida", "Informe de 1 a 20 pastas autorizadas.")


def _quote(value):
    if _BAD.search(value):
        raise ImapError("argumento_invalido", "Texto de busca invalido.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(value):
    parsed = value if isinstance(value, date) else datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    return parsed.strftime("%d-%b-%Y")


# -- MIME ---------------------------------------------------------------------------------------------------------------
def _clean(value, limit=500):
    try:
        text = str(make_header(decode_header(str(value or ""))))
    except Exception:
        text = str(value or "")
    return re.sub(r"[\r\n\t\x00-\x1f]+", " ", text).strip()[:limit]


def _html_to_text(markup):
    """Reduce HTML to text without loading anything: scripts, styles, comments and tags are dropped."""
    markup = re.sub(r"(?is)<(script|style|head|svg)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?s)<!--.*?-->", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]*>", " ", markup))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def parse_message(raw: bytes, max_chars=MAX_TEXT_CHARS):
    """Safe summary of a raw message: headers, bounded text and a list of attachments (never decoded)."""
    msg = message_from_bytes(raw, policy=policy.default)
    headers = {name.lower().replace("-", "_"): _clean(msg.get(name)) for name in
               ("From", "To", "Cc", "Subject", "Date", "Message-ID", "In-Reply-To", "References")}
    state = {"parts": 0, "limited": False, "plain": [], "html": [], "attachments": []}
    ids = lambda value: re.findall(r"<[^<>\s]{1,250}>", str(value or ""))[:20]  # noqa: E731
    sender_name, sender = parseaddr(str(msg.get("From") or ""))
    recipients = [a.lower() for _, a in getaddresses([str(msg.get("To") or ""), str(msg.get("Cc") or "")]) if "@" in a][:20]
    auto = {name.lower().replace("-", "_"): _clean(msg.get(name), 120).lower()
            for name in ("Auto-Submitted", "Precedence", "List-Id", "X-Autoreply", "X-Autorespond", "Return-Path",
                         "X-Auto-Response-Suppress", "X-Assistente-Origem") if msg.get(name) is not None}

    def visit(part, depth):
        state["parts"] += 1
        if depth > MAX_DEPTH or state["parts"] > MAX_PARTS:
            state["limited"] = True
            return
        if part.is_multipart():
            for child in part.iter_parts():
                visit(child, depth + 1)
            return
        kind, name = part.get_content_type(), part.get_filename()
        if name or part.get_content_disposition() == "attachment" or not kind.startswith("text/"):
            body = part.get_payload(decode=True) or b""
            state["attachments"].append({"filename": _clean(name or "", 200) or None, "content_type": kind, "size": len(body)})
            return
        try:
            content = part.get_content()
        except Exception:
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        (state["plain"] if kind == "text/plain" else state["html"]).append(content if isinstance(content, str) else "")

    visit(msg, 0)
    text = "\n\n".join(state["plain"]).strip() or _html_to_text("\n".join(state["html"]))
    truncated = len(text) > max_chars
    return {**headers, "sender": sender.lower()[:320], "sender_name": _clean(sender_name, 120) or None, "recipients": recipients,
            "ids": {"message_id": (ids(msg.get("Message-ID")) or [None])[0], "in_reply_to": (ids(msg.get("In-Reply-To")) or [None])[0],
                    "references": ids(msg.get("References"))}, "auto": auto,
            "text": text[:max_chars], "truncated": truncated or state["limited"],
            "attachments": state["attachments"], "parts_limited": state["limited"], "untrusted": True}


# -- the read-only client -----------------------------------------------------------------------------------------------
class ReadOnlyImap:
    def __init__(self, config: ImapConfig, secret, ssl_context=None, factory=None):
        config.validate()
        self.config, self._secret, self._ctx, self._factory = config, secret, ssl_context, factory
        self._conn, self._selected = None, None
        self.sent = []  # every command that left this object, for tests and diagnostics (never arguments with secrets)

    # connection ---------------------------------------------------------------------------------------------------
    def connect(self):
        try:
            if self.config.tls:
                ctx = self._ctx or ssl.create_default_context(cafile=self.config.ca_file)
                ctx.check_hostname, ctx.verify_mode = True, ssl.CERT_REQUIRED
                self._conn = (self._factory or imaplib.IMAP4_SSL)(self.config.host, self.config.port, ssl_context=ctx,
                                                                  timeout=self.config.timeout)
            else:
                self._conn = (self._factory or imaplib.IMAP4)(self.config.host, self.config.port, timeout=self.config.timeout)
            self.sent.append("LOGIN")
            self._conn.login(self.config.username, self._secret)
        except imaplib.IMAP4.error:
            self.close()
            raise ImapError("autenticacao_recusada", "O servidor recusou o usuario ou a senha (use uma senha de aplicativo, se o provedor exigir).") from None
        except ssl.SSLCertVerificationError:
            raise ImapError("tls_invalido", "O certificado do servidor nao foi aceito (confira o endereco e a validade).") from None
        except ssl.SSLError:
            raise ImapError("tls_invalido", "Falha de TLS: o servidor nao respondeu como IMAP seguro (confira host e porta).") from None
        except (socket.timeout, TimeoutError):
            raise ImapError("tempo_esgotado", "O servidor IMAP nao respondeu a tempo.") from None
        except OSError:
            raise ImapError("sem_conexao", "Nao foi possivel conectar ao servidor IMAP (confira host, porta e rede).") from None
        return self

    def close(self):
        conn, self._conn, self._selected = self._conn, None, None
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass

    def __enter__(self):
        return self.connect() if self._conn is None else self

    def __exit__(self, *exc):
        self.close()

    # guarded commands ---------------------------------------------------------------------------------------------------
    def _need(self):
        if self._conn is None:
            raise ImapError("sem_conexao", "Nao conectado.")
        return self._conn

    def _call(self, label, function, *args):
        self.sent.append(label)
        try:
            status, data = function(*args)
        except (socket.timeout, TimeoutError):
            raise ImapError("tempo_esgotado", "O servidor IMAP nao respondeu a tempo.") from None
        except (imaplib.IMAP4.error, OSError):
            raise ImapError("erro_do_servidor", "O servidor IMAP recusou ou interrompeu o comando.") from None
        if status != "OK":
            raise ImapError("erro_do_servidor", "O servidor IMAP recusou o comando.")
        return data

    def _folder(self, name):
        if name not in self.config.folders:
            raise ImapError("pasta_nao_autorizada", "Esta pasta nao esta autorizada para a conta.")
        return name

    def _examine(self, folder):
        """Open a folder read-only (EXAMINE) and return its UIDVALIDITY."""
        self._folder(folder)
        conn = self._need()
        self._call("EXAMINE", conn.select, _quote(folder), True)
        if self._selected != folder:
            self._selected = folder
        data = conn.response("UIDVALIDITY")[1]
        try:
            return int(data[-1])
        except (TypeError, ValueError, IndexError):
            raise ImapError("erro_do_servidor", "O servidor nao informou UIDVALIDITY.") from None

    def _fetch(self, uid, items):
        inner = items.strip()
        # Only BODY.PEEK, sizes and UID are allowed: RFC822 / BODY[] without PEEK would mark the message as read.
        if not (inner.startswith("(") and inner.endswith(")")) or _FETCH_TOKEN.sub("", inner[1:-1]).strip():
            raise ImapError("comando_proibido", "Item de leitura nao permitido.")
        return self._call("UID FETCH", self._need().uid, "FETCH", str(int(uid)), items)

    # public read-only operations ----------------------------------------------------------------------------------------
    def list_folders(self):
        data = self._call("LIST", self._need().list)
        names = set()
        for line in data:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            match = re.search(r'"((?:[^"\\]|\\.)*)"\s*$|\s(\S+)\s*$', text)
            if match:
                names.add((match.group(1) or match.group(2) or "").strip('"').replace('\\"', '"'))
        return [{"name": f, "present": f in names} for f in self.config.folders]

    def _summaries(self, uids):
        messages = []
        for uid in uids:
            data = self._fetch(uid, "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])")
            raw = next((part[1] for part in data if isinstance(part, tuple)), b"")
            head = parse_message(raw, 0)
            size = re.search(rb"RFC822\.SIZE (\d+)", next((p[0] for p in data if isinstance(p, tuple)), b""))
            messages.append({"uid": int(uid), "from": head["from"], "to": head["to"], "subject": head["subject"],
                             "date": head["date"], "message_id": head["message_id"], "size": int(size.group(1)) if size else None})
        return messages

    def _page(self, folder, criteria, limit, before_uid):
        validity = self._examine(folder)
        limit = max(1, min(int(limit), MAX_PAGE))
        if before_uid is not None:
            criteria = f"{criteria} UID 1:{max(int(before_uid) - 1, 1)}"
        data = self._call("UID SEARCH", self._need().uid, "SEARCH", criteria)
        uids = sorted((int(u) for u in (data[0] or b"").split()), reverse=True)
        page = uids[:limit]
        more = len(uids) > limit
        return {"folder": folder, "uidvalidity": validity, "messages": self._summaries(page),
                "next_before_uid": page[-1] if more and page else None}

    def list_messages(self, folder, limit=20, before_uid=None, since=None):
        criteria = "ALL" if not since else f"SINCE {_imap_date(since)}"
        return self._page(folder, criteria, limit, before_uid)

    def search(self, folder, *, text=None, sender=None, subject=None, since=None, before=None, limit=20, before_uid=None):
        parts = []
        for key, value in (("TEXT", text), ("FROM", sender), ("SUBJECT", subject)):
            if value:
                parts.append(f"{key} {_quote(str(value)[:200])}")
        if since:
            parts.append(f"SINCE {_imap_date(since)}")
        if before:
            parts.append(f"BEFORE {_imap_date(before)}")
        if not parts:
            raise ImapError("argumento_invalido", "Informe ao menos um criterio de busca.")
        return self._page(folder, " ".join(parts), limit, before_uid)

    def validity(self, folder):
        """UIDVALIDITY of an authorized folder (opened read-only)."""
        return self._examine(folder)

    def uids_after(self, folder, after_uid, limit=20):
        """Oldest-first UIDs greater than ``after_uid`` with the folder's UIDVALIDITY (for incremental ingestion)."""
        validity = self._examine(folder)
        first = max(int(after_uid), 0) + 1
        data = self._call("UID SEARCH", self._need().uid, "SEARCH", f"UID {first}:*")
        uids = sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u >= first)  # "n:*" always returns the last one
        return validity, uids[: max(1, min(int(limit), MAX_PAGE))]

    def read_message(self, folder, uid, uidvalidity, max_bytes=None):
        """Read one message without touching its flags. UIDVALIDITY must match what the caller listed."""
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
            raise ImapError("uid_invalido", "UID invalido.")
        current = self._examine(folder)
        if current != uidvalidity:
            raise ImapError("uidvalidity_mudou", "A pasta foi reorganizada pelo servidor; liste as mensagens novamente.")
        limit = min(int(max_bytes or self.config.max_bytes), self.config.max_bytes)
        data = self._fetch(uid, f"(UID RFC822.SIZE BODY.PEEK[]<0.{limit}>)")
        tuples = [p for p in data if isinstance(p, tuple)]
        if not tuples:
            raise ImapError("uid_nao_encontrado", "Mensagem nao encontrada nesta pasta.")
        size = re.search(rb"RFC822\.SIZE (\d+)", tuples[0][0])
        parsed = parse_message(tuples[0][1])
        total = int(size.group(1)) if size else len(tuples[0][1])
        parsed.update(uid=uid, uidvalidity=current, folder=folder, size=total,
                      truncated=parsed["truncated"] or total > limit)
        return parsed
