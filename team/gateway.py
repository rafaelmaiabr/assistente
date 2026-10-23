"""Tool gateway: the only path from an agent run to an MCP tool.

Every call is authorized against explicit grants (agent, skill, company, connection, operation),
validated against the *approved catalog* (never the remote server's own claims), bounded in time,
size and count, cancellable, and re-checked after the tool answers. Results are untrusted data:
secrets are redacted, size is capped, and the decision (not the arguments or the result) is logged.

The transport (`McpTransport`) is an interface. Automated tests use a fake; a real adapter for the
installed ``mcp`` package plugs in later without changing these rules. Identity and account are
injected here from backend state: arguments from a model cannot set them (reserved names are
refused at catalog approval, and unknown arguments are rejected).
"""
import asyncio
import json
import time
from abc import ABC, abstractmethod

from .access import AccessContext, AccessError, OperatorContext, PermissionDenied
from .prompting import ExternalItem
from .schema import redact_secrets
from .skills import SkillDenied

POLICY_VERSION = "gateway-1"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_BYTES = 65536
DEFAULT_MAX_ITEMS = 200
DEFAULT_MAX_CALLS = 20
_TOKEN = object()


class GatewayError(AccessError):
    """Typed, safe-to-show failure: ``code`` is stable, the message never carries data or secrets."""
    code = "error"
    message = "Falha ao executar a ferramenta."

    def __init__(self):
        super().__init__(self.message)


class ToolNotFound(GatewayError):
    code, message = "not_found", "Ferramenta nao disponivel."


class ToolDenied(GatewayError):
    code, message = "denied", "Ferramenta nao autorizada para esta execucao."


class InvalidArguments(GatewayError):
    code, message = "invalid_arguments", "Argumentos invalidos para a ferramenta."


class ToolTimeout(GatewayError):
    code, message = "timeout", "A ferramenta excedeu o tempo limite."


class ToolCancelled(GatewayError):
    code, message = "cancelled", "Execucao cancelada."


class ResultTooLarge(GatewayError):
    code, message = "too_large", "O resultado da ferramenta excede o limite permitido."


class ToolUnavailable(GatewayError):
    code, message = "unavailable", "A ferramenta nao respondeu como esperado."


class BudgetExceeded(GatewayError):
    code, message = "budget_exceeded", "Limite de chamadas ou prazo da execucao atingido."


class ResultBlocked(GatewayError):
    code, message = "blocked", "Resultado bloqueado por seguranca."


class McpTransport(ABC):
    """What the gateway needs from an MCP client. ``server`` is built from the approved catalog entry."""

    @abstractmethod
    async def describe(self, server: dict, tool: str) -> dict | None:
        """Remote metadata for a tool (for example ``{"read_only": True}``) or None if the server lacks it."""

    @abstractmethod
    async def call_tool(self, server: dict, tool: str, arguments: dict, *, identity: dict, secret: str | None):
        """Run the tool. ``identity`` and ``secret`` come from the backend, never from the model."""


class Execution:
    """Trusted description of one agent run; only ``Gateway.open_execution`` creates it."""

    def __init__(self, token, ctx, run, deadline, max_calls):
        if token is not _TOKEN:
            raise PermissionDenied("execucao so pode ser criada pelo gateway")
        self.ctx, self.run, self.deadline, self.max_calls = ctx, run, deadline, max_calls
        self.tenant_id, self.run_id = ctx.tenant_id, run["id"]
        self.agent_id, self.agent_version = run["agent_id"], run["agent_version"]
        self.calls = 0
        self._cancel = asyncio.Event()

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()


class ToolResult:
    """Untrusted data returned by a tool; hand it to the model only through ``as_external_item``."""

    def __init__(self, alias, operation, data, size, redacted):
        self.alias, self.operation, self.data, self.size, self.redacted = alias, operation, data, size, redacted

    def as_external_item(self):
        return ExternalItem(self.alias, "ferramenta", json.dumps(self.data, ensure_ascii=False))


def validate_arguments(rules, arguments):
    """Flat schema check: known names only, required present, types, bounds, enums, string length."""
    if not isinstance(arguments, dict):
        raise InvalidArguments()
    if set(arguments) - set(rules):
        raise InvalidArguments()
    clean = {}
    for name, rule in rules.items():
        if name not in arguments:
            if rule.get("required"):
                raise InvalidArguments()
            continue
        value, kind = arguments[name], rule["type"]
        ok = (isinstance(value, str) and len(value) <= rule.get("max_length", 2000)) if kind == "string" else \
            (isinstance(value, int) and not isinstance(value, bool)) if kind == "integer" else \
            (isinstance(value, (int, float)) and not isinstance(value, bool)) if kind == "number" else \
            isinstance(value, bool)
        if not ok:
            raise InvalidArguments()
        if kind in ("integer", "number") and (value < rule.get("minimum", float("-inf"))
                                              or value > rule.get("maximum", float("inf"))):
            raise InvalidArguments()
        if "enum" in rule and value not in rule["enum"]:
            raise InvalidArguments()
        clean[name] = value
    return clean


class Gateway:
    def __init__(self, repository, access, skills, connections, transports, max_calls=DEFAULT_MAX_CALLS):
        self.repository, self.access, self.skills, self.connections = repository, access, skills, connections
        self.transports = transports  # catalog transport kind ("http", "sse", "stdio") -> McpTransport
        self.max_calls = max_calls
        self._operator = access.local_operator()

    # -- executions ----------------------------------------------------------------------------------------
    def open_execution(self, ctx, run_id, deadline_seconds=None) -> Execution:
        """Bind a run to the gateway. ``ctx`` is the agent context the backend derived for that run."""
        if not isinstance(ctx, AccessContext) or ctx.role != "agent":
            raise PermissionDenied()
        with self.repository.transaction(ctx.tenant_id) as session:
            run = session.get("agent_run", run_id) if isinstance(run_id, str) else None
        if run is None or run["agent_id"] != ctx.principal_id or run["state"] not in ("enfileirada", "em_execucao"):
            raise PermissionDenied()
        seconds = deadline_seconds if deadline_seconds is not None else run["budget"]["deadline_seconds"]
        return Execution(_TOKEN, ctx, run, time.monotonic() + seconds, self.max_calls)

    # -- listing -----------------------------------------------------------------------------------------------
    def _routes(self, execution):
        """alias -> route, computed from current grants and the approved catalog (fail closed)."""
        routes = {}
        grants = sorted(self.skills.usable_grants(execution.ctx, execution.agent_id, execution.agent_version),
                        key=lambda pair: pair[0]["id"])
        tenant_ctx = self.access.tenant_context(self._operator, execution.tenant_id)
        with self.repository.transaction(execution.tenant_id) as session:
            for grant, skill in grants:
                connection = session.get("connection", grant["connection_id"]) if grant["connection_id"] else None
                entry = self.repository.get_global("mcp_catalog_entry", connection.get("catalog_entry_id") or "") \
                    if connection else None
                if entry is None or entry["status"] != "ativo":
                    continue
                for tool, operations in (skill.get("tool_operations") or {}).items():
                    server, _, name = tool.partition(".")
                    spec = (entry.get("tool_specs") or {}).get(name)
                    if server != entry["catalog_key"] or name not in entry["tools"] or spec is None:
                        continue  # unknown or unclassified: not exposed
                    operation = spec["operation"]
                    if operation not in grant["operations"] or operation not in operations:
                        continue
                    alias, number = tool, 1
                    while alias in routes:
                        number += 1
                        alias = f"{tool}~{number}"
                    routes[alias] = {"grant": grant, "skill": skill, "connection": connection, "entry": entry,
                                     "tool": name, "spec": spec, "operation": operation, "ctx": tenant_ctx}
        return routes

    def list_tools(self, execution):
        """Tools this run may call right now: alias, operation and argument rules. Hidden tools never appear."""
        self._check_execution(execution)
        return [{"name": alias, "operation": route["operation"], "arguments": route["spec"].get("arguments", {}),
                 "skill": route["skill"]["skill_key"]} for alias, route in sorted(self._routes(execution).items())]

    # -- calling -----------------------------------------------------------------------------------------------
    @staticmethod
    def _check_execution(execution):
        if not isinstance(execution, Execution):
            raise PermissionDenied()

    def _record(self, execution, alias, operation, decision, reason, size=None, started=None):
        elapsed = None if started is None else int((time.monotonic() - started) * 1000)
        with self.repository.transaction(execution.tenant_id) as session:
            session.record_tool_decision({
                "run_id": execution.run_id, "agent_id": execution.agent_id, "agent_version": execution.agent_version,
                "tool": str(alias)[:120], "operation": operation, "decision": decision, "reason": reason,
                "policy_version": POLICY_VERSION, "result_bytes": size, "duration_ms": elapsed})

    async def call_tool(self, execution, alias, arguments):
        """Authorize, validate, run with limits, re-check, sanitize, record. Returns a ``ToolResult``."""
        self._check_execution(execution)
        started, operation = time.monotonic(), None
        try:
            result = await self._call(execution, alias, arguments, started)
        except GatewayError as error:
            self._record(execution, alias, getattr(error, "operation", None), "denied" if isinstance(
                error, (ToolDenied, ToolNotFound, InvalidArguments, BudgetExceeded)) else "error", error.code,
                started=started)
            raise
        self._record(execution, alias, result.operation, "allowed", "ok", result.size, started)
        return result

    async def _call(self, execution, alias, arguments, started):
        if execution.cancelled:
            raise ToolCancelled()
        if execution.calls >= execution.max_calls or time.monotonic() >= execution.deadline:
            raise BudgetExceeded()
        route = self._routes(execution).get(alias) if isinstance(alias, str) else None
        if route is None:
            raise ToolNotFound()  # hidden, revoked and nonexistent tools look the same
        spec, operation = route["spec"], route["operation"]
        arguments = validate_arguments(spec.get("arguments", {}), arguments)
        try:
            self.skills.authorize_use(execution.ctx, execution.agent_id, route["skill"]["skill_key"], operation,
                                      connection_id=route["connection"]["id"], agent_version=execution.agent_version)
        except SkillDenied:
            raise ToolDenied() from None
        transport = self.transports.get(route["entry"]["transport"])
        if transport is None:
            raise ToolUnavailable()
        server = {"transport": route["entry"]["transport"], "url": route["entry"]["url"],
                  "argv": self.connections.stdio_command(route["entry"]["id"]) if route["entry"]["transport"] == "stdio" else None,
                  "key": route["entry"]["catalog_key"]}
        identity = {"tenant_id": execution.tenant_id, "agent_id": execution.agent_id, "run_id": execution.run_id,
                    "connection_id": route["connection"]["id"], "operation": operation}
        timeout = min(spec.get("timeout_seconds", DEFAULT_TIMEOUT), max(execution.deadline - time.monotonic(), 0.001))
        execution.calls += 1

        async def invoke(secret):
            meta = await transport.describe(server, route["tool"])
            if meta is None:
                raise ToolUnavailable()
            if operation == "read" and meta.get("read_only") is False:
                raise ToolDenied()  # the remote server disagrees with the approved classification: fail closed
            return await transport.call_tool(server, route["tool"], arguments, identity=identity, secret=secret)

        work = asyncio.ensure_future(self._with_credential(route, invoke))
        stop = asyncio.ensure_future(execution._cancel.wait())
        try:
            done, _ = await asyncio.wait({work, stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if work not in done:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                raise ToolCancelled() if stop in done else ToolTimeout()
            raw = work.result()
        except GatewayError:
            raise
        except AccessError:
            raise ToolDenied() from None  # revoked or unavailable credential, connection or connector
        except Exception:
            raise ToolUnavailable() from None
        finally:
            stop.cancel()
            if not work.done():
                work.cancel()
        # Revalidate after the tool answered: a grant revoked mid-flight discards the result.
        try:
            self.skills.authorize_use(execution.ctx, execution.agent_id, route["skill"]["skill_key"], operation,
                                      connection_id=route["connection"]["id"], agent_version=execution.agent_version)
        except SkillDenied:
            raise ToolDenied() from None
        if execution.cancelled:
            raise ToolCancelled()
        return self._shape(alias, operation, spec, raw)

    async def _with_credential(self, route, invoke):
        return await self.connections.use_credential_async(
            route["ctx"], route["connection"]["id"], route["connection"]["provider"], invoke, optional=True)

    def _shape(self, alias, operation, spec, raw):
        try:
            data = json.loads(json.dumps(raw, default=str))
        except (TypeError, ValueError):
            raise ResultBlocked() from None
        limit = spec.get("max_result_bytes", DEFAULT_MAX_BYTES)
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > limit:
            raise ResultTooLarge()
        items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), []) \
            if isinstance(data, dict) else []
        if len(items) > spec.get("max_items", DEFAULT_MAX_ITEMS):
            raise ResultTooLarge()
        clean = redact_secrets(data)
        size = len(json.dumps(clean, ensure_ascii=False).encode("utf-8"))
        return ToolResult(alias, operation, clean, size, redacted=clean != data)
