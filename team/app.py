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
from .onboarding import OnboardingService
from .triage import TriageService
from .knowledge import KnowledgeService
from .outbound import OutboundGateway
from .connections import ConnectionService, LauncherRegistry
from .llm import ModelPolicyService
from .service import TeamService
from .connectors import ConnectorRegistry
from .connectors.catalog import ConnectorCatalog
from .connectors.erp import ERP
from .connectors.consulta_http import register_http_driver
from .connectors.http_routes import ROUTES_FILE, RouteStore
from .connectors.mail import ImapSetup, SmtpSetup, register_email_drivers
from .connectors.setup import ConnectorAudit
from .imap_mcp import ImapAccountRegistry, ImapAccounts, ImapTransport
from .skills import SkillService
from .smtp_transport import SmtpAccountRegistry, SmtpAccounts
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
            repo = persistence.repository(self.root, self.config, self.vault())
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
            data = persistence.data_dir(self.root)
            imap_registry = ImapAccountRegistry(data / "imap-accounts.json", access.operator_id)
            smtp_registry = SmtpAccountRegistry(data / "smtp-accounts.json", access.operator_id)
            registry = ConnectorRegistry()
            registry.register_capability(ERP)  # no ERP driver yet (Bling is issue #104): the panel says so
            register_email_drivers(registry, ImapTransport(imap_registry), ImapSetup(ImapAccounts(connections, imap_registry)),
                                   SmtpSetup(SmtpAccounts(connections, smtp_registry)))
            register_http_driver(registry, RouteStore(self.root / ROUTES_FILE))  # the same routes file the chat reads (#120)
            registry.attach(connections)
            cases = CaseService(repo, access)
            handoffs = HandoffService(repo, access, cases)
            approvals = ApprovalService(repo, access, cases)
            queue = JobQueue(repo, access)
            knowledge = KnowledgeService(repo, access)
            team = TeamService(repo, access)
            triage = TriageService(repo, access, cases, handoffs)
            self._built = SimpleNamespace(
                repo=repo, access=access, operator=access.local_operator(), team=team, triage=triage,
                onboarding=OnboardingService(access, team, agents, triage),
                policies=policies, agents=agents, connections=connections,
                registry=registry, connectors=ConnectorCatalog(registry, connections, ConnectorAudit(data / "connector-audit.jsonl")), skills=SkillService(repo, access, agents, capability_of=registry.capability_of),
                cases=cases, handoffs=handoffs, approvals=approvals, queue=queue,
                knowledge=knowledge, gaps=GapService(repo, access, cases, knowledge), identity=IdentityService(repo, access),
                control=OperatorControl(repo, access, cases, approvals, handoffs, queue),
                outbound=OutboundGateway(repo, access, cases, approvals, connections, {}, handoffs=handoffs))
        return self._built
