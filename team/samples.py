"""Synthetic, deterministic example documents (no real company, contact or credential)."""
import hashlib

NOW = "2026-01-15T12:00:00Z"

_NAMES = [
    ("tenant", "ten"), ("dep_fin", "dep"), ("dep_sup", "dep"), ("agt_fin", "agt"), ("agt_sup", "agt"),
    ("skill", "skl"), ("grant", "grt"), ("conn", "con"), ("cred", "crd"), ("case", "cas"), ("task", "tsk"),
    ("approval", "apr"), ("handoff", "hof"), ("evidence", "evd"), ("run", "run"), ("command", "cmd"),
    ("event", "evt"), ("corr", "cor"), ("user", "usr"),
]
IDS = {name: f"{prefix}_{number:032x}" for number, (name, prefix) in enumerate(_NAMES, start=1)}


def _doc(kind, key, tenant=True, stamped=True, **fields):
    doc = {"kind": kind, "schema_version": 1, "id": IDS[key]}
    if tenant:
        doc["tenant_id"] = IDS["tenant"]
    doc.update(fields)
    if stamped:
        doc.update(revision=1, created_at=NOW, updated_at=NOW)
    return doc


def bundle():
    """A small, internally consistent team: one tenant, two departments, two agents, one case."""
    return [
        _doc("tenant", "tenant", tenant=False, name="Empresa Exemplo", status="ativo"),
        _doc("department", "dep_fin", name="Financeiro", scope="Cobrancas e analise financeira", status="ativo"),
        _doc("department", "dep_sup", name="Suporte", scope="Atendimento tecnico", status="ativo"),
        _doc("skill_definition", "skill", tenant=False, skill_key="financeiro.consultar-pendencias",
             skill_version="1.0.0", content_hash=hashlib.sha256(b"exemplo").hexdigest(),
             instructions="Consultar pendencias financeiras e resumir com fontes.",
             required_tools=["listar-cobrancas"], operations=["read"], status="ativo"),
        _doc("connection", "conn", label="ERP de exemplo (MCP)", provider="erp-exemplo",
             connection_kind="mcp", transport="http", endpoint="http://127.0.0.1:9000/mcp",
             credential_ref=IDS["cred"], status="ativo"),
        _doc("skill_grant", "grant", agent_id=IDS["agt_fin"], skill_id=IDS["skill"], skill_version="1.0.0",
             connection_id=IDS["conn"], operations=["read"], resource="cobrancas", expires_at=None,
             granted_by=IDS["user"]),
        _doc("agent_definition", "agt_fin", department_id=IDS["dep_fin"], agent_version=1, name="Financeiro",
             personality={"tone": "profissional e objetivo", "initiative": "propor proximos passos"},
             scope="Cobrancas, recebimentos e analise financeira", out_of_scope="Encaminhar ao responsavel",
             skill_grant_ids=[IDS["grant"]], handoff_target_ids=[IDS["agt_sup"]],
             model_policy_ref="local-primeiro", voice_profile_ref="pf_dora", status="ativo"),
        _doc("agent_definition", "agt_sup", department_id=IDS["dep_sup"], agent_version=1, name="Suporte",
             personality={"tone": "claro e paciente", "initiative": "pedir informacao que falta"},
             scope="Diagnostico tecnico de sites", out_of_scope="Encaminhar a Secretaria",
             skill_grant_ids=[], handoff_target_ids=[IDS["agt_fin"]], model_policy_ref="local-primeiro",
             voice_profile_ref=None, status="ativo"),
        _doc("case", "case", department_id=IDS["dep_sup"], state="em_execucao",
             owner_agent_id=IDS["agt_sup"], customer_ref="cliente-exemplo",
             objective="Site do cliente indisponivel", closed_at=None),
        _doc("task", "task", case_id=IDS["case"], parent_task_id=None, assignee_agent_id=IDS["agt_fin"],
             state="pendente", objective="Analisar renovacao do dominio",
             expected_result="Valor, prazo e recomendacao com fontes", due_at=None),
        _doc("agent_run", "run", agent_id=IDS["agt_fin"], agent_version=1, department_id=IDS["dep_fin"],
             case_id=IDS["case"], task_id=IDS["task"], state="enfileirada",
             budget={"max_steps": 8, "token_budget": 6000, "deadline_seconds": 120},
             skill_versions=["financeiro.consultar-pendencias@1.0.0"], started_at=None, finished_at=None),
        _doc("approval_request", "approval", case_id=IDS["case"], requested_by=IDS["agt_fin"],
             action="renovar-dominio", payload_hash=hashlib.sha256(b"payload-exemplo").hexdigest(),
             expires_at="2026-01-16T12:00:00Z", state="pendente", single_use=True, decided_by=None),
        _doc("handoff", "handoff", case_id=IDS["case"], task_id=IDS["task"], handoff_kind="delegacao",
             from_agent_id=IDS["agt_sup"], to_agent_id=IDS["agt_fin"], reason="Analisar renovacao",
             context_summary="Dominio sem resposta DNS", sources=["evidencia-1"], state="proposto",
             idempotency_key="hof-exemplo-1", expected_case_revision=1, return_requested=True,
             expires_at=None),
        _doc("evidence", "evidence", case_id=IDS["case"], source="consulta DNS sintetica",
             collected_at=NOW, summary="Sem registro A para o dominio de exemplo", content_hash=None),
        _doc("command", "command", stamped=False, type="case.delegate-task", case_id=IDS["case"],
             task_id=IDS["task"], correlation_id=IDS["corr"], causation_id=None, actor_id=IDS["agt_sup"],
             expected_revision=1, idempotency_key="cmd-exemplo-1", issued_at=NOW,
             payload={"task_id": IDS["task"]}),
        _doc("event", "event", stamped=False, type="case.task-delegated", case_id=IDS["case"],
             correlation_id=IDS["corr"], causation_id=IDS["command"], actor_id=IDS["agt_sup"], revision=2,
             occurred_at=NOW, payload={"task_id": IDS["task"]}),
    ]
