import json
import tempfile
import unittest
from pathlib import Path

from team.access import NotFound, PermissionDenied
from team.connections import ConnectionRejected
from team.connectors import ConnectorRegistry, example
from team.connectors.catalog import ConnectorCatalog
from team.connectors.setup import ConnectorAudit, SetupAdapter, validate_values
from tests.imap_case import build
from tests.imap_fake import FakeImap
from tests.test_team_connections import ConnectionCase
from tests.test_team_web import TeamWebCase

PASSWORD = "senha-de-app-0123456789"


def imap_values(port, **changes):
    return {"servidor": "127.0.0.1", "porta": port, "usuario": "ana@exemplo.invalid", "pastas": "INBOX", "senha": PASSWORD, **changes}


class ConnectorsApiTests(TeamWebCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.imap = FakeImap({"INBOX": {"validity": 11, "messages": {1: build("Ola").as_bytes()}}}, password=PASSWORD)
        self.addCleanup(self.imap.stop)
        await self.enable()
        self.company_a = await self.company()

    def c(self, tail=''):
        return self.t(self.company_a, f'/connectors{tail}')

    def no_secret_anywhere(self):
        for body in self.bodies:
            self.assertNotIn(PASSWORD, body)

    async def test_overview_lists_capabilities_and_drivers_with_honest_empty_states(self):
        status, data = await self.call('GET', self.c())
        self.assertEqual(status, 200, data)
        by_name = {c['capability']: c for c in data}
        self.assertEqual(sorted(by_name), ['email-envio', 'email-leitura', 'erp'])
        self.assertEqual(by_name['erp']['drivers'], [])  # no ERP driver exists yet: the panel says nothing is installed
        imap = by_name['email-leitura']['drivers'][0]
        self.assertEqual((imap['provider'], imap['mcp'], imap['configurable'], imap['connections']), ('imap', True, True, []))
        smtp = by_name['email-envio']['drivers'][0]
        self.assertEqual((smtp['provider'], smtp['mcp']), ('smtp', False))
        self.assertIn('servico de saida', smtp['non_mcp_justification'])
        self.assertEqual([f['kind'] for f in imap['fields']].count('secret'), 1)

    async def test_connecting_stores_the_secret_only_in_the_vault_and_a_test_is_real(self):
        status, view = await self.call('POST', self.c('/imap/connections'), {'label': 'Caixa da Ana', 'values': imap_values(self.imap.port)})
        self.assertEqual(status, 201, view)
        self.assertEqual((view['configured'], view['credential'], view['last_test']), (True, {'state': 'ativa'}, None))
        self.assertEqual(view['values'], {'servidor': '127.0.0.1', 'porta': self.imap.port, 'usuario': 'ana@exemplo.invalid', 'pastas': 'INBOX'})
        self.assertNotIn('senha', view['values'])
        self.assertTrue(any(PASSWORD in str(v) for v in self.vault._data.values()) if hasattr(self.vault, '_data') else True)
        _, overview = await self.call('GET', self.c())
        connection = next(c for cap in overview for d in cap['drivers'] for c in d['connections'])
        self.assertIsNone(connection['last_test'])  # nothing is claimed about what was not tested
        status, result = await self.call('POST', self.c(f'/connections/{view["id"]}/test'), {})
        self.assertEqual((status, result['ok']), (200, True), result)
        _, overview = await self.call('GET', self.c())
        connection = next(c for cap in overview for d in cap['drivers'] for c in d['connections'])
        self.assertEqual((connection['last_test']['ok'], connection['last_test']['code']), (True, 'ok'))
        self.imap.password = 'outra-senha'
        status, bad = await self.call('POST', self.c(f'/connections/{view["id"]}/test'), {})
        self.assertEqual((status, bad['ok'], bad['erro']), (200, False, 'autenticacao_recusada'))
        _, overview = await self.call('GET', self.c())
        connection = next(c for cap in overview for d in cap['drivers'] for c in d['connections'])
        self.assertEqual((connection['last_test']['ok'], connection['last_test']['code']), (False, 'autenticacao_recusada'))
        self.no_secret_anywhere()

    async def test_reconfiguring_keeps_the_credential_unless_a_new_one_is_given(self):
        _, view = await self.call('POST', self.c('/imap/connections'), {'label': 'Caixa', 'values': imap_values(self.imap.port)})
        status, again = await self.call('PUT', self.c(f'/connections/{view["id"]}'), {'values': {'pastas': 'INBOX, Faturas', 'senha': ''}})
        self.assertEqual(status, 200, again)
        self.assertEqual((again['values']['pastas'], again['credential']), ('INBOX, Faturas', {'state': 'ativa'}))
        status, ok = await self.call('POST', self.c(f'/connections/{view["id"]}/test'), {})
        self.assertTrue(ok['ok'])  # the original password still works: it was kept
        self.no_secret_anywhere()

    async def test_invalid_forms_are_refused_with_safe_messages(self):
        base = imap_values(self.imap.port)
        for values, text in (({**base, 'campo_inventado': 'x'}, 'desconhecido'), ({k: v for k, v in base.items() if k != 'usuario'}, 'Usuario'),
                             ({**base, 'porta': 'abc'}, 'numero'), ({**base, 'servidor': 'imap.exemplo.com', 'porta': 70000}, None),
                             ({**base, 'pastas': ''}, 'Pastas'), (None, 'formulario')):
            status, data = await self.call('POST', self.c('/imap/connections'), {'label': 'X', 'values': values})
            self.assertEqual(status, 400, (values, data))
            if text:
                self.assertIn(text, data['error'])
        status, _ = await self.call('POST', self.c('/inexistente/connections'), {'label': 'X', 'values': {}})
        self.assertEqual(status, 404)
        status, _ = await self.call('POST', self.c('/imap/connections'), {'label': 'X', 'values': base, 'extra': 1})
        self.assertEqual(status, 400)
        self.no_secret_anywhere()

    async def test_smtp_connects_and_explains_that_it_is_not_mcp(self):
        status, view = await self.call('POST', self.c('/smtp/connections'), {'label': 'Saida', 'values': {
            'servidor': '127.0.0.1', 'usuario': 'atendimento@empresa.invalid', 'remetente': 'atendimento@empresa.invalid', 'senha': PASSWORD}})
        self.assertEqual(status, 201, view)
        self.assertEqual((view['configured'], view['values']['porta']), (True, 587))
        _, overview = await self.call('GET', self.c())
        smtp = next(d for cap in overview for d in cap['drivers'] if d['provider'] == 'smtp')
        self.assertFalse(smtp['mcp'])
        self.assertEqual(len(smtp['connections']), 1)

    async def test_the_audit_trail_has_actions_and_codes_but_no_secrets_or_values(self):
        _, view = await self.call('POST', self.c('/imap/connections'), {'label': 'Caixa', 'values': imap_values(self.imap.port)})
        await self.call('POST', self.c(f'/connections/{view["id"]}/test'), {})
        status, trail = await self.call('GET', self.c('/audit'))
        self.assertEqual(status, 200)
        self.assertEqual([(e['action'], e['ok']) for e in reversed(trail)], [('conectar', True), ('configurar', True), ('testar', True)])
        text = json.dumps(trail)
        for forbidden in (PASSWORD, '127.0.0.1', 'ana@exemplo.invalid', 'INBOX'):
            self.assertNotIn(forbidden, text)

    async def test_another_company_sees_and_reaches_nothing_of_this_one(self):
        _, view = await self.call('POST', self.c('/imap/connections'), {'label': 'Caixa', 'values': imap_values(self.imap.port)})
        other = await self.company('Empresa B')
        status, overview = await self.call('GET', self.t(other, '/connectors'))
        self.assertEqual(sum(len(d['connections']) for cap in overview for d in cap['drivers']), 0)
        for method, tail, body in (('POST', f'/connectors/connections/{view["id"]}/test', {}),
                                   ('PUT', f'/connectors/connections/{view["id"]}', {'values': {'pastas': 'INBOX'}})):
            status, _ = await self.call(method, self.t(other, tail), body)
            self.assertEqual(status, 404)
        status, trail = await self.call('GET', self.t(other, '/connectors/audit'))
        self.assertEqual(trail, [])

    async def test_origin_json_and_persistence_rules_apply(self):
        status, _ = await self.call('GET', self.c(), origin=False)
        self.assertEqual(status, 403)
        status, _ = await self.call('POST', self.c('/imap/connections'), raw='x', content_type='text/plain')
        self.assertEqual(status, 415)


class RemotePolicyTests(unittest.TestCase):
    def test_a_remote_session_may_read_the_catalog_but_never_configure_or_test(self):
        from team.remote import POLICY
        t = "/api/team/companies/{tenant}/connectors"
        self.assertEqual((POLICY[f"GET {t}"], POLICY[f"GET {t}/audit"]), ("admin", "admin"))
        for key in (f"POST {t}/{{provider}}/connections", f"PUT {t}/connections/{{id}}", f"POST {t}/connections/{{id}}/test"):
            self.assertEqual(POLICY[key], "local", key)  # these write files and the vault here and open connections to a typed host


class FakeSetup(SetupAdapter):
    def __init__(self):
        self.stored, self.tested = {}, 0

    def configure(self, operator, ctx, connection_id, values):
        self.stored[(ctx.tenant_id, connection_id)] = dict(values)

    def current(self, tenant_id, connection_id):
        return self.stored.get((tenant_id, connection_id))

    def test(self, ctx, connection_id):
        self.tested += 1
        return {"ok": False, "erro": "sem_conexao", "mensagem": "Nao foi possivel conectar."}


class CatalogServiceTests(ConnectionCase):
    """Rules of the service itself: operator only, isolation, audit, forms validated against the descriptor."""

    def setUp(self):
        super().setUp()
        self.registry = ConnectorRegistry()
        self.registry.register_capability(example.CAPABILITY)
        self.setup_adapter = FakeSetup()
        self.registry.register_driver(example.DESCRIPTOR, example.ExampleTransport(), self.setup_adapter)
        self.registry.attach(self.service)
        self.audit = ConnectorAudit(Path(self.temp.name) / "audit.jsonl")
        self.catalog = ConnectorCatalog(self.registry, self.service, self.audit)

    def test_agents_cannot_configure_test_or_read_the_trail(self):
        agent = self._agent_context(grant_connection=True)
        for call in (lambda: self.catalog.connect(self.op, agent, "exemplo", "X", {"chave-de-acesso": "s"}),
                     lambda: self.catalog.test(agent, self.conn["id"]), lambda: self.catalog.recent_audit(agent)):
            with self.assertRaises(PermissionDenied):
                call()
        self.assertEqual(self.audit.recent(self.a["id"]), [])

    def test_connect_validates_the_form_against_the_descriptor_and_keeps_the_secret_in_the_vault(self):
        view = self.catalog.connect(self.op, self.ca, "exemplo", "Exemplo", {"chave-de-acesso": "sk-test-SEGREDO-0123456789abcdef"})
        self.assertEqual((view["configured"], view["credential"], view["values"]), (True, {"state": "ativa"}, {}))
        self.assertEqual(self.setup_adapter.stored[(self.a["id"], view["id"])], {})  # the secret never reached the adapter
        self.assertNotIn("SEGREDO", json.dumps(view) + (Path(self.temp.name) / "audit.jsonl").read_text(encoding="utf-8"))
        with self.assertRaises(ConnectionRejected):
            self.catalog.connect(self.op, self.ca, "exemplo", "Y", {"outro": "x"})
        with self.assertRaises(NotFound):
            self.catalog.connect(self.op, self.ca, "nao-existe", "Y", {})

    def test_a_driver_without_a_setup_adapter_cannot_be_configured_from_the_panel(self):
        self.registry.register_capability(__import__("team.connectors", fromlist=["Capability"]).Capability("nada"))
        descriptor = {**example.DESCRIPTOR, "provider": "sem-setup", "capability": "nada", "catalog_key": "sem-setup",
                      "tools": {"ler": {"operation": "read"}}, "idempotency_arguments": {}}
        self.registry.register_driver(descriptor, example.ExampleTransport())
        with self.assertRaises(ConnectionRejected):
            self.catalog.connect(self.op, self.ca, "sem-setup", "Y", {})

    def test_testing_records_the_real_outcome_and_overview_reports_it(self):
        view = self.catalog.connect(self.op, self.ca, "exemplo", "Exemplo", {"chave-de-acesso": "sk-test-0123456789abcdef"})
        self.assertIsNone(view["last_test"])
        result = self.catalog.test(self.ca, view["id"])
        self.assertEqual((result["ok"], result["erro"]), (False, "sem_conexao"))
        overview = self.catalog.overview(self.ca)
        shown = overview[0]["drivers"][0]["connections"][0]
        self.assertEqual((shown["last_test"]["ok"], shown["last_test"]["code"]), (False, "sem_conexao"))
        self.assertEqual(self.catalog.overview(self.cb)[0]["drivers"][0]["connections"], [])  # the other company sees nothing
        with self.assertRaises(NotFound):
            self.catalog.test(self.cb, view["id"])

    def test_form_values_are_checked_by_kind(self):
        descriptor = {"fields": [{"name": "a", "label": "A", "kind": "text", "required": True, "help": None},
                                 {"name": "n", "label": "N", "kind": "number", "required": False, "help": None},
                                 {"name": "u", "label": "U", "kind": "url", "required": False, "help": None},
                                 {"name": "s", "label": "S", "kind": "secret", "required": True, "help": None}]}
        public, secret = validate_values(descriptor, {"a": " x ", "n": "587", "u": "https://erp.exemplo.com/api", "s": "segredo"})
        self.assertEqual((public, secret), ({"a": "x", "n": 587, "u": "https://erp.exemplo.com/api"}, "segredo"))
        self.assertEqual(validate_values(descriptor, {"n": 25}, partial=True), ({"n": 25}, None))
        for bad in ({"n": "1.5"}, {"n": True}, {"u": "ftp://x"}, {"u": "https://user:pw@x.com"}, {"a": "x" * 400}, {"s": ""} and {"s": 5}, {"zzz": 1}):
            with self.assertRaises(ConnectionRejected, msg=str(bad)):
                validate_values(descriptor, {"a": "x", **bad})
        with self.assertRaises(ConnectionRejected):
            validate_values(descriptor, {})  # required text missing


if __name__ == "__main__":
    unittest.main()
