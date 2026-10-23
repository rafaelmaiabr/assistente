"""Optional end-to-end check: real Ollama + the team runtime + gateway with a *fake* MCP tool.

    .venv/Scripts/python.exe scripts/smoke_team_runtime.py

Builds a throwaway database, one company and agent, a fictitious ``consultar`` tool and runs one
turn against the local model from config.json. Output is non-deterministic (small local models may
or may not call the tool); it reports what happened and never contacts anything but Ollama.
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from settings import load_config  # noqa: E402
from team import persistence  # noqa: E402
from team.gateway import McpTransport  # noqa: E402
from team.vault import MemoryVault  # noqa: E402


class FictionalTransport(McpTransport):
    async def describe(self, server, tool):
        return {"read_only": True}

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        return {"simulado": True, "cliente": arguments.get("cliente"), "cobrancas": [{"id": 1, "valor": "R$ 120,00 (ficticio)"}]}


async def main() -> int:
    config = load_config()
    with tempfile.TemporaryDirectory() as root:
        persistence.enable(root)
        runtime = persistence.build_runtime(root, config, transports={"http": FictionalTransport()}, vault=MemoryVault(),
                                            legacy_elevenlabs=False)
        team, access, agents = persistence.team_service(root), runtime.access, runtime.agents
        op = access.local_operator()
        tenant = team.create_company(op, "Empresa de demonstracao")
        ctx = access.tenant_context(op, tenant["id"])
        dep = team.create(ctx, "department", name="Financeiro", scope="Demonstracao", status="ativo")
        agent = agents.create_agent(
            ctx, department_id=dep["id"], name="Financeiro", goals=["Consultar cobrancas"],
            personality={"tone": "objetivo", "initiative": "proativo"}, scope="Cobrancas (dados ficticios)",
            out_of_scope="Outros assuntos", model_policy_ref="local-primeiro")
        connections, skills = runtime.connections, runtime.skills
        entry = connections.approve_mcp_server(
            op, catalog_key="erp-demo", entry_version="1.0.0", transport="http", url="http://127.0.0.1:1/mcp",
            tools=["consultar"], tool_specs={"consultar": {"operation": "read", "arguments": {
                "cliente": {"type": "string", "required": True, "max_length": 40}}}})
        connections.register_connector("erp-demo")  # trusted wiring: only registered providers may use credentials
        connection = connections.create_connection(ctx, label="ERP ficticio", provider="erp-demo", connection_kind="mcp",
                                                   catalog_entry_id=entry["id"])
        folder = Path(root) / "skill"
        folder.mkdir()
        (folder / "skill.json").write_text(json.dumps({
            "manifest_version": 1, "id": "demo.consultar", "version": "1.0.0", "fictitious": True,
            "description": "[DEMONSTRACAO] Consulta cobrancas ficticias.", "instructions": "Use a ferramenta para consultar.",
            "required_tools": [{"tool": "erp-demo.consultar", "operations": ["read"]}]}), encoding="utf-8")
        skill = skills.install(op, folder, skills.review_package(folder).content_hash)
        skills.grant_skill(ctx, agent_id=agent["id"], skill_id=skill["id"], operations=["read"],
                           connection_id=connection["id"], attach=True)
        run = agents.start_run(ctx, agent["id"], budget={"max_steps": 4, "token_budget": 4000, "deadline_seconds": 120})
        started = time.monotonic()
        outcome = await runtime.run(tenant["id"], run["id"], "Consulte as cobrancas do cliente Acme e resuma em uma frase.")
        print(f"status={outcome.status} motivo={outcome.reason or '-'} passos={outcome.steps} "
              f"ferramentas={outcome.tool_calls} tokens={outcome.tokens} tempo={time.monotonic() - started:.1f}s")
        print("resposta:", outcome.text[:300] or "(vazia)")
        return 0 if outcome.status == "concluida" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
