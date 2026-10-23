"""Sign-in with Google for the remote listener: configuration, the authorization-code flow with PKCE, and rate limits.

The flow is the standard OpenID Connect authorization code flow:

* ``begin`` makes a one-time ``state``, a ``nonce`` and a PKCE verifier, and remembers them on the server together with a hash
  of a random value that is also placed in a short-lived cookie of the same browser. The callback is accepted only when the
  state is known, unused and not expired **and** the cookie matches, so a link crafted by someone else cannot sign a victim into
  the attacker's session (login CSRF) and a stolen ``state`` is useless in another browser.
* ``complete`` exchanges the code at Google's token endpoint over verified TLS (the client secret comes from the vault and never
  appears in a URL, a log or an answer) and checks the ID token claims: issuer, audience, expiry, nonce, a verified email and,
  if configured, the Google Workspace domain. The token arrives straight from the token endpoint, so its signature is not
  re-checked (OpenID Connect Core, 3.1.3.7).
* The person is sent back only to the application root; errors travel as short fixed codes, never as provider text.

Nothing here starts a listener or opens a port.
"""
import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from .oauth import OAuthError, UrllibHttp, _jwt_claims

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SECRET_SLOT = ("plataforma", "google-login")  # (tenant, credential) in the vault; not a company
PENDING_TTL = 600
MAX_PENDING = 200
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
LOGIN_ERRORS = ("nao_configurado", "cancelado", "estado_invalido", "troca_recusada", "provedor_indisponivel", "identidade_invalida",
                "email_nao_verificado", "dominio_nao_permitido", "conta_nao_autorizada", "limite")


class ConfigError(ValueError):
    pass


class LoginFailed(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code if code in LOGIN_ERRORS else "troca_recusada"


@dataclass(frozen=True)
class RemoteConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8766
    public_origin: str = ""
    client_id: str = ""
    allowed_domains: tuple = ()
    idle_minutes: int = 480
    session_hours: int = 168
    tls_cert: str | None = None
    tls_key: str | None = None
    trust_forwarded_for: bool = False

    @classmethod
    def from_config(cls, config):
        """Validate the ``remote`` section of config.json. Disabled (the default) needs nothing else."""
        raw = (config or {}).get("remote") or {}
        if not isinstance(raw, dict):
            raise ConfigError("remote deve ser um objeto")
        known = {"enabled", "bind", "port", "public_origin", "google_client_id", "allowed_domains", "idle_minutes", "session_hours",
                 "tls_cert", "tls_key", "trust_forwarded_for"}
        if set(raw) - known:
            raise ConfigError("remote tem campos desconhecidos: " + ", ".join(sorted(set(raw) - known)))
        if raw.get("enabled") is not True:
            return cls()
        port, idle, hours = raw.get("port", 8766), raw.get("idle_minutes", 480), raw.get("session_hours", 168)
        for name, value, low, high in (("port", port, 1024, 65535), ("idle_minutes", idle, 5, 1440), ("session_hours", hours, 1, 720)):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ConfigError(f"remote.{name} deve ser um inteiro entre {low} e {high}")
        domains = raw.get("allowed_domains", [])
        if not isinstance(domains, list) or any(not isinstance(d, str) or not d.strip() for d in domains):
            raise ConfigError("remote.allowed_domains deve ser uma lista de dominios")
        client_id = raw.get("google_client_id")
        if not isinstance(client_id, str) or not client_id.strip():
            raise ConfigError("remote.google_client_id e obrigatorio")
        origin = _origin(raw.get("public_origin"))
        bind = raw.get("bind", "127.0.0.1")
        cert, key = raw.get("tls_cert"), raw.get("tls_key")
        if not _is_loopback(bind) and not (cert and key):
            raise ConfigError("Escutar fora do loopback exige TLS (remote.tls_cert e remote.tls_key); "
                              "atras de um proxy com TLS, mantenha bind em 127.0.0.1")
        if bool(cert) != bool(key):
            raise ConfigError("Informe remote.tls_cert e remote.tls_key juntos")
        return cls(True, bind, port, origin, client_id.strip(), tuple(d.strip().lower() for d in domains), idle, hours, cert, key,
                   raw.get("trust_forwarded_for") is True)

    @property
    def redirect_uri(self):
        return self.public_origin + "/auth/google/callback"

    @property
    def host(self):
        return urlsplit(self.public_origin).netloc

    @property
    def secure(self):
        return self.public_origin.startswith("https://")


def _is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _origin(value):
    parts = urlsplit(value if isinstance(value, str) else "")
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.path not in ("", "/") \
            or parts.query or parts.fragment:
        raise ConfigError("remote.public_origin deve ser so o endereco (https://nome[:porta]), sem caminho")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise ConfigError("remote.public_origin precisa usar https (http so em localhost, para desenvolvimento)")
    return f"{parts.scheme}://{parts.netloc}".lower()


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class RateLimiter:
    """Sliding-window limiter in memory, keyed by a caller-chosen string. One process, so no shared state is needed."""

    def __init__(self, limit, window, clock=time.monotonic, max_keys=5000):
        self.limit, self.window, self.clock, self.max_keys = limit, window, clock, max_keys
        self.hits = {}

    def allow(self, key):
        now = self.clock()
        queue = self.hits.get(key)
        if queue is None:
            if len(self.hits) >= self.max_keys:
                self.hits = {k: q for k, q in self.hits.items() if q and q[-1] > now - self.window}
                if len(self.hits) >= self.max_keys:
                    return False
            queue = self.hits[key] = deque()
        while queue and queue[0] <= now - self.window:
            queue.popleft()
        if len(queue) >= self.limit:
            return False
        queue.append(now)
        return True


def setup_status(config, vault):
    """What the operator needs to know to finish the setup: the exact address to register at Google and what is still missing."""
    remote = RemoteConfig.from_config(config)
    if not remote.enabled:
        return {"enabled": False, "missing": ["remote.enabled"], "redirect_uri": None, "secret_configured": False}
    try:
        configured = bool(vault.get(*SECRET_SLOT))
    except Exception:
        configured = False
    missing = [] if configured else ["segredo do cliente Google no cofre"]
    return {"enabled": True, "redirect_uri": remote.redirect_uri, "listen": f"{remote.bind}:{remote.port}", "public_origin": remote.public_origin,
            "tls_direct": bool(remote.tls_cert), "allowed_domains": list(remote.allowed_domains), "secret_configured": configured, "missing": missing}


class GoogleLogin:
    def __init__(self, config, vault, http=None, clock=time.time, endpoints=None):
        self.config, self.vault, self.clock = config, vault, clock
        self.http = http or UrllibHttp()
        self.auth_url, self.token_url = (endpoints or {}).get("auth_url", AUTH_URL), (endpoints or {}).get("token_url", TOKEN_URL)
        self.pending = {}

    def configured(self):
        try:
            return bool(self.vault.get(*SECRET_SLOT))
        except Exception:
            return False

    def _prune(self):
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        while len(self.pending) >= MAX_PENDING:
            self.pending.pop(next(iter(self.pending)))

    def begin(self):
        """Return ``(authorization URL, binder)``; the binder goes in the browser's short-lived cookie."""
        if not self.configured():
            raise LoginFailed("nao_configurado")
        self._prune()
        state, nonce, verifier, binder = (secrets.token_urlsafe(24) for _ in range(4))
        self.pending[state] = {"binder": hashlib.sha256(binder.encode()).hexdigest(), "nonce": nonce, "verifier": verifier,
                               "expires": self.clock() + PENDING_TTL}
        query = urlencode({
            "client_id": self.config.client_id, "redirect_uri": self.config.redirect_uri, "response_type": "code",
            "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account",
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"})
        return f"{self.auth_url}?{query}", binder

    def complete(self, params, binder):
        """Check the callback and return ``{sub, email, name}`` of the verified Google identity."""
        state = params.get("state")
        flow = self.pending.pop(state, None) if isinstance(state, str) else None  # single use, even when the rest fails
        if flow is None or flow["expires"] <= self.clock() or not isinstance(binder, str) or not hmac.compare_digest(
                flow["binder"], hashlib.sha256(binder.encode()).hexdigest()):
            raise LoginFailed("estado_invalido")
        if "error" in params:
            raise LoginFailed("cancelado")
        code = params.get("code")
        if not isinstance(code, str) or not code or len(code) > 2048:
            raise LoginFailed("estado_invalido")
        try:
            secret = self.vault.get(*SECRET_SLOT)
        except Exception:
            secret = None
        if not secret:
            raise LoginFailed("nao_configurado")
        try:
            status, data = self.http.post_form(self.token_url, {
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.config.redirect_uri,
                "client_id": self.config.client_id, "client_secret": secret, "code_verifier": flow["verifier"]})
        except OAuthError:
            raise LoginFailed("provedor_indisponivel") from None
        if status != 200 or not isinstance(data.get("id_token"), str):
            raise LoginFailed("troca_recusada")
        return self._identity(_jwt_claims(data["id_token"]), flow["nonce"])

    def _identity(self, claims, nonce):
        now = self.clock()
        audience = claims.get("aud")
        valid_aud = audience == self.config.client_id or (isinstance(audience, list) and self.config.client_id in audience)
        expires = claims.get("exp")
        if (claims.get("iss") not in ISSUERS or not valid_aud or isinstance(expires, bool) or not isinstance(expires, (int, float))
                or expires <= now or claims.get("nonce") != nonce
                or not isinstance(claims.get("sub"), str) or not claims["sub"] or not isinstance(claims.get("email"), str)):
            raise LoginFailed("identidade_invalida")
        if claims.get("email_verified") not in (True, "true"):
            raise LoginFailed("email_nao_verificado")
        if self.config.allowed_domains:
            domain = str(claims.get("hd") or "").lower()
            if domain not in self.config.allowed_domains:
                raise LoginFailed("dominio_nao_permitido")
        name = claims.get("name") if isinstance(claims.get("name"), str) else None
        return {"sub": claims["sub"], "email": claims["email"], "name": name}
