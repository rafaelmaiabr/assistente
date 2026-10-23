"""The suites: gates (zero violations allowed) and measured suites (compared with a committed baseline).

Gates: autorizacao, aprovacao, dono_unico, envio_duplicado, vazamento_entre_empresas, injecao, evidencias.
Measured: triagem, escopo, cobranca_incorreta (their expected results come from the versioned corpus).
Everything runs offline against fakes; the only classifier used here is a scripted one.
"""
import asyncio
import json
from datetime import datetime, timezone

from team.access import NotFound, PermissionDenied
from team.cases import CaseRejected
from team.finance import analyse
from team.triage import TriageService, parse_answer

from . import gates
from .world import World

GATES = ("autorizacao", "aprovacao", "dono_unico", "envio_duplicado", "vazamento_entre_empresas", "injecao", "evidencias")
MEASURED = ("triagem", "escopo", "cobranca_incorreta")
ALL = GATES + MEASURED
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


class Scripted:
    """A model stand-in that always gives the same (possibly malicious) answer."""

    def __init__(self, answer):
        self.answer = answer

    async def classify(self, message, catalog):
        return parse_answer(json.dumps(self.answer), {item["name"] for item in catalog})


def result(check_id, problem):
    return {"id": check_id, "passed": problem is None, "detail": problem or ""}


def guarded(check_id, call):
    """Run one check; an unexpected exception is itself a failed check, never a crash of the whole report."""
    try:
        return result(check_id, call())
    except Exception as error:
        return result(check_id, f"erro inesperado: {type(error).__name__}")


def corpus_versions():
    return {name: gates.corpus(name)["corpus_version"] for name in ("triagem", "injecao", "evidencias", "escopo", "cobranca_incorreta", "vazamento", "duplicatas")}


# -- gates ------------------------------------------------------------------------------------------------------------------
def run_function_gate(world, suite):
    return [guarded(function.__name__, lambda f=function: f(world)) for function in gates.CHECKS[suite]]


def run_duplicates(world):
    checks = []
    for item in gates.corpus("duplicatas")["scenarios"]:
        def one(item=item):
            effects = gates.duplicate_effects(world, item["scenario"], item["repeats"])
            return None if effects == item["expect_effects"] else f"{item['scenario']}: {effects} efeito(s) em {item['repeats']} repeticoes (esperado {item['expect_effects']})"
        checks.append(guarded(item["id"], one))
    return checks


def run_leakage(world):
    checks = []
    for item in gates.corpus("vazamento")["attempts"]:
        checks.append(guarded(item["id"], lambda item=item: gates.leakage_outcome(world, item["via"])))
    return checks


def run_injection(world):
    data = gates.corpus("injecao")
    catalog = gates.corpus("triagem")["catalog"]
    checks = []
    for item in data["items"]:
        def one(item=item):
            service = TriageService(None, None, None, None, Scripted(item["trap"]))
            outcome = asyncio.run(service.evaluate(item["text"], catalog))
            if outcome.decision != item["expect_decision"]:
                return f"decisao {outcome.decision} (esperado {item['expect_decision']})"
            allowed = set(item.get("expect_capabilities_subset_of", []))
            if outcome.decision in ("rotear", "misto") and not set(outcome.capabilities) <= allowed:
                return f"roteou para {sorted(set(outcome.capabilities) - allowed)}, que so a resposta do modelo indicou"
            return None
        checks.append(guarded(item["id"], one))
    return checks


def run_evidence(world):
    data = gates.corpus("evidencias")
    checks = []
    for item in data["evidence"]:
        def one(item=item):
            case = world.working_case()
            ctx = world.agent_ctx() if item["actor"] == "agente" else world.ca
            accepted = True
            try:
                world.cases.add_evidence(ctx, case["id"], source="fonte sintetica", summary="Resumo da evidencia", evidence_type=item["evidence_type"],
                                         reliability=item["reliability"], content_hash=("a" * 64) if item["with_hash"] else None)
            except CaseRejected:
                accepted = False
            return None if accepted == (item["expect"] == "ok") else f"{item['actor']} {item['reliability']}/{item['evidence_type']}: {'aceita' if accepted else 'recusada'} (esperado {item['expect']})"
        checks.append(guarded(item["id"], one))
    for item in data["resolution"]:
        def resolve(item=item):
            case = world.working_case()
            ctx = world.agent_ctx() if item["actor"] == "agente" else world.ca
            if item.get("summary_only"):
                world.cases.add_summary(world.ca, case["id"], "Resumo gerado: tudo certo")
            for evidence in item["evidence"]:
                world.cases.add_evidence(world.ca, case["id"], source="fonte", summary="Prova", evidence_type=evidence["evidence_type"],
                                         reliability=evidence["reliability"], content_hash=("b" * 64) if evidence.get("with_hash") else None)
            current = world.cases.get_case(world.ca, case["id"])
            accepted = True
            try:
                world.cases.transition(ctx, case["id"], current["revision"], "resolvida", reason="Concluida", manual_resolution=bool(item.get("manual")))
            except (CaseRejected, PermissionDenied):
                accepted = False
            return None if accepted == (item["expect"] == "ok") else f"resolver ({item['actor']}): {'aceito' if accepted else 'recusado'} (esperado {item['expect']})"
        checks.append(guarded(item["id"], resolve))
    return checks


# -- measured suites ---------------------------------------------------------------------------------------------------------
def run_triage(world):
    data = gates.corpus("triagem")
    checks = []
    for item in data["items"]:
        def one(item=item):
            service = TriageService(None, None, None, None, Scripted(item.get("llm_answer", {"capacidades": []})))
            outcome = asyncio.run(service.evaluate(item["text"], data["catalog"]))
            ok = outcome.decision == item["expected_decision"] and sorted(outcome.capabilities) == sorted(item["expected_capabilities"])
            return None if ok else f"[{item['category']}] {outcome.decision} {sorted(outcome.capabilities)} (esperado {item['expected_decision']} {sorted(item['expected_capabilities'])})"
        checks.append(guarded(item["id"], one))
    return checks


def run_scope(world):
    catalog = gates.corpus("triagem")["catalog"]
    service = TriageService(None, None, None, None, None)
    checks = []
    for item in gates.corpus("escopo")["items"]:
        def one(item=item):
            outcome = asyncio.run(service.evaluate(item["text"], catalog))
            return None if outcome.decision not in item["expect_not"] else f"foi {outcome.decision} para {outcome.capabilities}"
        checks.append(guarded(item["id"], one))
    return checks


def run_billing(world):
    data = gates.corpus("cobranca_incorreta")
    checks = []
    for item in data["items"]:
        def one(item=item):
            records = [{**data["defaults"], **raw} for raw in item["records"]]
            for record in records:
                record.update(tenant_id="ten_x", revision=1, created_at="2026-05-01T00:00:00Z", updated_at="2026-05-01T00:00:00Z", kind="financial_record",
                              schema_version=1, batch_id="lote", imported_by="usr_x", note=None, source="sintetico",
                              counterparty_key=record["counterparty"].lower())
            analysis = analyse(records, data["company_names"], NOW, document=item.get("document"))
            codes = sorted({finding["codigo"] for finding in analysis["achados"]})
            blocking = any(finding["bloqueia"] for finding in analysis["achados"])
            problems = []
            missing = sorted(set(item["expect_codes"]) - set(codes))
            if missing or (item.get("exact") and codes != sorted(item["expect_codes"])):
                problems.append(f"achados {codes} (esperado {'exatamente ' if item.get('exact') else 'ao menos '}{sorted(item['expect_codes'])})")
            if blocking != item["expect_blocking"]:
                problems.append(f"bloqueia={blocking} (esperado {item['expect_blocking']})")
            if "expect_total_cents" in item:
                totals = {c: v["a_pagar"] for c, v in analysis["totais_confiaveis_por_moeda"].items()}
                if totals != item["expect_total_cents"]:
                    problems.append(f"totais {totals} (esperado {item['expect_total_cents']})")
            if analysis.get("executado") is not False:
                problems.append("a analise declarou ter executado algo")
            return "; ".join(problems) or None
        checks.append(guarded(item["id"], one))
    return checks


RUNNERS = {
    "autorizacao": lambda w: run_function_gate(w, "autorizacao"), "aprovacao": lambda w: run_function_gate(w, "aprovacao"),
    "dono_unico": lambda w: run_function_gate(w, "dono_unico"), "envio_duplicado": run_duplicates, "vazamento_entre_empresas": run_leakage,
    "injecao": run_injection, "evidencias": run_evidence, "triagem": run_triage, "escopo": run_scope, "cobranca_incorreta": run_billing,
}


def run_all(only=None):
    """Run the suites in a fresh fictitious world. Returns ``{suite: {"kind", "checks"}}`` in a fixed order."""
    selected = [name for name in ALL if only is None or name in only]
    world = World()
    try:
        return {name: {"kind": "gate" if name in GATES else "medida", "checks": RUNNERS[name](world)} for name in selected}
    finally:
        world.close()
