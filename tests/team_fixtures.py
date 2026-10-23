"""Shared fixture for gateway and runtime tests: two companies, an agent with a granted MCP skill, a fake MCP."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from team.access import AccessService
from team.agents import AgentService
from team.connections import ConnectionService, LauncherRegistry
from team.gateway import Gateway, McpTransport
from team.llm import LlmClient, LlmReply, ModelPolicyService
from team.runtime import AgentRuntime
from team.ids import new_id
from team.repository import SqliteRepository
from team.service import TeamService
from team.skills import SkillService
from team.vault import MemoryVault

SECRET = "sk-test-GATEWAY-0123456789abcdef"
PROFILE = dict(name="Financeiro", goals=["Resumir"], personality={"tone": "objetivo", "initiative": "proativo"},
               scope="Cobrancas", out_of_scope="Outros", model_policy_ref="local-primeiro")
SPECS = {
    "listar-cobrancas": {"operation": "read", "arguments": {
        "cliente": {"type": "string", "required": True, "max_length": 40},
        "limite": {"type": "integer", "minimum": 1, "maximum": 50},
        "ordem": {"type": "string", "enum": ["data", "valor"]}}, "max_items": 5, "max_result_bytes": 2000},
    "criar-cobranca": {"operation": "write", "arguments": {"cliente": {"type": "string", "required": True}}},
}
MANIFEST = {"manifest_version": 1, "id": "teste.cobrancas", "version": "1.0.0", "description": "Skill de teste.",
            "instructions": "Consulte cobrancas.",
            "required_tools": [{"tool": "erp-demo.listar-cobrancas", "operations": ["read"]},
                               {"tool": "erp-demo.criar-cobranca", "operations": ["write"]}]}


class FakeTransport(McpTransport):
    def __init__(self):
        self.remote = {"listar-cobrancas": {"read_only": True}, "criar-cobranca": {"read_only": False},
                       "apagar-tudo": {"read_only": False}}
        self.calls, self.delay, self.cancelled, self.before_return = [], 0, False, None
        self.result = {"cobrancas": [{"id": 1, "valor": 10}]}

    async def describe(self, server, tool):
        return self.remote.get(tool)

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        self.calls.append({"tool": tool, "arguments": arguments, "identity": identity, "secret": secret})
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.before_return:
            self.before_return()
        return self.result


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = SqliteRepository(root / "team.sqlite3")
        self.repo.migrate()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.skills = SkillService(self.repo, self.access, self.agents)
        self.connections = ConnectionService(self.repo, self.access, MemoryVault(),
                                             LauncherRegistry(root / "launchers.json", self.access.operator_id))
        self.transport = FakeTransport()
        self.gateway = Gateway(self.repo, self.access, self.skills, self.connections, {"http": self.transport})
        self.connections.register_connector("erp-demo")
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        dep = self.team.create(self.ca, "department", name="Fin", scope="x", status="ativo")
        self.agent = self.agents.create_agent(self.ca, department_id=dep["id"], **PROFILE)
        self.entry = self.connections.approve_mcp_server(
            self.op, catalog_key="erp-demo", entry_version="1.0.0", transport="http", url="http://127.0.0.1:9000/mcp",
            tools=["listar-cobrancas", "criar-cobranca", "apagar-tudo"], tool_specs=SPECS)
        self.conn = self.connections.create_connection(
            self.ca, label="ERP", provider="erp-demo", connection_kind="mcp", catalog_entry_id=self.entry["id"])
        self.connections.set_credential(self.ca, self.conn["id"], SECRET)
        folder = root / "pkg"
        folder.mkdir()
        (folder / "skill.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        review = self.skills.review_package(folder)
        self.skill = self.skills.install(self.op, folder, review.content_hash)
        self.grant = self.skills.grant_skill(self.ca, agent_id=self.agent["id"], skill_id=self.skill["id"],
                                             operations=["read"], connection_id=self.conn["id"], attach=True)

    def open(self, deadline_seconds=None, ctx=None):
        run = self.agents.start_run(self.ca, self.agent["id"], budget={"max_steps": 8, "token_budget": 100, "deadline_seconds": 60})
        agent_ctx = self.access.agent_context(self.op, self.a["id"], run["id"])
        return self.gateway.open_execution(ctx or agent_ctx, run["id"], deadline_seconds), run

    def decisions(self, run):
        with self.repo.transaction(self.a["id"]) as session:
            return session.tool_decisions(run["id"])


CONFIG = {"model": "qwen2.5:7b", "ollama_url": "http://127.0.0.1:11435/v1", "max_concurrent_generations": 1}
BUDGET = {"max_steps": 6, "token_budget": 10000, "deadline_seconds": 30}
TOOL = "erp-demo_listar-cobrancas"  # the model sees a provider-safe name for erp-demo.listar-cobrancas


class FakeLlm(LlmClient):
    def __init__(self, script=None, delay=0):
        self.script, self.delay, self.calls, self.active, self.peak, self.cancelled = list(script or []), delay, [], 0, 0, False

    async def complete(self, messages, tools, *, model, max_tokens):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, "model": model, "max_tokens": max_tokens})
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            step = self.script.pop(0) if self.script else LlmReply("fim")
            if isinstance(step, Exception):
                raise step
            return step
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active -= 1


def tool_call(arguments=None, name=TOOL, call_id="c1", usage=None):
    return LlmReply("", [{"id": call_id, "name": name, "arguments": {"cliente": "Acme"} if arguments is None else arguments}],
                    usage or {})


class RuntimeCase(GatewayCase):
    def setUp(self):
        super().setUp()
        self.policies = ModelPolicyService(self.repo, self.access, CONFIG)
        self.agents.model_policies = self.policies.known_names
        self.llms = {"local": FakeLlm()}
        self.factory_secrets = []

        def factory(choice, secret):
            self.factory_secrets.append((choice.name, secret))
            return self.llms[choice.name]

        self.runtime = AgentRuntime(self.repo, self.access, self.agents, self.skills, self.gateway, self.policies,
                                    CONFIG, client_factory=factory, connections=self.connections)

    def new_run(self, budget=None):
        return self.agents.start_run(self.ca, self.agent["id"], budget=budget or BUDGET)

    def state(self, run):
        with self.repo.transaction(self.a["id"]) as session:
            return session.get("agent_run", run["id"])

    async def go(self, run=None, message="Quais cobrancas estao abertas?", **kwargs):
        run = run or self.new_run()
        return run, await self.runtime.run(self.a["id"], run["id"], message, **kwargs)

    def use_policy(self, **kwargs):
        policy = self.policies.create_policy(self.ca, **kwargs)
        agent = self.agents.get_agent(self.ca, self.agent["id"])
        self.agents.update_agent(self.ca, self.agent["id"], agent["revision"], model_policy_ref=policy["name"])
        return policy
