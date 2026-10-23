"""Guided setup: create a company, its departments and agents from a versioned template, after a preview.

A template (``team/templates/*.json``) is data. It never contains an account, a connection, a credential, a skill or an address:
applying it creates only the company, departments, agents, triage capabilities, the handoff targets between agents and the
read/write access to cases those agents need. The preview lists exactly that, with a hash; applying needs the hash of the
preview that was shown, so what is created is what was reviewed. Only the local owner can do this.
"""
import json
import re
from pathlib import Path

from .access import NotFound, OperatorContext, PermissionDenied
from .agents import _OVERRIDE, _plain, _texts
from .llm import BUILTIN_POLICIES
from .schema import content_hash, find_secrets

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TOP = {"template_id", "template_version", "title", "description", "departments", "agents", "capabilities", "handoffs", "case_access"}
AGENT_FIELDS = {"key", "department", "name", "capabilities", "goals", "personality", "scope", "out_of_scope", "model_policy_ref"}
NOT_INCLUDED = ["Nenhuma conta, conexão, credencial, skill ou endereço é criado ou conectado.",
                "Nada é enviado a clientes nem aprovado: toda ação externa continua dependendo de aprovação.",
                "Tudo o que for criado pode ser editado ou desativado depois nas seções da Equipe."]


class OnboardingRejected(ValueError):
    """The template, the name or the confirmation is not acceptable; the message is safe to show."""


def _text(value, limit, what):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise OnboardingRejected(f"{what} invalido no modelo.")
    return value.strip()


def validate_template(data):
    """Check a template's shape and references. Returns it unchanged; raises ``OnboardingRejected``."""
    if not isinstance(data, dict) or set(data) - TOP or not TOP - {"description", "handoffs", "case_access"} <= set(data):
        raise OnboardingRejected("Modelo com campos inesperados ou faltando.")
    if not _SLUG.match(str(data["template_id"])) or isinstance(data["template_version"], bool) or not isinstance(data["template_version"], int):
        raise OnboardingRejected("Identificacao ou versao do modelo invalida.")
    _text(data["title"], 120, "Titulo")
    if find_secrets(data):
        raise OnboardingRejected("Modelos nao podem conter segredos.")
    if re.search(r"https?://|@[a-z0-9.-]+\.[a-z]{2,}", json.dumps(data)):
        raise OnboardingRejected("Modelos nao podem conter enderecos nem contas.")
    departments = {}
    for item in data["departments"]:
        if set(item) != {"key", "name", "scope"} or not _KEY.match(str(item["key"])) or item["key"] in departments:
            raise OnboardingRejected("Departamento invalido ou repetido no modelo.")
        _text(item["name"], 120, "Nome do departamento"), _text(item["scope"], 500, "Escopo do departamento")
        departments[item["key"]] = item
    agents = {}
    for item in data["agents"]:
        if set(item) - AGENT_FIELDS or not {"key", "department", "name", "goals", "personality", "scope", "out_of_scope", "model_policy_ref"} <= set(item):
            raise OnboardingRejected("Agente com campos inesperados ou faltando no modelo.")
        if not _KEY.match(str(item["key"])) or item["key"] in agents or item["department"] not in departments:
            raise OnboardingRejected("Agente repetido ou com departamento desconhecido no modelo.")
        if item["model_policy_ref"] not in BUILTIN_POLICIES:
            raise OnboardingRejected("Politica de modelo desconhecida no modelo (use uma politica local embutida).")
        if not isinstance(item["goals"], list) or not item["goals"] or not isinstance(item.get("capabilities", []), list):
            raise OnboardingRejected("Objetivos ou capacidades invalidos no modelo.")
        _text(item["name"], 120, "Nome do agente"), _text(item["scope"], 500, "Escopo do agente"), _text(item["out_of_scope"], 500, "Fora de escopo")
        if any(pattern.search(_plain(text)) for text in _texts(item) if isinstance(text, str) for pattern in _OVERRIDE):
            raise OnboardingRejected("O perfil de um agente do modelo tenta anular regras ou aprovacoes; o preview ja recusa o que a criacao recusaria.")
        agents[item["key"]] = item
    capabilities = {}
    for item in data["capabilities"]:
        if set(item) - {"name", "description", "keywords", "requires_customer"} or not _SLUG.match(str(item.get("name"))) or item["name"] in capabilities \
                or not isinstance(item.get("keywords"), list) or not item["keywords"]:
            raise OnboardingRejected("Capacidade invalida ou repetida no modelo.")
        capabilities[item["name"]] = item
    for agent in agents.values():
        if any(c not in capabilities for c in agent.get("capabilities", [])):
            raise OnboardingRejected("Agente com capacidade que o modelo nao define.")
    for link in data.get("handoffs", []):
        if set(link) != {"from", "to"} or link["from"] not in agents or link["to"] not in agents or link["from"] == link["to"]:
            raise OnboardingRejected("Encaminhamento invalido no modelo.")
    if any(key not in agents for key in data.get("case_access", [])):
        raise OnboardingRejected("Acesso a demandas para um agente que nao existe no modelo.")
    return data


def load_templates(directory=TEMPLATES_DIR):
    found = {}
    for path in sorted(Path(directory).glob("*.json")):
        data = validate_template(json.loads(path.read_text(encoding="utf-8")))
        if data["template_id"] in found:
            raise OnboardingRejected("Modelo repetido.")
        found[data["template_id"]] = data
    return found


class OnboardingService:
    def __init__(self, access, team, agents, triage, templates=None):
        self.access, self.team, self.agents, self.triage = access, team, agents, triage
        self.templates = load_templates() if templates is None else {t["template_id"]: validate_template(t) for t in templates}

    def _operator(self, operator):
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.access.operator_id:
            raise PermissionDenied("apenas o dono local configura a equipe por modelo")

    def list_templates(self, operator):
        self._operator(operator)
        return [{"id": t["template_id"], "version": t["template_version"], "title": t["title"], "description": t.get("description", ""),
                 "departments": len(t["departments"]), "agents": len(t["agents"]), "capabilities": len(t["capabilities"])}
                for t in self.templates.values()]

    def _template(self, template_id):
        template = self.templates.get(template_id) if isinstance(template_id, str) else None
        if template is None:
            raise NotFound()
        return template

    def preview(self, operator, template_id, company_name):
        """Exactly what applying would create. Writes nothing."""
        self._operator(operator)
        template = self._template(template_id)
        name = _text(company_name, 120, "Nome da empresa") if isinstance(company_name, str) else _text("", 1, "Nome da empresa")
        if any(c["name"].strip().lower() == name.lower() for c in self.team.list_companies(operator)):
            raise OnboardingRejected("Ja existe uma empresa com este nome; escolha outro.")
        names = {a["key"]: a["name"] for a in template["agents"]}
        plan = {
            "template": {"id": template["template_id"], "version": template["template_version"], "title": template["title"]},
            "company": {"name": name},
            "departments": [{"key": d["key"], "name": d["name"], "scope": d["scope"]} for d in template["departments"]],
            "agents": [{"key": a["key"], "name": a["name"], "department": a["department"], "capabilities": list(a.get("capabilities", [])),
                        "model_policy_ref": a["model_policy_ref"], "scope": a["scope"], "out_of_scope": a["out_of_scope"], "goals": list(a["goals"]),
                        "personality": dict(a["personality"])} for a in template["agents"]],
            "capabilities": [{"name": c["name"], "description": c["description"], "keywords": list(c["keywords"]),
                              "requires_customer": bool(c.get("requires_customer"))} for c in template["capabilities"]],
            "handoffs": [{"de": names[h["from"]], "para": names[h["to"]], "keys": [h["from"], h["to"]]} for h in template.get("handoffs", [])],
            "case_access": [{"agent": names[k], "key": k, "acesso": "ler e escrever nas demandas da empresa"} for k in template.get("case_access", [])],
            "nao_inclui": list(NOT_INCLUDED),
        }
        plan["plan_hash"] = content_hash({k: v for k, v in plan.items() if k != "nao_inclui"})
        return plan

    def apply(self, operator, template_id, company_name, plan_hash):
        """Create what the preview showed. If anything fails midway, the company is left deactivated and the error says so."""
        plan = self.preview(operator, template_id, company_name)
        if not isinstance(plan_hash, str) or plan_hash != plan["plan_hash"]:
            raise OnboardingRejected("O modelo ou o nome mudou desde a pre-visualizacao; confira de novo antes de criar.")
        company = self.team.create_company(operator, plan["company"]["name"])
        try:
            ctx = self.access.tenant_context(operator, company["id"])
            departments = {d["key"]: self.team.create(ctx, "department", name=d["name"], scope=d["scope"], status="ativo") for d in plan["departments"]}
            for c in plan["capabilities"]:  # an agent can only claim capabilities that already exist
                self.triage.create_capability(ctx, name=c["name"], description=c["description"], keywords=c["keywords"], requires_customer=c["requires_customer"])
            agents = {}
            for a in plan["agents"]:
                agents[a["key"]] = self.agents.create_agent(
                    ctx, department_id=departments[a["department"]]["id"], name=a["name"], goals=a["goals"], personality=a["personality"],
                    scope=a["scope"], out_of_scope=a["out_of_scope"], model_policy_ref=a["model_policy_ref"], capabilities=a["capabilities"])
            for grant in plan["case_access"]:
                self.team.create(ctx, "access_grant", principal_id=agents[grant["key"]]["id"], resource_kind="case", resource_id=None,
                                 actions=["read", "write"], granted_by=operator.principal_id, expires_at=None)
            targets = {}
            for link in plan["handoffs"]:
                targets.setdefault(link["keys"][0], []).append(agents[link["keys"][1]]["id"])
            for key, ids in targets.items():
                current = self.agents.get_agent(ctx, agents[key]["id"])
                self.agents.update_agent(ctx, current["id"], current["revision"], handoff_target_ids=ids)
        except Exception as error:
            self._deactivate(operator, company)
            raise OnboardingRejected("A criacao falhou no meio (" + (str(error)[:120] or type(error).__name__)
                                     + "). A empresa foi criada DESATIVADA para nao ficar pela metade; revise e apague os dados de teste.") from None
        return {"company": {"id": company["id"], "name": company["name"]}, "departments": len(departments), "agents": len(agents),
                "capabilities": len(plan["capabilities"]), "plan_hash": plan["plan_hash"]}

    def _deactivate(self, operator, company):
        try:
            current = next(c for c in self.team.list_companies(operator) if c["id"] == company["id"])
            self.team.update_company(operator, company["id"], current["revision"], status="inativo")
        except Exception:
            pass
