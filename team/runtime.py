"""Agent runtime: runs one persisted ``agent_run`` independent of the web UI or the voice session.

The factory picks the pinned agent version, its granted skills and tools (through the gateway) and
the model (through the company's policy). The loop is bounded by steps, tokens, time and tool calls,
can be cancelled, serializes generations per model endpoint and falls back along the policy. The
existing browser chat does not use it yet; binding chat and voice to runs is T31.
"""
import asyncio
import json
import re
import time
from dataclasses import dataclass, field

from .access import AccessError, NotFound
from .gateway import GatewayError
from .llm import (
    DEFAULT_MAX_OUTPUT, GenerationLimiter, LlmClient, LlmError, LlmTimeout, LlmUnavailable, NoModelAvailable,
    OpenAICompatClient, now_stamp, route)
from .prompting import compile_context, escape

MAX_TOOL_CALLS_PER_STEP = 4
_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")
_OPERATION_LABEL = {"read": "leitura", "write": "escrita", "send": "envio", "spend": "pagamento"}


@dataclass
class RunOutcome:
    status: str  # concluida | falhou | cancelada | orcamento
    text: str = ""
    reason: str = ""
    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    model: str = ""
    fallback_used: bool = False
    errors: list = field(default_factory=list)


def _tool_schema(tool, safe):
    properties, required = {}, []
    for name, rule in tool["arguments"].items():
        prop = {"type": rule["type"]}
        for source, target in (("max_length", "maxLength"), ("minimum", "minimum"), ("maximum", "maximum"), ("enum", "enum")):
            if source in rule:
                prop[target] = rule[source]
        properties[name] = prop
        if rule.get("required"):
            required.append(name)
    return {"type": "function", "function": {
        # A plain sentence: small local models stop calling tools when the description starts with an identifier.
        "name": safe, "description": f"Ferramenta de {_OPERATION_LABEL[tool['operation']]} da skill {tool['skill']}.",
        "parameters": {"type": "object", "properties": properties, "required": required,
                       "additionalProperties": False}}}


class AgentRuntime:
    def __init__(self, repository, access, agents, skills, gateway, policies, config, client_factory=None,
                 limiter=None, connections=None):
        self.repository, self.access, self.agents, self.skills = repository, access, agents, skills
        self.gateway, self.policies, self.config, self.connections = gateway, policies, config, connections
        self.limiter = limiter or GenerationLimiter(config.get("max_concurrent_generations", 1))
        self._factory = client_factory or self._default_client
        self._clients = {}
        self._operator = access.local_operator()
        self._active = {}

    # -- clients: one per endpoint, shared by every agent (no model copy per department) ---------------------
    def _default_client(self, choice, secret):
        if choice.external:
            return OpenAICompatClient(choice.base_url, api_key=secret)
        if choice.base_url not in self._clients:
            self._clients[choice.base_url] = OpenAICompatClient(choice.base_url)
        return self._clients[choice.base_url]

    def cancel(self, run_id):
        """Cancel a running execution: stops the model call in flight and propagates to tool calls."""
        active = self._active.get(run_id)
        if active:
            active["cancel"].set()
            active["execution"].cancel()
            for task in list(active["tasks"]):
                task.cancel()

    # -- run state ----------------------------------------------------------------------------------------------
    def _set_state(self, tenant_id, run_id, state):
        with self.repository.transaction(tenant_id) as session:
            current = session.get("agent_run", run_id)
            now = now_stamp()
            changes = {"state": state, "revision": current["revision"] + 1, "updated_at": now}
            if state == "em_execucao":
                changes["started_at"] = now
            else:
                changes["finished_at"] = now
            session.save({**current, **changes})

    # -- the run ---------------------------------------------------------------------------------------------------
    async def run(self, tenant_id, run_id, user_message, *, case=None, external=(), hints=None, policy_text="") -> RunOutcome:
        ctx = self.access.tenant_context(self._operator, tenant_id)
        agent_ctx = self.access.agent_context(self._operator, tenant_id, run_id)
        with self.repository.transaction(tenant_id) as session:
            run = session.get("agent_run", run_id)
            names = {a["id"]: a["name"] for a in session.list("agent_definition", limit=1000)}
        snapshot = self.agents.get_version(ctx, run["agent_id"], run["agent_version"])  # pinned: edits do not leak in
        definition = snapshot["definition"]
        policy = self.policies.resolve(tenant_id, definition["model_policy_ref"])
        execution = self.gateway.open_execution(agent_ctx, run_id)
        tools = self.gateway.list_tools(execution)
        safe_names, llm_tools = {}, []
        for tool in tools:
            safe = _UNSAFE.sub("_", tool["name"])
            while safe in safe_names:
                safe += "_"
            safe_names[safe] = tool["name"]
            llm_tools.append(_tool_schema(tool, safe))
        skills = [(f"{skill['skill_key']}@{skill['skill_version']}", skill["instructions"])
                  for _, skill in self.skills.usable_grants(agent_ctx, run["agent_id"], run["agent_version"])]
        try:
            compiled = compile_context(snapshot, policy_text=policy_text, skill_instructions=skills, case=case,
                                       external=external, user_message=user_message, agent_names=names,
                                       authorized_tools=list(safe_names))  # the names the model actually sees
        except ValueError:  # the version was disabled after the run was queued
            self._set_state(tenant_id, run_id, "em_execucao")
            self._set_state(tenant_id, run_id, "falhou")
            return RunOutcome("falhou", reason="versao_desativada")
        messages = [{"role": "system", "content": compiled.system}, *compiled.messages]
        budget = run["budget"]
        deadline = time.monotonic() + budget["deadline_seconds"]
        active = {"cancel": asyncio.Event(), "execution": execution, "tasks": set()}
        self._active[run_id] = active
        self._set_state(tenant_id, run_id, "em_execucao")
        outcome = RunOutcome("falhou")
        try:
            outcome = await self._loop(ctx, run, policy, hints, messages, llm_tools, safe_names, execution, active,
                                       budget, deadline)
        except asyncio.CancelledError:
            outcome = RunOutcome("cancelada", reason="cancelada")
        finally:
            self._active.pop(run_id, None)
            final = {"concluida": "concluida", "cancelada": "cancelada"}.get(outcome.status, "falhou")
            if outcome.reason == "prazo":
                final = "expirada"
            self._set_state(tenant_id, run_id, final)
        return outcome

    async def _complete(self, ctx, candidates, messages, llm_tools, policy, active, deadline, outcome):
        """Try each allowed candidate in order; only unavailability or timeout moves on to the next."""
        max_tokens = policy["limits"].get("max_output_tokens", DEFAULT_MAX_OUTPUT)
        last = None
        for index, choice in enumerate(candidates):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LlmTimeout()

            async def call(secret, choice=choice):
                client = self._factory(choice, secret)
                async with self.limiter.slot(choice):
                    return await asyncio.wait_for(
                        client.complete(messages, llm_tools, model=choice.model, max_tokens=max_tokens), remaining)

            task = asyncio.ensure_future(self._with_secret(ctx, choice, call))
            active["tasks"].add(task)
            try:
                reply = await task
                outcome.model, outcome.fallback_used = choice.name, index > 0
                return reply
            except asyncio.TimeoutError:
                last = LlmTimeout()
            except (LlmUnavailable, LlmTimeout) as error:
                last = error
            except AccessError:
                last = LlmUnavailable()  # an unusable external credential counts as unavailable
            finally:
                active["tasks"].discard(task)
            outcome.errors.append(last.code)
        raise last or NoModelAvailable()

    async def _with_secret(self, ctx, choice, call):
        if not choice.external:
            return await call(None)
        if self.connections is None:
            raise LlmUnavailable()
        connection = self.connections.get_connection(ctx, choice.connection_id)
        return await self.connections.use_credential_async(ctx, choice.connection_id, connection["provider"], call)

    async def _loop(self, ctx, run, policy, hints, messages, llm_tools, safe_names, execution, active, budget, deadline):
        outcome = RunOutcome("falhou")
        try:
            candidates = route(policy, hints)
        except NoModelAvailable as error:
            outcome.reason = error.code
            return outcome
        while True:
            if active["cancel"].is_set():
                outcome.status, outcome.reason = "cancelada", "cancelada"
                return outcome
            if outcome.steps >= budget["max_steps"]:
                outcome.status, outcome.reason = "orcamento", "passos"
                return outcome
            if outcome.tokens >= budget["token_budget"]:
                outcome.status, outcome.reason = "orcamento", "tokens"
                return outcome
            if time.monotonic() >= deadline:
                outcome.status, outcome.reason = "falhou", "prazo"
                return outcome
            outcome.steps += 1
            try:
                reply = await self._complete(ctx, candidates, messages, llm_tools, policy, active, deadline, outcome)
            except asyncio.CancelledError:
                if active["cancel"].is_set():
                    outcome.status, outcome.reason = "cancelada", "cancelada"
                    return outcome
                raise
            except LlmTimeout:
                outcome.reason = "prazo" if time.monotonic() >= deadline else "llm_timeout"
                return outcome
            except LlmError as error:
                outcome.reason = error.code
                return outcome
            used = reply.usage.get("prompt_tokens", 0) + reply.usage.get("completion_tokens", 0)
            outcome.tokens += used or max(1, len(reply.text) // 4)
            if not reply.tool_calls:
                if not reply.text.strip():  # nothing usable came back (for example a malformed tool call)
                    outcome.reason = "resposta_vazia"
                    return outcome
                outcome.status, outcome.text = "concluida", reply.text
                return outcome
            messages.append({"role": "assistant", "content": reply.text or None, "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"] or {})}}
                for c in reply.tool_calls[:MAX_TOOL_CALLS_PER_STEP]]})
            for call in reply.tool_calls[:MAX_TOOL_CALLS_PER_STEP]:
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": await self._tool(execution, safe_names, call, outcome)})

    async def _tool(self, execution, safe_names, call, outcome):
        alias = safe_names.get(call["name"])
        if alias is None or call["arguments"] is None:
            return "Erro: ferramenta indisponivel ou argumentos invalidos."
        outcome.tool_calls += 1
        try:
            result = await self.gateway.call_tool(execution, alias, call["arguments"])
        except GatewayError as error:
            return f"Erro: {error.code}."
        item = result.as_external_item()
        return (f'<dados_externos fonte="{escape(item.source)}" tipo="ferramenta">\n{escape(item.text)}\n'
                "</dados_externos>")
