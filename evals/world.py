"""A small deterministic company for the gates: two companies, agents, a fake mail provider and a clock we control.

Nothing here touches the network, a model or a paid account. Built in a temporary folder and discarded.
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService
from team.agents import AgentService
from team.approvals import ApprovalService
from team.cases import CaseService
from team.connections import ConnectionService, LauncherRegistry
from team.control import OperatorControl
from team.finance import FinanceService
from team.handoffs import HandoffService
from team.identity import IdentityService
from team.ids import new_id
from team.knowledge import KnowledgeService
from team.outbound import OutboundGateway, OutboundTransport, ProviderRejected
from team.repository import SqliteRepository
from team.service import TeamService
from team.vault import MemoryVault

PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Suporte",
               out_of_scope="Outros", model_policy_ref="local-primeiro")
CLIENT = "cliente@exemplo.invalid"
BUDGET = {"max_steps": 4, "token_budget": 100, "deadline_seconds": 60}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class FakeProvider(OutboundTransport):
    """Counts what really left the building. ``lose_response`` accepts the message and then never answers."""

    def __init__(self):
        self.sent, self.mode, self.lost, self.delay = [], "ok", [], 0

    async def send(self, account, message, *, dedup_key, secret):
        if self.mode == "reject":
            raise ProviderRejected()
        self.sent.append({"to": message["to"], "dedup": dedup_key})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "lose_response":
            self.lost.append(dedup_key)
            raise TimeoutError()
        return {"provider_id": f"prov-{len(self.sent)}"}

    async def lookup(self, account, dedup_key):
        return None  # the provider offers no way to look a message up: an unknown outcome must stay unknown


class World:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="assistente-evals-")
        base = Path(self.temp.name)
        self.repo = SqliteRepository(base / "team.sqlite3")
        self.repo.migrate()
        self.clock = Clock()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases, clock=self.clock)
        self.approvals = ApprovalService(self.repo, self.access, self.cases, clock=self.clock)
        self.control = OperatorControl(self.repo, self.access, self.cases, self.approvals, self.handoffs)
        self.identity = IdentityService(self.repo, self.access, clock=self.clock)
        self.knowledge = KnowledgeService(self.repo, self.access)
        self.finance = FinanceService(self.repo, self.access, self.knowledge, clock=self.clock)
        self.vault = MemoryVault()
        self.connections = ConnectionService(self.repo, self.access, self.vault, LauncherRegistry(base / "launchers.json", self.access.operator_id))
        self.provider = FakeProvider()
        self.outbound = OutboundGateway(self.repo, self.access, self.cases, self.approvals, self.connections, {"email": self.provider},
                                        handoffs=self.handoffs, clock=self.clock, send_timeout=2)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Suporte", scope="x", status="ativo")
        self.dep2 = self.team.create(self.ca, "department", name="Financeiro", scope="x", status="ativo")
        self.dep_b = self.team.create(self.cb, "department", name="Suporte", scope="x", status="ativo")
        self.suporte = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Suporte", **PROFILE)
        self.financeiro = self.agents.create_agent(self.ca, department_id=self.dep2["id"], name="Financeiro", **PROFILE)
        self.sem_acesso = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Sem acesso", **PROFILE)
        self.agent_b = self.agents.create_agent(self.cb, department_id=self.dep_b["id"], name="Suporte B", **PROFILE)
        for agent in (self.suporte, self.financeiro):
            self.team.create(self.ca, "access_grant", principal_id=agent["id"], resource_kind="case", resource_id=None,
                             actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        self.team.create(self.cb, "access_grant", principal_id=self.agent_b["id"], resource_kind="case", resource_id=None,
                         actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        current = self.agents.get_agent(self.ca, self.suporte["id"])
        self.agents.update_agent(self.ca, self.suporte["id"], current["revision"], handoff_target_ids=[self.financeiro["id"]])
        self.connections.register_connector("smtp-demo")
        self.account = self.connections.create_connection(self.ca, label="SMTP", provider="smtp-demo", connection_kind="smtp")
        self.connections.set_credential(self.ca, self.account["id"], "segredo-sintetico-0123456789")
        contact = self.team.create(self.ca, "contact", display_name="Cliente", status="ativo")
        address = self.team.observe_address(self.ca, "email", CLIENT)
        self.team.link_address(self.ca, address["id"], contact["id"], address["revision"])

    def close(self):
        self.temp.cleanup()

    # -- builders ------------------------------------------------------------------------------------------------------
    def working_case(self, owner=None, tenant="a", department=None):
        ctx, dep, agent = (self.ca, department or self.dep, owner or self.suporte) if tenant == "a" else (self.cb, self.dep_b, self.agent_b)
        case = self.cases.open_case(ctx, department_id=dep["id"], objective="Atender", customer_ref="c1")
        case = self.cases.transition(ctx, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(ctx, case["id"], case["revision"], agent["id"])
        return self.cases.transition(ctx, case["id"], case["revision"], "em_execucao")

    def agent_ctx(self, agent=None, tenant="a"):
        ctx, tenant_id, default = (self.ca, self.a["id"], self.suporte) if tenant == "a" else (self.cb, self.b["id"], self.agent_b)
        run = self.agents.start_run(ctx, (agent or default)["id"], budget=BUDGET)
        return self.access.agent_context(self.op, tenant_id, run["id"])

    def member(self, email, role, departments=None):
        self.identity.invite(self.ca, self.a["id"], email, role, departments=departments)
        token, _ = self.identity.sign_in("sub-" + email, email)
        return self.identity.context(self.identity.resolve(token), self.a["id"])

    def request_approval(self, case, **params):
        approval, _ = self.approvals.request(self.ca, case["id"], action="enviar_email", resource="con_x", recipients=[CLIENT],
                                             params={"assunto": "Oi", **params})
        return approval

    @staticmethod
    def payload(approval):
        n = approval["normalized_action"]
        return {k: n[k] for k in ("action", "tenant_id", "case_id", "resource", "recipients", "amount_cents", "currency", "params")}

    def draft(self, case, **kwargs):
        options = dict(account_id=self.account["id"], to=[CLIENT], body="Estamos verificando o seu site.", subject="Seu chamado")
        options.update(kwargs)
        message, created = self.outbound.create_draft(self.agent_ctx(), case["id"], **options)
        return message, created

    def approved_message(self, case, **kwargs):
        message = self.outbound.submit(self.agent_ctx(), self.draft(case, **kwargs)[0]["id"])
        approval = self.approvals.get(self.ca, message["approval_id"])
        self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])
        return self.outbound.get_message(self.ca, message["id"])

    def draft_as(self, agent, case):
        return self.outbound.create_draft(self.agent_ctx(agent), case["id"], account_id=self.account["id"], to=[CLIENT], body="Ola", subject="x")

    def agent_ctx_run_id(self):
        """A run of company A: its id means nothing to company B."""
        return self.agents.start_run(self.ca, self.suporte["id"], budget=BUDGET)["id"]

    def outsider_identity(self):
        """Someone who belongs only to company A."""
        self.identity.invite(self.ca, self.a["id"], "so-da-a@exemplo.invalid", "admin")
        token, _ = self.identity.sign_in("sub-so-da-a", "so-da-a@exemplo.invalid")
        return self.identity.resolve(token)

    def dispatch(self, message):
        return asyncio.run(self.outbound.dispatch(self.a["id"], message["id"]))
