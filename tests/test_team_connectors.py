import copy
import unittest

from team.connections import ConnectionRejected
from team.connectors import Capability, ConnectorRegistry, DescriptorRejected, validate_descriptor
from team.connectors import example
from tests.connector_contract import ConnectorContractMixin
from tests.test_team_connections import ConnectionCase


def descriptor(**changes):
    return {**copy.deepcopy(example.DESCRIPTOR), **changes}


class ExampleDriverContract(ConnectorContractMixin, unittest.IsolatedAsyncioTestCase):
    """The kit run against the example driver; every real driver adds a class like this."""

    def setUp(self):
        self.descriptor, self.transport = example.DESCRIPTOR, example.ExampleTransport()

    def seed(self, tenant_id, marker):
        self.transport.items[tenant_id] = {"k": {"nome": marker}}

    def probe(self):
        return "listar-itens", {}

    def effects(self):
        return self.transport.effects


class LeakyTransport(example.ExampleTransport):
    """Deliberately bad driver: ignores dry-run, echoes the secret and shares data across companies."""

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        if tool == "criar-item":
            self.effects += 1
            return {"dry_run": False, "debug": secret}
        return {"itens": [i for tenant in self.items.values() for i in tenant.values()]}


class KitTests(unittest.TestCase):
    def test_the_kit_fails_a_driver_that_breaks_the_contract(self):
        class BadDriverContract(ExampleDriverContract):  # built here so discovery never runs it as a real suite
            def setUp(self):
                super().setUp()
                self.transport = LeakyTransport()

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(BadDriverContract).run(result)
        failed = {test.id().rsplit(".", 1)[1] for test, _ in result.failures + result.errors}
        self.assertTrue({"test_changing_tools_do_nothing_unless_dry_run_is_explicitly_off", "test_results_never_echo_the_secret",
                         "test_companies_are_isolated"} <= failed, failed)


class DescriptorTests(unittest.TestCase):
    def test_example_is_valid_and_normalized(self):
        clean = validate_descriptor(example.DESCRIPTOR)
        self.assertEqual((clean["provider"], clean["capability"], clean["protocol"]), ("exemplo", "exemplo", "mcp"))
        self.assertEqual(clean["fields"][0]["kind"], "secret")

    def test_non_mcp_needs_a_written_justification(self):
        base = descriptor(protocol="imap", mcp_transport=None)
        with self.assertRaises(DescriptorRejected):
            validate_descriptor(base)
        with self.assertRaises(DescriptorRejected):
            validate_descriptor({**base, "non_mcp_justification": "curto"})
        ok = validate_descriptor({**base, "non_mcp_justification": "Nao ha servidor MCP de IMAP que respeite pastas autorizadas."})
        self.assertEqual(ok["protocol"], "imap")
        with self.assertRaises(DescriptorRejected):  # MCP must not carry a justification, nor a transport without MCP
            validate_descriptor(descriptor(non_mcp_justification="x" * 40))
        with self.assertRaises(DescriptorRejected):
            validate_descriptor({**base, "non_mcp_justification": "x" * 40, "mcp_transport": "http"})

    def test_unclassified_generic_or_reserved_tools_are_rejected(self):
        for mutate in (
                lambda d: d["tools"].update({"sem-classe": {"arguments": {}}}),
                lambda d: d["tools"].update({"shell": {"operation": "read"}}),
                lambda d: d["tools"].update({"run-command": {"operation": "write"}}),
                lambda d: d["tools"]["listar-itens"]["arguments"].update({"tenant_id": {"type": "string"}}),
                lambda d: d["tools"]["listar-itens"].update({"operation": "apagar"}),
                lambda d: d["tools"].clear()):
            broken = descriptor()
            mutate(broken)
            with self.assertRaises(ConnectionRejected):
                validate_descriptor(broken)

    def test_changing_tools_must_support_dry_run(self):
        for operation in ("write", "send", "spend"):
            broken = descriptor()
            broken["tools"]["criar-item"]["operation"] = operation
            del broken["tools"]["criar-item"]["arguments"]["dry_run"]
            with self.assertRaises(DescriptorRejected):
                validate_descriptor(broken)
        wrong = descriptor()
        wrong["tools"]["criar-item"]["arguments"]["dry_run"] = {"type": "string"}
        with self.assertRaises(DescriptorRejected):
            validate_descriptor(wrong)

    def test_unknown_keys_bad_versions_and_bad_fields_are_rejected(self):
        for broken in (descriptor(extra=1), descriptor(version="1"), descriptor(provider="Com Espaco"),
                       descriptor(auth={"type": "senha"}), descriptor(idempotency_arguments={"listar-itens": "busca"}),
                       descriptor(idempotency_arguments={"criar-item": "inexistente"}),
                       descriptor(fields=[{"name": "a", "label": "A", "kind": "segredo"}]),
                       descriptor(fields=[{"name": "a", "label": "A", "kind": "text"}] * 2), "texto", None):
            with self.assertRaises(DescriptorRejected):
                validate_descriptor(broken)


class RegistryTests(unittest.TestCase):
    def make(self):
        registry = ConnectorRegistry()
        registry.register_capability(example.CAPABILITY)
        return registry

    def test_driver_needs_a_known_capability_and_its_required_tools(self):
        with self.assertRaises(DescriptorRejected):
            ConnectorRegistry().register_driver(example.DESCRIPTOR, example.ExampleTransport())
        registry = self.make()
        weak = descriptor()
        weak["tools"]["criar-item"]["operation"] = "send"  # required as write
        with self.assertRaises(DescriptorRejected):
            registry.register_driver(weak, example.ExampleTransport())
        missing = descriptor()
        del missing["tools"]["criar-item"], missing["idempotency_arguments"]["criar-item"]
        with self.assertRaises(DescriptorRejected):
            registry.register_driver(missing, example.ExampleTransport())
        with self.assertRaises(DescriptorRejected):
            registry.register_driver(example.DESCRIPTOR)  # local MCP without a transport

    def test_no_duplicate_provider_or_capability(self):
        registry = self.make()
        with self.assertRaises(DescriptorRejected):
            registry.register_capability(example.CAPABILITY)
        registry.register_driver(example.DESCRIPTOR, example.ExampleTransport())
        with self.assertRaises(DescriptorRejected):
            registry.register_driver(example.DESCRIPTOR, example.ExampleTransport())

    def test_resolve_follows_the_connection_provider_not_the_model(self):
        registry = self.make()
        driver = registry.register_driver(example.DESCRIPTOR, example.ExampleTransport())
        self.assertIs(registry.resolve({"provider": "exemplo"}, "exemplo"), driver)
        self.assertIsNone(registry.resolve({"provider": "exemplo"}, "erp"))
        self.assertIsNone(registry.resolve({"provider": "outro"}, "exemplo"))
        self.assertIsNone(registry.resolve(None, "exemplo"))
        self.assertEqual([d.provider for d in registry.drivers("exemplo")], ["exemplo"])
        self.assertEqual(registry.drivers("erp"), [])


class WiringTests(ConnectionCase, unittest.IsolatedAsyncioTestCase):
    """A new driver reaches the existing connection service and gateway transport without changing them."""

    def setUp(self):
        super().setUp()
        self.registry = ConnectorRegistry()
        self.registry.register_capability(example.CAPABILITY)
        self.transport = example.ExampleTransport()
        self.registry.register_driver(example.DESCRIPTOR, self.transport)
        self.registry.attach(self.service)

    def test_approve_creates_a_classified_catalog_entry_and_connection(self):
        entry = self.registry.approve(self.service, self.op, "exemplo")
        self.assertEqual((entry["catalog_key"], entry["transport"], entry["entry_version"]), ("exemplo", "local", "1.0.0"))
        self.assertEqual(entry["tool_specs"]["criar-item"]["operation"], "write")
        conn = self.service.create_connection(self.ca, label="Exemplo", provider="exemplo", connection_kind="mcp",
                                              catalog_entry_id=entry["id"])
        self.service.set_credential(self.ca, conn["id"], "sk-test-SEGREDO-0123456789abcdef")
        seen = self.service.use_credential(self.ca, conn["id"], "exemplo", lambda secret: len(secret) > 0)
        self.assertTrue(seen)
        self.assertIs(self.registry.resolve(self.service.get_connection(self.ca, conn["id"]), "exemplo").transport, self.transport)

    def test_only_mcp_registered_drivers_enter_the_mcp_catalog(self):
        with self.assertRaises(DescriptorRejected):
            self.registry.approve(self.service, self.op, "nao-registrado")
        self.registry.register_capability(Capability("leitura"))
        self.registry.register_driver({**descriptor(provider="imap-x", capability="leitura", protocol="imap", mcp_transport=None,
                                                    non_mcp_justification="Servidor MCP de IMAP nao respeita pastas autorizadas."),
                                       "tools": {"ler": {"operation": "read"}}, "idempotency_arguments": {}})
        with self.assertRaises(DescriptorRejected):
            self.registry.approve(self.service, self.op, "imap-x")

    async def test_local_router_dispatches_by_catalog_key_and_hides_unknown_servers(self):
        router = self.registry.local_transport()
        server = {"transport": "local", "key": "exemplo"}
        self.assertEqual(await router.describe(server, "listar-itens"), {"read_only": True})
        self.assertIsNone(await router.describe({"key": "desconhecido"}, "listar-itens"))
        self.assertIsNone(await router.describe(None, "listar-itens"))
        identity = {"tenant_id": self.a["id"], "agent_id": "a", "run_id": "r", "connection_id": "c", "operation": "read"}
        self.assertEqual(await router.call_tool(server, "listar-itens", {}, identity=identity, secret=None), {"itens": []})
        refused = await router.call_tool({"key": "desconhecido"}, "listar-itens", {}, identity=identity, secret=None)
        self.assertEqual(refused["erro"], "ferramenta_desconhecida")


if __name__ == "__main__":
    unittest.main()
