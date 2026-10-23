"""OAuth 2.0 for a local app: authorization code + PKCE, a one-shot loopback callback, tokens kept out of documents.

* The browser flow uses ``state`` (random, single use, compared in constant time), PKCE ``S256``, a ``nonce``, and a
  redirect to ``http://127.0.0.1:<random port>/oauth/callback`` served by a one-shot local listener that only answers
  GET on that exact path with the exact ``Host`` header, never reflects what it receives, gives up on repeated bad
  requests and on its deadline, and closes after the first valid outcome.
* The **refresh token** lives in the vault (store ``oauth``); the **access token** only in memory with its expiry.
  Documents hold metadata (account, scopes, state), never a token. Results, errors and status never contain tokens.
* ``access_token`` refreshes on demand, one refresh at a time per connection (concurrent callers share it). A refusal
  with ``invalid_grant`` means the access was revoked: the grant becomes ``revogada`` and the user must authorize again.
  A provider outage changes nothing.
* The operator registers the OAuth client (``client_id`` and, if the provider needs it, a client secret in the vault);
  this module never creates an account or a project at a provider.
* The provider's token can be broader than what this app uses: Gmail's IMAP scope is the whole mailbox. The app only
  reads the folders the operator authorized, but the status says plainly that the provider grants more.
"""
import base64
import hashlib
import re
import time
import hmac
import http.server
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .access import AccessContext, NotFound, PermissionDenied
from .connections import ConnectionRejected, CredentialError
from .ids import new_id
from .vault import VaultError

CALLBACK_PATH = "/oauth/callback"
MAX_BAD_REQUESTS = 5
MAX_RESPONSE = 64 * 1024
REFRESH_MARGIN = timedelta(seconds=60)
DEFAULT_TIMEOUT = 180.0
_ALLOWED_PARAMS = {"code", "state", "scope", "authuser", "prompt", "hd", "error", "error_description", "iss", "session_state"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class OAuthProvider:
    key: str
    label: str
    authorize_url: str
    token_url: str
    revoke_url: str | None
    scopes: tuple
    required_scopes: tuple
    imap_host: str
    imap_port: int
    notice: str
    auth_params: tuple = ()
    docs: str = ""
    flows: tuple = ("loopback",)  # how a person can authorize: "loopback" (browser redirect) and/or "device" (code on another screen)
    device_url: str | None = None
    tenant_param: bool = False  # URLs contain {tenant}: the operator must choose which directory may sign in
    revoke_help: str = ""
    account_claims: tuple = ("email",)


GOOGLE = OAuthProvider(
    key="google", label="Google (Gmail)", authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token", revoke_url="https://oauth2.googleapis.com/revoke",
    scopes=("https://mail.google.com/", "openid", "email"), required_scopes=("https://mail.google.com/",),
    imap_host="imap.gmail.com", imap_port=993, auth_params=(("access_type", "offline"), ("prompt", "consent")),
    notice=("O Google so oferece para IMAP o escopo de acesso total a caixa (https://mail.google.com/): o token concedido e mais amplo "
            "do que as pastas autorizadas aqui. Este aplicativo so le as pastas autorizadas e nunca altera mensagens, mas a permissao "
            "no Google continua sendo total ate voce revogar o acesso."),
    docs="https://developers.google.com/gmail/imap/xoauth2-protocol")

MICROSOFT = OAuthProvider(
    key="microsoft", label="Microsoft 365 / Outlook", authorize_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", revoke_url=None,
    device_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode", flows=("device",), tenant_param=True,
    scopes=("https://outlook.office.com/IMAP.AccessAsUser.All", "offline_access", "openid", "email", "profile"),
    required_scopes=("https://outlook.office.com/IMAP.AccessAsUser.All",), imap_host="outlook.office365.com", imap_port=993,
    account_claims=("preferred_username", "email"),
    notice=("A permissao IMAP.AccessAsUser.All da Microsoft da acesso a caixa inteira do usuario (todas as pastas, com direito de alterar), "
            "mais ampla do que as pastas autorizadas aqui. Este aplicativo so le as pastas autorizadas e nunca altera mensagens. "
            "Em organizacoes, o consentimento pode precisar de um administrador do Microsoft 365."),
    revoke_help=("A Microsoft nao oferece revogacao do token por este caminho. O acesso continua concedido ate ser removido em "
                 "https://myapps.microsoft.com (ou pelo administrador do Microsoft 365)."),
    docs="https://learn.microsoft.com/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth")
PROVIDERS = {"google": GOOGLE, "microsoft": MICROSOFT}

CONSUMERS_TENANT = "9188040d-6c67-4c5e-b112-d7b8f5c0b8f3"  # personal Microsoft accounts
_TENANT = re.compile(r"^(common|organizations|consumers|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|(?:[a-z0-9-]+\.)+[a-z]{2,})$", re.I)
_GUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
_AADSTS = re.compile(r"AADSTS(\d{4,7})")
# Known Microsoft sign-in errors turned into something a person can act on (code kept for support, nothing else echoed).
AADSTS_HELP = {
    "65001": ("consentimento_necessario", "O aplicativo ainda nao tem consentimento. Peca a um administrador do Microsoft 365 para conceder o consentimento da organizacao e tente de novo."),
    "90094": ("consentimento_do_administrador", "Esta permissao exige consentimento de um administrador da organizacao."),
    "65004": ("consentimento_negado", "O acesso foi recusado na tela de consentimento."),
    "700016": ("aplicativo_nao_encontrado", "O client_id nao existe neste diretorio. Confira o client_id e o tenant escolhidos."),
    "90002": ("tenant_nao_encontrado", "O tenant informado nao existe. Confira o identificador ou dominio do diretorio."),
    "50020": ("conta_de_outro_diretorio", "A conta pertence a outro diretorio que nao o permitido para este aplicativo."),
    "7000218": ("cliente_publico_desativado", "No registro do aplicativo, ative 'Permitir fluxos de cliente publico' (ou informe um segredo de cliente)."),
    "70011": ("escopo_invalido", "A permissao pedida nao existe ou nao foi adicionada ao registro do aplicativo."),
    "500011": ("exchange_indisponivel", "O Exchange Online nao esta disponivel para este diretorio (servico ou licenca ausente)."),
    "50076": ("mfa_necessaria", "A conta exige verificacao em duas etapas; conclua-a e tente de novo."),
    "53003": ("bloqueado_por_politica", "Uma politica de acesso condicional da organizacao bloqueou o acesso. Fale com o administrador."),
    "50173": ("reautorizacao_necessaria", "A senha da conta mudou ou o acesso expirou; autorize de novo."),
    "70008": ("reautorizacao_necessaria", "A autorizacao expirou; autorize de novo."),
    "700082": ("reautorizacao_necessaria", "O acesso ficou inativo por tempo demais; autorize de novo."),
}


def explain_error(data, default_code="troca_recusada", default_message="O provedor recusou a solicitacao."):
    """``(code, message)`` for a provider error body. Only a known code and, at most, the AADSTS number are exposed."""
    description = str(data.get("error_description") or "") if isinstance(data, dict) else ""
    match = _AADSTS.search(description)
    if match and match.group(1) in AADSTS_HELP:
        code, message = AADSTS_HELP[match.group(1)]
        return code, f"{message} (AADSTS{match.group(1)})"
    if match:
        return default_code, f"{default_message} (AADSTS{match.group(1)})"
    return default_code, default_message



class OAuthError(CredentialError):
    """An actionable OAuth failure with a stable ``code``. The text never contains a token or a secret."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class UrllibHttp:
    """Minimal HTTPS form POST: TLS verified, no redirects, bounded response. Loopback HTTP only when ``allow_loopback``."""

    def __init__(self, allow_loopback=False, timeout=15.0):
        self.allow_loopback, self.timeout = allow_loopback, timeout

    def post_form(self, url, fields):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" and not (self.allow_loopback and parts.scheme == "http" and parts.hostname in _LOOPBACK):
            raise OAuthError("endereco_inseguro", "O provedor precisa usar HTTPS.")
        body = urllib.parse.urlencode(fields).encode("ascii")
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read(MAX_RESPONSE)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(MAX_RESPONSE)
        except (urllib.error.URLError, OSError, TimeoutError):
            raise OAuthError("provedor_indisponivel", "Nao foi possivel falar com o provedor de login agora; tente novamente.") from None
        try:
            data = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwt_claims(token):
    """Claims of a JWT received directly from the token endpoint over TLS (signature not re-checked, per OIDC core 3.1.3.7)."""
    try:
        payload = str(token).split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _Device:
    flow_id: str
    tenant_id: str
    connection_id: str
    app: dict
    provider: OAuthProvider
    device_code: str
    interval: int
    deadline: datetime
    cancelled: bool = False


@dataclass
class _Flow:
    flow_id: str
    tenant_id: str
    connection_id: str
    app: dict
    provider: OAuthProvider
    state: str
    verifier: str
    nonce: str
    redirect_uri: str
    deadline: datetime
    done: threading.Event = field(default_factory=threading.Event)
    outcome: dict | None = None
    bad: int = 0
    server: object = None
    closed: bool = False


class OAuthService:
    def __init__(self, repository, access, connections, vault, http=None, clock=lambda: datetime.now(timezone.utc),
                 providers=None, callback_timeout=DEFAULT_TIMEOUT, sleep=None):
        self.repository, self.access, self.connections, self.vault = repository, access, connections, vault
        self.http, self.clock, self.providers = http or UrllibHttp(), clock, dict(providers or PROVIDERS)
        self.callback_timeout = callback_timeout
        self._flows, self._devices, self._lock = {}, {}, threading.Lock()
        self.sleep = sleep or time.sleep
        self._cache, self._refresh_locks = {}, {}
        connections.token_resolvers["oauth"] = self.access_token

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _operator(self, ctx):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied()

    def _provider(self, key):
        provider = self.providers.get(key)
        if provider is None:
            raise OAuthError("provedor_desconhecido", "Provedor de login desconhecido.")
        return provider

    def _app(self, session, provider_key):
        found = session.list("oauth_app", provider=provider_key, limit=1)
        if not found or found[0]["status"] != "ativo":
            raise OAuthError("aplicativo_nao_registrado", "Registre o aplicativo OAuth deste provedor (client_id) antes de conectar.")
        return found[0]

    @staticmethod
    def _endpoint(provider, app, name):
        url = getattr(provider, name)
        if url is None:
            raise OAuthError("fluxo_nao_suportado", "Este provedor nao oferece esta operacao.")
        return url.format(tenant=app.get("directory") or "common") if provider.tenant_param else url

    def _client_secret(self, tenant_id, app):
        if not app.get("secret_ref"):
            return None
        try:
            return self.vault.get(tenant_id, app["secret_ref"])
        except VaultError:
            raise OAuthError("cofre_indisponivel", "Cofre de credenciais indisponivel.") from None

    # -- operator setup ----------------------------------------------------------------------------------------------
    def register_app(self, ctx, provider_key, *, client_id, client_secret=None, label=None, tenant=None):
        """Record the OAuth client the operator created at the provider. The secret goes to the vault, never to a document."""
        self._operator(ctx)
        provider = self._provider(provider_key)
        if not (isinstance(client_id, str) and 8 <= len(client_id) <= 300 and client_id == client_id.strip() and " " not in client_id):
            raise ConnectionRejected("client_id invalido.")
        if provider.tenant_param:
            if not (isinstance(tenant, str) and _TENANT.match(tenant)):
                raise ConnectionRejected("Informe o tenant permitido: o identificador (GUID) ou dominio do diretorio, ou organizations/consumers/common.")
            tenant = tenant.lower()
        else:
            tenant = None
        secret_ref = None
        if client_secret:
            secret_ref = f"oauth-app-{provider.key}"
            try:
                self.vault.put(ctx.tenant_id, secret_ref, client_secret)
            except VaultError as error:
                raise OAuthError("cofre_indisponivel", str(error)) from None
        now = _stamp(self.clock())
        with self.repository.transaction(ctx.tenant_id) as session:
            existing = session.list("oauth_app", provider=provider.key, limit=1)
            doc = {"kind": "oauth_app", "schema_version": 1, "tenant_id": ctx.tenant_id, "created_at": now, "updated_at": now, "provider": provider.key,
                   "client_id": client_id, "secret_ref": secret_ref, "label": (label or provider.label)[:120], "status": "ativo", "directory": tenant}
            if existing:
                return session.save({**existing[0], **{k: doc[k] for k in ("client_id", "secret_ref", "label", "directory")}, "revision": existing[0]["revision"] + 1,
                                     "updated_at": now})
            return session.save({**doc, "id": new_id("oauth_app"), "revision": 1})

    # -- the browser flow --------------------------------------------------------------------------------------------
    def begin(self, ctx, connection_id, *, provider_key="google", login_hint=None):
        """Start an authorization. Returns the URL the person must open; nothing else happens until the callback arrives."""
        self._operator(ctx)
        provider = self._provider(provider_key)
        if "loopback" not in provider.flows:
            raise OAuthError("fluxo_nao_suportado", "Este provedor usa o codigo de dispositivo (begin_device).")
        app = self._checked_connection_and_app(ctx, connection_id, provider)
        state, verifier, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(64), secrets.token_urlsafe(16)
        flow = _Flow(new_id("correlation"), ctx.tenant_id, connection_id, app, provider, state, verifier, nonce, "",
                     self.clock() + timedelta(seconds=self.callback_timeout))
        server = _CallbackServer(flow, self)
        flow.server, flow.redirect_uri = server, f"http://127.0.0.1:{server.port}{CALLBACK_PATH}"
        query = {"response_type": "code", "client_id": app["client_id"], "redirect_uri": flow.redirect_uri, "scope": " ".join(provider.scopes),
                 "state": state, "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()), "code_challenge_method": "S256",
                 "nonce": nonce, **dict(provider.auth_params)}
        if login_hint:
            query["login_hint"] = str(login_hint)[:320]
        with self._lock:
            self._flows[flow.flow_id] = flow
        server.start(self.callback_timeout)
        return {"flow_id": flow.flow_id, "url": f"{self._endpoint(provider, app, 'authorize_url')}?{urllib.parse.urlencode(query)}",
                "redirect_uri": flow.redirect_uri, "expires_at": _stamp(flow.deadline), "scopes": list(provider.scopes), "aviso": provider.notice}

    def _checked_connection_and_app(self, ctx, connection_id, provider):
        with self.repository.transaction(ctx.tenant_id) as session:
            connection = session.get("connection", connection_id) if isinstance(connection_id, str) else None
            if connection is None:
                raise NotFound()
            if connection["connection_kind"] != "imap" or connection["status"] != "ativo":
                raise ConnectionRejected("A conexao precisa ser do tipo imap e estar ativa.")
            return self._app(session, provider.key)

    def pending(self, tenant_id, connection_id):
        with self._lock:
            return any(f.tenant_id == tenant_id and f.connection_id == connection_id and not f.closed for f in self._flows.values()) or any(
                d.tenant_id == tenant_id and d.connection_id == connection_id and not d.cancelled for d in self._devices.values())

    def cancel(self, ctx, flow_id):
        self._operator(ctx)
        with self._lock:
            device = self._devices.get(flow_id)
            if device is not None and device.tenant_id == ctx.tenant_id:
                device.cancelled = True
                return
            flow = self._flows.get(flow_id)
        if flow is None or flow.tenant_id != ctx.tenant_id:
            raise NotFound()
        self._finish(flow, {"error": "cancelado"})

    def _finish(self, flow, outcome):
        with self._lock:
            if flow.closed:
                return False
            flow.closed, flow.outcome = True, outcome
        flow.server.close()
        flow.done.set()
        return True

    # called by the listener thread ---------------------------------------------------------------------------------
    def _on_callback(self, flow, params):
        if flow.closed or self.clock() > flow.deadline:
            return 410
        if not hmac.compare_digest(str(params.get("state", "")), flow.state):
            flow.bad += 1
            if flow.bad >= MAX_BAD_REQUESTS:
                self._finish(flow, {"error": "callback_invalido"})
            return 400
        if "error" in params:
            self._finish(flow, {"error": "consentimento_negado" if params["error"] == "access_denied" else "erro_do_provedor"})
            return 200
        if not params.get("code"):
            flow.bad += 1
            return 400
        self._finish(flow, {"code": params["code"], "scope": params.get("scope")})
        return 200

    def complete(self, flow_id, timeout=None):
        """Block until the callback, cancellation or the deadline; then exchange the code and store the grant."""
        with self._lock:
            flow = self._flows.get(flow_id)
        if flow is None:
            raise NotFound()
        wait = timeout if timeout is not None else max((flow.deadline - self.clock()).total_seconds(), 0)
        if not flow.done.wait(wait):
            self._finish(flow, {"error": "tempo_esgotado"})
        with self._lock:
            self._flows.pop(flow_id, None)
        outcome = flow.outcome or {}
        messages = {"tempo_esgotado": "O tempo para autorizar acabou; comece de novo.", "cancelado": "A autorizacao foi cancelada.",
                    "consentimento_negado": "O acesso foi negado na tela de consentimento.",
                    "erro_do_provedor": "O provedor devolveu um erro na autorizacao.",
                    "callback_invalido": "Chamadas invalidas demais no retorno da autorizacao; comece de novo."}
        if "error" in outcome:
            raise OAuthError(outcome["error"], messages.get(outcome["error"], "Autorizacao nao concluida."))
        return self._exchange(flow, outcome["code"])

    # -- token exchange and storage ----------------------------------------------------------------------------------
    def _token_request(self, flow_or_app, fields, provider):
        app = flow_or_app
        payload = {**fields, "client_id": app["client_id"]}
        secret = self._client_secret(app["tenant_id"], app)
        if secret:
            payload["client_secret"] = secret
        return self.http.post_form(self._endpoint(provider, app, "token_url"), payload)

    def _exchange(self, flow, code):
        provider, app = flow.provider, flow.app
        status, data = self._token_request(app, {"grant_type": "authorization_code", "code": code, "redirect_uri": flow.redirect_uri,
                                                 "code_verifier": flow.verifier}, provider)
        if status != 200 or "access_token" not in data:
            reason = data.get("error") if isinstance(data.get("error"), str) else "erro"
            if reason == "invalid_grant" and "AADSTS" not in str(data.get("error_description") or ""):
                raise OAuthError("codigo_invalido", "O provedor recusou o codigo de autorizacao; comece de novo.")
            code, message = explain_error(data, "troca_recusada", "O provedor recusou a troca de tokens; confira client_id, tenant e segredo do aplicativo.")
            raise OAuthError(code, message)
        return self._conclude(flow, app, provider, data, flow.nonce)

    def _conclude(self, flow, app, provider, data, nonce):
        """Validate a successful token response, check who signed in, and store the grant (refresh token to the vault)."""
        refresh = data.get("refresh_token")
        granted = str(data.get("scope") or " ".join(provider.scopes)).split()
        if not refresh:
            raise OAuthError("sem_refresh_token", "O provedor nao devolveu um token de renovacao. Remova o acesso deste aplicativo na conta do "
                                                  "provedor e autorize de novo.")
        if not all(scope in granted for scope in provider.required_scopes):
            raise OAuthError("escopo_insuficiente", "O acesso concedido nao inclui a permissao de leitura da caixa de email; autorize de novo e marque todas as permissoes.")
        claims = _jwt_claims(data.get("id_token")) if data.get("id_token") else {}
        if claims and (claims.get("aud") != app["client_id"] or (nonce and claims.get("nonce") and claims["nonce"] != nonce)):
            raise OAuthError("identidade_invalida", "A identidade devolvida pelo provedor nao corresponde a esta autorizacao.")
        self._check_directory(provider, app, claims)
        account = next((claims[c] for c in provider.account_claims if isinstance(claims.get(c), str)), None)
        self._store_grant(flow, app, provider, refresh, granted, account)
        self._remember(flow.tenant_id, flow.connection_id, data)
        return self.status(self._tenant_ctx(flow.tenant_id), flow.connection_id)

    @staticmethod
    def _check_directory(provider, app, claims):
        """The directory (tenant) that signed the person in must be the one the operator allowed."""
        if not provider.tenant_param:
            return
        allowed, tid = str(app.get("directory") or "").lower(), str(claims.get("tid") or "").lower()
        wrong = ((_GUID.match(allowed) and tid != allowed) or (allowed == "organizations" and tid in ("", CONSUMERS_TENANT))
                 or (allowed == "consumers" and tid != CONSUMERS_TENANT) or (allowed == "common" and not tid))
        if wrong:
            raise OAuthError("tenant_nao_permitido", "A conta que entrou pertence a um diretorio diferente do permitido para esta conexao; nada foi guardado.")

    # -- device code flow (headless-friendly; used by Microsoft) ------------------------------------------------------
    def begin_device(self, ctx, connection_id, *, provider_key="microsoft"):
        """Ask the provider for a one-time code the person types on another screen. Nothing is stored until they approve."""
        self._operator(ctx)
        provider = self._provider(provider_key)
        if "device" not in provider.flows:
            raise OAuthError("fluxo_nao_suportado", "Este provedor usa o redirecionamento no navegador (begin).")
        app = self._checked_connection_and_app(ctx, connection_id, provider)
        status, data = self.http.post_form(self._endpoint(provider, app, "device_url"),
                                           {"client_id": app["client_id"], "scope": " ".join(provider.scopes)})
        if status != 200 or not all(isinstance(data.get(k), str) for k in ("device_code", "user_code", "verification_uri")):
            code, message = explain_error(data, "codigo_de_dispositivo_recusado", "O provedor nao iniciou o login por codigo.")
            raise OAuthError(code, message)
        device = _Device(new_id("correlation"), ctx.tenant_id, connection_id, app, provider, data["device_code"],
                         max(int(data.get("interval") or 5), 1), self.clock() + timedelta(seconds=int(data.get("expires_in") or 900)))
        with self._lock:
            self._devices[device.flow_id] = device
        return {"flow_id": device.flow_id, "user_code": data["user_code"], "verification_uri": data["verification_uri"], "expires_at": _stamp(device.deadline),
                "interval": device.interval, "aviso": provider.notice,
                "mensagem": f"Abra {data['verification_uri']} e digite o codigo {data['user_code']}. Confira que o endereco e o oficial do provedor "
                            "e que foi voce quem iniciou esta conexao; nunca digite um codigo que outra pessoa lhe enviou."}

    def complete_device(self, flow_id, timeout=None):
        """Poll until the person approves, declines, the code expires or the flow is cancelled; then store the grant."""
        with self._lock:
            device = self._devices.get(flow_id)
        if device is None:
            raise NotFound()
        limit = self.clock() + timedelta(seconds=timeout) if timeout is not None else device.deadline
        try:
            while True:
                if device.cancelled:
                    raise OAuthError("cancelado", "A autorizacao foi cancelada.")
                if self.clock() >= min(limit, device.deadline):
                    raise OAuthError("tempo_esgotado", "O tempo para autorizar acabou; comece de novo.")
                status, data = self._token_request({**device.app, "tenant_id": device.tenant_id},
                                                   {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": device.device_code},
                                                   device.provider)
                if status == 200 and "access_token" in data:
                    return self._conclude(device, device.app, device.provider, data, None) and self.status(self._tenant_ctx(device.tenant_id), device.connection_id)
                error = data.get("error")
                if error == "authorization_pending":
                    self.sleep(device.interval)
                elif error == "slow_down":
                    device.interval += 5
                    self.sleep(device.interval)
                elif error in ("authorization_declined", "access_denied"):
                    raise OAuthError("consentimento_negado", "O acesso foi recusado na tela de consentimento.")
                elif error == "expired_token":
                    raise OAuthError("tempo_esgotado", "O codigo expirou; comece de novo.")
                elif error == "bad_verification_code":
                    raise OAuthError("codigo_invalido", "O provedor nao reconheceu o codigo; comece de novo.")
                else:
                    code, message = explain_error(data, "troca_recusada", "O provedor recusou o login por codigo.")
                    raise OAuthError(code, message)
        finally:
            with self._lock:
                self._devices.pop(flow_id, None)

    def _tenant_ctx(self, tenant_id):
        return self.access.tenant_context(self.access.local_operator(), tenant_id)

    def _store_grant(self, flow, app, provider, refresh, granted, account):
        now = _stamp(self.clock())
        with self.repository.transaction(flow.tenant_id) as session:
            connection = session.get("connection", flow.connection_id)
            credential = session.get("credential", connection["credential_ref"]) if connection.get("credential_ref") else None
            credential_id = credential["id"] if credential else new_id("credential")
        try:
            self.vault.put(flow.tenant_id, credential_id, refresh)
        except VaultError:
            raise OAuthError("cofre_indisponivel", "Cofre de credenciais indisponivel.") from None
        with self.repository.transaction(flow.tenant_id) as session:
            connection = session.get("connection", flow.connection_id)
            cred = {"kind": "credential", "schema_version": 1, "id": credential_id, "tenant_id": flow.tenant_id, "label": connection["label"][:300],
                    "store": "oauth", "state": "ativa", "updated_at": now}
            existing = session.get("credential", credential_id)
            if existing is None:
                session.save({**cred, "revision": 1, "created_at": now})
                session.save({**connection, "credential_ref": credential_id, "revision": connection["revision"] + 1, "updated_at": now})
            else:
                session.save({**existing, **cred, "created_at": existing["created_at"], "revision": existing["revision"] + 1})
            grants = session.list("oauth_grant", limit=1000)
            grant = next((g for g in grants if g["connection_id"] == flow.connection_id), None)
            doc = {"kind": "oauth_grant", "schema_version": 1, "tenant_id": flow.tenant_id, "updated_at": now, "connection_id": flow.connection_id,
                   "app_id": app["id"], "provider": provider.key, "account": account, "scopes": granted, "state": "conectada", "granted_at": now,
                   "refreshed_at": None, "error_code": None}
            if grant is None:
                session.save({**doc, "id": new_id("oauth_grant"), "revision": 1, "created_at": now})
            else:
                session.save({**grant, **doc, "created_at": grant["created_at"], "revision": grant["revision"] + 1})

    def _remember(self, tenant_id, connection_id, data):
        expires = self.clock() + timedelta(seconds=int(data.get("expires_in") or 3600))
        self._cache[(tenant_id, connection_id)] = (data["access_token"], expires)

    # -- using the grant -----------------------------------------------------------------------------------------------
    def access_token(self, tenant_id, connection_id):
        """A valid access token for the connection, refreshed when it is about to expire. One refresh at a time."""
        cached = self._cache.get((tenant_id, connection_id))
        if cached and cached[1] - self.clock() > REFRESH_MARGIN:
            return cached[0]
        with self._lock:
            lock = self._refresh_locks.setdefault((tenant_id, connection_id), threading.Lock())
        with lock:
            cached = self._cache.get((tenant_id, connection_id))  # another caller may have refreshed while we waited
            if cached and cached[1] - self.clock() > REFRESH_MARGIN:
                return cached[0]
            return self._refresh(tenant_id, connection_id)

    def _grant(self, session, connection_id):
        return next((g for g in session.list("oauth_grant", limit=1000) if g["connection_id"] == connection_id), None)

    def _refresh(self, tenant_id, connection_id):
        with self.repository.transaction(tenant_id) as session:
            grant = self._grant(session, connection_id)
            connection = session.get("connection", connection_id)
            app = session.get("oauth_app", grant["app_id"]) if grant else None
            credential = session.get("credential", connection["credential_ref"]) if connection and connection.get("credential_ref") else None
        if grant is None or app is None or credential is None or credential["store"] != "oauth":
            raise OAuthError("nao_conectada", "A conexao ainda nao foi autorizada no provedor.")
        if grant["state"] != "conectada":
            raise OAuthError("reautorizacao_necessaria", "O acesso foi revogado ou expirou; autorize a conta de novo.")
        try:
            refresh = self.vault.get(tenant_id, credential["id"])
        except VaultError:
            raise OAuthError("cofre_indisponivel", "Cofre de credenciais indisponivel.") from None
        if not refresh:
            raise OAuthError("reautorizacao_necessaria", "O token de renovacao nao esta mais guardado; autorize a conta de novo.")
        provider = self._provider(grant["provider"])
        status, data = self._token_request({**app, "tenant_id": tenant_id}, {"grant_type": "refresh_token", "refresh_token": refresh}, provider)
        if status == 400 and data.get("error") in ("invalid_grant", "interaction_required"):
            self._mark(tenant_id, connection_id, "revogada", explain_error(data, "invalid_grant")[0])
            self._cache.pop((tenant_id, connection_id), None)
            raise OAuthError("reautorizacao_necessaria", "O acesso foi revogado no provedor; autorize a conta de novo.")
        if status != 200 or "access_token" not in data:
            raise OAuthError("provedor_indisponivel", "O provedor de login nao renovou o acesso agora; tente novamente em instantes.")
        if data.get("refresh_token") and data["refresh_token"] != refresh:  # rotation
            try:
                self.vault.put(tenant_id, credential["id"], data["refresh_token"])
            except VaultError:
                raise OAuthError("cofre_indisponivel", "Cofre de credenciais indisponivel.") from None
        self._remember(tenant_id, connection_id, data)
        self._mark(tenant_id, connection_id, "conectada", None, refreshed=True)
        return data["access_token"]

    def _mark(self, tenant_id, connection_id, state, error, refreshed=False):
        now = _stamp(self.clock())
        with self.repository.transaction(tenant_id) as session:
            grant = self._grant(session, connection_id)
            if grant is None:
                return
            changes = {"state": state, "error_code": error, "updated_at": now, "revision": grant["revision"] + 1}
            if refreshed:
                changes["refreshed_at"] = now
            session.save({**grant, **changes})
            if state == "revogada":
                connection = session.get("connection", connection_id)
                credential = session.get("credential", connection["credential_ref"]) if connection.get("credential_ref") else None
                if credential and credential["state"] == "ativa":
                    session.save({**credential, "state": "revogada", "revision": credential["revision"] + 1, "updated_at": now})

    def disconnect(self, ctx, connection_id):
        """Revoke at the provider (best effort), wipe the stored token and mark the grant revoked."""
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            connection = session.get("connection", connection_id) if isinstance(connection_id, str) else None
            grant = self._grant(session, connection_id) if connection else None
            credential = session.get("credential", connection["credential_ref"]) if connection and connection.get("credential_ref") else None
        if connection is None or grant is None or credential is None:
            raise NotFound()
        provider, revoked = self._provider(grant["provider"]), False
        try:
            token = self.vault.get(ctx.tenant_id, credential["id"])
        except VaultError:
            token = None
        if token and provider.revoke_url:
            try:
                status, _ = self.http.post_form(provider.revoke_url, {"token": token})
                revoked = status == 200
            except OAuthError:
                revoked = False
        try:
            self.vault.delete(ctx.tenant_id, credential["id"])
        except VaultError:
            raise OAuthError("cofre_indisponivel", "Acesso revogado, mas o token nao foi apagado do cofre; tente de novo.") from None
        self._cache.pop((ctx.tenant_id, connection_id), None)
        self._mark(ctx.tenant_id, connection_id, "revogada", None)
        return {"revogada_no_provedor": revoked, "aviso": None if revoked else (
            provider.revoke_help or "Nao foi possivel avisar o provedor. Remova o acesso deste aplicativo na conta do provedor para garantir.")}

    def status(self, ctx, connection_id):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            if session.get("connection", connection_id) is None:
                raise NotFound()
            grant = self._grant(session, connection_id)
        if grant is None:
            return {"estado": "aguardando_consentimento" if self.pending(ctx.tenant_id, connection_id) else "nao_conectada", "conta": None,
                    "escopos": [], "aviso_de_escopo": None}
        provider = self._provider(grant["provider"])
        return {"estado": grant["state"], "conta": grant["account"], "escopos": grant["scopes"], "concedida_em": grant["granted_at"],
                "renovada_em": grant["refreshed_at"], "erro": grant["error_code"], "provedor": provider.key, "aviso_de_escopo": provider.notice}


class _CallbackServer:
    """One-shot loopback listener for a single authorization."""

    def __init__(self, flow, service):
        outer = self
        self.flow, self.service = flow, service

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, text="Pode fechar esta janela e voltar ao aplicativo."):
                body = f"<!doctype html><meta charset=utf-8><title>Assistente</title><p>{text}</p>".encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != CALLBACK_PATH or self.headers.get("Host") != f"127.0.0.1:{outer.port}":
                    return self._reply(404, "Endereco invalido.")
                raw = urllib.parse.parse_qs(parts.query, keep_blank_values=True, max_num_fields=20)
                if any(name not in _ALLOWED_PARAMS or len(values) != 1 or len(values[0]) > 2048 for name, values in raw.items()):
                    outer.flow.bad += 1
                    if outer.flow.bad >= MAX_BAD_REQUESTS:
                        outer.service._finish(outer.flow, {"error": "callback_invalido"})
                    return self._reply(400, "Parametros invalidos.")
                status = outer.service._on_callback(outer.flow, {name: values[0] for name, values in raw.items()})
                self._reply(status, "Autorizacao recebida. Pode fechar esta janela." if status == 200 else "Nao foi possivel validar esta autorizacao.")

            def do_POST(self):
                self._reply(405, "Metodo nao permitido.")

            do_PUT = do_DELETE = do_PATCH = do_POST

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = None

    def start(self, timeout):
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        self.timer = threading.Timer(timeout + 1, lambda: self.service._finish(self.flow, {"error": "tempo_esgotado"}))
        self.timer.daemon = True  # never keep the process alive just to enforce a deadline
        self.timer.start()

    def close(self):
        if getattr(self, "timer", None):
            self.timer.cancel()
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
