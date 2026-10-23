"""Optional evals against the local Ollama model. Never part of CI and never a gate: a model is not deterministic.

It sends the triage corpus and the injection corpus through the real classifier and records, per item and in total, which
model answered (name and, when the server tells, version and digest), how long it took, how many tokens it used, whether the
decision matched and whether the call failed. A budget (calls and tokens) stops it early; whatever it did not run is reported
as skipped. Only the local model is contacted.
"""
import asyncio
import json
import statistics
import time

import aiohttp

from team.llm import OpenAICompatClient
from team.triage import LlmClassifier, TriageService

from . import gates


class Metered:
    """Wraps a client to count calls, tokens and time without changing what it returns."""

    def __init__(self, inner):
        self.inner, self.calls, self.tokens, self.last = inner, 0, 0, {}

    async def complete(self, messages, tools, *, model, max_tokens):
        self.calls += 1
        started = time.monotonic()
        self.last = {"latencia_ms": None, "tokens": 0, "erro": None}
        try:
            reply = await self.inner.complete(messages, tools, model=model, max_tokens=max_tokens)
        except Exception as error:
            self.last.update(latencia_ms=round((time.monotonic() - started) * 1000), erro=type(error).__name__)
            raise
        used = int(reply.usage.get("prompt_tokens", 0)) + int(reply.usage.get("completion_tokens", 0))
        self.tokens += used
        self.last.update(latencia_ms=round((time.monotonic() - started) * 1000), tokens=used)
        return reply


async def fetch_info(base_url, model, timeout=5):
    """Best effort: the Ollama version and the model digest. Missing information is reported, never guessed."""
    root = base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    info = {"modelo": model, "versao_do_servidor": "desconhecida", "digest": "desconhecido"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(f"{root}/api/version") as response:
                if response.status == 200:
                    info["versao_do_servidor"] = str((await response.json()).get("version") or "desconhecida")[:40]
            async with session.post(f"{root}/api/show", json={"model": model}) as response:
                if response.status == 200:
                    details = (await response.json()).get("details") or {}
                    info["familia"] = str(details.get("family") or "")[:40] or None
                    info["parametros"] = str(details.get("parameter_size") or "")[:20] or None
                    info["quantizacao"] = str(details.get("quantization_level") or "")[:20] or None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        pass
    return info


def _items():
    triage = gates.corpus("triagem")
    for item in triage["items"]:
        yield ("triagem", item["id"], item["text"], triage["catalog"], item["expected_decision"], sorted(item["expected_capabilities"]), None)
    for item in gates.corpus("injecao")["items"]:
        subset = item.get("expect_capabilities_subset_of")
        yield ("injecao", item["id"], item["text"], triage["catalog"], item["expect_decision"], None, subset)


async def run_ollama(config, *, limit=20, max_tokens=20000, client=None, info=None):
    model = config["model"]
    meter = Metered(client or OpenAICompatClient(config["ollama_url"], timeout=60))
    service = TriageService(None, None, None, None, LlmClassifier(meter, model))
    started = await fetch_info(config["ollama_url"], model) if info is None else info
    rows, skipped = [], 0
    for suite, item_id, text, catalog, expected, expected_caps, subset in _items():
        if meter.calls >= limit or meter.tokens >= max_tokens:
            skipped += 1
            continue
        before = meter.calls
        meter.last = {}
        outcome = await service.evaluate(text, catalog)
        used_model = meter.calls > before
        if expected_caps is not None:
            ok = outcome.decision == expected and sorted(outcome.capabilities) == expected_caps
        else:
            ok = outcome.decision == expected and (subset is None or outcome.decision not in ("rotear", "misto") or set(outcome.capabilities) <= set(subset))
        rows.append({"suite": suite, "id": item_id, "chamou_modelo": used_model, "decisao": outcome.decision, "ok": ok,
                     "latencia_ms": meter.last.get("latencia_ms") if used_model else None, "tokens": meter.last.get("tokens", 0) if used_model else 0,
                     "erro": meter.last.get("erro") if used_model else None})
    latencies = [r["latencia_ms"] for r in rows if r["latencia_ms"] is not None]
    return {"informacao": started, "orcamento": {"chamadas": limit, "tokens": max_tokens},
            "resumo": {"itens_avaliados": len(rows), "itens_ignorados_por_orcamento": skipped, "chamadas_ao_modelo": meter.calls,
                       "decisoes_corretas": sum(1 for r in rows if r["ok"]), "erros": sum(1 for r in rows if r["erro"]),
                       "tokens_total": meter.tokens, "latencia_mediana_ms": int(statistics.median(latencies)) if latencies else None,
                       "orcamento_esgotado": skipped > 0},
            "itens": rows, "observacao": "Resultado de um modelo local; nao e um gate e pode variar entre execucoes."}
