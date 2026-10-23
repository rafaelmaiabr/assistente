"""The remote listener: a second, separate web application for signed-in people.

It exists only when ``remote.enabled`` is set in config.json, listens on its own port (loopback by default, behind a TLS proxy
such as ``tailscale serve``, or directly with a certificate) and is never started by the portable build on its own. The local
application on 127.0.0.1:8765 is unchanged and is the only place the local owner exists.

What the remote application serves is deliberately small: the interface, ``/auth/*`` and the company-scoped team API. The chat
WebSocket, voices, persistence switch and everything else of the local application are **not registered here**, so no session can
reach them.

Every ``/api/team`` request goes through ``guard``:

1. the Host must be the configured public address (blocks DNS rebinding and access by bare IP);
2. a valid session cookie is required (looked up again each time; revocation is immediate);
3. the browser origin must be the public origin (a same-origin GET may omit it only with ``Sec-Fetch-Site: same-origin``);
   unsafe methods also need ``X-CSRF-Token`` and a JSON body;
4. rate limits apply per session;
5. the route must be in ``POLICY`` (default-deny: a route that is not listed answers 403) and, for company routes, the company
   in the URL must be one the person belongs to with at least the role the route needs.
"""
import asyncio
import hashlib
import hmac
import ipaddress

from aiohttp import web

from settings import ROOT
from . import web as team_web
from .identity import LoginDenied
from .login import LOGIN_ERRORS, LoginFailed, RateLimiter

# Minimum role per route, or "session" (any signed-in person, no company), "local" (never remote) .
POLICY = {
    "GET /api/team/state": "session",
    "POST /api/team/companies": "local",
    "PATCH /api/team/companies/{tenant}": "local",
    "GET /api/team/companies/{tenant}/departments": "leitor",
    "POST /api/team/companies/{tenant}/departments": "admin",
    "PATCH /api/team/companies/{tenant}/departments/{id}": "admin",
    "GET /api/team/companies/{tenant}/agent-options": "admin",
    "GET /api/team/companies/{tenant}/agents": "leitor",
    "POST /api/team/companies/{tenant}/agents": "admin",
    "POST /api/team/companies/{tenant}/agents/validate": "admin",
    "PATCH /api/team/companies/{tenant}/agents/{id}": "admin",
    "GET /api/team/companies/{tenant}/agents/{id}/versions": "leitor",
    "PATCH /api/team/companies/{tenant}/agents/{id}/versions/{version}": "admin",
    "GET /api/team/companies/{tenant}/skills": "admin",
    "GET /api/team/companies/{tenant}/skills/demos": "admin",
    "POST /api/team/companies/{tenant}/skills/review": "admin",
    "POST /api/team/companies/{tenant}/skills/install": "local",
    "PATCH /api/team/companies/{tenant}/skills/{id}": "local",
    "GET /api/team/companies/{tenant}/grants": "admin",
    "POST /api/team/companies/{tenant}/grants": "admin",
    "DELETE /api/team/companies/{tenant}/grants/{id}": "admin",
    "GET /api/team/companies/{tenant}/connections": "admin",
    "POST /api/team/companies/{tenant}/connections": "admin",
    "PATCH /api/team/companies/{tenant}/connections/{id}": "admin",
    "PUT /api/team/companies/{tenant}/connections/{id}/credential": "admin",
    "POST /api/team/companies/{tenant}/connections/{id}/credential/{action}": "admin",
    "DELETE /api/team/companies/{tenant}/connections/{id}/credential": "admin",
    "GET /api/team/companies/{tenant}/cases": "leitor",
    "POST /api/team/companies/{tenant}/cases": "atendente",
    "GET /api/team/companies/{tenant}/cases/{id}": "leitor",
    "POST /api/team/companies/{tenant}/cases/{id}/assign": "atendente",
    "POST /api/team/companies/{tenant}/cases/{id}/{action}": "atendente",
    "GET /api/team/companies/{tenant}/events": "leitor",
    "GET /api/team/companies/{tenant}/attention": "leitor",
    "POST /api/team/companies/{tenant}/approvals/{id}/{action}": "aprovador",
    "POST /api/team/companies/{tenant}/messages/{id}/resolve": "aprovador",
    "GET /api/team/companies/{tenant}/gaps": "leitor",
    "POST /api/team/companies/{tenant}/gaps": "atendente",
    "POST /api/team/companies/{tenant}/gaps/{id}/{action}": "admin",
    "GET /api/team/companies/{tenant}/mcp-catalog": "admin",
    "POST /api/team/companies/{tenant}/mcp-catalog": "local",
    "PATCH /api/team/companies/{tenant}/mcp-catalog/{id}": "local",
    "GET /api/team/companies/{tenant}/members": "admin",
    "POST /api/team/companies/{tenant}/members": "admin",
    "PATCH /api/team/companies/{tenant}/members/{user_id}": "admin",
}
LEVEL = {"leitor": 1, "atendente": 2, "aprovador": 3, "admin": 4}
SAFE_METHODS = ("GET", "HEAD")
IDENTITY = "identity"
STATE = web.AppKey("remote_state", object)


class RemoteState:
    """What the remote application shares between handlers: configuration, login flow and limiters."""

    def __init__(self, config, team_app, login, auth_limiter=None, api_limiter=None, write_limiter=None):
        self.config, self.team_app, self.login = config, team_app, login
        self.auth_limiter = auth_limiter or RateLimiter(20, 60)
        self.api_limiter = api_limiter or RateLimiter(900, 60)
        self.write_limiter = write_limiter or RateLimiter(90, 60)

    @property
    def session_cookie(self):
        return ("__Host-" if self.config.secure else "") + "assistente_session"

    @property
    def login_cookie(self):
        return ("__Host-" if self.config.secure else "") + "assistente_login"


def csrf_token(session_token):
    return hashlib.sha256(b"csrf:" + session_token.encode()).hexdigest()


def client_key(request, trust_forwarded):
    """Who to rate-limit when there is no session: the peer, or the last X-Forwarded-For entry of a trusted local proxy."""
    peer = request.remote or "?"
    if trust_forwarded:
        try:
            if ipaddress.ip_address(peer).is_loopback:
                forwarded = [part.strip() for part in request.headers.get("X-Forwarded-For", "").split(",") if part.strip()]
                if forwarded:
                    return forwarded[-1][:64]
        except ValueError:
            pass
    return peer


def _deny(message, status, code=None, **extra):
    return web.json_response({"error": message, **({"code": code} if code else {}), **extra}, status=status)


def _headers(config):
    headers = {
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin", "Permissions-Policy": "camera=(), geolocation=(), payment=()",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
                                   "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"}
    if config.secure:
        headers["Strict-Transport-Security"] = "max-age=31536000"
    return headers


@web.middleware
async def guard(request, handler):
    state = request.app[STATE]
    config = state.config
    if request.host.lower() != config.host:
        raise web.HTTPForbidden(text="Host nao permitido")
    response = None
    if request.path.startswith("/api/team"):
        response = await _guard_api(request, handler, state)
    try:
        if response is None:
            response = await handler(request)
    except web.HTTPException as error:  # redirects and errors raised by handlers get the same headers
        error.headers.update(_headers(config))
        raise
    if not response.prepared:
        response.headers.update(_headers(config))
    return response


async def _guard_api(request, handler, state):
    config = state.config
    services = state.team_app.services()
    if services is None:
        return _deny("O atendimento persistente esta desligado neste computador.", 503, "persistence_disabled")
    token = request.cookies.get(state.session_cookie)
    identity = await asyncio.to_thread(services.identity.resolve, token)
    if identity is None:
        return _deny("Entre com sua conta Google.", 401, "unauthenticated", login_url="/auth/google/start")
    origin = request.headers.get("Origin")
    safe = request.method in SAFE_METHODS
    same_site = origin is None and request.headers.get("Sec-Fetch-Site") == "same-origin" and safe
    if not same_site and origin != config.public_origin:
        return _deny("Origem nao permitida.", 403)
    if not safe:
        if request.content_type != "application/json":
            return _deny("Envie JSON.", 415)
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf_token(token)):
            return _deny("Sessao expirada; recarregue a pagina.", 403, "csrf")
    limiter = state.api_limiter if safe else state.write_limiter
    if not limiter.allow(("api" if safe else "write", identity.session_id)):
        return web.json_response({"error": "Muitas requisicoes; aguarde um instante."}, status=429, headers={"Retry-After": "30"})
    resource = request.match_info.route.resource
    need = POLICY.get(f"{request.method} {resource.canonical}") if resource is not None else None
    if need is None or need == "local":
        return _deny("Acao nao permitida neste acesso.", 403)
    if need != "session":
        role = identity.memberships.get(request.match_info.get("tenant"))
        if role is None:
            return _deny("Recurso nao encontrado.", 404)  # the same answer as a company that does not exist
        if LEVEL[role] < LEVEL[need]:
            return _deny("Seu papel nesta empresa nao permite esta acao.", 403)
    request[IDENTITY] = identity
    return None


# -- /auth ---------------------------------------------------------------------------------------------------------------
def _redirect(location, state, clear_login=False, session=None):
    response = web.HTTPSeeOther(location=location)
    if clear_login:
        response.del_cookie(state.login_cookie, path="/")
    if session is not None:
        response.set_cookie(state.session_cookie, session, max_age=state.config.session_hours * 3600, path="/", httponly=True,
                            secure=state.config.secure, samesite="Lax")
    return response


def _error_redirect(state, code):
    return _redirect(f"/?login_error={code if code in LOGIN_ERRORS else 'troca_recusada'}", state, clear_login=True)


async def google_start(request):
    state = request.app[STATE]
    if not state.auth_limiter.allow(("auth", client_key(request, state.config.trust_forwarded_for))):
        raise _error_redirect(state, "limite")
    try:
        url, binder = state.login.begin()
    except LoginFailed as error:
        raise _error_redirect(state, error.code) from None
    response = web.HTTPFound(location=url)
    response.set_cookie(state.login_cookie, binder, max_age=600, path="/", httponly=True, secure=state.config.secure, samesite="Lax")
    raise response


async def google_callback(request):
    state = request.app[STATE]
    if not state.auth_limiter.allow(("auth", client_key(request, state.config.trust_forwarded_for))):
        raise _error_redirect(state, "limite")
    services = state.team_app.services()
    if services is None:
        raise _error_redirect(state, "nao_configurado")
    params = {k: v for k, v in request.query.items() if k in ("code", "state", "error")}
    try:
        claims = await asyncio.to_thread(state.login.complete, params, request.cookies.get(state.login_cookie))
        token, _ = await asyncio.to_thread(services.identity.sign_in, claims["sub"], claims["email"], claims["name"])
    except LoginFailed as error:
        raise _error_redirect(state, error.code) from None
    except LoginDenied:
        raise _error_redirect(state, "conta_nao_autorizada") from None
    raise _redirect("/", state, clear_login=True, session=token)


async def session_info(request):
    state = request.app[STATE]
    services = state.team_app.services()
    token = request.cookies.get(state.session_cookie)
    identity = await asyncio.to_thread(services.identity.resolve, token) if services is not None else None
    if identity is None:
        return web.json_response({"mode": "remote", "authenticated": False, "login_url": "/auth/google/start",
                                  "configured": state.login.configured()}, status=401)
    companies = []
    for tenant_id, role in sorted(identity.memberships.items()):
        tenant = services.repo.get_global("tenant", tenant_id)
        if tenant is not None:
            companies.append({"id": tenant_id, "name": tenant["name"], "role": role})
    return web.json_response({"mode": "remote", "authenticated": True, "user": {"email": identity.email, "name": identity.name},
                              "companies": companies, "csrf": csrf_token(token)})


async def logout(request):
    state = request.app[STATE]
    services = state.team_app.services()
    token = request.cookies.get(state.session_cookie)
    if request.headers.get("Origin") != state.config.public_origin or request.content_type != "application/json":
        return _deny("Origem nao permitida.", 403)
    if token and services is not None:
        identity = await asyncio.to_thread(services.identity.resolve, token)
        if identity is not None and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf_token(token)):
            return _deny("Sessao expirada; recarregue a pagina.", 403, "csrf")
        await asyncio.to_thread(services.identity.sign_out, token)
    response = web.json_response({"ok": True})
    response.del_cookie(state.session_cookie, path="/")
    return response


async def health(request):
    return web.json_response({"app": "assistente", "mode": "remote"})


async def index(request):
    path = ROOT / "frontend/dist/index.html"
    if not path.exists():
        raise web.HTTPServiceUnavailable(text="Interface nao compilada.")
    return web.FileResponse(path)


def build_app(config, team_app, login, **limiters):
    """The remote web application. ``config`` is a validated ``RemoteConfig``; nothing is listening until it is run."""
    app = web.Application(middlewares=[guard])
    app[STATE] = RemoteState(config, team_app, login, **limiters)
    team_web.register(app, team_app)
    app.router.add_get("/", index)
    app.router.add_get("/api/health", health)
    app.router.add_get("/auth/session", session_info)
    app.router.add_get("/auth/google/start", google_start)
    app.router.add_get("/auth/google/callback", google_callback)
    app.router.add_post("/auth/logout", logout)
    assets = ROOT / "frontend/dist/assets"
    if assets.exists():
        app.router.add_static("/assets/", assets)
    return app


def ssl_context(config):
    if not config.tls_cert:
        return None
    import ssl
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config.tls_cert, config.tls_key)
    return context
