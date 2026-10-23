"""A complete fictitious company for end-to-end scenarios: every outside system is a fake and the clock is ours.

Nothing here touches the network, a model or a paid account. IMAP and SMTP are real protocol conversations with local
fake servers, the web is a scripted ``FakeNetwork``, the registrar is a person (manual execution) and the triage
classifier is scripted. ``restart()`` rebuilds every service on the same database file to simulate a process restart.
"""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from team.access import AccessService
from team.agents import AgentService
from team.approvals import ApprovalService
from team.attachments import AttachmentStore
from team.cases import CaseService
from team.connections import ConnectionService, LauncherRegistry
from team.control import OperatorControl
from team.diagnostics import Diagnostics
from team.email_ingest import EmailIngest
from team.finance import FinanceService
from team.handoffs import HandoffService
from team.ids import new_id
from team.imap_mcp import ImapAccountRegistry, ImapAccounts
from team.jobs import JobQueue
from team.outbound import OutboundGateway
from team.renewal import RenewalFlow
from team.repository import SqliteRepository
from team.service import TeamService
from team.smtp_transport import (
    FakeEmailTransport, RoutingEmailTransport, SmtpAccountRegistry, SmtpAccounts, SmtpTransport)
from team.triage import TriageService
from team.vault import MemoryVault
from tests.imap_fake import FakeImap
from tests.smtp_fake import FakeSmtp
from tests.test_team_diagnostics import PUBLIC, FakeNetwork, HttpReply, rdap_body

PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")
BUDGET = {"max_steps": 6, "token_budget": 1000, "deadline_seconds": 120}
CUSTOMER = "cliente.um@exemplo.invalid"
OTHER_CUSTOMER = "cliente.dois@exemplo.invalid"
OWN = "atendimento@empresa.invalid"
DOMAIN = "exemplo.com.br"


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)

    def stamp(self, **delta):
        return (self.now + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def mail(subject, sender, body, mid, to=OWN, **headers):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"], msg["Date"] = sender, to, subject, mid, "Mon, 01 Jun 2026 10:00:00 +0000"
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return msg.as_bytes()


class ScriptedClassifier:
    """A model stand-in: answers only what the scenario scripted, otherwise "no capability"."""

    def __init__(self):
        self.calls, self.answers = [], {}

    async def classify(self, message, catalog):
        import json
        from team.triage import parse_answer
        self.calls.append(message)
        return parse_answer(json.dumps(self.answers.get(message, {"capacidades": []})), {c["name"] for c in catalog})


class World(unittest.TestCase):
    """Base class: ``self.w`` style helpers are plain attributes of the test case."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "team.sqlite3"
        self.clock = Clock()
        self.net = FakeNetwork()
        self.imap = FakeImap({"INBOX": {"validity": 11, "messages": {}}}, user=OWN, password="senha-imap-0123")
        self.smtp = FakeSmtp(user=OWN, password="senha-smtp-0123")
        self.addCleanup(self.imap.stop)
        self.addCleanup(self.smtp.stop)
        self.vault = MemoryVault()
        self.build()
        self.populate()

    # -- construction -------------------------------------------------------------------------------------------------
    def build(self):
        """(Re)build every service over the same database file, as a restarted process would."""
        self.repo = SqliteRepository(self.path)
        self.repo.migrate()
        self.access = getattr(self, "access", None) or AccessService(self.repo, new_id("user"))
        self.access.repository = self.repo
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases, clock=self.clock)
        self.approvals = ApprovalService(self.repo, self.access, self.cases, clock=self.clock)
        self.queue = JobQueue(self.repo, self.access)
        self.control = OperatorControl(self.repo, self.access, self.cases, self.approvals, self.handoffs, self.queue)
        self.connections = ConnectionService(self.repo, self.access, self.vault, LauncherRegistry(self.root / "launchers.json", self.access.operator_id))
        scripted = getattr(self, "classifier", None)
        self.classifier = ScriptedClassifier()
        self.classifier.answers = scripted.answers if scripted else {}
        self.triage = TriageService(self.repo, self.access, self.cases, self.handoffs, self.classifier)
        self.finance = FinanceService(self.repo, self.access, clock=self.clock)
        self.imap_registry = ImapAccountRegistry(self.root / "imap-accounts.json", self.access.operator_id)
        self.imap_accounts = ImapAccounts(self.connections, self.imap_registry)
        self.ingest = EmailIngest(self.repo, self.access, self.cases, self.connections, self.imap_registry, clock=self.clock)
        self.smtp_registry = SmtpAccountRegistry(self.root / "smtp-accounts.json", self.access.operator_id)
        self.smtp_accounts = SmtpAccounts(self.connections, self.smtp_registry)
        self.store = AttachmentStore(self.root)
        self.simulated = FakeEmailTransport()
        self.router = RoutingEmailTransport(self.smtp_registry, SmtpTransport(self.smtp_registry, self.store), self.simulated)
        self.outbound = OutboundGateway(self.repo, self.access, self.cases, self.approvals, self.connections, {"email": self.router},
                                        handoffs=self.handoffs, clock=self.clock, send_timeout=5, attachments=self.store,
                                        message_id_domain=self.smtp_registry.message_id_domain)
        self.flow = RenewalFlow(self.repo, self.access, self.cases, self.handoffs, self.approvals,
                                lambda tenant: Diagnostics(self.net, lambda host: host.endswith(DOMAIN), self.clock), self.finance, clock=self.clock)
        self.op = self.access.local_operator()

    def restart(self):
        """A new process on the same files: nothing in memory survives."""
        self.build()
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])

    def populate(self):
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Atendimento", scope="Atendimento", status="ativo")
        self.triage.create_capability(self.ca, name="suporte-tecnico", description="Site, dominio e certificado", keywords=["site", "dominio", "certificado", "fora do ar"])
        self.triage.create_capability(self.ca, name="financeiro", description="Cobrancas e faturas", keywords=["fatura", "boleto", "cobranca", "segunda via"])
        self.secretaria = self.agent("Secretaria", capabilities=[])
        self.suporte = self.agent("Suporte", capabilities=["suporte-tecnico"])
        self.financeiro = self.agent("Financeiro", capabilities=["financeiro"])
        self.allow(self.secretaria, self.suporte, self.financeiro)
        self.allow(self.suporte, self.financeiro, self.secretaria)
        self.allow(self.financeiro, self.suporte)
        for agent in (self.secretaria, self.suporte, self.financeiro):
            self.team.create(self.ca, "access_grant", principal_id=agent["id"], resource_kind="case", resource_id=None,
                             actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        for address in (CUSTOMER,):
            contact = self.team.create(self.ca, "contact", display_name="Cliente Um", status="ativo")
            observed = self.team.observe_address(self.ca, "email", address)
            self.team.link_address(self.ca, observed["id"], contact["id"], observed["revision"])
        # the mailbox: IMAP to read, SMTP to answer, both with passwords only in the vault
        self.imap_conn = self.connections.create_connection(self.ca, label="Caixa", provider="imap", connection_kind="imap")
        self.connections.set_credential(self.ca, self.imap_conn["id"], self.imap.password)
        self.imap_accounts.configure(self.op, self.ca, self.imap_conn["id"], host="127.0.0.1", port=self.imap.port, username=OWN,
                                     folders=["INBOX"], tls=False)
        self.smtp_conn = self.connections.create_connection(self.ca, label="Saida", provider="smtp", connection_kind="smtp")
        self.connections.set_credential(self.ca, self.smtp_conn["id"], self.smtp.password)
        self.smtp_accounts.configure(self.op, self.ca, self.smtp_conn["id"], host="127.0.0.1", port=self.smtp.port, username=OWN,
                                     from_address=OWN, security="plain", from_name="Atendimento", timeout=3)
        self.ingest.bind_mailbox(self.ca, self.imap_conn["id"], "INBOX", self.dep["id"])
        self.net.rdap[DOMAIN] = rdap_body(expires="2026-05-01T00:00:00Z")  # the site's domain is expired in the registry

    # -- helpers ------------------------------------------------------------------------------------------------------
    def agent(self, name, capabilities=()):
        return self.agents.create_agent(self.ca, department_id=self.dep["id"], name=name, capabilities=list(capabilities), **PROFILE)

    def allow(self, source, *targets):
        current = self.agents.get_agent(self.ca, source["id"])
        self.agents.update_agent(self.ca, source["id"], current["revision"], handoff_target_ids=[t["id"] for t in targets])

    def ctx(self, agent):
        run = self.agents.start_run(self.ca, agent["id"], budget=BUDGET)
        return self.access.agent_context(self.op, self.a["id"], run["id"])

    def put_mail(self, uid, raw):
        self.imap.folders["INBOX"]["messages"][uid] = raw

    def receive(self):
        return self.ingest.ingest(self.ca, self.imap_conn["id"], "INBOX")

    def case(self, case_id):
        return self.cases.get_case(self.ca, case_id)

    def all_cases(self):
        return self.cases.list_cases(self.ca, limit=1000)

    def run_triage(self, case, text):
        return asyncio.run(self.triage.triage(self.ca, case["id"], text, secretaria_agent_id=self.secretaria["id"]))

    def pending_transfer(self, case):
        return next(h for h in self.handoffs.list_handoffs(self.ca, case_id=case["id"]) if h["state"] == "proposto")

    def healthy_web(self):
        self.net.dns[DOMAIN] = [PUBLIC]
        self.net.web[(DOMAIN, "https", "/")] = HttpReply(200, {}, b"", {"not_after": "2027-01-01T00:00:00Z"})
        self.net.rdap[DOMAIN] = rdap_body(expires="2027-06-15T00:00:00Z")

    def reply_to_customer(self, owner, case, body, **kwargs):
        """The owner drafts, asks for approval, the operator approves and the gateway sends. Returns the sent message."""
        draft, _ = self.outbound.create_draft(self.ctx(owner), case["id"], account_id=self.smtp_conn["id"], to=[CUSTOMER], body=body,
                                              subject="Re: seu chamado", **kwargs)
        submitted = self.outbound.submit(self.ctx(owner), draft["id"])
        approval = self.approvals.get(self.ca, submitted["approval_id"])
        self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])
        return asyncio.run(self.outbound.dispatch(self.a["id"], draft["id"]))

    def timeline_text(self, case):
        import json
        return json.dumps(self.cases.timeline(self.ca, case["id"]), ensure_ascii=False)
