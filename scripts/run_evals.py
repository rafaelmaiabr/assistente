"""Run the offline evals and gates; exit 1 on any gate violation or regression.

    python scripts/run_evals.py                          # everything, text summary
    python scripts/run_evals.py --json out.json --markdown out.md
    python scripts/run_evals.py --suite autorizacao --suite aprovacao
    python scripts/run_evals.py --update-baseline        # accept today's measured results as the new baseline (review the diff)
    python scripts/run_evals.py --ollama                 # ALSO ask the local model (optional, records latency/tokens/errors; never a gate)

The corpus is synthetic (evals/corpus). Nothing here touches the network unless --ollama is given, and then only the local model.
"""
import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals import runner, suites  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", action="append", choices=suites.ALL, help="run only this suite (repeatable)")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--stamp", action="store_true", help="add the generation time (the report is otherwise reproducible byte for byte)")
    parser.add_argument("--ollama", action="store_true")
    parser.add_argument("--ollama-limit", type=int, default=20, help="maximum number of model calls (default 20)")
    parser.add_argument("--ollama-tokens", type=int, default=20000, help="stop after this many tokens (default 20000)")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if args.stamp else None
    report = runner.run(args.suite, stamp=stamp)
    if args.ollama:
        from evals.ollama import run_ollama
        from settings import load_config
        report["ollama"] = asyncio.run(run_ollama(load_config(), limit=args.ollama_limit, max_tokens=args.ollama_tokens))
    if args.update_baseline:
        if args.suite:
            print("--update-baseline exige rodar todas as suites.", file=sys.stderr)
            return 2
        runner.save_baseline(report)
        print(f"Base atualizada em {runner.BASELINE_PATH.relative_to(ROOT)}; revise o diff antes de commitar.")
    if args.json:
        args.json.write_text(runner.to_json(report), encoding="utf-8", newline="\n")
    if args.markdown:
        args.markdown.write_text(runner.to_markdown(report), encoding="utf-8", newline="\n")
    print(runner.to_markdown(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
