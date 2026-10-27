"""Contract kit for connector drivers: mix into an ``IsolatedAsyncioTestCase`` and set up the hooks.

    class MyDriverContract(ConnectorContractMixin, unittest.IsolatedAsyncioTestCase):
        def setUp(self):
            self.descriptor, self.transport = MY_DESCRIPTOR, MyTransport()
        def seed(self, tenant_id, marker): ...   # store data for one company that carries ``marker``
        def probe(self): ...                      # -> (read tool, arguments) that would list the seeded data
        def effects(self): ...                    # -> number of real side effects the driver has performed

The checks cover what every driver must guarantee regardless of vendor: trusted classification, arguments, dry-run by
default, no secret in results, and isolation between companies. Vendor specifics belong in the driver's own tests.
"""
import json

from team.connectors import validate_descriptor
from team.gateway import InvalidArguments, validate_arguments

SECRET = "sk-test-SEGREDO-CONTRATO-0123456789"
_SAMPLES = {"string": "x", "integer": 1, "number": 1, "boolean": False}


def sample_arguments(spec, with_dry_run=False):
    """Smallest valid arguments for a tool spec (required only); ``dry_run`` is left out unless asked."""
    arguments = {}
    for name, rule in spec.get("arguments", {}).items():
        if name == "dry_run" and not with_dry_run:
            continue
        if rule.get("required") or name == "dry_run":
            arguments[name] = rule["enum"][0] if "enum" in rule else _SAMPLES[rule["type"]]
    return arguments


class ConnectorContractMixin:
    def seed(self, tenant_id, marker):
        raise NotImplementedError

    def probe(self):
        raise NotImplementedError

    def effects(self):
        raise NotImplementedError

    def server(self):
        return {"transport": self.descriptor.get("mcp_transport"), "key": self.descriptor.get("catalog_key", self.descriptor["provider"])}

    def identity(self, tenant_id="tenant-a", operation="read"):
        return {"tenant_id": tenant_id, "agent_id": "agent-1", "run_id": "run-1", "connection_id": "conn-1", "operation": operation}

    def tools(self):
        return validate_descriptor(self.descriptor)["tools"]

    async def call(self, tool, arguments, tenant_id="tenant-a", secret=SECRET):
        spec = self.tools()[tool]
        return await self.transport.call_tool(self.server(), tool, validate_arguments(spec.get("arguments", {}), arguments),
                                              identity=self.identity(tenant_id, spec["operation"]), secret=secret)

    # -- the contract ------------------------------------------------------------------------------
    def test_descriptor_is_valid_and_every_tool_is_classified(self):
        for name, spec in self.tools().items():
            self.assertIn(spec["operation"], ("read", "write", "send", "spend"), name)
            if spec["operation"] != "read":
                self.assertEqual(spec["arguments"]["dry_run"]["type"], "boolean", name)

    async def test_remote_claims_agree_with_the_trusted_classification(self):
        for name, spec in self.tools().items():
            meta = await self.transport.describe(self.server(), name)
            self.assertIsNotNone(meta, name)
            self.assertEqual(bool(meta.get("read_only")), spec["operation"] == "read", name)
        self.assertIsNone(await self.transport.describe(self.server(), "ferramenta-inexistente"))

    async def test_unknown_tools_are_refused(self):
        result = await self.transport.call_tool(self.server(), "ferramenta-inexistente", {}, identity=self.identity(), secret=SECRET)
        self.assertIn("erro", result)

    def test_unknown_and_scope_arguments_are_rejected_by_the_declared_rules(self):
        for name, spec in self.tools().items():
            base = sample_arguments(spec)
            for extra in ("tenant_id", "connection_id", "credential", "token", "campo_inventado"):
                with self.assertRaises(InvalidArguments, msg=f"{name}.{extra}"):
                    validate_arguments(spec.get("arguments", {}), {**base, extra: "x"})

    async def test_changing_tools_do_nothing_unless_dry_run_is_explicitly_off(self):
        for name, spec in self.tools().items():
            if spec["operation"] == "read":
                continue
            before = self.effects()
            result = await self.call(name, sample_arguments(spec))
            self.assertIs(result.get("dry_run"), True, name)
            self.assertEqual(self.effects(), before, name)

    async def test_results_never_echo_the_secret(self):
        for name, spec in self.tools().items():
            result = await self.call(name, sample_arguments(spec))
            self.assertNotIn(SECRET, json.dumps(result, default=str), name)

    async def test_companies_are_isolated(self):
        self.seed("tenant-a", "MARCADOR-DA-EMPRESA-A")
        tool, arguments = self.probe()
        own = await self.call(tool, arguments, tenant_id="tenant-a")
        other = await self.call(tool, arguments, tenant_id="tenant-b")
        self.assertIn("MARCADOR-DA-EMPRESA-A", json.dumps(own, default=str))
        self.assertNotIn("MARCADOR-DA-EMPRESA-A", json.dumps(other, default=str))
