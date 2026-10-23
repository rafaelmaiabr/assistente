"""A fake OAuth 2.0 provider (authorization-code + PKCE, refresh, revoke) served on a loopback port."""
import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse


def _jwt(claims):
    part = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'none'})}.{part(claims)}.assinatura"


class FakeOAuthProvider:
    def __init__(self, client_id="cliente-teste-1234567", client_secret="segredo-do-app-456", email="atendimento@empresa.invalid"):
        self.client_id, self.client_secret, self.email = client_id, client_secret, email
        self.codes, self.refresh_tokens, self.access_tokens = {}, {}, set()
        self.calls, self.refresh_count, self.revoked = [], 0, []
        self.expires_in, self.rotate, self.no_refresh_token, self.scope_override = 3600, False, False, None
        self.refresh_status, self.revoke_status, self.delay = 200, 200, 0.0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                fields = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(size).decode()).items()}
                outer.calls.append((self.path, fields))
                status, body = outer.handle(self.path, fields)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.authorize_url = f"http://127.0.0.1:{self.port}/authorize"
        self.token_url = f"http://127.0.0.1:{self.port}/token"
        self.revoke_url = f"http://127.0.0.1:{self.port}/revoke"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- the "browser" side ------------------------------------------------------------------------------------------
    def authorize(self, url, *, approve=True, scope=None):
        """What the provider does after the person approves: validate the request and return the callback URL to open."""
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        assert query["client_id"] == self.client_id and query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256", "PKCE S256 is required"
        assert query["redirect_uri"].startswith("http://127.0.0.1:"), "only a loopback redirect is allowed"
        if not approve:
            return query["redirect_uri"] + "?" + urllib.parse.urlencode({"error": "access_denied", "state": query["state"]})
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"challenge": query["code_challenge"], "redirect_uri": query["redirect_uri"], "nonce": query.get("nonce"),
                            "scope": scope or self.scope_override or query["scope"], "used": False}
        return query["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": query["state"]})

    def revoke_access(self):
        """The person removes the app's access in the provider's account page."""
        for token in self.refresh_tokens:
            self.refresh_tokens[token]["revoked"] = True

    # -- the endpoints -----------------------------------------------------------------------------------------------
    def claims(self):
        return {"email": self.email}

    def _tokens(self, scope, nonce=None, with_refresh=True, refresh=None):
        access = "ya29." + secrets.token_urlsafe(32)
        self.access_tokens.add(access)
        body = {"access_token": access, "expires_in": self.expires_in, "scope": scope, "token_type": "Bearer",
                "id_token": _jwt({"aud": self.client_id, "nonce": nonce, "exp": int(time.time()) + 3600, **self.claims()})}
        if with_refresh and not self.no_refresh_token:
            refresh = refresh or "1//" + secrets.token_urlsafe(40)
            self.refresh_tokens.setdefault(refresh, {"revoked": False, "scope": scope})
            body["refresh_token"] = refresh
        return body

    def handle(self, path, fields):
        if self.delay:
            time.sleep(self.delay)
        if path == "/revoke":
            self.revoked.append(fields.get("token"))
            if fields.get("token") in self.refresh_tokens:
                self.refresh_tokens[fields["token"]]["revoked"] = True
            return self.revoke_status, {}
        if fields.get("client_id") != self.client_id or (self.client_secret and fields.get("client_secret") != self.client_secret):
            return 401, {"error": "invalid_client"}
        if fields.get("grant_type") == "authorization_code":
            record = self.codes.get(fields.get("code", ""))
            if not record or record["used"] or record["redirect_uri"] != fields.get("redirect_uri"):
                return 400, {"error": "invalid_grant"}
            expected = base64.urlsafe_b64encode(hashlib.sha256(fields.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
            if expected != record["challenge"]:
                return 400, {"error": "invalid_grant", "error_description": "PKCE verification failed"}
            record["used"] = True
            return 200, self._tokens(record["scope"], record["nonce"])
        if fields.get("grant_type") == "refresh_token":
            self.refresh_count += 1
            if self.refresh_status != 200:
                return self.refresh_status, {"error": "temporarily_unavailable"}
            record = self.refresh_tokens.get(fields.get("refresh_token", ""))
            if not record or record["revoked"]:
                return 400, {"error": "invalid_grant"}
            body = self._tokens(record["scope"], with_refresh=self.rotate, refresh=None)
            if self.rotate:
                record["revoked"] = True  # the old one is spent
            else:
                body.pop("refresh_token", None)
            return 200, body
        return 400, {"error": "unsupported_grant_type"}


class FakeMicrosoft(FakeOAuthProvider):
    """Microsoft identity platform stand-in: tenant in the path, public client, device-code flow and AADSTS errors."""

    def __init__(self, tenant="11111111-2222-3333-4444-555555555555", email="ana@contoso.invalid"):
        super().__init__(client_id="aplicativo-microsoft-1234", client_secret=None, email=email)
        self.tenant, self.tid = tenant, tenant
        self.devices, self.paths, self.poll_script, self.fail_token = {}, [], [], None
        self.device_url = f"http://127.0.0.1:{self.port}/{{tenant}}/oauth2/v2.0/devicecode"
        self.token_url = f"http://127.0.0.1:{self.port}/{{tenant}}/oauth2/v2.0/token"
        self.revoke_url = None
        self.authorize_url = f"http://127.0.0.1:{self.port}/{{tenant}}/oauth2/v2.0/authorize"

    def claims(self):
        return {"preferred_username": self.email, "tid": self.tid}

    def approve_device(self, user_code, *, decline=False):
        for record in self.devices.values():
            if record["user_code"] == user_code:
                record["state"] = "declined" if decline else "approved"
                return
        raise AssertionError("unknown user code")

    def handle(self, path, fields):
        self.paths.append(path)
        parts = path.strip("/").split("/")
        tenant, endpoint = parts[0], parts[-1]
        if tenant not in (self.tenant, "organizations", "common", "consumers"):
            return 400, {"error": "invalid_request", "error_description": "AADSTS90002: Tenant not found."}
        if fields.get("client_id") != self.client_id:
            return 400, {"error": "unauthorized_client", "error_description": "AADSTS700016: Application not found in the directory."}
        if endpoint == "devicecode":
            device_code, user_code = secrets.token_urlsafe(24), "ABCD" + secrets.token_hex(2).upper()
            self.devices[device_code] = {"user_code": user_code, "state": "pending", "scope": fields.get("scope", "")}
            return 200, {"device_code": device_code, "user_code": user_code, "verification_uri": "https://login.microsoft.invalid/device",
                         "expires_in": 900, "interval": 5, "message": "ignored"}
        if self.fail_token:
            return self.fail_token
        if fields.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
            record = self.devices.get(fields.get("device_code", ""))
            if record is None:
                return 400, {"error": "bad_verification_code"}
            if self.poll_script:
                return self.poll_script.pop(0)
            if record["state"] == "pending":
                return 400, {"error": "authorization_pending"}
            if record["state"] == "declined":
                return 400, {"error": "authorization_declined"}
            return 200, self._tokens(self.scope_override or record["scope"].replace(" offline_access", ""))
        return super().handle("/token", fields)
