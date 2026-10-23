import contextlib
import io
import sys
import unittest
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

from team.login import SECRET_SLOT, RemoteConfig, setup_status
from team.remote import RemoteState, _redirect, client_key, csrf_token
from team.vault import MemoryVault

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import remote_setup  # noqa: E402

REMOTE = {"remote": {"enabled": True, "public_origin": "https://pc.example.invalid", "google_client_id": "id.apps.googleusercontent.com"}}


class SetupScriptTests(unittest.TestCase):
    def run_script(self, command, vault, config=None, secret=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = remote_setup.main([command], vault=vault, config=REMOTE if config is None else config, secret_reader=lambda prompt: secret)
        return code, out.getvalue(), err.getvalue()

    def test_status_shows_the_address_to_register_and_what_is_missing_without_leaking_the_secret(self):
        vault = MemoryVault()
        code, out, _ = self.run_script("status", vault)
        self.assertEqual(code, 0)
        self.assertIn("https://pc.example.invalid/auth/google/callback", out)
        self.assertIn("segredo do cliente Google no cofre", out)
        vault.put(*SECRET_SLOT, "segredo-super-secreto-123")
        code, out, _ = self.run_script("status", vault)
        self.assertNotIn("segredo-super-secreto-123", out)
        self.assertEqual(setup_status(REMOTE, vault)["missing"], [])

    def test_the_secret_goes_only_to_the_vault_and_can_be_removed(self):
        vault = MemoryVault()
        code, out, err = self.run_script("set-secret", vault, secret="  segredo-super-secreto-123 ")
        self.assertEqual(code, 0)
        self.assertEqual(vault.get(*SECRET_SLOT), "segredo-super-secreto-123")
        self.assertNotIn("segredo-super-secreto-123", out + err)
        self.assertEqual(self.run_script("set-secret", vault, secret="   ")[0], 1)
        self.assertEqual(vault.get(*SECRET_SLOT), "segredo-super-secreto-123")      # an empty answer changes nothing
        self.assertEqual(self.run_script("clear-secret", vault)[0], 0)
        self.assertIsNone(vault.get(*SECRET_SLOT))

    def test_an_invalid_configuration_is_reported_not_traced(self):
        bad = {"remote": {**REMOTE["remote"], "bind": "0.0.0.0"}}
        code, _, err = self.run_script("status", MemoryVault(), config=bad)
        self.assertEqual(code, 2)
        self.assertIn("TLS", err)

    def test_status_when_remote_is_off(self):
        code, out, _ = self.run_script("status", MemoryVault(), config={})
        self.assertEqual(code, 0)
        self.assertIn('"enabled": false', out)


class CookieAndClientTests(unittest.TestCase):
    def state(self, secure=True, trust=False):
        config = RemoteConfig.from_config({"remote": {**REMOTE["remote"], "public_origin": "https://pc.example.invalid" if secure else "http://localhost:8766",
                                                     "trust_forwarded_for": trust}})
        return RemoteState(config, None, None)

    def test_over_https_the_session_cookie_is_host_prefixed_secure_httponly_and_lax(self):
        state = self.state()
        self.assertEqual((state.session_cookie, state.login_cookie), ("__Host-assistente_session", "__Host-assistente_login"))
        response = _redirect("/", state, clear_login=True, session="token-de-sessao-longo-o-bastante")
        cookie = response.cookies["__Host-assistente_session"]
        self.assertEqual((cookie["secure"], cookie["httponly"], cookie["samesite"], cookie["path"]), (True, True, "Lax", "/"))
        self.assertEqual(int(cookie["max-age"]), 168 * 3600)
        self.assertNotIn("domain", {k for k, v in cookie.items() if v})                 # a __Host- cookie has no Domain

    def test_on_plain_localhost_for_development_the_names_have_no_prefix(self):
        state = self.state(secure=False)
        self.assertEqual(state.session_cookie, "assistente_session")
        cookie = _redirect("/", state, session="x" * 30).cookies["assistente_session"]
        self.assertFalse(cookie["secure"])

    def test_the_csrf_token_depends_on_the_session_and_is_not_the_session(self):
        self.assertNotEqual(csrf_token("a" * 30), csrf_token("b" * 30))
        self.assertNotIn("a" * 30, csrf_token("a" * 30))
        self.assertEqual(csrf_token("a" * 30), csrf_token("a" * 30))

    def test_forwarded_for_is_trusted_only_from_a_local_proxy_and_only_when_configured(self):
        def request(peer, forwarded):
            headers = {"X-Forwarded-For": forwarded} if forwarded else {}
            return make_mocked_request("GET", "/", headers=headers, transport=type("T", (), {"get_extra_info": lambda self, name, default=None: (peer, 1) if name == "peername" else default})())
        self.assertEqual(client_key(request("127.0.0.1", "203.0.113.9, 198.51.100.7"), True), "198.51.100.7")     # the last hop added by our proxy
        self.assertEqual(client_key(request("127.0.0.1", "203.0.113.9"), False), "127.0.0.1")
        self.assertEqual(client_key(request("198.51.100.20", "203.0.113.9"), True), "198.51.100.20")             # a direct client cannot pick its key
        self.assertEqual(client_key(request("127.0.0.1", None), True), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
