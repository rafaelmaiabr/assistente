"""Capabilities, drivers and their registry.

A *capability* (``erp``, ``email``...) names what a company wants done; a *driver* (Bling, IMAP...) is the vendor
that does it. The registry is trusted backend wiring: agents and skills never pick a vendor, the company's
connection (``provider``) does. Registering a driver attaches it to the existing pieces (connector name for the
credential vault, approved catalog entry for the gateway, a local transport router) and changes none of them.
"""
from dataclasses import dataclass, field

from ..enums import OPERATIONS
from ..gateway import McpTransport
from .descriptor import DescriptorRejected, validate_descriptor


@dataclass(frozen=True)
class Capability:
    """What a company can ask for, independent of the vendor.

    ``required`` maps each tool every driver must offer to its exact operation class; ``optional`` does the same for
    tools a driver may leave out (it then *does not support* them; nothing is simulated). For any contract tool the
    driver offers, ``arguments`` (tool -> {argument: (type, required)}) must be declared as given, ``bindings``
    (tool -> {approval key: argument}) must match the approval fields, and ``idempotent`` (tool -> argument) names the
    argument that carries the vendor idempotency key. Drivers may add arguments and tools of their own.
    """
    name: str
    required: dict = field(default_factory=dict)
    optional: dict = field(default_factory=dict)
    arguments: dict = field(default_factory=dict)
    bindings: dict = field(default_factory=dict)
    idempotent: dict = field(default_factory=dict)

    def operations(self):
        return {**self.optional, **self.required}


@dataclass(frozen=True)
class Driver:
    descriptor: dict
    transport: McpTransport | None = None

    @property
    def provider(self):
        return self.descriptor["provider"]

    @property
    def capability(self):
        return self.descriptor["capability"]

    @property
    def catalog_key(self):
        """Key of the approved catalog entry (what the gateway puts in ``server["key"]``); defaults to the provider."""
        return self.descriptor["catalog_key"]


class LocalRouter(McpTransport):
    """One ``local`` gateway transport for every in-process driver, dispatched by catalog key (= provider)."""

    def __init__(self, registry):
        self.registry = registry

    def _driver(self, server):
        driver = self.registry.by_catalog_key(server.get("key")) if isinstance(server, dict) else None
        return driver if driver is not None and driver.transport is not None else None

    async def describe(self, server, tool):
        driver = self._driver(server)
        return await driver.transport.describe(server, tool) if driver else None

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        driver = self._driver(server)
        if driver is None:
            return {"erro": "ferramenta_desconhecida", "mensagem": "Ferramenta nao disponivel."}
        return await driver.transport.call_tool(server, tool, arguments, identity=identity, secret=secret)


class ConnectorRegistry:
    def __init__(self):
        self._capabilities, self._drivers = {}, {}

    def register_capability(self, capability: Capability):
        if capability.name in self._capabilities:
            raise DescriptorRejected("Capacidade ja registrada.")
        for tool, operation in capability.required.items():
            if operation not in OPERATIONS:
                raise DescriptorRejected(f"Operacao invalida para {tool} na capacidade {capability.name}.")
        self._capabilities[capability.name] = capability

    def register_driver(self, descriptor, transport: McpTransport | None = None) -> Driver:
        clean = validate_descriptor(descriptor)
        capability = self._capabilities.get(clean["capability"])
        if capability is None:
            raise DescriptorRejected("Capacidade desconhecida: registre a capacidade antes do driver.")
        if clean["provider"] in self._drivers or self.by_catalog_key(clean["catalog_key"]) is not None:
            raise DescriptorRejected("Ja existe um driver para este provider ou catalogo.")
        for tool, operation in capability.required.items():
            if clean["tools"].get(tool, {}).get("operation") != operation:
                raise DescriptorRejected(f"A capacidade {capability.name} exige a ferramenta {tool} classificada como {operation}.")
        self._check_contract(capability, clean)
        if clean["protocol"] == "mcp" and clean["mcp_transport"] == "local" and transport is None:
            raise DescriptorRejected("Driver MCP local precisa de um transporte.")
        driver = Driver(clean, transport)
        self._drivers[clean["provider"]] = driver
        return driver

    @staticmethod
    def _check_contract(capability, clean):
        tools = clean["tools"]
        for tool, operation in capability.optional.items():
            if tool in tools and tools[tool]["operation"] != operation:
                raise DescriptorRejected(f"A ferramenta {tool} da capacidade {capability.name} deve ser classificada como {operation}.")
        for tool, expected in capability.arguments.items():
            declared = tools.get(tool, {}).get("arguments", {})
            for argument, (kind, required) in expected.items():
                rule = declared.get(argument)
                if tool in tools and (rule is None or rule["type"] != kind or bool(rule.get("required")) != required):
                    raise DescriptorRejected(f"{tool} deve declarar o argumento {argument} ({kind}{', obrigatorio' if required else ''}).")
        for tool, binds in capability.bindings.items():
            for key, argument in binds.items():
                if tool in tools and tools[tool].get(key) != argument:
                    raise DescriptorRejected(f"{tool} deve ligar {key} ao argumento {argument} (campo mostrado na aprovacao).")
        for tool, argument in capability.idempotent.items():
            if tool in tools and clean["idempotency_arguments"].get(tool) != argument:
                raise DescriptorRejected(f"{tool} deve declarar {argument} como chave de idempotencia.")

    def get(self, provider):
        return self._drivers.get(provider) if isinstance(provider, str) else None

    def by_catalog_key(self, key):
        return next((d for d in self._drivers.values() if d.catalog_key == key), None) if isinstance(key, str) else None

    def supports(self, provider, tool):
        driver = self.get(provider)
        return driver is not None and tool in driver.descriptor["tools"]

    def unsupported(self, provider):
        """Optional contract tools the driver leaves out: reported to the caller, never simulated."""
        driver = self.get(provider)
        capability = self._capabilities.get(driver.capability) if driver else None
        return [] if capability is None else sorted(set(capability.optional) - set(driver.descriptor["tools"]))

    def drivers(self, capability=None):
        return sorted((d for d in self._drivers.values() if capability in (None, d.capability)), key=lambda d: d.provider)

    def resolve(self, connection, capability):
        """The driver serving ``capability`` for a connection view (``provider``), or None. Never model input."""
        driver = self.get(connection.get("provider")) if isinstance(connection, dict) else None
        return driver if driver is not None and driver.capability == capability else None

    # -- wiring to the existing pieces (backend, operator) -------------------------------------------
    def attach(self, connections):
        """Let the connection service hand credentials to each driver's connector."""
        for driver in self._drivers.values():
            connections.register_connector(driver.provider)

    def local_transport(self) -> LocalRouter:
        return LocalRouter(self)

    def approve(self, connections, operator, provider, *, url=None, launcher=None, status="ativo"):
        """Operator step: put an MCP driver's tools in the approved catalog with its trusted classification."""
        driver = self.get(provider)
        if driver is None or driver.descriptor["protocol"] != "mcp":
            raise DescriptorRejected("Apenas drivers MCP registrados entram no catalogo MCP.")
        descriptor = driver.descriptor
        return connections.approve_mcp_server(
            operator, catalog_key=driver.catalog_key, entry_version=descriptor["version"], transport=descriptor["mcp_transport"],
            url=url, launcher=launcher, tools=list(descriptor["tools"]), tool_specs=descriptor["tools"], status=status)
