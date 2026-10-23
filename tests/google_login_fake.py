"""A stand-in for Google's sign-in: issues codes, checks PKCE and the client secret, and returns a signed-in identity."""
import base64
import hashlib
import json
import secrets
import time
import urllib.parse


def jwt(claims):
    part = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256'})}.{part(claims)}.assinatura"


class FakeGoogleLogin:
    """Doubles as the browser side (``authorize``) and as the HTTP client of the token endpoint (``post_form``)."""

    def __init__(self, client_id="cliente-login.apps.googleusercontent.com", client_secret="segredo-do-login-123", redirect_uri=None):
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.codes, self.calls = {}, []
        self.unavailable = False
        self.status = 200

    def authorize(self, url, *, sub="sub-1", email="ana@empresa.invalid", name="Ana", **overrides):
        """What happens when the person approves: returns the callback query (code and state) the browser is sent to."""
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        assert query["client_id"] == self.client_id and query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256", "PKCE S256 is required"
        assert query["scope"] == "openid email profile" and "client_secret" not in query, "no secret in the URL"
        if self.redirect_uri:
            assert query["redirect_uri"] == self.redirect_uri
        code = secrets.token_urlsafe(16)
        claims = {"iss": "https://accounts.google.com", "aud": self.client_id, "sub": sub, "email": email, "email_verified": True,
                  "name": name, "nonce": query["nonce"], "iat": int(time.time()), "exp": int(time.time()) + 600}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        self.codes[code] = {"challenge": query["code_challenge"], "claims": claims, "redirect_uri": query["redirect_uri"]}
        return {"code": code, "state": query["state"]}

    def post_form(self, url, fields):
        from team.oauth import OAuthError
        self.calls.append((url, dict(fields)))
        if self.unavailable:
            raise OAuthError("provedor_indisponivel", "indisponivel")
        entry = self.codes.pop(fields.get("code"), None)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(fields.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
        if (self.status != 200 or entry is None or fields.get("client_secret") != self.client_secret or entry["challenge"] != challenge
                or fields.get("redirect_uri") != entry["redirect_uri"] or fields.get("client_id") != self.client_id):
            return 400, {"error": "invalid_grant", "error_description": "Bad Request"}
        return 200, {"id_token": jwt(entry["claims"]), "access_token": "nao-usado", "token_type": "Bearer"}
