"""Agent definitions with immutable published versions.

Every change publishes a new version: the definition, an immutable ``agent_profile_version``
snapshot and a ``config.agent-published`` event (with its outbox row) commit in one transaction.
A run records the version it used, so later edits never rewrite history. Instructions in a profile
shape the model's behavior; they never grant tools or approvals (see ``prompting``).
"""
import re
import unicodedata
from datetime import datetime, timezone

from .access import AccessContext, AccessError, NotFound, PermissionDenied
from .ids import new_id
from .repository import IntegrityViolation, RevisionConflict
from .schema import content_hash

EDITABLE = ("department_id", "name", "goals", "personality", "scope", "out_of_scope", "routing_policy",
            "model_policy_ref", "voice_profile_ref", "handoff_target_ids", "skill_grant_ids", "status", "capabilities")
DEFAULT_MODEL_POLICIES = frozenset({"local-primeiro", "legacy-local"})  # built-in; companies add their own (T08)

# Defense in depth, not a guarantee: profile text that tries to cancel product rules or approvals is refused.
_OVERRIDE = [re.compile(pattern) for pattern in (
    r"\bignor\w*\s+(\w+\s+){0,3}(regras|instrucoes|politicas|restricoes)",
    r"\b(ignore|disregard|forget|override)\b\s+(\w+\s+){0,3}(rules|instructions|polic\w+|restrictions)",
    r"\bsem\s+(a\s+)?(aprovacao|autorizacao|confirmacao)\b",
    r"\bwithout\s+(human\s+)?(approval|authori[sz]ation)\b",
    r"\b(desativ|desligu|contorn|burl|pul)\w*\s+(\w+\s+)?(aprovac|regras|politic|autorizac|restric)",
    r"\b(revele|mostre|exiba|reveal|show)\b.{0,30}\b(prompt|instrucoes do sistema|system prompt)\b",
)]


class ProfileRejected(ValueError):
    """The definition is not acceptable; the message is safe to show."""


class AgentUnavailable(AccessError):
    """The agent or the requested version is inactive."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _texts(doc):
    yield doc["name"]
    yield from doc.get("goals") or []
    yield from doc["personality"].values()
    yield doc["scope"]
    yield doc["out_of_scope"]


class AgentService:
    def __init__(self, repository, access, voice_profile_ids=lambda: frozenset(),
                 model_policies=lambda session: DEFAULT_MODEL_POLICIES):
        self.repository, self.access = repository, access
        self.voice_profile_ids, self.model_policies = voice_profile_ids, model_policies

    # -- validation ----------------------------------------------------------------------------------
    def _check(self, session, doc):
        for text in _texts(doc):
            normalized = _plain(text) if isinstance(text, str) else ""
            if any(pattern.search(normalized) for pattern in _OVERRIDE):
                raise ProfileRejected("O perfil contem instrucoes que tentam anular regras do produto ou aprovacoes.")
        if session.get("department", doc["department_id"]) is None:
            raise NotFound()
        if doc["model_policy_ref"] not in self.model_policies(session):
            raise ProfileRejected("Politica de modelo desconhecida.")
        if doc.get("voice_profile_ref") is not None and doc["voice_profile_ref"] not in self.voice_profile_ids():
            raise ProfileRejected("Perfil de voz inexistente.")
        for target in doc["handoff_target_ids"]:
            if target == doc["id"]:
                raise ProfileRejected("Um agente nao pode encaminhar para si mesmo.")
            if session.get("agent_definition", target) is None:
                raise NotFound()
        for name in doc.get("capabilities") or []:
            if not session.list("capability", name=name, limit=1):
                raise ProfileRejected(f"Capacidade desconhecida: {name[:40]}.")
        for grant_id in doc["skill_grant_ids"]:
            grant = session.get("skill_grant", grant_id)
            if grant is None or grant["agent_id"] != doc["id"]:
                raise NotFound()

    def _publish(self, ctx, session, doc, created):
        """Save the definition, its immutable version and the config event in the caller's transaction."""
        self._check(session, doc)
        try:
            session.save(doc)
            snapshot = {"kind": "agent_profile_version", "schema_version": 1, "id": new_id("agent_profile_version"),
                        "tenant_id": ctx.tenant_id, "revision": 1, "created_at": doc["updated_at"],
                        "updated_at": doc["updated_at"], "agent_id": doc["id"], "agent_version": doc["agent_version"],
                        "definition": doc, "content_hash": content_hash(doc), "status": "ativo",
                        "created_by": ctx.principal_id}
            session.save(snapshot)
        except IntegrityViolation:
            raise ProfileRejected("Nao foi possivel publicar: referencia ou versao duplicada.") from None
        session.record_event({
            "kind": "event", "schema_version": 1, "id": new_id("event"), "tenant_id": ctx.tenant_id,
            "type": "config.agent-published", "case_id": None, "correlation_id": new_id("correlation"),
            "causation_id": None, "actor_id": ctx.principal_id, "revision": doc["revision"],
            "occurred_at": doc["updated_at"],
            "payload": {"agent_id": doc["id"], "agent_version": doc["agent_version"],
                        "content_hash": snapshot["content_hash"], "created": created}}, topic="config")
        return snapshot

    def _operator(self, ctx):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator":
            raise PermissionDenied()

    # -- CRUD ------------------------------------------------------------------------------------------
    def create_agent(self, ctx, *, department_id, name, goals, personality, scope, out_of_scope, model_policy_ref,
                     routing_policy="encaminhar", voice_profile_ref=None, handoff_target_ids=(), skill_grant_ids=(),
                     capabilities=()):
        self._operator(ctx)
        now = _now()
        doc = {"kind": "agent_definition", "schema_version": 1, "id": new_id("agent_definition"),
               "tenant_id": ctx.tenant_id, "revision": 1, "created_at": now, "updated_at": now,
               "department_id": department_id, "agent_version": 1, "name": name, "goals": list(goals),
               "personality": personality, "scope": scope, "out_of_scope": out_of_scope,
               "routing_policy": routing_policy, "skill_grant_ids": list(skill_grant_ids),
               "handoff_target_ids": list(handoff_target_ids), "capabilities": list(capabilities),
               "model_policy_ref": model_policy_ref,
               "voice_profile_ref": voice_profile_ref, "status": "ativo"}
        with self.repository.transaction(ctx.tenant_id) as session:
            self._publish(ctx, session, doc, created=True)
            return doc

    def update_agent(self, ctx, agent_id, expected_revision, /, **changes):
        """Any change publishes the next version; earlier versions stay exactly as they were."""
        self._operator(ctx)
        if set(changes) - set(EDITABLE):
            raise ProfileRejected("Campos nao editaveis: " + ", ".join(sorted(set(changes) - set(EDITABLE))))
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("agent_definition", agent_id) if isinstance(agent_id, str) else None
            if current is None:
                raise NotFound()
            if current["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            doc = {**current, **changes, "revision": expected_revision + 1,
                   "agent_version": current["agent_version"] + 1, "updated_at": _now()}
            self._publish(ctx, session, doc, created=False)
            return doc

    def get_agent(self, ctx, agent_id):
        with self.repository.transaction(ctx.tenant_id) as session:
            doc = session.get("agent_definition", agent_id) if isinstance(agent_id, str) else None
            if doc is None or not self.access.allowed(ctx, session, "read", "agent_definition", agent_id):
                raise NotFound()
            return doc

    def list_agents(self, ctx, department_id=None):
        filters = {"department_id": department_id} if department_id else {}
        with self.repository.transaction(ctx.tenant_id) as session:
            return [d for d in session.list("agent_definition", limit=1000, **filters)
                    if self.access.allowed(ctx, session, "read", "agent_definition", d["id"])]

    # -- versions ------------------------------------------------------------------------------------------
    def _version(self, session, ctx, agent_id, version):
        found = session.list("agent_profile_version", agent_id=agent_id, agent_version=version, limit=1) \
            if isinstance(agent_id, str) and isinstance(version, int) else []
        if not found or not self.access.allowed(ctx, session, "read", "agent_profile_version", found[0]["id"]):
            raise NotFound()
        return found[0]

    def get_version(self, ctx, agent_id, version):
        with self.repository.transaction(ctx.tenant_id) as session:
            return self._version(session, ctx, agent_id, version)

    def list_versions(self, ctx, agent_id):
        with self.repository.transaction(ctx.tenant_id) as session:
            versions = session.list("agent_profile_version", agent_id=agent_id, limit=1000) \
                if isinstance(agent_id, str) else []
            return sorted((v for v in versions
                           if self.access.allowed(ctx, session, "read", "agent_profile_version", v["id"])),
                          key=lambda v: v["agent_version"])

    def set_version_status(self, ctx, agent_id, version, status):
        """Disable (or re-enable) one published version for *new* runs; its content never changes."""
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            current = self._version(session, ctx, agent_id, version)
            return session.save({**current, "status": status, "revision": current["revision"] + 1,
                                 "updated_at": _now()})

    # -- runs ----------------------------------------------------------------------------------------------------
    def start_run(self, ctx, agent_id, *, budget, version=None, case_id=None, task_id=None, skill_versions=()):
        """Create an ``agent_run`` pinned to a published, active version of an active agent."""
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            agent = session.get("agent_definition", agent_id) if isinstance(agent_id, str) else None
            if agent is None:
                raise NotFound()
            if agent["status"] != "ativo":
                raise AgentUnavailable("Agente desativado.")
            snapshot = self._version(session, ctx, agent_id, agent["agent_version"] if version is None else version)
            if snapshot["status"] != "ativo":
                raise AgentUnavailable("Esta versao do agente foi desativada.")
            now = _now()
            run = {"kind": "agent_run", "schema_version": 1, "id": new_id("agent_run"), "tenant_id": ctx.tenant_id,
                   "revision": 1, "created_at": now, "updated_at": now, "agent_id": agent_id,
                   "agent_version": snapshot["agent_version"],
                   "department_id": snapshot["definition"]["department_id"], "case_id": case_id, "task_id": task_id,
                   "state": "enfileirada", "budget": budget, "skill_versions": list(skill_versions),
                   "started_at": None, "finished_at": None}
            try:
                return session.save(run)
            except IntegrityViolation:
                raise AgentUnavailable("Nao foi possivel iniciar a execucao com esta versao.") from None
