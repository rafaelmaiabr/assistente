"""Report, baseline and regression detection.

A report lists, per suite, how many checks passed and which failed. It has no timestamp unless asked, so the same code
and corpus give a byte-identical report. The release is blocked by any **gate violation** (a gate needs zero) or by a
**regression** (a measured suite passing fewer checks than the committed baseline, or having fewer checks than it did).
"""
import json
from pathlib import Path

from . import suites

REPORT_VERSION = 1
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


def build_report(results, baseline=None, stamp=None):
    summary, violations, regressions = {}, [], []
    for name, data in results.items():
        checks = data["checks"]
        failed = [{"id": c["id"], "detalhe": c["detail"]} for c in checks if not c["passed"]]
        summary[name] = {"tipo": data["kind"], "total": len(checks), "aprovados": len(checks) - len(failed), "falhas": failed}
        if data["kind"] == "gate":
            violations += [{"suite": name, **f} for f in failed]
        elif baseline is not None and name in baseline.get("suites", {}):
            expected = baseline["suites"][name]
            if summary[name]["aprovados"] < expected["min_passed"]:
                regressions.append({"suite": name, "motivo": "menos aprovados que a base",
                                    "aprovados": summary[name]["aprovados"], "minimo": expected["min_passed"]})
            if summary[name]["total"] < expected["total"]:
                regressions.append({"suite": name, "motivo": "cobertura reduzida (menos itens que a base)",
                                    "total": summary[name]["total"], "base": expected["total"]})
    report = {"report_version": REPORT_VERSION, "corpus": suites.corpus_versions(), "suites": summary,
              "violacoes_de_gate": violations, "regressoes": regressions, "ok": not violations and not regressions}
    if stamp:
        report["gerado_em"] = stamp
    return report


def baseline_from(report):
    """Baseline = what the measured suites pass today. Updating it is an explicit, reviewable act."""
    return {"baseline_version": 1, "suites": {name: {"total": s["total"], "min_passed": s["aprovados"]}
                                              for name, s in report["suites"].items() if s["tipo"] == "medida"}}


def load_baseline(path=BASELINE_PATH):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_baseline(report, path=BASELINE_PATH):
    Path(path).write_text(json.dumps(baseline_from(report), ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def to_json(report):
    return json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def to_markdown(report):
    lines = ["# Relatorio de evals", "", f"Resultado: **{'OK' if report['ok'] else 'FALHOU'}**", "",
             "| Suite | Tipo | Aprovados | Total |", "|---|---|---|---|"]
    lines += [f"| {name} | {s['tipo']} | {s['aprovados']} | {s['total']} |" for name, s in report["suites"].items()]
    if report["violacoes_de_gate"]:
        lines += ["", "## Violacoes de gate (tolerancia zero)"] + [f"- `{v['suite']}/{v['id']}`: {v['detalhe']}" for v in report["violacoes_de_gate"]]
    if report["regressoes"]:
        lines += ["", "## Regressoes em relacao a base"] + [f"- `{r['suite']}`: {r['motivo']} ({', '.join(f'{k}={v}' for k, v in r.items() if k not in ('suite', 'motivo'))})" for r in report["regressoes"]]
    failing = [(name, s) for name, s in report["suites"].items() if s["tipo"] == "medida" and s["falhas"]]
    if failing:
        lines += ["", "## Itens medidos que nao passaram (informativo)"]
        for name, s in failing:
            lines += [f"- `{name}/{f['id']}`: {f['detalhe']}" for f in s["falhas"]]
    lines += ["", f"Corpus: {', '.join(f'{k} v{v}' for k, v in report['corpus'].items())}.",
              "", "Os gates mostram que os cenarios listados se comportaram como esperado; nao provam que o sistema seja invulneravel."]
    return "\n".join(lines) + "\n"


def run(only=None, baseline_path=BASELINE_PATH, stamp=None):
    return build_report(suites.run_all(only), load_baseline(baseline_path), stamp)
