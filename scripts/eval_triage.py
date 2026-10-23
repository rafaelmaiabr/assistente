"""Measure the triage rules (and optionally a real local model) on the synthetic corpus.

    python scripts/eval_triage.py            # rules only, no model needed
    python scripts/eval_triage.py --llm      # also asks the local Ollama model when no rule applies

Prints accuracy per category, the mismatches and the thresholds used. The corpus is synthetic
(tests/data/triage_corpus.json); it measures this pipeline, not real customer traffic.
"""
import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from team.triage import CLARIFY_MIN, ROUTE_MIN, ROUTE_MIN_MODEL, LlmClassifier, TriageService  # noqa: E402


async def main(use_llm: bool) -> int:
    corpus = json.loads((ROOT / "tests/data/triage_corpus.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "tests/data/triage_catalog.json").read_text(encoding="utf-8"))
    classifier = None
    if use_llm:
        from settings import load_config
        from team.llm import OpenAICompatClient
        config = load_config()
        classifier = LlmClassifier(OpenAICompatClient(config["ollama_url"], timeout=120), config["model"])
    service = TriageService(None, None, None, None, classifier)
    stats, misses = defaultdict(lambda: [0, 0]), []
    for item in corpus:
        result = await service.evaluate(item["text"], catalog)
        ok = result.decision == item["expected_decision"] and sorted(result.capabilities) == sorted(item["expected_capabilities"])
        stats[item["category"]][0] += ok
        stats[item["category"]][1] += 1
        if not ok:
            misses.append((item["category"], item["text"][:60], result.decision, result.capabilities, result.confidence))
    total = sum(v[1] for v in stats.values())
    right = sum(v[0] for v in stats.values())
    print(f"limiares: regra {ROUTE_MIN}%, so modelo {ROUTE_MIN_MODEL}% (e >= 4 palavras), esclarecer >= {CLARIFY_MIN}%; "
          f"classificador: {'LLM local' if use_llm else 'nenhum (so regras)'}")
    for category, (hit, count) in sorted(stats.items()):
        print(f"  {category:28s} {hit}/{count}")
    print(f"total: {right}/{total} ({100 * right / total:.0f}%)")
    for miss in misses:
        print("  erro:", miss)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args().llm)))
