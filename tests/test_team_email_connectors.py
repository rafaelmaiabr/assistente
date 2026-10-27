import copy
import tempfile
import unittest
from pathlib import Path

from team.access import AccessService
from team.connectors import ConnectorRegistry, DescriptorRejected
from team.connectors.mail import (
    EMAIL_READ, EMAIL_SEND, IMAP_DESCRIPTOR, IMAP_PROVIDER, READ_CAPABILITY, SEND_CAPABILITY, SMTP_DESCRIPTOR, SMTP_PROVIDER,
    register_email_drivers)
from team.ids import new_id
from team.imap_client import ImapConfig
from team.repository import SqliteRepository
from team.imap_mcp import CATALOG_KEY, TOOL_SPECS, TOOLS, ImapAccountRegistry, ImapTransport
from tests.connector_contract import SECRET, ConnectorContractMixin
from tests.imap_case import build
from tests.imap_fake import FakeImap
from tests.test_team_imap_mcp import ImapMcpCase

MARKER = "MARCADOR-DA-EMPRESA-A"


class ImapDriverContract(ConnectorContractMixin, unittest.IsolatedAsyncioTestCase):
    """The generic connector kit (#101) against the real IMAP MCP transport, with a fake IMAP server."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.imap = FakeImap({"INBOX": {"validity": 11, "messages": {1: build(MARKER).as_bytes()}}}, password=SECRET)
        self.addCleanup(self.imap.stop)
        repo = SqliteRepository(Path(self.temp.name) / "team.sqlite3")
        repo.migrate()
        access = AccessService(repo, new_id("user"))
        self.operator = access.local_operator()
        self.accounts = ImapAccountRegistry(Path(self.temp.name) / "imap-accounts.json", access.operator_id)
        self.descriptor, self.transport = IMAP_DESCRIPTOR, ImapTransport(self.accounts)

    def seed(self, tenant_id, marker):
        self.accounts.register(self.operator, tenant_id, "conn-1", ImapConfig(
            host="127.0.0.1", port=self.imap.port, username="ana@exemplo.invalid", folders=("INBOX",), tls=False))

    def probe(self):
        return "listar-mensagens", {"pasta": "INBOX"}

    def effects(self):
        return 0  # read-only by construction: there is nothing that could change

    async def test_a_configured_account_answers_through_the_contract_and_never_leaks_the_password(self):
        self.seed("tenant-a", MARKER)
        result = await self.call("listar-mensagens", {"pasta": "INBOX"})
        self.assertIn(MARKER, str(result))
        self.assertNotIn(SECRET, str(result))
        refused = await self.call("listar-mensagens", {"pasta": "Privado"})
        self.assertEqual(refused["erro"], "pasta_nao_autorizada")


class ContractSurfaceTests(unittest.TestCase):
    def test_the_read_contract_is_exactly_the_tool_surface_approved_before_the_layer(self):
        registry = ConnectorRegistry()
        imap, _ = register_email_drivers(registry, object())
        self.assertEqual(imap.descriptor["tools"], TOOL_SPECS)  # same names, arguments and classification
        self.assertEqual(sorted(imap.descriptor["tools"]), sorted(TOOLS))
        self.assertTrue(all(spec["operation"] == "read" for spec in imap.descriptor["tools"].values()))
        self.assertEqual((imap.catalog_key, imap.descriptor["version"]), (CATALOG_KEY, "1.0.0"))
        self.assertEqual((imap.descriptor["protocol"], imap.descriptor["mcp_transport"]), ("mcp", "local"))

    def test_smtp_is_declared_as_a_non_mcp_driver_with_the_reason_and_the_approval_contract(self):
        registry = ConnectorRegistry()
        _, smtp = register_email_drivers(registry, object())
        descriptor = smtp.descriptor
        self.assertEqual((descriptor["protocol"], descriptor["capability"]), ("smtp", SEND_CAPABILITY))
        self.assertIn("servico de saida", descriptor["non_mcp_justification"])
        tool = descriptor["tools"]["enviar-mensagem"]
        self.assertEqual((tool["operation"], tool["recipient_argument"]), ("send", "para"))
        self.assertEqual(descriptor["idempotency_arguments"], {"enviar-mensagem": "chave"})

    def test_smtp_is_never_offered_to_the_gateway_catalog(self):
        registry = ConnectorRegistry()
        register_email_drivers(registry, object())
        with self.assertRaises(DescriptorRejected):
            registry.approve(None, None, SMTP_PROVIDER)

    def test_connections_resolve_to_their_driver_by_provider_and_capability(self):
        registry = ConnectorRegistry()
        imap, smtp = register_email_drivers(registry, object())
        self.assertIs(registry.resolve({"provider": IMAP_PROVIDER}, READ_CAPABILITY), imap)
        self.assertIs(registry.resolve({"provider": SMTP_PROVIDER}, SEND_CAPABILITY), smtp)
        self.assertIsNone(registry.resolve({"provider": SMTP_PROVIDER}, READ_CAPABILITY))
        self.assertIsNone(registry.resolve({"provider": IMAP_PROVIDER}, SEND_CAPABILITY))
        self.assertIsNone(registry.resolve({"provider": "bling"}, READ_CAPABILITY))

    def test_another_mail_driver_must_meet_the_same_contracts(self):
        def broken(mutate, capability_name):
            descriptor = copy.deepcopy(IMAP_DESCRIPTOR if capability_name == READ_CAPABILITY else SMTP_DESCRIPTOR)
            descriptor["provider"], descriptor["catalog_key"] = "outro-email", "outro-email"
            mutate(descriptor)
            registry = ConnectorRegistry()
            registry.register_capability(EMAIL_READ)
            registry.register_capability(EMAIL_SEND)
            with self.assertRaises(DescriptorRejected):
                registry.register_driver(descriptor, object())

        broken(lambda d: d["tools"].pop("ler-mensagem"), READ_CAPABILITY)                              # a read tool is missing
        broken(lambda d: d["tools"]["ler-mensagem"].update(operation="write"), READ_CAPABILITY)         # reads must stay reads
        broken(lambda d: d["tools"]["ler-mensagem"]["arguments"]["uid"].update(type="string"), READ_CAPABILITY)
        broken(lambda d: d["tools"]["ler-mensagem"]["arguments"]["uidvalidity"].pop("required"), READ_CAPABILITY)
        broken(lambda d: d["tools"]["enviar-mensagem"].pop("recipient_argument"), SEND_CAPABILITY)      # approval would not show the recipient
        broken(lambda d: d["tools"]["enviar-mensagem"].update(operation="write"), SEND_CAPABILITY)      # sending is classified as send
        broken(lambda d: d["idempotency_arguments"].clear(), SEND_CAPABILITY)                           # no idempotency key
        broken(lambda d: d["tools"]["enviar-mensagem"]["arguments"]["corpo"].pop("required"), SEND_CAPABILITY)

    def test_a_driver_can_use_a_catalog_key_different_from_its_provider_but_not_a_taken_one(self):
        registry = ConnectorRegistry()
        register_email_drivers(registry, object())
        twin = copy.deepcopy(IMAP_DESCRIPTOR)
        twin["provider"] = "imap-2"
        with self.assertRaises(DescriptorRejected):  # same catalog key as the first IMAP driver
            registry.register_driver(twin, object())
        twin["catalog_key"] = "imap-2"
        registry.register_driver(twin, object())


class GatewayThroughTheLayerTests(ImapMcpCase):
    """The existing IMAP setup, with the gateway's ``local`` transport coming from the registry instead of directly."""

    def setUp(self):
        super().setUp()
        from team.gateway import Gateway
        self.layer = ConnectorRegistry()
        register_email_drivers(self.layer, ImapTransport(self.registry))
        self.layer.attach(self.connections)
        self.gateway = Gateway(self.repo, self.access, self.skills, self.connections,
                               {"http": self.transport, "local": self.layer.local_transport()})

    async def test_reading_works_unchanged_through_the_registry_router(self):
        self.configure()
        execution, run = self.open()
        names = sorted(t["name"] for t in self.gateway.list_tools(execution) if t["name"].startswith(CATALOG_KEY))
        self.assertEqual(names, sorted(f"{CATALOG_KEY}.{tool}" for tool in TOOLS))
        result = await self.gateway.call_tool(execution, f"{CATALOG_KEY}.listar-mensagens", {"pasta": "INBOX"})
        self.assertEqual({m["subject"] for m in result.data["messages"]}, {"Fatura de junho", "Instrucao"})
        self.assertEqual(self.decisions(run)[-1]["decision"], "allowed")
        # the catalog entry the operator approved earlier is the one the driver describes
        entry = self.imap_entry
        self.assertEqual((entry["catalog_key"], entry["entry_version"], entry["tool_specs"]), (CATALOG_KEY, "1.0.0", TOOL_SPECS))

    async def test_the_router_hides_a_server_that_no_driver_serves(self):
        router = self.layer.local_transport()
        self.assertIsNone(await router.describe({"key": "servidor-desconhecido"}, "listar-pastas"))


if __name__ == "__main__":
    unittest.main()
