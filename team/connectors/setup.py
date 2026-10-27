"""Operator-side configuration of a connector: validated form values, a per-driver adapter, and a sanitized audit trail.

The values a person types are checked against the driver's descriptor (``fields``): unknown names are refused, required
ones must be present, and the kind of each field decides where it may go. Fields of kind ``secret`` go **only** to the
vault (through the connection service); everything else goes to the driver's account registry (host, user, folders...).
Nothing in this module returns, logs or audits a secret: the audit trail holds identifiers, an action and a result code.
"""
import json
import os
import re
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..connections import ConnectionRejected

MAX_TEXT, MAX_SECRET = 300, 500
_ACTIONS = ("conectar", "configurar", "testar")


class SetupAdapter:
    """What the panel needs from a driver that can be configured. Subclasses wrap the existing account services."""

    def configure(self, operator, ctx, connection_id, values):
        """Store the non-secret ``values`` (already validated) for this connection."""
        raise NotImplementedError

    def current(self, tenant_id, connection_id):
        """The non-secret values stored for this connection, or None when it is not configured. Never a secret."""
        raise NotImplementedError

    def test(self, ctx, connection_id):
        """Try the connection with the vaulted credential: ``{"ok": bool, "erro"?, "mensagem"?}``. Must never echo a secret."""
        raise NotImplementedError

    # -- browser sign-in (OAuth), only for drivers whose descriptor says ``auth.type == "oauth"`` ---------------------------------
    def oauth_begin(self, ctx, connection_id):
        """Start the authorization: ``{"flow_id", "url", "expires_at"}``. The person opens ``url`` themselves."""
        raise NotImplementedError

    def oauth_complete(self, flow_id, timeout=None):
        """Block until the browser step ends: ``{"ok": bool, "erro"?, "mensagem"?}``. Run it off the event loop."""
        raise NotImplementedError

    def oauth_cancel(self, ctx, flow_id):
        raise NotImplementedError


class OAuthBrowserSetup(SetupAdapter):
    """Base for drivers that sign in through the browser using the OAuth service (#34/#35): subclasses provide the account
    settings (``configure``/``current``) and the provider key; the begin/complete/cancel hooks wrap ``OAuthService``."""

    provider_key = None

    def __init__(self, oauth):
        self.oauth = oauth

    def oauth_begin(self, ctx, connection_id):
        started = self.oauth.begin(ctx, connection_id, provider_key=self.provider_key)
        return {"flow_id": started["flow_id"], "url": started["url"], "expires_at": started.get("expires_at")}

    def oauth_complete(self, flow_id, timeout=None):
        from ..oauth import OAuthError
        try:
            self.oauth.complete(flow_id, timeout)
        except OAuthError as error:
            return {"ok": False, "erro": error.code, "mensagem": error.message}
        return {"ok": True}

    def oauth_cancel(self, ctx, flow_id):
        self.oauth.cancel(ctx, flow_id)


def validate_values(descriptor, values, *, partial=False):
    """Split ``values`` into (public, secret). ``partial`` allows omitting required fields (reconfiguring)."""
    if not isinstance(values, dict):
        raise ConnectionRejected("Informe os campos do formulario.")
    fields = {field["name"]: field for field in descriptor["fields"]}
    unknown = set(values) - set(fields)
    if unknown:
        raise ConnectionRejected("Campo desconhecido para este conector.")
    public, secret = {}, None
    for name, field in fields.items():
        value = values.get(name)
        blank = value is None or (isinstance(value, str) and not value.strip())
        if blank:
            if field["required"] and not partial and field["kind"] != "secret":
                raise ConnectionRejected(f"Preencha o campo: {field['label']}.")
            continue
        kind = field["kind"]
        if kind == "secret":
            if not (isinstance(value, str) and 1 <= len(value) <= MAX_SECRET):
                raise ConnectionRejected(f"{field['label']} invalido.")
            secret = value
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"\d{1,5}", str(value).strip()):
                raise ConnectionRejected(f"{field['label']} deve ser um numero.")
            public[name] = int(str(value).strip())
        elif kind == "url":
            parts = urlsplit(value) if isinstance(value, str) else None
            if not (parts and parts.scheme in ("http", "https") and parts.hostname and not parts.username and len(value) <= 500):
                raise ConnectionRejected(f"{field['label']} deve ser uma URL http(s) sem credenciais.")
            public[name] = value.strip()
        else:
            if not (isinstance(value, str) and len(value.strip()) <= MAX_TEXT):
                raise ConnectionRejected(f"{field['label']} invalido.")
            public[name] = value.strip()
    return public, secret


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ConnectorAudit:
    """Append-only JSON-lines trail of connector configuration: who, what, which connection, the result code. No secrets,
    no field values, no free text from a server (only fixed codes)."""

    def __init__(self, path):
        self.path, self._lock = path, threading.Lock()

    def record(self, tenant_id, actor, action, provider, connection_id, ok, code=None):
        entry = {"at": _now(), "tenant_id": tenant_id, "actor": str(actor)[:80], "action": action, "provider": provider,
                 "connection_id": connection_id, "ok": bool(ok), "code": None if code is None else str(code)[:60]}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def recent(self, tenant_id, limit=50, connection_id=None):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        found = []
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("tenant_id") == tenant_id and (connection_id is None or entry.get("connection_id") == connection_id):
                found.append(entry)
                if len(found) >= limit:
                    break
        return found
