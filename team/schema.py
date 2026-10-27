"""Executable schemas for the team domain, validated with the standard library only.

A field spec is a string ``<type>[?]`` (``?`` = optional/nullable) or a nested dict of specs.
Types: str, text, int, posint, bool, ts, sha256, semver, slug, url, actor, obj,
``id:<kind>`` / ``ref:<kind>`` (opaque id of an entity kind; ``ref`` is also checked across a
bundle), ``enum:<Name>`` and ``list:<type>``. Unknown fields are rejected.
"""
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from .enums import ENUMS, LLM_PROVIDERS
from .ids import ACTOR_PREFIXES, PREFIXES, is_id

SCHEMA_VERSION = 1
SUPPORTED_VERSIONS = (1,)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_TEXT = 8000

# Secrets live in the vault and are referenced by id; never inline (as keys or values).
_SECRET_KEY = re.compile(
    r"(^|_)(password|passwd|secret|token|apikey|api_key|authorization|private_key|bearer)(_|$)", re.I)
_SAFE_KEYS = {"token_budget", "max_tokens"}
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}|Bearer\s+\S{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|://[^/\s:@]+:[^/\s@]+@)")


class ContractError(ValueError):
    def __init__(self, errors, code="invalid"):
        self.errors = list(errors)
        self.code = code
        super().__init__("; ".join(self.errors))


def _entity(kind, tenant=True, envelope=False, **fields):
    spec = {"kind": "str", "schema_version": "posint", "id": f"id:{kind}"}
    if tenant:
        spec["tenant_id"] = "ref:tenant"
    spec.update(fields)
    if not envelope:
        spec.update(revision="posint", created_at="ts", updated_at="ts")
    return spec


SCHEMAS = {
    "tenant": _entity("tenant", tenant=False, name="str", status="enum:Status"),
    "department": _entity("department", name="str", scope="text", status="enum:Status"),
    "agent_definition": _entity(
        "agent_definition", department_id="ref:department", agent_version="posint", name="str",
        personality={"tone": "text", "initiative": "text"}, scope="text", out_of_scope="text",
        skill_grant_ids="list:ref:skill_grant", handoff_target_ids="list:ref:agent_definition",
        model_policy_ref="slug", voice_profile_ref="slug?", status="enum:Status",
        goals="list:text?", routing_policy="enum:RoutingPolicy?", capabilities="list:slug?"),
    # What a company can ask its team to do; triage routes by these names, never by ids written by a model.
    "capability": _entity(
        "capability", name="slug", description="text", keywords="list:str", requires_customer="bool?",
        status="enum:Status"),
    # Append-only, verifiable record of one triage decision (the message is kept only as a hash).
    "triage_decision": _entity(
        "triage_decision", case_id="ref:case", decision="enum:TriageDecision", method="enum:TriageMethod",
        capabilities="list:slug", confidence_pct="int", reasons="list:obj", question="text?",
        message_hash="sha256", catalog_hash="sha256", threshold_route="int", threshold_clarify="int",
        rounds="posint", decided_by="actor"),
    # Immutable snapshot of one published agent version; only ``status`` (disabling) may change later.
    "agent_profile_version": _entity(
        "agent_profile_version", agent_id="ref:agent_definition", agent_version="posint", definition="obj",
        content_hash="sha256", status="enum:Status", created_by="actor"),
    "agent_run": _entity(
        "agent_run", agent_id="ref:agent_definition", agent_version="posint", department_id="ref:department",
        case_id="ref:case?", task_id="ref:task?", state="enum:RunState",
        budget={"max_steps": "posint", "token_budget": "posint", "deadline_seconds": "posint"},
        skill_versions="list:str", started_at="ts?", finished_at="ts?"),
    # Skills are a global declarative catalog: instructions never grant access by themselves.
    "skill_definition": _entity(
        "skill_definition", tenant=False, skill_key="slug", skill_version="semver", content_hash="sha256",
        instructions="text", required_tools="list:slug", operations="list:enum:Operation",
        status="enum:Status", description="text?", inputs="list:slug?", outputs="list:slug?",
        risks="list:enum:RiskTag?", settings="list:slug?", tool_operations="obj?", fictitious="bool?"),
    "skill_grant": _entity(
        "skill_grant", agent_id="ref:agent_definition", skill_id="ref:skill_definition",
        skill_version="semver", connection_id="ref:connection?", operations="list:enum:Operation",
        resource="str?", expires_at="ts?", granted_by="actor", revoked_at="ts?"),
    "connection": _entity(
        "connection", label="str", provider="slug", connection_kind="enum:ConnectionKind",
        transport="enum:McpTransport?", endpoint="url?", credential_ref="ref:credential?",
        catalog_entry_id="ref:mcp_catalog_entry?", scopes="list:slug?", status="enum:Status",
        allowed_recipient_domains="list:str?"),
    # Routing rules and models for the LLM; external providers stay off until the company enables and acknowledges.
    "model_policy": _entity(
        "model_policy", name="slug", models="obj", rules="list:obj", default="slug", fallbacks="list:slug",
        limits="obj", external_enabled="bool", external_acknowledged_at="ts?"),
    # Metadata only: the secret value lives in the vault under (tenant_id, credential id).
    "credential": _entity("credential", label="str", store="enum:CredentialStore", state="enum:CredentialState"),
    # Approved MCP servers (global, operator-approved). stdio names a launcher id, never a command line.
    "mcp_catalog_entry": _entity(
        "mcp_catalog_entry", tenant=False, catalog_key="slug", entry_version="semver",
        transport="enum:McpTransport", url="url?", launcher="slug?", tools="list:slug", approved_by="actor",
        status="enum:Status", tool_specs="obj?"),
    "case": _entity(
        "case", department_id="ref:department", state="enum:CaseState",
        owner_agent_id="ref:agent_definition?", customer_ref="str?", objective="text", closed_at="ts?",
        priority="enum:Priority?", origin="enum:Origin?", operator_control="bool?", owner_version="posint?"),
    "task": _entity(
        "task", case_id="ref:case", parent_task_id="ref:task?", assignee_agent_id="ref:agent_definition",
        state="enum:TaskState", objective="text", expected_result="text", due_at="ts?"),
    "approval_request": _entity(
        "approval_request", case_id="ref:case", requested_by="actor", action="str",
        payload_hash="sha256", expires_at="ts", state="enum:ApprovalState", single_use="bool",
        decided_by="actor?", resource="str?", recipient="obj?", amount_cents="int?", currency="slug?",
        normalized_action="obj?", policy_ref="slug?", decided_at="ts?", consumed_at="ts?", reason="text?",
        effect_outcome="enum:EffectOutcome?", effect_detail="text?"),
    "handoff": _entity(
        "handoff", case_id="ref:case", task_id="ref:task?", handoff_kind="enum:HandoffKind",
        from_agent_id="ref:agent_definition", to_agent_id="ref:agent_definition", reason="text",
        context_summary="text", sources="list:str", state="enum:HandoffState", idempotency_key="str",
        expected_case_revision="posint", return_requested="bool", expires_at="ts?",
        objective="text?", expected_result="text?", evidence_ids="list:ref:evidence?", return_to="ref:agent_definition?",
        attention="bool?", resolution="text?"),
    "evidence": _entity(
        "evidence", case_id="ref:case", source="str", collected_at="ts", summary="text",
        content_hash="sha256?", evidence_type="enum:EvidenceType?", reliability="enum:Reliability?",
        recorded_by="actor?"),
    # Outbound messages: a draft is data; sending is a separate, gated step (the table doubles as the outbox).
    "outbound_message": _entity(
        "outbound_message", case_id="ref:case", channel="enum:OutboundChannel", account_id="ref:connection",
        to="list:str", subject="str?", body="text", state="enum:MessageState", dedup_key="str",
        owner_agent_id="ref:agent_definition", owner_version="posint", draft_hash="sha256",
        approval_id="ref:approval_request?", provider_id="str?", sent_at="ts?", attempts="int", last_error="str?",
        created_by="actor", cc="list:str?", bcc="list:str?", attachments="list:obj?", message_id="str?", in_reply_to="str?",
        references="list:str?", provider_result="str?", simulated="bool?"),
    # Knowledge: versioned published documents with a source; retrieval treats their text as untrusted data.
    "knowledge_document": _entity(
        "knowledge_document", department_id="ref:department?", doc_key="slug", version="posint", title="str", body="text",
        source="str", topic="slug?", status="enum:DocStatus", valid_until="ts?", published_by="actor",
        content_hash="sha256", suspicious="bool?"),
    # An OAuth client the operator registered with a provider. The client secret (if any) lives in the vault.
    "oauth_app": _entity("oauth_app", provider="slug", client_id="str", secret_ref="str?", label="str", status="enum:Status", directory="str?"),
    # What one connection was granted: metadata only. The refresh token lives in the vault, access tokens in memory.
    "oauth_grant": _entity(
        "oauth_grant", connection_id="ref:connection", app_id="ref:oauth_app", provider="slug", account="str?", scopes="list:str",
        state="enum:OAuthState", granted_at="ts", refreshed_at="ts?", error_code="str?"),
    # An explicit, opt-in routine. It only enqueues jobs; it never grants a permission or an approval.
    "schedule": _entity(
        "schedule", name="str", job_kind="slug", payload="obj", timezone="str", frequency="enum:ScheduleFrequency",
        time_of_day="str?", weekday="int?", interval_minutes="posint?", catch_up="enum:CatchUp", max_runs_per_day="posint",
        state="enum:ScheduleState", state_reason="str?", next_run_at="ts", last_run_at="ts?", last_job_id="str?",
        runs_day="str?", runs_today="int", overlaps="int", budget_skips="int", catchup_skipped="int", failures="int",
        opt_in="bool", created_by="actor"),
    # An alert for the operator. Repeats inside the cool-down raise the counter instead of creating noise.
    "notification": _entity(
        "notification", severity="enum:NotifySeverity", topic="slug", dedupe_key="str", message="text", subject_ref="str?",
        state="enum:NotifyState", count="posint", last_seen_at="ts"),
    # One mailbox folder bound to a department. The cursor is (UIDVALIDITY, last UID); a new UIDVALIDITY resets it.
    "mailbox_cursor": _entity("mailbox_cursor", connection_id="ref:connection", department_id="ref:department", folder="str",
                              uidvalidity="posint?", last_uid="int", resets="int", status="enum:Status"),
    # Every message looked at, kept or ignored, with its identifiers so replies thread by headers (never by subject).
    "email_message": _entity(
        "email_message", connection_id="ref:connection", folder="str", uidvalidity="posint", uid="posint", message_id="str?",
        in_reply_to="str?", references="list:str", sender="str", sender_name="str?", recipients="list:str",
        classification="enum:EmailClass", case_id="ref:case?", identity="enum:IdentityState", subject_hash="sha256",
        body_hash="sha256", reason="str?"),
    # A financial fact imported on purpose. Amount, currency and dates stay empty when the source lacks them:
    # nothing is inferred. Free text (note) is untrusted data.
    "financial_record": _entity(
        "financial_record", record_kind="enum:RecordKind", counterparty="str", beneficiary="str?", payer="str?",
        document_number="str?", reference="str?", amount_cents="int?", currency="str?", issued_at="ts?", due_at="ts?",
        origin="enum:RecordOrigin", source="str", source_date="ts", batch_id="str", imported_by="actor", status="enum:Status",
        note="str?"),
    # A host the company authorizes for network diagnostics; nothing outside this list is ever probed.
    "monitored_asset": _entity("monitored_asset", hostname="str", note="str?", status="enum:Status", created_by="actor"),
    # Memory: preferences and dated facts. Anything inferred is only a proposal until the operator confirms it.
    # Something the team was asked to do and could not: no instruction (skill) or no tool. The agent stops and a person
    # decides whether to write a skill, build a tool or decline (the refusal is kept as a confirmed memory).
    "capability_gap": _entity(
        "capability_gap", case_id="ref:case", reported_by="actor", requested="text", tried="list:str", gap_type="enum:GapType",
        topic="slug", occurrences="posint", related_case_ids="list:ref:case", state="enum:GapState",
        decision="enum:GapDecision?", decision_note="text?", decided_by="actor?", decided_at="ts?", follow_up="str?",
        memory_id="ref:memory_item?", fulfilled_at="ts?"),
    # A task handed to an agent outside this process (A2A). The case owner stays responsible: this record only follows the remote
    # task. ``state`` "desconhecido" = the send may or may not have arrived (no remote id yet); it blocks a blind repeat.
    "a2a_task": _entity(
        "a2a_task", case_id="ref:case", connection_id="ref:connection", requested_by="actor", message_id="str", remote_task_id="str?",
        remote_state="str?", state="enum:A2ATaskState", objective="text", expected_result="text?", expires_at="ts", checked_at="ts?",
        result_text="text?", attention="bool", detail="text?"),
    # One row of the ERP charge ledger (#110): a charge's reconciled state, an event already applied (idempotency) or an issue
    # request whose outcome may be unknown. (tenant, namespace, provider, entry_key) is unique, so a duplicate cannot be stored twice.
    "erp_ledger_entry": _entity("erp_ledger_entry", namespace="enum:LedgerNamespace", provider="slug", entry_key="str", payload="obj"),
    "memory_item": _entity(
        "memory_item", scope="enum:MemoryScope", department_id="ref:department?", subject="str?",
        memory_kind="enum:MemoryKind", text="text", state="enum:MemoryState", source="str", proposed_by="actor",
        decided_by="actor?", decided_at="ts?", observed_at="ts", expires_at="ts?"),
    # Timeline entries are append-only: internal notes, messages exchanged with the customer, and AI summaries.
    "case_entry": _entity(
        "case_entry", case_id="ref:case", entry_type="enum:EntryType", direction="enum:Direction?",
        channel="enum:EntryChannel?", author="actor", body="text"),
    # Contacts: a person's identity is separate from the addresses seen or linked to them.
    "contact": _entity("contact", display_name="str", status="enum:Status"),
    "contact_address": _entity(
        "contact_address", contact_id="ref:contact?", channel="enum:Channel", address="str",
        link_state="enum:LinkState"),
    "contact_link": _entity(
        "contact_link", contact_id="ref:contact", department_id="ref:department", relation="enum:Relation"),
    # Default-deny: an agent or operator reaches a resource only through an explicit grant.
    "access_grant": _entity(
        "access_grant", principal_id="actor", resource_kind="enum:ResourceKind", resource_id="str?",
        actions="list:enum:AccessAction", granted_by="actor", expires_at="ts?"),
    # Envelopes. ``case_id`` may be null only for configuration messages (type "config.*").
    "command": _entity(
        "command", envelope=True, type="slug", case_id="ref:case?", task_id="ref:task?",
        correlation_id="id:correlation", causation_id="id:envelope?", actor_id="actor",
        expected_revision="posint?", idempotency_key="str", issued_at="ts", payload="obj"),
    "event": _entity(
        "event", envelope=True, type="slug", case_id="ref:case?", correlation_id="id:correlation",
        causation_id="id:envelope?", actor_id="actor", revision="posint", occurred_at="ts", payload="obj"),
}


def content_hash(definition) -> str:
    """sha256 of the canonical JSON of a document; identifies a published version's content."""
    canonical = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def kinds():
    return tuple(SCHEMAS)


def _is_ts(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _split(spec):
    optional = spec.endswith("?")
    return (spec[:-1] if optional else spec), optional


def _check(spec, value, path, errors):
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            errors.append(f"{path}: esperado objeto")
        else:
            _check_fields(spec, value, path, errors)
        return
    base, optional = _split(spec)
    if value is None:
        if not optional:
            errors.append(f"{path}: obrigatorio")
        return
    head, _, rest = base.partition(":")
    ok, label = True, base
    if head == "list":
        if not isinstance(value, list):
            errors.append(f"{path}: esperado lista")
            return
        for index, item in enumerate(value):
            _check(rest, item, f"{path}[{index}]", errors)
        return
    if head in ("id", "ref"):
        ok = is_id(value, "cmd") or is_id(value, "evt") if rest == "envelope" else is_id(value, PREFIXES[rest])
        label = f"id opaco {rest}"
    elif head == "enum":
        ok, label = value in ENUMS[rest], f"um de {', '.join(ENUMS[rest])}"
    elif base in ("str", "text"):
        limit = MAX_TEXT if base == "text" else 300
        ok, label = isinstance(value, str) and value.strip() != "" and len(value) <= limit, f"texto 1-{limit}"
    elif base in ("int", "posint"):
        ok = isinstance(value, int) and not isinstance(value, bool) and (base == "int" or value >= 1)
    elif base == "bool":
        ok = isinstance(value, bool)
    elif base == "ts":
        ok, label = _is_ts(value), "data/hora ISO-8601 com fuso"
    elif base == "sha256":
        ok = isinstance(value, str) and bool(_SHA256.match(value))
    elif base == "semver":
        ok, label = isinstance(value, str) and bool(_SEMVER.match(value)), "versao X.Y.Z"
    elif base == "slug":
        ok = isinstance(value, str) and bool(_SLUG.match(value))
    elif base == "url":
        parts = urlparse(value) if isinstance(value, str) else None
        ok = bool(parts and parts.scheme in ("http", "https") and parts.hostname and not parts.username)
        label = "url http(s) sem credenciais"
    elif base == "actor":
        ok, label = isinstance(value, str) and any(is_id(value, p) for p in ACTOR_PREFIXES), "id de ator (usr/agt/sys)"
    elif base == "obj":
        ok = isinstance(value, dict)
    if not ok:
        errors.append(f"{path}: valor invalido, esperado {label}")


def _check_fields(spec, doc, path, errors):
    for name in doc:
        if name not in spec:
            errors.append(f"{path}.{name}: campo desconhecido")
    for name, field in spec.items():
        if name in doc:
            _check(field, doc[name], f"{path}.{name}", errors)
        elif not (isinstance(field, str) and field.endswith("?")):
            errors.append(f"{path}.{name}: obrigatorio")


def find_secrets(value, path="$"):
    """Paths of inline secrets: suspicious key names (except ``*_ref``/``*_id``) or secret-like values."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            safe = key in _SAFE_KEYS or key.endswith(("_ref", "_id", "_ids", "_hash"))
            if not safe and _SECRET_KEY.search(key):
                found.append(f"{path}.{key}")
            else:
                found += find_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += find_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        found.append(path)
    return found


def _rule_address(doc):
    linked = doc["link_state"] == "vinculado"
    if linked != (doc["contact_id"] is not None):
        return ["$.contact_id: obrigatorio se vinculado e proibido se observado"]
    value = doc["address"]
    if doc["channel"] == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return ["$.address: email invalido"]
    if doc["channel"] == "telefone" and not re.fullmatch(r"\+?[0-9 ()-]{6,20}", value):
        return ["$.address: telefone invalido"]
    return []


def _rule_grant(doc):
    if doc["resource_id"] is not None and not is_id(doc["resource_id"], PREFIXES[doc["resource_kind"]]):
        return ["$.resource_id: deve ser um id opaco do tipo do recurso ou nulo (todos da empresa)"]
    return [] if doc["actions"] else ["$.actions: informe ao menos uma acao"]


_WHEN_KEYS = {"task", "complexity"}


def _rule_policy(doc):
    errors, models = [], doc["models"]
    if not models or len(models) > 10:
        return ["$.models: informe de 1 a 10 modelos"]
    for name, model in models.items():
        if not (_SLUG.match(str(name)) and isinstance(model, dict)):
            errors.append(f"$.models.{str(name)[:30]}: entrada invalida")
            continue
        if model.get("provider") not in LLM_PROVIDERS or not isinstance(model.get("model"), str)                 or not model["model"].strip() or set(model) - {"provider", "model", "base_url", "connection_id"}:
            errors.append(f"$.models.{name}: provider (ollama/externo) e model sao obrigatorios")
            continue
        parts = urlparse(model.get("base_url") or "")
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            errors.append(f"$.models.{name}.base_url: url http(s) sem credenciais")
        if model["provider"] == "externo" and not is_id(model.get("connection_id"), "con"):
            errors.append(f"$.models.{name}.connection_id: provedor externo exige uma conexao com credencial no cofre")
        if model["provider"] == "ollama" and "connection_id" in model:
            errors.append(f"$.models.{name}: ollama local nao usa conexao")
    for name in [doc["default"], *doc["fallbacks"]]:
        if name not in models:
            errors.append(f"$: modelo desconhecido na politica: {name}")
    for index, rule in enumerate(doc["rules"]):
        when = rule.get("when") if isinstance(rule, dict) else None
        if not isinstance(rule, dict) or set(rule) != {"when", "use"} or rule["use"] not in models                 or not isinstance(when, dict) or not when or set(when) - _WHEN_KEYS                 or not all(isinstance(v, list) and v and all(isinstance(i, str) and _SLUG.match(i) for i in v)
                           for v in when.values()):
            errors.append(f"$.rules[{index}]: use {{'when': {{task|complexity: [..]}}, 'use': modelo}}")
    limits = doc["limits"]
    if set(limits) - {"max_output_tokens"} or not all(
            isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 32768 for v in limits.values()):
        errors.append("$.limits: somente max_output_tokens entre 1 e 32768")
    if doc["external_enabled"] and not doc["external_acknowledged_at"]:
        errors.append("$.external_enabled: exige external_acknowledged_at (aviso de envio a terceiros)")
    return errors


def _rule_catalog(doc):
    if doc["transport"] == "stdio":
        return [] if doc["launcher"] and doc["url"] is None else ["$.launcher: stdio exige launcher e proibe url"]
    if doc["transport"] == "local":
        return [] if doc["url"] is None and doc["launcher"] is None else ["$.url: local (servidor embutido) nao usa url nem launcher"]
    return [] if doc["url"] and doc["launcher"] is None else ["$.url: http/sse exige url e proibe launcher"]


def _rule_case(doc):
    # One owner once work starts; before that (or when closed without work) the case sits in the triage queue.
    if doc["owner_agent_id"] is None and doc["state"] in (
            "em_execucao", "aguardando_cliente", "aguardando_aprovacao", "aguardando_subtarefa", "pausada", "resolvida"):
        return ["$.owner_agent_id: obrigatorio apos a triagem (um unico responsavel)"]
    return []


def _rule_entry(doc):
    external = doc["entry_type"] == "mensagem_externa"
    if external != (doc["direction"] is not None and doc["channel"] is not None):
        return ["$: mensagem_externa exige direction e channel; notas e resumos nao os possuem"]
    return []


def _rule_triage(doc):
    errors = []
    if not 0 <= doc["confidence_pct"] <= 100:
        errors.append("$.confidence_pct: use de 0 a 100")
    if doc["decision"] == "esclarecer" and not doc["question"]:
        errors.append("$.question: obrigatoria quando a decisao e pedir esclarecimento")
    if doc["decision"] in ("rotear", "misto") and not doc["capabilities"]:
        errors.append("$.capabilities: obrigatorias para rotear")
    return errors


def _rule_a2a_task(doc):
    errors = []
    has_remote = doc.get("remote_task_id") is not None
    if doc["state"] in ("pendente", "em_execucao", "aguardando", "concluida") and not has_remote:
        errors.append("$.remote_task_id: obrigatorio depois que o agente externo aceitou a tarefa")
    if doc["state"] == "desconhecido" and has_remote:
        errors.append("$.state: resultado desconhecido nao tem tarefa remota")
    if doc.get("result_text") is not None and doc["state"] != "concluida":
        errors.append("$.result_text: so existe quando a tarefa foi concluida")
    return errors


def _rule_gap(doc):
    errors = []
    if doc["case_id"] not in doc["related_case_ids"] or len(doc["related_case_ids"]) > 20:
        errors.append("$.related_case_ids: deve conter a demanda de origem e no maximo 20 demandas")
    if doc["occurrences"] < len(doc["related_case_ids"]) or len(doc["tried"]) > 10:
        errors.append("$.occurrences/tried: contagem inconsistente ou tentativas demais")
    decided = doc["state"] != "aberta"
    if decided != all(doc.get(k) is not None for k in ("decision", "decision_note", "decided_by", "decided_at")):
        errors.append("$.decision: decisao, justificativa, autor e data existem somente depois de decidida")
    if doc["state"] == "atendida" and (doc["decision"] == "nao_atender" or doc.get("fulfilled_at") is None):
        errors.append("$.state: so uma lacuna com skill ou ferramenta pode ser atendida, com data")
    if (doc.get("decision") == "nao_atender") != (doc.get("memory_id") is not None):
        errors.append("$.memory_id: obrigatoria so quando a decisao e nao atender")
    return errors


def _rule_memory(doc):
    errors = []
    if (doc["scope"] == "departamento") != (doc["department_id"] is not None):
        errors.append("$.department_id: obrigatorio so no escopo departamento")
    if (doc["scope"] == "cliente") != (doc["subject"] is not None):
        errors.append("$.subject: obrigatorio so no escopo cliente")
    if doc["memory_kind"] == "fato" and doc["expires_at"] is None:
        errors.append("$.expires_at: fatos datados precisam de validade")
    return errors


_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _rule_financial(doc):
    errors = []
    if doc["currency"] is not None and not _CURRENCY.match(doc["currency"]):
        errors.append("$.currency: use o codigo ISO de 3 letras maiusculas (ex.: BRL)")
    if (doc["amount_cents"] is None) != (doc["currency"] is None):
        errors.append("$.currency: valor e moeda andam juntos (informe os dois ou nenhum)")
    if doc["amount_cents"] is not None and doc["amount_cents"] < 0:
        errors.append("$.amount_cents: nao pode ser negativo")
    return errors


_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _rule_schedule(doc):
    errors, frequency = [], doc["frequency"]
    if doc["opt_in"] is not True:
        errors.append("$.opt_in: a rotina precisa ser ativada explicitamente")
    if frequency == "intervalo":
        if doc["interval_minutes"] is None or not 5 <= doc["interval_minutes"] <= 10080:
            errors.append("$.interval_minutes: de 5 a 10080 minutos")
        if doc["time_of_day"] is not None or doc["weekday"] is not None:
            errors.append("$.time_of_day: intervalo nao usa horario nem dia da semana")
    else:
        if doc["time_of_day"] is None or not _CLOCK.match(doc["time_of_day"]):
            errors.append("$.time_of_day: use HH:MM")
        if doc["interval_minutes"] is not None:
            errors.append("$.interval_minutes: so para frequencia intervalo")
        if (frequency == "semanal") != (doc["weekday"] is not None) or (doc["weekday"] is not None and not 0 <= doc["weekday"] <= 6):
            errors.append("$.weekday: 0 (segunda) a 6, obrigatorio so na frequencia semanal")
    if doc["max_runs_per_day"] > 1440:
        errors.append("$.max_runs_per_day: no maximo 1440")
    return errors


def _rule_asset(doc):
    return [] if _HOSTNAME.match(doc["hostname"]) else ["$.hostname: use um nome DNS minusculo (ex.: exemplo.com.br)"]


_RULES = {"a2a_task": _rule_a2a_task, "capability_gap": _rule_gap, "schedule": _rule_schedule, "financial_record": _rule_financial, "monitored_asset": _rule_asset, "memory_item": _rule_memory, "case": _rule_case, "triage_decision": _rule_triage, "case_entry": _rule_entry, "contact_address": _rule_address, "access_grant": _rule_grant, "mcp_catalog_entry": _rule_catalog,
          "model_policy": _rule_policy}


def validate(kind, doc):
    """Return ``doc`` when valid; raise ContractError otherwise (no mutation, no I/O)."""
    if kind not in SCHEMAS:
        raise ContractError([f"tipo desconhecido: {kind}"], "unknown_kind")
    if not isinstance(doc, dict):
        raise ContractError(["$: esperado objeto"])
    version = doc.get("schema_version")
    if isinstance(version, bool) or version not in SUPPORTED_VERSIONS:
        raise ContractError([f"schema_version nao suportada: {version!r}"], "unsupported_schema_version")
    secrets = [f"{p}: segredo embutido; use credential_ref" for p in find_secrets(doc)]
    if secrets:
        raise ContractError(secrets, "secret")
    errors = []
    if doc.get("kind") != kind:
        errors.append(f"$.kind: esperado '{kind}'")
    _check_fields(SCHEMAS[kind], doc, "$", errors)
    if kind in ("command", "event") and doc.get("case_id") is None \
            and not str(doc.get("type", "")).startswith("config."):
        errors.append("$.case_id: obrigatorio exceto em mensagens config.*")
    if not errors and kind in _RULES:
        errors += _RULES[kind](doc)
    if errors:
        raise ContractError(errors)
    return doc


def _refs(spec, value, found):
    if isinstance(spec, dict):
        for name, field in spec.items():
            if isinstance(value, dict) and name in value:
                _refs(field, value[name], found)
    elif value is not None:
        base, _ = _split(spec)
        if base.startswith("list:"):
            for item in value:
                _refs(base[5:], item, found)
        elif base.startswith("ref:"):
            found.append((base[4:], value))


def validate_bundle(docs):
    """Validate documents and cross references; every reference must resolve inside one tenant."""
    errors, index = [], {}
    for position, doc in enumerate(docs):
        try:
            validate(doc.get("kind") if isinstance(doc, dict) else None, doc)
        except ContractError as error:
            errors += [f"[{position}] {e}" for e in error.errors]
            continue
        if doc["id"] in index:
            errors.append(f"[{position}] id duplicado: {doc['id']}")
        index[doc["id"]] = doc
    for doc in index.values():
        tenant = doc["id"] if doc["kind"] == "tenant" else doc.get("tenant_id")
        found = []
        _refs(SCHEMAS[doc["kind"]], doc, found)
        for target_kind, target_id in found:
            target = index.get(target_id)
            if target is None or target["kind"] != target_kind:
                errors.append(f"{doc['id']}: referencia invalida para {target_kind} {target_id}")
            elif target_kind not in ("skill_definition", "mcp_catalog_entry") and (
                    target["id"] if target_kind == "tenant" else target.get("tenant_id")) != tenant:
                errors.append(f"{doc['id']}: referencia {target_id} pertence a outra empresa")
    if errors:
        raise ContractError(errors)
    return docs


def redact_secrets(value):
    """Copy of ``value`` with secret-looking keys and values replaced by ``***`` (for tool results)."""
    if isinstance(value, dict):
        return {key: "***" if (key not in _SAFE_KEYS and not key.endswith(("_ref", "_id", "_ids", "_hash"))
                               and _SECRET_KEY.search(str(key))) else redact_secrets(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("***", value)
    return value


def _json_type(spec):
    if isinstance(spec, dict):
        return _object_schema(spec)
    base, optional = _split(spec)
    head, _, rest = base.partition(":")
    if head == "list":
        out = {"type": "array", "items": _json_type(rest)}
    elif head in ("id", "ref"):
        prefix = "(cmd|evt)" if rest == "envelope" else PREFIXES[rest]
        out = {"type": "string", "pattern": f"^{prefix}_[0-9a-f]{{32}}$"}
    elif head == "enum":
        out = {"type": "string", "enum": list(ENUMS[rest])}
    elif base == "actor":
        out = {"type": "string", "pattern": f"^({'|'.join(ACTOR_PREFIXES)})_[0-9a-f]{{32}}$"}
    else:
        out = {
            "str": {"type": "string", "minLength": 1, "maxLength": 300},
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
            "int": {"type": "integer"}, "posint": {"type": "integer", "minimum": 1},
            "bool": {"type": "boolean"}, "ts": {"type": "string", "format": "date-time"},
            "sha256": {"type": "string", "pattern": _SHA256.pattern},
            "semver": {"type": "string", "pattern": _SEMVER.pattern},
            "slug": {"type": "string", "pattern": _SLUG.pattern},
            "url": {"type": "string", "format": "uri", "pattern": "^https?://"},
            "obj": {"type": "object"},
        }[base]
    return {"anyOf": [out, {"type": "null"}]} if optional else out


def _object_schema(spec):
    required = [n for n, f in spec.items() if not (isinstance(f, str) and f.endswith("?"))]
    return {"type": "object", "additionalProperties": False, "required": required,
            "properties": {n: _json_type(f) for n, f in spec.items()}}


def export_json_schema(kind):
    """JSON Schema (draft 2020-12) mirroring SCHEMAS; structural only, ``validate`` is authoritative."""
    schema = _object_schema(SCHEMAS[kind])
    schema["properties"]["kind"] = {"const": kind}
    schema["properties"]["schema_version"] = {"const": SCHEMA_VERSION}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": kind, **schema}
