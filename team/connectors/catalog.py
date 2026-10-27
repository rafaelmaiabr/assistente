"""The connector catalog for the panel: what can be connected, what each company has, and how to configure and test it.

Operator-only for every change. Secrets reach the vault through the connection service and are never returned: results carry
state ("ativa", "revogada"), fixed result codes and the non-secret values the operator typed. A connection counts as
tested only when a real test ran (the audit trail holds the last one); nothing is inferred.
"""
from ..access import NotFound, PermissionDenied
from ..connections import ConnectionRejected
from .setup import validate_values


def _public(driver):
    descriptor = driver.descriptor
    return {"provider": driver.provider, "capability": driver.capability, "version": descriptor["version"],
            "mcp": descriptor["protocol"] == "mcp", "protocol": descriptor["protocol"],
            "non_mcp_justification": descriptor["non_mcp_justification"],
            "auth": descriptor["auth"]["type"], "fields": descriptor["fields"], "configurable": driver.setup is not None,
            "tools": sorted(descriptor["tools"])}


class ConnectorCatalog:
    def __init__(self, registry, connections, audit):
        self.registry, self.connections, self.audit = registry, connections, audit

    # -- guards ---------------------------------------------------------------------------------------------------------
    def _operator(self, ctx):
        if not self.connections.access.is_operator(ctx, "admin"):
            raise PermissionDenied()

    def _driver(self, provider, *, configurable=True):
        driver = self.registry.get(provider)
        if driver is None:
            raise NotFound()
        if configurable and driver.setup is None:
            raise ConnectionRejected("Este conector ainda nao pode ser configurado pelo painel.")
        return driver

    def _connection(self, ctx, connection_id):
        connection = self.connections.get_connection(ctx, connection_id)  # same-company, visible-only lookup
        return connection, self._driver(connection["provider"])

    # -- views ----------------------------------------------------------------------------------------------------------
    def _connection_view(self, ctx, connection, driver):
        configured = driver.setup is not None and driver.setup.current(ctx.tenant_id, connection["id"]) is not None
        tests = [e for e in self.audit.recent(ctx.tenant_id, limit=200, connection_id=connection["id"]) if e["action"] == "testar"]
        last = tests[0] if tests else None
        return {"id": connection["id"], "label": connection["label"], "status": connection["status"],
                "revision": connection["revision"], "credential": connection["credential"] and {"state": connection["credential"]["state"]},
                "configured": configured,
                "values": driver.setup.current(ctx.tenant_id, connection["id"]) if configured else None,
                "last_test": last and {"at": last["at"], "ok": last["ok"], "code": last["code"]}}

    def overview(self, ctx):
        """Capabilities, their drivers, and this company's connections with the real state of each."""
        mine = self.connections.list_connections(ctx)
        result = []
        for capability in self.registry.capabilities():
            drivers = []
            for driver in self.registry.drivers(capability.name):
                connections = [self._connection_view(ctx, c, driver) for c in mine if c["provider"] == driver.provider]
                drivers.append({**_public(driver), "connections": connections})
            result.append({"capability": capability.name, "label": capability.label or capability.name, "drivers": drivers})
        return result

    def recent_audit(self, ctx, limit=50):
        self._operator(ctx)
        return self.audit.recent(ctx.tenant_id, limit=min(max(int(limit), 1), 200))

    # -- changes (operator only) --------------------------------------------------------------------------------------------
    def _catalog_entry(self, ctx, driver, operator):
        for entry in self.connections.list_mcp_catalog(ctx):
            if entry["catalog_key"] == driver.catalog_key and entry["status"] == "ativo":
                return entry
        return self.registry.approve(self.connections, operator, driver.provider)

    def connect(self, operator, ctx, provider, label, values):
        """Create the connection, store the settings and the credential. Returns the connection view."""
        self._operator(ctx)
        driver = self._driver(provider)
        public, secret = validate_values(driver.descriptor, values)
        kind, entry_id = ("mcp", self._catalog_entry(ctx, driver, operator)["id"]) if driver.descriptor["protocol"] == "mcp" \
            else (driver.descriptor["protocol"], None)
        created = self.connections.create_connection(ctx, label=label, provider=provider, connection_kind=kind, catalog_entry_id=entry_id)
        self.audit.record(ctx.tenant_id, ctx.principal_id, "conectar", provider, created["id"], True, "criada")
        try:
            return self._apply(operator, ctx, created["id"], driver, public, secret, "configurar")
        except Exception as error:
            self.audit.record(ctx.tenant_id, ctx.principal_id, "configurar", provider, created["id"], False, type(error).__name__)
            raise

    def reconfigure(self, operator, ctx, connection_id, values):
        """Change the settings; a blank secret keeps the current credential."""
        self._operator(ctx)
        connection, driver = self._connection(ctx, connection_id)
        public, secret = validate_values(driver.descriptor, values, partial=True)
        return self._apply(operator, ctx, connection["id"], driver, public, secret, "configurar")

    def _apply(self, operator, ctx, connection_id, driver, public, secret, action):
        current = driver.setup.current(ctx.tenant_id, connection_id) or {}
        merged = {**current, **public}
        missing = [f["label"] for f in driver.descriptor["fields"] if f["required"] and f["kind"] != "secret" and f["name"] not in merged]
        if missing:
            raise ConnectionRejected(f"Preencha o campo: {missing[0]}.")
        driver.setup.configure(operator, ctx, connection_id, merged)
        if secret is not None:
            self.connections.set_credential(ctx, connection_id, secret)
        self.audit.record(ctx.tenant_id, ctx.principal_id, action, driver.provider, connection_id, True, "salva")
        connection = self.connections.get_connection(ctx, connection_id)
        return self._connection_view(ctx, connection, driver)

    def test(self, ctx, connection_id):
        """Run a real test with the vaulted credential. The result is recorded, whatever it is."""
        self._operator(ctx)
        connection, driver = self._connection(ctx, connection_id)
        result = driver.setup.test(ctx, connection_id)
        outcome = {"ok": bool(result.get("ok")), "erro": result.get("erro"), "mensagem": result.get("mensagem")}
        self.audit.record(ctx.tenant_id, ctx.principal_id, "testar", driver.provider, connection_id, outcome["ok"],
                          outcome["erro"] or "ok")
        return outcome
