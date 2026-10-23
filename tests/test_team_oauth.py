import dataclasses
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.connections import ConnectionRejected, ConnectionService, CredentialError, LauncherRegistry
from team.ids import new_id
from team.imap_mcp import ImapAccountRegistry, ImapAccounts
from team.oauth import GOOGLE, CALLBACK_PATH, OAuthError, OAuthService, UrllibHttp
from team.repository import SqliteRepository
from team.service import TeamService
from team.vault import MemoryVault
from tests.imap_fake import FakeImap
from tests.oauth_fake import FakeOAuthProvider


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def fetch(url, host=None):
    """GET a URL like a browser would; returns (status, body). HTTP errors are returned, not raised."""
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


class OAuthCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "team.sqlite3"
        self.repo = SqliteRepository(self.path)
        self.repo.migrate()
        self.clock = Clock()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.vault = MemoryVault()
        self.connections = ConnectionService(self.repo, self.access, self.vault, LauncherRegistry(Path(self.temp.name) / "l.json", self.access.operator_id))
        self.provider = FakeOAuthProvider()
        self.addCleanup(self.provider.stop)
        self.imap = FakeImap({"INBOX": {"validity": 11, "messages": {}}}, user=self.provider.email, password="nao-usada")
        self.imap.oauth_tokens = self.provider.access_tokens  # the mail server accepts exactly the tokens the provider issued
        self.addCleanup(self.imap.stop)
        profile = dataclasses.replace(GOOGLE, authorize_url=self.provider.authorize_url, token_url=self.provider.token_url,
                                      revoke_url=self.provider.revoke_url, imap_host="127.0.0.1", imap_port=self.imap.port)
        self.oauth = OAuthService(self.repo, self.access, self.connections, self.vault, UrllibHttp(allow_loopback=True), clock=self.clock,
                                  providers={"google": profile}, callback_timeout=30)
        self.registry = ImapAccountRegistry(Path(self.temp.name) / "imap.json", self.access.operator_id)
        self.accounts = ImapAccounts(self.connections, self.registry)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.conn = self.connections.create_connection(self.ca, label="Gmail", provider="imap", connection_kind="imap")
        self.accounts.configure(self.op, self.ca, self.conn["id"], host="127.0.0.1", port=self.imap.port, username=self.provider.email,
                                folders=["INBOX"], tls=False, auth="xoauth2")
        self.oauth.register_app(self.ca, "google", client_id=self.provider.client_id, client_secret=self.provider.client_secret)

    def connect(self, **kwargs):
        flow = self.oauth.begin(self.ca, self.conn["id"])
        callback = self.provider.authorize(flow["url"], **kwargs)
        status, _ = fetch(callback)
        return flow, callback, status

    def connected(self):
        flow, _, status = self.connect()
        self.assertEqual(status, 200)
        return self.oauth.complete(flow["flow_id"], timeout=5)

    def stored(self):
        """Everything persisted anywhere: documents (the whole database file) and the vault."""
        return self.path.read_bytes().decode("latin-1") + json.dumps({str(k): v for k, v in self.vault.items.items()})


class FlowTests(OAuthCase):
    def test_authorization_code_with_pkce_connects_the_mailbox_and_keeps_tokens_out_of_documents(self):
        flow = self.oauth.begin(self.ca, self.conn["id"])
        query = {k: v[0] for k, v in __import__("urllib.parse").parse.parse_qs(__import__("urllib.parse").parse.urlsplit(flow["url"]).query).items()}
        self.assertEqual((query["code_challenge_method"], query["response_type"], query["access_type"], query["prompt"]), ("S256", "code", "offline", "consent"))
        self.assertTrue(query["redirect_uri"].startswith("http://127.0.0.1:") and query["redirect_uri"].endswith(CALLBACK_PATH))
        self.assertNotIn(self.provider.client_secret, flow["url"])  # the client secret never travels through the browser
        self.assertNotIn("code_verifier", flow["url"])
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "aguardando_consentimento")
        status, page = fetch(self.provider.authorize(flow["url"]))
        self.assertEqual(status, 200)
        self.assertNotIn("code=", page)  # the callback page never reflects what it received
        result = self.oauth.complete(flow["flow_id"], timeout=5)
        self.assertEqual((result["estado"], result["conta"], result["escopos"][0]), ("conectada", self.provider.email, "https://mail.google.com/"))
        self.assertIn("token concedido e mais amplo", result["aviso_de_escopo"])
        refresh = next(iter(self.provider.refresh_tokens))
        access = next(iter(self.provider.access_tokens))
        stored = self.stored()
        self.assertNotIn(access, stored.replace(json.dumps({str(k): v for k, v in self.vault.items.items()}), ""))  # no access token in the database
        self.assertNotIn(refresh, self.path.read_bytes().decode("latin-1"))  # the refresh token is in the vault only
        self.assertIn(refresh, json.dumps({str(k): v for k, v in self.vault.items.items()}))
        self.assertNotIn(self.provider.client_secret, self.path.read_bytes().decode("latin-1"))
        view = self.connections.get_connection(self.ca, self.conn["id"])
        self.assertEqual((view["credential"]["state"], view["credential"]["store"]), ("ativa", "oauth"))
        for text in (json.dumps(result), json.dumps(view)):
            self.assertNotIn(refresh, text)
            self.assertNotIn(access, text)

    def test_the_mailbox_is_read_with_the_access_token_through_xoauth2(self):
        self.connected()
        self.assertEqual(self.accounts.test(self.ca, self.conn["id"])["ok"], True)
        self.assertIn("AUTHENTICATE", self.imap.commands() if hasattr(self.imap, "commands") else [])

    def test_a_wrong_state_or_a_tampered_callback_is_refused_and_does_not_end_the_flow(self):
        flow = self.oauth.begin(self.ca, self.conn["id"])
        good = self.provider.authorize(flow["url"])
        base = good.split("?")[0]
        code = good.split("code=")[1].split("&")[0]
        self.assertEqual(fetch(f"{base}?code={code}&state=estado-errado")[0], 400)
        self.assertEqual(fetch(f"{base}?code={code}")[0], 400)  # no state at all
        self.assertEqual(fetch(base.replace(CALLBACK_PATH, "/outro") + "?" + good.split("?")[1])[0], 404)
        self.assertEqual(fetch(good, host="evil.example")[0], 404)  # a rebinding-style Host header
        self.assertEqual(fetch(good + "&extra=1")[0], 400)  # unexpected parameter
        self.assertEqual(fetch(good + "&code=outro")[0], 400)  # repeated parameter
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "aguardando_consentimento")
        self.assertEqual(fetch(good)[0], 200)  # the genuine callback still works
        self.assertEqual(self.oauth.complete(flow["flow_id"], timeout=5)["estado"], "conectada")

    def test_a_replayed_callback_and_a_tampered_code_do_not_connect_anything(self):
        flow, callback, _ = self.connect()
        self.oauth.complete(flow["flow_id"], timeout=5)
        with self.assertRaises(Exception):
            fetch(callback)  # the one-shot listener is gone: the connection is refused
        second = self.oauth.begin(self.ca, self.conn["id"])
        forged = self.provider.authorize(second["url"]).replace("code=", "code=x")
        self.assertEqual(fetch(forged)[0], 200)  # state matches, so the listener accepts it...
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete(second["flow_id"], timeout=5)  # ...but the provider refuses the code
        self.assertEqual(raised.exception.code, "codigo_invalido")

    def test_too_many_bad_requests_close_the_listener(self):
        flow = self.oauth.begin(self.ca, self.conn["id"])
        base = self.provider.authorize(flow["url"]).split("?")[0]
        for _ in range(6):
            try:
                fetch(f"{base}?state=nada")
            except Exception:
                break
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete(flow["flow_id"], timeout=5)
        self.assertEqual(raised.exception.code, "callback_invalido")

    def test_denied_consent_timeout_and_cancellation(self):
        flow, _, status = self.connect(approve=False)
        self.assertEqual(status, 200)
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete(flow["flow_id"], timeout=5)
        self.assertEqual(raised.exception.code, "consentimento_negado")
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "nao_conectada")
        waiting = self.oauth.begin(self.ca, self.conn["id"])
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete(waiting["flow_id"], timeout=0.2)
        self.assertEqual(raised.exception.code, "tempo_esgotado")
        pending = self.oauth.begin(self.ca, self.conn["id"])
        callback = self.provider.authorize(pending["url"])
        self.oauth.cancel(self.ca, pending["flow_id"])
        with self.assertRaises(Exception):
            fetch(callback)  # the port is closed after cancelling
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete(pending["flow_id"], timeout=1)
        self.assertEqual(raised.exception.code, "cancelado")
        self.assertEqual(self.provider.codes and sum(1 for c in self.provider.codes.values() if c["used"]), 0)

    def test_missing_refresh_token_and_insufficient_scope_are_explained_and_store_nothing(self):
        self.provider.no_refresh_token = True
        flow, _, _ = self.connect()
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete(flow["flow_id"], timeout=5)
        self.assertEqual(raised.exception.code, "sem_refresh_token")
        self.assertIn("Remova o acesso", raised.exception.message)
        self.provider.no_refresh_token, self.provider.scope_override = False, "openid email"
        flow, _, _ = self.connect()
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete(flow["flow_id"], timeout=5)
        self.assertEqual(raised.exception.code, "escopo_insuficiente")
        self.assertEqual(self.vault.items, {(self.a["id"], "oauth-app-google"): self.provider.client_secret})
        self.assertEqual(self.connections.get_connection(self.ca, self.conn["id"])["credential"], None)


class TokenTests(OAuthCase):
    def test_an_expired_access_token_is_refreshed_once_even_with_concurrent_callers(self):
        self.connected()
        first = self.oauth.access_token(self.a["id"], self.conn["id"])
        self.assertEqual(self.oauth.access_token(self.a["id"], self.conn["id"]), first)  # still valid: no provider call
        self.assertEqual(self.provider.refresh_count, 0)
        self.clock.now += timedelta(hours=2)
        results, barrier = [], threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(self.oauth.access_token(self.a["id"], self.conn["id"]))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.provider.refresh_count, 1)  # one refresh served all eight callers
        self.assertEqual(len(set(results)), 1)
        self.assertNotEqual(results[0], first)
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "conectada")
        self.assertIsNotNone(self.oauth.status(self.ca, self.conn["id"])["renovada_em"])

    def test_a_rotated_refresh_token_replaces_the_old_one(self):
        self.provider.rotate = True
        self.connected()
        self.clock.now += timedelta(hours=2)
        self.oauth.access_token(self.a["id"], self.conn["id"])
        self.clock.now += timedelta(hours=2)
        self.oauth.access_token(self.a["id"], self.conn["id"])  # works only if the new refresh token was saved
        self.assertEqual(self.provider.refresh_count, 2)

    def test_access_revoked_at_the_provider_needs_a_new_authorization(self):
        self.connected()
        self.provider.revoke_access()
        self.clock.now += timedelta(hours=2)
        with self.assertRaises(OAuthError) as raised:
            self.oauth.access_token(self.a["id"], self.conn["id"])
        self.assertEqual(raised.exception.code, "reautorizacao_necessaria")
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "revogada")
        with self.assertRaises(CredentialError) as failure:  # a connector gets a clean failure instead of a token
            self.accounts.test(self.ca, self.conn["id"])
        self.assertNotIn("ya29", str(failure.exception))
        flow, _, _ = self.connect()  # authorizing again restores it
        self.assertEqual(self.oauth.complete(flow["flow_id"], timeout=5)["estado"], "conectada")
        self.assertTrue(self.accounts.test(self.ca, self.conn["id"])["ok"])

    def test_a_provider_outage_changes_nothing_and_is_not_confused_with_revocation(self):
        self.connected()
        self.clock.now += timedelta(hours=2)
        self.provider.refresh_status = 503
        with self.assertRaises(OAuthError) as raised:
            self.oauth.access_token(self.a["id"], self.conn["id"])
        self.assertEqual(raised.exception.code, "provedor_indisponivel")
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "conectada")
        self.provider.refresh_status = 200
        self.assertTrue(self.oauth.access_token(self.a["id"], self.conn["id"]))

    def test_disconnecting_revokes_at_the_provider_and_wipes_the_vault(self):
        self.connected()
        refresh = next(iter(self.provider.refresh_tokens))
        result = self.oauth.disconnect(self.ca, self.conn["id"])
        self.assertEqual(result["revogada_no_provedor"], True)
        self.assertEqual(self.provider.revoked, [refresh])
        self.assertNotIn(refresh, json.dumps({str(k): v for k, v in self.vault.items.items()}))
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "revogada")
        with self.assertRaises(Exception):
            self.oauth.access_token(self.a["id"], self.conn["id"])

    def test_disconnecting_with_the_provider_down_still_wipes_locally_and_says_so(self):
        self.connected()
        self.provider.revoke_status = 500
        result = self.oauth.disconnect(self.ca, self.conn["id"])
        self.assertEqual(result["revogada_no_provedor"], False)
        self.assertIn("Remova o acesso", result["aviso"])
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "revogada")


class SafetyTests(OAuthCase):
    def test_only_the_operator_of_the_company_manages_oauth(self):
        flow = self.oauth.begin(self.ca, self.conn["id"])
        for call in (lambda: self.oauth.begin("nao-e-contexto", self.conn["id"]), lambda: self.oauth.disconnect("x", self.conn["id"]),
                     lambda: self.oauth.register_app("x", "google", client_id="abcdefgh12345")):
            with self.assertRaises(PermissionDenied):
                call()
        with self.assertRaises(NotFound):
            self.oauth.begin(self.cb, self.conn["id"])
        with self.assertRaises(NotFound):
            self.oauth.cancel(self.cb, flow["flow_id"])
        with self.assertRaises(NotFound):
            self.oauth.status(self.cb, self.conn["id"])
        self.oauth.cancel(self.ca, flow["flow_id"])

    def test_setup_requires_a_registered_app_an_imap_connection_and_a_known_provider(self):
        other = self.connections.create_connection(self.ca, label="Outra", provider="smtp", connection_kind="smtp")
        with self.assertRaises(ConnectionRejected):
            self.oauth.begin(self.ca, other["id"])
        with self.assertRaises(OAuthError) as raised:
            self.oauth.begin(self.ca, self.conn["id"], provider_key="yahoo")
        self.assertEqual(raised.exception.code, "provedor_desconhecido")
        fresh = self.connections.create_connection(self.cb, label="B", provider="imap", connection_kind="imap")
        with self.assertRaises(OAuthError) as raised:
            self.oauth.begin(self.cb, fresh["id"])  # company B never registered an app
        self.assertEqual(raised.exception.code, "aplicativo_nao_registrado")
        for bad in ("", "curto", "tem espaco no meio", " espaco"):
            with self.assertRaises(ConnectionRejected):
                self.oauth.register_app(self.ca, "google", client_id=bad)

    def test_the_provider_must_use_https_unless_it_is_a_loopback_test_server(self):
        strict = UrllibHttp()
        with self.assertRaises(OAuthError) as raised:
            strict.post_form("http://accounts.exemplo.invalid/token", {"a": "b"})
        self.assertEqual(raised.exception.code, "endereco_inseguro")
        with self.assertRaises(OAuthError):
            strict.post_form(self.provider.token_url, {"a": "b"})  # loopback http is refused unless explicitly allowed

    def test_imap_rejects_a_bad_token_with_an_oauth_specific_hint_and_never_echoes_it(self):
        self.imap.oauth_tokens.clear()
        self.connected()
        self.imap.oauth_tokens.clear()  # the mail server stops accepting the token (for example IMAP disabled)
        result = self.accounts.test(self.ca, self.conn["id"])
        self.assertEqual((result["ok"], result["erro"]), (False, "autenticacao_recusada"))
        self.assertIn("autorize a conexao de novo", result["mensagem"])
        self.assertNotIn("ya29", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
