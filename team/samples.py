"""Synthetic, deterministic example documents (no real company, contact or credential)."""
import hashlib

from .schema import content_hash

NOW = "2026-01-15T12:00:00Z"

_NAMES = [
    ("tenant", "ten"), ("dep_fin", "dep"), ("dep_sup", "dep"), ("agt_fin", "agt"), ("agt_sup", "agt"),
    ("skill", "skl"), ("grant", "grt"), ("conn", "con"), ("cred", "crd"), ("case", "cas"), ("task", "tsk"),
    ("approval", "apr"), ("handoff", "hof"), ("evidence", "evd"), ("run", "run"), ("command", "cmd"),
    ("event", "evt"), ("corr", "cor"), ("user", "usr"), ("contact", "ctc"), ("address", "adr"),
    ("observed", "adr"), ("link", "lnk"), ("acl", "acl"), ("mcpentry", "mcp"), ("apv_fin", "apv"), ("apv_sup", "apv"), ("policy", "mpl"), ("entry", "ent"), ("cap", "cap"), ("tri", "tri"), ("msg", "msg"), ("kdoc", "kdc"), ("mem", "mem"),
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


def profile_version(agent, version_id, status="ativo"):
    """Immutable snapshot document for an agent definition (what publishing stores)."""
    return {"kind": "agent_profile_version", "schema_version": 1, "id": version_id, "tenant_id": agent["tenant_id"],
            "revision": 1, "created_at": NOW, "updated_at": NOW, "agent_id": agent["id"],
            "agent_version": agent["agent_version"], "definition": agent, "content_hash": content_hash(agent),
            "status": status, "created_by": IDS["user"]}


def bundle():
    """A small, internally consistent team: one tenant, two departments, two agents, one case."""
    finance = _doc("agent_definition", "agt_fin", department_id=IDS["dep_fin"], agent_version=1, name="Financeiro",
         personality={"tone": "profissional e objetivo", "initiative": "propor proximos passos"},
         scope="Cobrancas, recebimentos e analise financeira", out_of_scope="Encaminhar ao responsavel",
         skill_grant_ids=[IDS["grant"]], handoff_target_ids=[IDS["agt_sup"]],
         model_policy_ref="local-primeiro", voice_profile_ref="pf_dora", status="ativo",
         goals=["Resumir pendencias financeiras com fontes"], routing_policy="encaminhar",
         capabilities=["financeiro"])
    support = _doc("agent_definition", "agt_sup", department_id=IDS["dep_sup"], agent_version=1, name="Suporte",
         personality={"tone": "claro e paciente", "initiative": "pedir informacao que falta"},
         scope="Diagnostico tecnico de sites", out_of_scope="Encaminhar a Secretaria",
         skill_grant_ids=[], handoff_target_ids=[IDS["agt_fin"]], model_policy_ref="local-primeiro",
         voice_profile_ref=None, status="ativo", goals=["Diagnosticar o problema do cliente"],
         routing_policy="esclarecer")
    versions = [("apv_fin", "agt_fin", finance), ("apv_sup", "agt_sup", support)]
    return [
        _doc("tenant", "tenant", tenant=False, name="Empresa Exemplo", status="ativo"),
        _doc("department", "dep_fin", name="Financeiro", scope="Cobrancas e analise financeira", status="ativo"),
        _doc("department", "dep_sup", name="Suporte", scope="Atendimento tecnico", status="ativo"),
        _doc("skill_definition", "skill", tenant=False, skill_key="financeiro.consultar-pendencias",
             skill_version="1.0.0", content_hash=hashlib.sha256(b"exemplo").hexdigest(),
             instructions="Consultar pendencias financeiras e resumir com fontes.",
             required_tools=["listar-cobrancas"], operations=["read"], status="ativo"),
        _doc("mcp_catalog_entry", "mcpentry", tenant=False, catalog_key="erp-exemplo", entry_version="1.0.0",
             transport="http", url="http://127.0.0.1:9000/mcp", launcher=None, tools=["listar-cobrancas"],
             approved_by=IDS["user"], status="ativo"),
        _doc("capability", "cap", name="financeiro", description="Cobrancas, boletos e pagamentos",
             keywords=["boleto", "cobranca", "pagamento"], requires_customer=True, status="ativo"),
        _doc("model_policy", "policy", name="local-primeiro",
             models={"local": {"provider": "ollama", "model": "qwen2.5:7b", "base_url": "http://127.0.0.1:11435/v1"}},
             rules=[{"when": {"complexity": ["baixa"]}, "use": "local"}], default="local", fallbacks=["local"],
             limits={"max_output_tokens": 800}, external_enabled=False, external_acknowledged_at=None),
        _doc("credential", "cred", label="Credencial do ERP de exemplo", store="cofre-local", state="ativa"),
        _doc("connection", "conn", label="ERP de exemplo (MCP)", provider="erp-exemplo",
             connection_kind="mcp", transport="http", endpoint="http://127.0.0.1:9000/mcp",
             credential_ref=IDS["cred"], catalog_entry_id=IDS["mcpentry"], scopes=["cobrancas.read"],
             status="ativo"),
        _doc("skill_grant", "grant", agent_id=IDS["agt_fin"], skill_id=IDS["skill"], skill_version="1.0.0",
             connection_id=IDS["conn"], operations=["read"], resource="cobrancas", expires_at=None,
             granted_by=IDS["user"]),
        finance,
        support,
        *[_doc("agent_profile_version", key, agent_id=IDS[agent], agent_version=1, status="ativo",
               definition=definition, content_hash=content_hash(definition), created_by=IDS["user"])
          for key, agent, definition in versions],
        _doc("case", "case", department_id=IDS["dep_sup"], state="em_execucao",
             owner_agent_id=IDS["agt_sup"], customer_ref="cliente-exemplo",
             objective="Site do cliente indisponivel", closed_at=None, priority="alta", origin="email"),
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
             collected_at=NOW, summary="Sem registro A para o dominio de exemplo", content_hash=None,
             evidence_type="fato", reliability="declarada", recorded_by=IDS["agt_sup"]),
        _doc("triage_decision", "tri", case_id=IDS["case"], decision="rotear", method="regra",
             capabilities=["financeiro"], confidence_pct=90,
             reasons=[{"tipo": "regra", "capacidade": "financeiro", "palavras": ["boleto", "cobranca"]}],
             question=None, message_hash=hashlib.sha256(b"mensagem de exemplo").hexdigest(),
             catalog_hash=hashlib.sha256(b"catalogo de exemplo").hexdigest(), threshold_route=70, threshold_clarify=40,
             rounds=1, decided_by=IDS["user"]),
        _doc("outbound_message", "msg", case_id=IDS["case"], channel="email", account_id=IDS["conn"],
             to=["cliente@exemplo.invalid"], subject="Atualizacao do seu chamado", body="Estamos verificando o seu site.",
             state="rascunho", dedup_key="msg-exemplo-1", owner_agent_id=IDS["agt_sup"], owner_version=1,
             draft_hash=hashlib.sha256(b"rascunho de exemplo").hexdigest(), approval_id=None, provider_id=None,
             sent_at=None, attempts=0, last_error=None, created_by=IDS["agt_sup"]),
        _doc("knowledge_document", "kdoc", department_id=IDS["dep_fin"], doc_key="politica-de-cobranca", version=1,
             title="Politica de cobranca", body="Boletos vencem em 10 dias; renegociacao exige aprovacao.",
             source="manual interno", topic="cobranca", status="publicado", valid_until=None, published_by=IDS["user"],
             content_hash=hashlib.sha256(b"politica").hexdigest(), suspicious=False),
        _doc("memory_item", "mem", scope="cliente", department_id=None, subject="cliente-exemplo", memory_kind="preferencia",
             text="Prefere contato por email.", state="confirmada", source="confirmado pelo operador",
             proposed_by=IDS["agt_sup"], decided_by=IDS["user"], decided_at=NOW, observed_at=NOW, expires_at=None),
        _doc("case_entry", "entry", case_id=IDS["case"], entry_type="nota_interna", direction=None, channel=None,
             author=IDS["agt_sup"], body="Cliente relatou site fora do ar desde a manha."),
        _doc("contact", "contact", display_name="Cliente Exemplo", status="ativo"),
        _doc("contact_address", "address", contact_id=IDS["contact"], channel="email",
             address="cliente@exemplo.invalid", link_state="vinculado"),
        _doc("contact_address", "observed", contact_id=None, channel="email",
             address="remetente@exemplo.invalid", link_state="observado"),
        _doc("contact_link", "link", contact_id=IDS["contact"], department_id=IDS["dep_sup"], relation="cliente"),
        _doc("access_grant", "acl", principal_id=IDS["agt_fin"], resource_kind="case", resource_id=IDS["case"],
             actions=["read"], granted_by=IDS["user"], expires_at=None),
        _doc("command", "command", stamped=False, type="case.delegate-task", case_id=IDS["case"],
             task_id=IDS["task"], correlation_id=IDS["corr"], causation_id=None, actor_id=IDS["agt_sup"],
             expected_revision=1, idempotency_key="cmd-exemplo-1", issued_at=NOW,
             payload={"task_id": IDS["task"]}),
        _doc("event", "event", stamped=False, type="case.task-delegated", case_id=IDS["case"],
             correlation_id=IDS["corr"], causation_id=IDS["command"], actor_id=IDS["agt_sup"], revision=2,
             occurred_at=NOW, payload={"task_id": IDS["task"]}),
    ]
