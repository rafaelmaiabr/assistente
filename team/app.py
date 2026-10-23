"""Service container for the local backend: builds the team services once persistence is switched on.

The web API asks this object for services on each request, so enabling or disabling persistence takes
effect without restarting the server, and tests can inject a fake vault and fake voice profiles.
"""
from types import SimpleNamespace

from . import persistence
from .access import AccessService
from .agents import AgentService
from .approvals import ApprovalService
from .cases import CaseService
from .control import OperatorControl
from .handoffs import HandoffService
from .gaps import GapService
from .identity import IdentityService
from .jobs import JobQueue
from .knowledge import KnowledgeService
from .outbound import OutboundGateway
from .connections import ConnectionService, LauncherRegistry
from .llm import ModelPolicyService
from .service import TeamService
from .skills import SkillService
from .vault import LegacyElevenLabsAdapter, WindowsVault


class TeamApp:
    def __init__(self, root, config, vault=None, legacy_elevenlabs=None, voice_profiles=None):
        self.root, self.config = root, config
        self._vault, self._legacy, self._voices = vault, legacy_elevenlabs, voice_profiles
        self._built = None

    def vault(self):
        """The credential vault in use (the Windows vault unless a test injected another)."""
        return self._vault or WindowsVault(self.root)

    def enabled(self) -> bool:
        return persistence.status(self.root)["enabled"]

    def voice_profiles(self):
        """``[{id, name, provider}]`` from the current voice settings (never the credential)."""
        if self._voices is not None:
            return self._voices()
        from voice_profiles import VoiceProfiles  # imported lazily: only the backend ships it
        data = VoiceProfiles(self.root, secrets=object()).snapshot()
        return [{"id": p["id"], "name": p["name"], "provider": p["provider"]} for p in data["profiles"]]

    def services(self):
        """The team services, or None while persistent attendance is switched off."""
        if not self.enabled():
            self._built = None
            return None
        if self._built is None:
            repo = persistence.repository(self.root)
            repo.migrate()
            access = AccessService(repo, persistence.operator_id(self.root))
            policies = ModelPolicyService(repo, access, self.config)
            agents = AgentService(repo, access, lambda: {p["id"] for p in self.voice_profiles()}, policies.known_names)
            legacy = self._legacy
            if legacy is None:
                try:
                    from voice_profiles import WindowsSecrets
                    legacy = LegacyElevenLabsAdapter(WindowsSecrets(self.root))
                except Exception:
                    legacy = None
            connections = ConnectionService(
                repo, access, self._vault or WindowsVault(self.root),
                LauncherRegistry(persistence.data_dir(self.root) / "launchers.json", access.operator_id), legacy)
            cases = CaseService(repo, access)
            handoffs = HandoffService(repo, access, cases)
            approvals = ApprovalService(repo, access, cases)
            queue = JobQueue(repo, access)
            knowledge = KnowledgeService(repo, access)
            self._built = SimpleNamespace(
                repo=repo, access=access, operator=access.local_operator(), team=TeamService(repo, access),
                policies=policies, agents=agents, connections=connections, skills=SkillService(repo, access, agents),
                cases=cases, handoffs=handoffs, approvals=approvals, queue=queue,
                knowledge=knowledge, gaps=GapService(repo, access, cases, knowledge), identity=IdentityService(repo, access),
                control=OperatorControl(repo, access, cases, approvals, handoffs, queue),
                outbound=OutboundGateway(repo, access, cases, approvals, connections, {}, handoffs=handoffs))
        return self._built
