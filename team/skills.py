"""Declarative skill packages, operator review and explicit grants.

A package is data: one ``skill.json`` manifest (plus optional text files it names). Importing never
runs code from the package. A skill describes work and the MCP tools it needs; it grants nothing.
Only a ``skill_grant`` (a fixed skill version for one agent, an authorized connection and listed
operations) lets an agent use it, and updating a manifest never widens an existing grant.
"""
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .access import AccessContext, AccessError, NotFound, OperatorContext, PermissionDenied
from .enums import OPERATIONS, RISK_TAGS
from .ids import new_id
from .repository import IntegrityViolation
from .schema import content_hash

MANIFEST_VERSION = 1
MAX_MANIFEST = 64 * 1024
MAX_FILE = 32 * 1024
MAX_PACKAGE = 256 * 1024
MAX_FILES = 20
TEXT_SUFFIXES = (".md", ".txt", ".json")
FIELDS = {"manifest_version", "id", "version", "description", "instructions", "instructions_file", "inputs",
          "outputs", "required_tools", "risks", "settings", "fictitious"}
COMMAND_KEYS = {"command", "commands", "script", "scripts", "exec", "run", "entrypoint", "shell", "cmd", "install",
                "postinstall", "preinstall", "hooks", "bin", "main"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TOOL = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\.[a-z0-9][a-z0-9._-]{0,40}$")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SkillRejected(ValueError):
    """The package or request is refused; the message is safe to show."""


class SkillDenied(AccessError):
    """No usable grant. One message for every reason, so callers learn nothing about other grants."""

    def __init__(self):
        super().__init__("skill sem concessao valida")


@dataclass(frozen=True)
class SkillReview:
    """What the operator reviews before installing: the exact content, permissions and risks."""
    skill_key: str
    skill_version: str
    content_hash: str
    description: str
    permissions: list
    risks: list
    settings: list
    fictitious: bool
    previous_version: str
    added_permissions: list
    definition: dict


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _version_key(version):
    return tuple(int(part) for part in _SEMVER.match(version).groups())


def _text_file(base: Path, name):
    if not isinstance(name, str) or not _FILENAME.match(name) or not name.endswith((".md", ".txt")):
        raise SkillRejected("instructions_file deve ser um nome simples .md ou .txt dentro do pacote.")
    path = base / name
    resolved = Path(os.path.realpath(path))
    if path.is_symlink() or not path.is_file() or Path(os.path.realpath(base)) not in resolved.parents:
        raise SkillRejected("instructions_file nao e um arquivo regular do pacote.")
    if path.stat().st_size > MAX_FILE:
        raise SkillRejected("instructions_file excede o limite de tamanho.")
    return path.read_text(encoding="utf-8")


def _scan_package(path: Path):
    """Return (directory, manifest bytes); refuse anything that is not plain data."""
    if path.is_symlink():
        raise SkillRejected("Atalhos nao sao aceitos.")
    if path.is_file():
        base, manifest = path.parent, path
        entries = [path]
    elif path.is_dir():
        base, manifest = path, path / "skill.json"
        entries = sorted(path.iterdir())
    else:
        raise SkillRejected("Pacote nao encontrado.")
    if len(entries) > MAX_FILES:
        raise SkillRejected("Pacote com arquivos demais.")
    total = 0
    for entry in entries:
        if entry.is_symlink() or entry.is_dir() or not entry.is_file():
            raise SkillRejected("Pacote contem diretorio, atalho ou item que nao e arquivo regular.")
        if not _FILENAME.match(entry.name) or entry.suffix.lower() not in TEXT_SUFFIXES:
            raise SkillRejected(f"Pacote contem arquivo nao permitido: {entry.name[:40]}. So texto (.json, .md, .txt).")
        total += entry.stat().st_size
    if total > MAX_PACKAGE:
        raise SkillRejected("Pacote excede o limite de tamanho.")
    if not manifest.is_file() or manifest.stat().st_size > MAX_MANIFEST:
        raise SkillRejected("Manifesto skill.json ausente ou grande demais.")
    return base, manifest.read_bytes()


def parse_manifest(base: Path, raw: bytes) -> dict:
    """Validate a manifest and return it with instructions resolved; never executes anything."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise SkillRejected("Manifesto nao e um JSON valido.") from None
    if not isinstance(data, dict):
        raise SkillRejected("Manifesto deve ser um objeto JSON.")
    if COMMAND_KEYS & {str(key).lower() for key in data}:
        raise SkillRejected("Pacote com comando ou script: skills sao declarativas e nao executam codigo.")
    if set(data) - FIELDS:
        raise SkillRejected("Campos desconhecidos: " + ", ".join(sorted(set(data) - FIELDS))[:120])
    if data.get("manifest_version") != MANIFEST_VERSION or isinstance(data.get("manifest_version"), bool):
        raise SkillRejected("manifest_version nao suportada.")
    if not (isinstance(data.get("id"), str) and _SLUG.match(data["id"])):
        raise SkillRejected("id invalido (use letras minusculas, numeros, ponto, hifen).")
    if not (isinstance(data.get("version"), str) and _SEMVER.match(data["version"])):
        raise SkillRejected("version deve ser X.Y.Z.")
    description = data.get("description")
    if not (isinstance(description, str) and 1 <= len(description) <= 1000):
        raise SkillRejected("description obrigatoria (ate 1000 caracteres).")
    if ("instructions" in data) == ("instructions_file" in data):
        raise SkillRejected("Informe exatamente um entre instructions e instructions_file.")
    instructions = data["instructions"] if "instructions" in data else _text_file(base, data["instructions_file"])
    if not (isinstance(instructions, str) and 1 <= len(instructions.strip()) <= 8000):
        raise SkillRejected("Instrucoes obrigatorias (ate 8000 caracteres).")
    lists = {}
    for field in ("inputs", "outputs", "settings"):
        value = data.get(field, [])
        if not (isinstance(value, list) and len(value) <= 20 and all(isinstance(i, str) and _SLUG.match(i) for i in value)):
            raise SkillRejected(f"{field} deve ser uma lista de identificadores simples.")
        lists[field] = value
    risks = data.get("risks", [])
    if not (isinstance(risks, list) and set(risks) <= set(RISK_TAGS) and all(isinstance(r, str) for r in risks)):
        raise SkillRejected("risks contem valor desconhecido. Validos: " + ", ".join(RISK_TAGS))
    tools = {}
    required = data.get("required_tools", [])
    if not (isinstance(required, list) and len(required) <= 20):
        raise SkillRejected("required_tools deve ser uma lista (ate 20).")
    for item in required:
        if not (isinstance(item, dict) and set(item) == {"tool", "operations"} and isinstance(item["tool"], str)
                and _TOOL.match(item["tool"]) and isinstance(item["operations"], list) and item["operations"]
                and all(op in OPERATIONS for op in item["operations"])):
            raise SkillRejected("Cada ferramenta requerida precisa de tool (servidor.ferramenta) e operations validas.")
        if item["tool"] in tools:
            raise SkillRejected("Ferramenta requerida repetida.")
        tools[item["tool"]] = sorted(set(item["operations"]))
    fictitious = data.get("fictitious", False)
    if not isinstance(fictitious, bool):
        raise SkillRejected("fictitious deve ser verdadeiro ou falso.")
    return {"id": data["id"], "version": data["version"], "description": description,
            "instructions": instructions.strip(), "inputs": lists["inputs"], "outputs": lists["outputs"],
            "settings": lists["settings"], "risks": sorted(set(risks)), "tools": tools, "fictitious": fictitious}


def _pairs(tools):
    return {(tool, op) for tool, ops in tools.items() for op in ops}


class SkillService:
    def __init__(self, repository, access, agents=None, capability_of=None):
        self.repository, self.access, self.agents = repository, access, agents
        self.capability_of = capability_of  # catalog key -> capability name; without it a skill names a catalog key only

    def serves(self, entry, server):
        """Whether the ``server`` part of a skill tool names this approved catalog entry (#105).

        A skill may name the entry's own key (``imap-somente-leitura.listar-mensagens``) or the capability its driver
        serves (``email-leitura.listar-mensagens``). The second form lets a connection swap drivers without a new skill;
        the connection, never the model, decides which driver answers."""
        if server == entry["catalog_key"]:
            return True
        capability = self.capability_of(entry["catalog_key"]) if self.capability_of else None
        return capability is not None and server == capability

    def _operator_only(self, operator):
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.access.operator_id:
            raise PermissionDenied()

    def _operator_ctx(self, ctx):
        if not self.access.is_operator(ctx, "admin"):
            raise PermissionDenied()

    def _installed(self, key):
        found = [s for s in self.repository.list_global("skill_definition", limit=1000) if s["skill_key"] == key]
        return sorted(found, key=lambda s: _version_key(s["skill_version"]))

    # -- review and install ----------------------------------------------------------------------------
    def review_package(self, path) -> SkillReview:
        """Validate a local package and describe what installing it would permit. Changes nothing."""
        base, raw = _scan_package(Path(path))
        manifest = parse_manifest(base, raw)
        installed = self._installed(manifest["id"])
        previous = installed[-1] if installed else None
        before = _pairs(previous.get("tool_operations") or {}) if previous else set()
        added = sorted(_pairs(manifest["tools"]) - before)
        definition = {
            "skill_key": manifest["id"], "skill_version": manifest["version"], "instructions": manifest["instructions"],
            "required_tools": sorted(manifest["tools"]),
            "operations": sorted({op for ops in manifest["tools"].values() for op in ops}) or ["read"],
            "description": manifest["description"], "inputs": manifest["inputs"], "outputs": manifest["outputs"],
            "risks": manifest["risks"], "settings": manifest["settings"], "tool_operations": manifest["tools"],
            "fictitious": manifest["fictitious"]}
        permissions = [{"tool": tool, "operations": ops} for tool, ops in sorted(manifest["tools"].items())]
        return SkillReview(manifest["id"], manifest["version"], content_hash(definition), manifest["description"],
                           permissions, manifest["risks"], manifest["settings"], manifest["fictitious"],
                           previous["skill_version"] if previous else "",
                           [{"tool": t, "operation": o} for t, o in added], definition)

    def install(self, operator, path, approved_hash, accept_new_permissions=False):
        """Install a reviewed package. ``approved_hash`` must be the hash the operator saw in the review."""
        self._operator_only(operator)
        review = self.review_package(path)
        if approved_hash != review.content_hash:
            raise SkillRejected("O conteudo do pacote difere do que foi revisado.")
        installed = self._installed(review.skill_key)
        same = [s for s in installed if s["skill_version"] == review.skill_version]
        if same:
            if same[0]["content_hash"] == review.content_hash:
                return same[0]
            raise SkillRejected("Esta versao ja esta instalada com outro conteudo; publique uma nova versao.")
        if installed and _version_key(review.skill_version) < _version_key(installed[-1]["skill_version"]):
            raise SkillRejected("A versao deve ser maior que a instalada.")
        if installed and review.added_permissions and not accept_new_permissions:
            listed = ", ".join(f"{p['tool']}:{p['operation']}" for p in review.added_permissions)[:300]
            raise SkillRejected("A nova versao pede permissoes adicionais e exige confirmacao: " + listed)
        now = _now()
        doc = {"kind": "skill_definition", "schema_version": 1, "id": new_id("skill_definition"), "revision": 1,
               "created_at": now, "updated_at": now, "content_hash": review.content_hash, "status": "ativo",
               **review.definition}
        try:
            return self.repository.save_global(doc)
        except IntegrityViolation:
            raise SkillRejected("Skill ja instalada.") from None

    def set_skill_status(self, operator, skill_id, status):
        """Deactivating a skill blocks new uses by every grant of that version."""
        self._operator_only(operator)
        current = self.repository.get_global("skill_definition", skill_id)
        if current is None:
            raise NotFound()
        return self.repository.save_global({**current, "status": status, "revision": current["revision"] + 1,
                                            "updated_at": _now()})

    def list_skills(self, ctx):
        if not isinstance(ctx, (AccessContext, OperatorContext)):
            raise PermissionDenied()
        return self.repository.list_global("skill_definition", limit=1000)

    # -- grants ------------------------------------------------------------------------------------------
    def _check_connection(self, session, skill, connection_id):
        needs = skill.get("tool_operations") or {}
        if connection_id is None:
            if needs:
                raise SkillRejected("Esta skill usa ferramentas MCP: informe a conexao autorizada.")
            return
        connection = session.get("connection", connection_id) if isinstance(connection_id, str) else None
        if connection is None:
            raise NotFound()
        if connection["status"] != "ativo":
            raise SkillRejected("Conexao inativa.")
        if not needs:
            return
        entry = self.repository.get_global("mcp_catalog_entry", connection.get("catalog_entry_id") or "")
        if connection["connection_kind"] != "mcp" or entry is None or entry["status"] != "ativo":
            raise SkillRejected("A conexao nao aponta para um servidor MCP aprovado e ativo.")
        for tool in needs:
            server, _, name = tool.partition(".")
            if not self.serves(entry, server) or name not in entry["tools"]:
                raise SkillRejected(f"O servidor aprovado nao oferece a ferramenta requerida: {tool}.")

    def grant_skill(self, ctx, *, agent_id, skill_id, operations, connection_id=None, resource=None,
                    expires_at=None, attach=False):
        self._operator_ctx(ctx)
        skill = self.repository.get_global("skill_definition", skill_id) if isinstance(skill_id, str) else None
        if skill is None:
            raise NotFound()
        if skill["status"] != "ativo":
            raise SkillRejected("Skill desativada.")
        if not (isinstance(operations, (list, tuple)) and operations and set(operations) <= set(skill["operations"])):
            raise SkillRejected("As operacoes devem existir e estar dentro do que a skill declara.")
        now = _now()
        grant = {"kind": "skill_grant", "schema_version": 1, "id": new_id("skill_grant"), "tenant_id": ctx.tenant_id,
                 "revision": 1, "created_at": now, "updated_at": now, "agent_id": agent_id, "skill_id": skill_id,
                 "skill_version": skill["skill_version"], "connection_id": connection_id,
                 "operations": sorted(set(operations)), "resource": resource, "expires_at": expires_at,
                 "granted_by": ctx.principal_id, "revoked_at": None}
        with self.repository.transaction(ctx.tenant_id) as session:
            if session.get("agent_definition", agent_id) is None:
                raise NotFound()
            self._check_connection(session, skill, connection_id)
            for existing in session.list("skill_grant", agent_id=agent_id, limit=1000):
                if (existing["skill_id"], existing["connection_id"], existing["resource"]) == \
                        (skill_id, connection_id, resource) and existing.get("revoked_at") is None:
                    raise SkillRejected("Ja existe uma concessao ativa para esta skill, conexao e recurso.")
            session.save(grant)
            session.record_event({
                "kind": "event", "schema_version": 1, "id": new_id("event"), "tenant_id": ctx.tenant_id,
                "type": "config.skill-granted", "case_id": None, "correlation_id": new_id("correlation"),
                "causation_id": None, "actor_id": ctx.principal_id, "revision": 1, "occurred_at": now,
                "payload": {"grant_id": grant["id"], "agent_id": agent_id, "skill_key": skill["skill_key"],
                            "skill_version": skill["skill_version"], "operations": grant["operations"]}}, topic="config")
        if attach:
            if self.agents is None:
                raise SkillRejected("Servico de agentes nao configurado para anexar.")
            agent = self.agents.get_agent(ctx, agent_id)
            self.agents.update_agent(ctx, agent_id, agent["revision"],
                                     skill_grant_ids=[*agent["skill_grant_ids"], grant["id"]])
        return grant

    def revoke_grant(self, ctx, grant_id):
        """Revoked grants stop authorizing new calls immediately, including for older agent versions."""
        self._operator_ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("skill_grant", grant_id) if isinstance(grant_id, str) else None
            if current is None:
                raise NotFound()
            if current.get("revoked_at"):
                return current
            now = _now()
            return session.save({**current, "revoked_at": now, "revision": current["revision"] + 1, "updated_at": now})

    def list_grants(self, ctx, agent_id=None):
        filters = {"agent_id": agent_id} if agent_id else {}
        with self.repository.transaction(ctx.tenant_id) as session:
            return [g for g in session.list("skill_grant", limit=1000, **filters)
                    if self.access.allowed(ctx, session, "read", "skill_grant", g["id"])]

    # -- use (called by the tool gateway, T07) -------------------------------------------------------------
    def usable_grants(self, ctx, agent_id, agent_version=None):
        """``[(grant, skill)]`` valid right now for an agent (version-pinned): the basis for listing tools."""
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()
        now = datetime.now(timezone.utc)
        found = []
        with self.repository.transaction(ctx.tenant_id) as session:
            agent = session.get("agent_definition", agent_id) if isinstance(agent_id, str) else None
            if agent is None or agent["status"] != "ativo":
                return []
            definition = agent
            if agent_version is not None:
                versions = session.list("agent_profile_version", agent_id=agent_id, agent_version=agent_version, limit=1)
                if not versions:
                    return []
                definition = versions[0]["definition"]
            for grant_id in definition["skill_grant_ids"]:
                grant = session.get("skill_grant", grant_id)
                if grant is None or grant["agent_id"] != agent_id or grant.get("revoked_at"):
                    continue
                if grant["expires_at"] and datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00")) <= now:
                    continue
                skill = self.repository.get_global("skill_definition", grant["skill_id"])
                if skill is None or skill["status"] != "ativo":
                    continue
                if grant["connection_id"] is not None:
                    connection = session.get("connection", grant["connection_id"])
                    if connection is None or connection["status"] != "ativo":
                        continue
                found.append((grant, skill))
        return found

    def authorize_use(self, ctx, agent_id, skill_key, operation, *, connection_id=None, resource=None,
                      agent_version=None):
        """Return the grant that allows this use, or raise ``SkillDenied``. Pins to the run's agent version."""
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()
        now = datetime.now(timezone.utc)
        with self.repository.transaction(ctx.tenant_id) as session:
            agent = session.get("agent_definition", agent_id) if isinstance(agent_id, str) else None
            if agent is None or agent["status"] != "ativo":
                raise SkillDenied()
            definition = agent
            if agent_version is not None:
                found = session.list("agent_profile_version", agent_id=agent_id, agent_version=agent_version, limit=1)
                if not found:
                    raise SkillDenied()
                definition = found[0]["definition"]
            for grant_id in definition["skill_grant_ids"]:
                grant = session.get("skill_grant", grant_id)
                if grant is None or grant["agent_id"] != agent_id or grant.get("revoked_at"):
                    continue
                if grant["expires_at"] and datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00")) <= now:
                    continue
                skill = self.repository.get_global("skill_definition", grant["skill_id"])
                if skill is None or skill["status"] != "ativo" or skill["skill_key"] != skill_key:
                    continue
                if operation not in grant["operations"]:
                    continue
                if grant["resource"] is not None and grant["resource"] != resource:
                    continue
                if grant["connection_id"] is not None:
                    connection = session.get("connection", grant["connection_id"])
                    if connection is None or connection["status"] != "ativo" or \
                            (connection_id is not None and connection_id != grant["connection_id"]):
                        continue
                elif connection_id is not None:
                    continue
                return grant
        raise SkillDenied()
