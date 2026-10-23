import dataclasses
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService, NotFound
from team.connections import ConnectionRejected, ConnectionService, CredentialError, LauncherRegistry
from team.ids import new_id
from team.imap_mcp import ImapAccountRegistry, ImapAccounts
from team.oauth import AADSTS_HELP, CONSUMERS_TENANT, MICROSOFT, OAuthError, OAuthService, UrllibHttp, explain_error
from team.repository import SqliteRepository
from team.service import TeamService
from team.vault import MemoryVault
from tests.imap_fake import FakeImap
from tests.oauth_fake import FakeMicrosoft


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class MicrosoftCase(unittest.TestCase):
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
        self.ms = FakeMicrosoft()
        self.addCleanup(self.ms.stop)
        self.imap = FakeImap({"INBOX": {"validity": 11, "messages": {}}}, user=self.ms.email, password="nao-usada")
        self.imap.oauth_tokens = self.ms.access_tokens
        self.addCleanup(self.imap.stop)
        profile = dataclasses.replace(MICROSOFT, authorize_url=self.ms.authorize_url, token_url=self.ms.token_url, device_url=self.ms.device_url,
                                      imap_host="127.0.0.1", imap_port=self.imap.port)
        self.oauth = OAuthService(self.repo, self.access, self.connections, self.vault, UrllibHttp(allow_loopback=True), clock=self.clock,
                                  providers={"microsoft": profile}, sleep=self.clock.sleep)
        self.registry = ImapAccountRegistry(Path(self.temp.name) / "imap.json", self.access.operator_id)
        self.accounts = ImapAccounts(self.connections, self.registry)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.conn = self.connections.create_connection(self.ca, label="Outlook", provider="imap", connection_kind="imap")
        self.accounts.configure(self.op, self.ca, self.conn["id"], host="127.0.0.1", port=self.imap.port, username=self.ms.email,
                                folders=["INBOX"], tls=False, auth="xoauth2")
        self.register()

    def register(self, tenant=None):
        return self.oauth.register_app(self.ca, "microsoft", client_id=self.ms.client_id, tenant=tenant or self.ms.tenant)

    def sign_in(self, **kwargs):
        flow = self.oauth.begin_device(self.ca, self.conn["id"])
        self.ms.approve_device(flow["user_code"], **kwargs)
        return flow

    def connected(self):
        flow = self.sign_in()
        return self.oauth.complete_device(flow["flow_id"], timeout=60)


class DeviceFlowTests(MicrosoftCase):
    def test_the_device_code_flow_connects_the_mailbox_for_the_allowed_directory(self):
        flow = self.oauth.begin_device(self.ca, self.conn["id"])
        self.assertTrue(flow["user_code"].startswith("ABCD") and flow["verification_uri"].startswith("https://"))
        self.assertIn("nunca digite um codigo que outra pessoa lhe enviou", flow["mensagem"])
        self.assertIn("consentimento pode precisar de um administrador", flow["aviso"])
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "aguardando_consentimento")
        self.ms.approve_device(flow["user_code"])
        status = self.oauth.complete_device(flow["flow_id"], timeout=60)
        self.assertEqual((status["estado"], status["conta"], status["provedor"]), ("conectada", self.ms.email, "microsoft"))
        self.assertIn("IMAP.AccessAsUser.All", status["escopos"][0])
        self.assertIn("mais ampla do que as pastas autorizadas", status["aviso_de_escopo"])
        refresh = next(iter(self.ms.refresh_tokens))
        self.assertIn(refresh, json.dumps({str(k): v for k, v in self.vault.items.items()}))
        self.assertNotIn(refresh, self.path.read_bytes().decode("latin-1"))  # the refresh token is only in the vault
        self.assertNotIn(next(iter(self.ms.access_tokens)), self.path.read_bytes().decode("latin-1"))
        self.assertEqual(self.connections.get_connection(self.ca, self.conn["id"])["credential"]["store"], "oauth")
        self.assertTrue(self.accounts.test(self.ca, self.conn["id"])["ok"])  # XOAUTH2 against the mail server
        self.assertTrue(all(p.startswith(f"/{self.ms.tenant}/") for p in self.ms.paths))  # the chosen directory is in every URL

    def test_polling_waits_for_the_person_and_slows_down_when_told_to(self):
        flow = self.oauth.begin_device(self.ca, self.conn["id"])
        started = self.clock.now
        self.ms.poll_script = [(400, {"error": "authorization_pending"}), (400, {"error": "slow_down"}), (400, {"error": "authorization_pending"})]
        self.ms.approve_device(flow["user_code"])
        self.assertEqual(self.oauth.complete_device(flow["flow_id"], timeout=300)["estado"], "conectada")
        waited = (self.clock.now - started).total_seconds()
        self.assertGreaterEqual(waited, 5 + 10 + 10)  # interval 5, then slow_down adds 5 for the rest
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "conectada")

    def test_declined_expired_cancelled_and_unknown_codes_store_nothing(self):
        flow = self.sign_in(decline=True)
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete_device(flow["flow_id"], timeout=60)
        self.assertEqual(raised.exception.code, "consentimento_negado")
        waiting = self.oauth.begin_device(self.ca, self.conn["id"])
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete_device(waiting["flow_id"], timeout=12)  # nobody approves
        self.assertEqual(raised.exception.code, "tempo_esgotado")
        expired = self.oauth.begin_device(self.ca, self.conn["id"])
        self.ms.poll_script = [(400, {"error": "expired_token"})]
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete_device(expired["flow_id"], timeout=60)
        self.assertEqual(raised.exception.code, "tempo_esgotado")
        cancelled = self.oauth.begin_device(self.ca, self.conn["id"])
        self.oauth.cancel(self.ca, cancelled["flow_id"])
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete_device(cancelled["flow_id"], timeout=60)
        self.assertEqual(raised.exception.code, "cancelado")
        with self.assertRaises(NotFound):
            self.oauth.complete_device("nao-existe")
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "nao_conectada")
        self.assertEqual(self.ms.refresh_tokens, {})

    def test_an_account_from_another_directory_is_refused_and_nothing_is_stored(self):
        self.ms.tid = "99999999-8888-7777-6666-555555555555"  # the person signed in with an account of a different organization
        flow = self.sign_in()
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete_device(flow["flow_id"], timeout=60)
        self.assertEqual(raised.exception.code, "tenant_nao_permitido")
        self.assertEqual(self.connections.get_connection(self.ca, self.conn["id"])["credential"], None)
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "nao_conectada")

    def test_organizations_rejects_personal_accounts_and_consumers_requires_them(self):
        self.register(tenant="organizations")
        self.ms.tid = CONSUMERS_TENANT
        flow = self.sign_in()
        with self.assertRaises(OAuthError) as raised:
            self.oauth.complete_device(flow["flow_id"], timeout=60)
        self.assertEqual(raised.exception.code, "tenant_nao_permitido")
        self.register(tenant="consumers")
        flow = self.sign_in()
        self.assertEqual(self.oauth.complete_device(flow["flow_id"], timeout=60)["estado"], "conectada")

    def test_the_allowed_directory_is_required_and_validated(self):
        for bad in (None, "", "tem espaco", "../x", "a" * 5, "http://x"):
            with self.assertRaises(ConnectionRejected, msg=str(bad)):
                self.oauth.register_app(self.ca, "microsoft", client_id=self.ms.client_id, tenant=bad)
        self.assertEqual(self.register(tenant="Contoso.OnMicrosoft.com")["directory"], "contoso.onmicrosoft.com")

    def test_the_redirect_flow_is_not_offered_for_this_provider(self):
        with self.assertRaises(OAuthError) as raised:
            self.oauth.begin(self.ca, self.conn["id"], provider_key="microsoft")
        self.assertEqual(raised.exception.code, "fluxo_nao_suportado")


class DiagnosticsTests(MicrosoftCase):
    def test_organization_consent_and_other_refusals_become_actionable_messages(self):
        cases = {"65001": "consentimento_necessario", "90094": "consentimento_do_administrador", "700016": "aplicativo_nao_encontrado",
                 "7000218": "cliente_publico_desativado", "500011": "exchange_indisponivel", "53003": "bloqueado_por_politica"}
        for number, expected in cases.items():
            flow = self.sign_in()
            self.ms.poll_script = [(400, {"error": "invalid_grant", "error_description": f"AADSTS{number}: algo longo e tecnico com detalhes internos 0xABC"})]
            with self.assertRaises(OAuthError) as raised:
                self.oauth.complete_device(flow["flow_id"], timeout=60)
            self.assertEqual(raised.exception.code, expected, number)
            self.assertIn(f"AADSTS{number}", raised.exception.message)
            self.assertNotIn("0xABC", raised.exception.message)  # the provider's own text is never echoed
        self.assertIn("administrador", AADSTS_HELP["65001"][1])
        self.assertEqual(explain_error({"error_description": "AADSTS99999: x"})[0], "troca_recusada")
        self.assertEqual(explain_error({})[0], "troca_recusada")

    def test_the_provider_refusing_to_start_the_device_flow_is_explained(self):
        self.ms.fail_token = None
        self.register(tenant="22222222-2222-2222-2222-222222222222")  # a directory this fake does not know
        with self.assertRaises(OAuthError) as raised:
            self.oauth.begin_device(self.ca, self.conn["id"])
        self.assertEqual(raised.exception.code, "tenant_nao_encontrado")
        self.oauth.register_app(self.ca, "microsoft", client_id="outro-aplicativo-9999", tenant=self.ms.tenant)
        with self.assertRaises(OAuthError) as raised:
            self.oauth.begin_device(self.ca, self.conn["id"])
        self.assertEqual(raised.exception.code, "aplicativo_nao_encontrado")

    def test_imap_disabled_or_a_refused_token_points_at_the_account_not_the_password(self):
        self.connected()
        self.imap.oauth_tokens.clear()  # Exchange refuses the token (for example IMAP disabled for the mailbox)
        result = self.accounts.test(self.ca, self.conn["id"])
        self.assertEqual((result["ok"], result["erro"]), (False, "autenticacao_recusada"))
        self.assertIn("IMAP desativado na conta", result["mensagem"])
        self.assertIn("autorize a conexao de novo", result["mensagem"])
        self.assertNotIn("senha", result["mensagem"].lower().replace("senha de aplicativo", ""))  # no advice to use the main password


class LifecycleTests(MicrosoftCase):
    def test_refresh_reauthorization_and_logout(self):
        self.connected()
        first = self.oauth.access_token(self.a["id"], self.conn["id"])
        self.clock.now += timedelta(hours=2)
        second = self.oauth.access_token(self.a["id"], self.conn["id"])
        self.assertNotEqual(first, second)
        self.assertEqual(self.ms.refresh_count, 1)
        self.ms.fail_token = (400, {"error": "invalid_grant", "error_description": "AADSTS50173: The provided grant has expired due to it being revoked."})
        self.clock.now += timedelta(hours=2)
        with self.assertRaises(OAuthError) as raised:
            self.oauth.access_token(self.a["id"], self.conn["id"])
        self.assertEqual(raised.exception.code, "reautorizacao_necessaria")
        status = self.oauth.status(self.ca, self.conn["id"])
        self.assertEqual((status["estado"], status["erro"]), ("revogada", "reautorizacao_necessaria"))
        with self.assertRaises(CredentialError):
            self.accounts.test(self.ca, self.conn["id"])
        self.ms.fail_token = None
        self.assertEqual(self.oauth.complete_device(self.sign_in()["flow_id"], timeout=60)["estado"], "conectada")
        result = self.oauth.disconnect(self.ca, self.conn["id"])
        self.assertEqual(result["revogada_no_provedor"], False)
        self.assertIn("myapps.microsoft.com", result["aviso"])  # honest: the token must be removed at the provider
        self.assertEqual(self.oauth.status(self.ca, self.conn["id"])["estado"], "revogada")
        self.assertEqual({k: v for k, v in self.vault.items.items()}, {})  # refresh token wiped from the vault

    def test_no_token_or_secret_reaches_any_result(self):
        status = self.connected()
        text = json.dumps([status, self.oauth.status(self.ca, self.conn["id"]), self.connections.get_connection(self.ca, self.conn["id"])])
        for token in list(self.ms.access_tokens) + list(self.ms.refresh_tokens):
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
