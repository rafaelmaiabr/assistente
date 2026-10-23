"""Opt-in switch for persistent cases; the ephemeral chat keeps working either way.

State lives under ``<data root>/team-data`` (outside Git and the packaged resources). Turning the
switch off never deletes the database, and enabling never imports earlier conversations.
"""
import json
import os
from pathlib import Path

from .db import MigrationError
from .access import AccessService
from .agents import AgentService
from .approvals import ApprovalService
from .cases import CaseService
from .gateway import Gateway
from .jobs import JobQueue
from .llm import ModelPolicyService
from .runtime import AgentRuntime
from .connections import ConnectionService, LauncherRegistry
from .ids import is_id, new_id
from .repository import SqliteRepository
from .service import TeamService
from .skills import SkillService
from .vault import LegacyElevenLabsAdapter, WindowsVault

DIRECTORY = "team-data"
DATABASE = "team.sqlite3"
FLAG = "persistence.json"
NOTICE_OFF = ("Atendimento persistente desativado. O chat continua efêmero: o histórico é descartado "
              "ao atualizar a página.")
NOTICE_ON = ("Atendimento persistente ativado. Demandas e configurações da equipe ficam salvas neste "
             "computador. Conversas anteriores não são importadas e o chat efêmero continua disponível.")


def data_dir(root) -> Path:
    return Path(root) / DIRECTORY


def repository(root) -> SqliteRepository:
    return SqliteRepository(data_dir(root) / DATABASE)


def _enabled(root) -> bool:
    try:
        return json.loads((data_dir(root) / FLAG).read_text(encoding="utf-8")).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def status(root, error=None) -> dict:
    enabled = _enabled(root)
    return {"enabled": enabled, "notice": NOTICE_ON if enabled else NOTICE_OFF, "error": error}


def enable(root) -> dict:
    """Migrate the database first; the flag is only set when that succeeds."""
    try:
        repository(root).migrate()
    except MigrationError as error:
        return status(root, error=str(error))
    return _write(root, True)


def disable(root) -> dict:
    return _write(root, False)


def _write(root, enabled) -> dict:
    folder = data_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / (FLAG + ".tmp")
    temp.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
    os.replace(temp, folder / FLAG)
    return status(root)


def operator_id(root) -> str:
    """Stable id of the single trusted local operator, created on first use."""
    path = data_dir(root) / "operator.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("operator_id")
        if is_id(value, "usr"):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    value = new_id("user")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"operator_id": value}), encoding="utf-8")
    os.replace(temp, path)
    return value


def team_service(root) -> TeamService:
    """Service over the (already migrated) local database; the backend creates contexts from it."""
    repo = repository(root)
    return TeamService(repo, AccessService(repo, operator_id(root)))


def connection_service(root, vault=None, legacy_elevenlabs=None) -> ConnectionService:
    """Connections over the Windows vault; the voice key is reused through a read-only adapter."""
    repo = repository(root)
    access = AccessService(repo, operator_id(root))
    if legacy_elevenlabs is None:
        from voice_profiles import WindowsSecrets  # imported lazily: only the backend has it
        legacy_elevenlabs = LegacyElevenLabsAdapter(WindowsSecrets(root))
    launchers = LauncherRegistry(data_dir(root) / "launchers.json", access.operator_id)
    return ConnectionService(repo, access, vault or WindowsVault(root), launchers, legacy_elevenlabs)


def agent_service(root, voice_profile_ids=None) -> AgentService:
    """Agent definitions; voice references are checked against the current voice profiles."""
    repo = repository(root)
    if voice_profile_ids is None:
        def voice_profile_ids():
            from voice_profiles import VoiceProfiles  # imported lazily: only the backend has it
            return {profile["id"] for profile in VoiceProfiles(root, secrets=object()).snapshot()["profiles"]}
    return AgentService(repo, AccessService(repo, operator_id(root)), voice_profile_ids)


def skill_service(root, agents=None) -> SkillService:
    """Skill catalog and grants; ``agents`` lets grants be attached to an agent version."""
    repo = repository(root)
    return SkillService(repo, AccessService(repo, operator_id(root)), agents)


def build_runtime(root, config, transports=None, client_factory=None, vault=None, legacy_elevenlabs=None):
    """Wire repository, access, agents, skills, gateway, policies and the runtime for the local database.

    Independent of the web UI and of any LiveKit session. ``transports`` maps MCP transport kinds to
    ``McpTransport`` objects (none exist for the real MCP package yet, so tools stay unavailable).
    """
    repo = repository(root)
    access = AccessService(repo, operator_id(root))
    policies = ModelPolicyService(repo, access, config)
    voices = None
    if legacy_elevenlabs is not False:
        def voices():
            from voice_profiles import VoiceProfiles  # imported lazily: only the backend has it
            return {profile["id"] for profile in VoiceProfiles(root, secrets=object()).snapshot()["profiles"]}
    agents = AgentService(repo, access, voices or (lambda: frozenset()), policies.known_names)
    skills = SkillService(repo, access, agents)
    connections = connection_service(root, vault=vault, legacy_elevenlabs=legacy_elevenlabs or None)
    approvals = ApprovalService(repo, access, CaseService(repo, access))
    gateway = Gateway(repo, access, skills, connections, transports or {}, approvals=approvals)
    return AgentRuntime(repo, access, agents, skills, gateway, policies, config, client_factory=client_factory,
                        connections=connections)


def job_queue(root) -> JobQueue:
    """Durable job queue on the local database (no worker is started by the application yet)."""
    repo = repository(root)
    return JobQueue(repo, AccessService(repo, operator_id(root)))
