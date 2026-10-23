"""LLM access for agent runs: clients, rule-based routing, the generation limiter and model policies.

Local Ollama is the default. A company can add an external provider, but it stays unusable until an
operator enables it *for that company* and acknowledges the notice that conversation content is sent
to a third party. Its credential comes from the vault through a connection; no environment variable
or global key is ever read as a silent fallback.
"""
import asyncio
import json
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

from .access import AccessContext, AccessError, NotFound, PermissionDenied
from .ids import new_id
from .repository import IntegrityViolation, RevisionConflict

BUILTIN_POLICIES = ("local-primeiro", "legacy-local")
EXTERNAL_NOTICE = ("Ao ativar o provedor externo, o conteudo das demandas processadas por esta politica "
                   "(mensagens, dados e resultados de ferramentas) sera enviado a um terceiro e pode gerar custos. "
                   "Mantenha desativado para processar tudo localmente.")
DEFAULT_MAX_OUTPUT = 800


class LlmError(AccessError):
    code, message = "llm_error", "Falha ao consultar o modelo."

    def __init__(self):
        super().__init__(self.message)


class LlmUnavailable(LlmError):
    code, message = "llm_unavailable", "O modelo nao esta disponivel."


class LlmTimeout(LlmError):
    code, message = "llm_timeout", "O modelo excedeu o tempo limite."


class NoModelAvailable(LlmError):
    code, message = "no_model", "Nenhum modelo permitido esta disponivel para esta politica."


class PolicyRejected(ValueError):
    """Invalid policy request; the message is safe to show."""


@dataclass(frozen=True)
class LlmReply:
    text: str = ""
    tool_calls: list = field(default_factory=list)  # [{"id", "name", "arguments": dict | None}]
    usage: dict = field(default_factory=dict)  # prompt_tokens, completion_tokens (may be absent)


@dataclass(frozen=True)
class ModelChoice:
    name: str
    provider: str
    model: str
    base_url: str
    connection_id: str | None
    reason: str

    @property
    def external(self):
        return self.provider == "externo"

    @property
    def limiter_key(self):
        return (self.provider, self.base_url)


class LlmClient(ABC):
    @abstractmethod
    async def complete(self, messages, tools, *, model, max_tokens) -> LlmReply: ...


class OpenAICompatClient(LlmClient):
    """Chat completions over HTTP (Ollama's /v1 or any compatible endpoint). Errors never echo the body."""

    def __init__(self, base_url, api_key="ollama", timeout=120):
        self.base_url, self.api_key, self.timeout = base_url.rstrip("/"), api_key, timeout

    async def complete(self, messages, tools, *, model, max_tokens):
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3, "stream": False}
        if tools:
            body["tools"] = tools
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.post(f"{self.base_url}/chat/completions", json=body,
                                        headers={"Authorization": f"Bearer {self.api_key}"}) as response:
                    if response.status != 200:
                        raise LlmUnavailable()
                    data = await response.json()
            message = data["choices"][0]["message"]
            calls = []
            for call in message.get("tool_calls") or []:
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    arguments = None
                calls.append({"id": str(call.get("id") or new_id("correlation")), "name": call["function"]["name"],
                              "arguments": arguments if isinstance(arguments, dict) else None})
            usage = data.get("usage") or {}
            return LlmReply(message.get("content") or "", calls, {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0)})
        except asyncio.TimeoutError:
            raise LlmTimeout() from None
        except LlmError:
            raise
        except (aiohttp.ClientError, KeyError, IndexError, TypeError, ValueError):
            raise LlmUnavailable() from None


class GenerationLimiter:
    """At most N simultaneous generations per endpoint, shared by every agent, department and company.
    One shared slot per local model server avoids loading a copy of the model per agent."""

    def __init__(self, local_limit=1, external_limit=4):
        self.local_limit, self.external_limit = local_limit, external_limit
        self._semaphores = {}

    @asynccontextmanager
    async def slot(self, choice):
        key = choice.limiter_key
        if key not in self._semaphores:
            self._semaphores[key] = asyncio.Semaphore(self.external_limit if choice.external else self.local_limit)
        async with self._semaphores[key]:
            yield


def builtin_policy(config, name):
    """The local policies that always exist; they follow the current config.json (model and Ollama URL)."""
    if name not in BUILTIN_POLICIES:
        return None
    return {"name": name, "builtin": True,
            "models": {"local": {"provider": "ollama", "model": config["model"], "base_url": config["ollama_url"]}},
            "rules": [], "default": "local", "fallbacks": [], "limits": {"max_output_tokens": DEFAULT_MAX_OUTPUT},
            "external_enabled": False, "external_acknowledged_at": None}


def route(policy, hints=None):
    """Ordered candidate models: the first matching rule (else the default), then the fallbacks.
    External models are skipped unless the company enabled and acknowledged them."""
    hints = hints or {}
    chosen, reason = policy["default"], "padrao"
    for index, rule in enumerate(policy["rules"]):
        if all(hints.get(key) in values for key, values in rule["when"].items()):
            chosen, reason = rule["use"], f"regra {index + 1}"
            break
    allowed_external = bool(policy.get("external_enabled") and policy.get("external_acknowledged_at"))
    order, candidates = [chosen, *[name for name in policy["fallbacks"] if name != chosen]], []
    for position, name in enumerate(order):
        model = policy["models"][name]
        if model["provider"] == "externo" and not allowed_external:
            continue
        candidates.append(ModelChoice(name, model["provider"], model["model"], model["base_url"],
                                      model.get("connection_id"), reason if position == 0 else f"fallback {name}"))
    if not candidates:
        raise NoModelAvailable()
    return candidates


def now_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ModelPolicyService:
    def __init__(self, repository, access, config):
        self.repository, self.access, self.config = repository, access, config

    def _operator(self, ctx):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator":
            raise PermissionDenied()

    def known_names(self, session):
        """Policy names usable by agents of the session's company (pass the caller's open session)."""
        return set(BUILTIN_POLICIES) | {p["name"] for p in session.list("model_policy", limit=1000)}

    def names_for(self, tenant_id):
        with self.repository.transaction(tenant_id) as session:
            return self.known_names(session)

    def resolve(self, tenant_id, ref):
        """Policy document for a ``model_policy_ref``: built-in local policies or the company's own."""
        builtin = builtin_policy(self.config, ref)
        if builtin:
            return builtin
        with self.repository.transaction(tenant_id) as session:
            found = session.list("model_policy", name=ref, limit=1)
        if not found:
            raise NotFound()
        return found[0]

    def _check_connections(self, session, models):
        for name, model in models.items():
            if model.get("provider") == "externo":
                connection = session.get("connection", model.get("connection_id") or "")
                if connection is None:
                    raise NotFound()

    def create_policy(self, ctx, *, name, models, default, rules=(), fallbacks=(), limits=None):
        self._operator(ctx)
        if name in BUILTIN_POLICIES:
            raise PolicyRejected("Este nome e reservado para a politica local do sistema.")
        now = now_stamp()
        doc = {"kind": "model_policy", "schema_version": 1, "id": new_id("model_policy"), "tenant_id": ctx.tenant_id,
               "revision": 1, "created_at": now, "updated_at": now, "name": name, "models": models,
               "rules": list(rules), "default": default, "fallbacks": list(fallbacks),
               "limits": limits if limits is not None else {"max_output_tokens": DEFAULT_MAX_OUTPUT},
               "external_enabled": False, "external_acknowledged_at": None}
        with self.repository.transaction(ctx.tenant_id) as session:
            self._check_connections(session, models)
            try:
                return session.save(doc)
            except IntegrityViolation:
                raise PolicyRejected("Ja existe uma politica com este nome.") from None

    def update_policy(self, ctx, policy_id, expected_revision, /, **changes):
        """Changing models or rules turns external providers off again: the notice must be re-acknowledged."""
        self._operator(ctx)
        if set(changes) - {"models", "rules", "default", "fallbacks", "limits"}:
            raise PolicyRejected("Somente modelos, regras, padrao, fallbacks e limites podem ser alterados.")
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("model_policy", policy_id) if isinstance(policy_id, str) else None
            if current is None:
                raise NotFound()
            if current["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            updated = {**current, **changes, "external_enabled": False, "external_acknowledged_at": None,
                       "revision": expected_revision + 1, "updated_at": now_stamp()}
            self._check_connections(session, updated["models"])
            return session.save(updated)

    def enable_external(self, ctx, policy_id, acknowledged):
        """Turn on the external provider for this company's policy; requires acknowledging ``EXTERNAL_NOTICE``."""
        self._operator(ctx)
        if acknowledged is not True:
            raise PolicyRejected(EXTERNAL_NOTICE)
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("model_policy", policy_id) if isinstance(policy_id, str) else None
            if current is None:
                raise NotFound()
            if not any(m["provider"] == "externo" for m in current["models"].values()):
                raise PolicyRejected("A politica nao tem modelo externo.")
            now = now_stamp()
            return session.save({**current, "external_enabled": True, "external_acknowledged_at": now,
                                 "revision": current["revision"] + 1, "updated_at": now})

    def disable_external(self, ctx, policy_id):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("model_policy", policy_id) if isinstance(policy_id, str) else None
            if current is None:
                raise NotFound()
            return session.save({**current, "external_enabled": False, "external_acknowledged_at": None,
                                 "revision": current["revision"] + 1, "updated_at": now_stamp()})

    def list_policies(self, ctx):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            return session.list("model_policy", limit=1000)
