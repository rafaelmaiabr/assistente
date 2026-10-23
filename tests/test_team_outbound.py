import asyncio
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.approvals import ApprovalService
from team.cases import CaseService
from team.connections import ConnectionService, LauncherRegistry
from team.db import connect
from team.handoffs import HandoffService
from team.ids import new_id
from team.jobs import JobQueue
from team.outbound import Busy, OutboundError, OutboundGateway, OutboundTransport, ProviderRejected, outbound_handler
from team.repository import SqliteRepository
from team.schema import ContractError
from team.service import TeamService
from team.vault import MemoryVault
from team.worker import Worker

PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Suporte",
               out_of_scope="Outros", model_policy_ref="local-primeiro")
CLIENT = "cliente@exemplo.com"
SECRET = "smtp-senha-0123456789"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class FakeProvider(OutboundTransport):
    def __init__(self):
        self.sent, self.mode, self.delay, self.lost = [], "ok", 0, []
        self.concurrent = self.peak = 0

    async def send(self, account, message, *, dedup_key, secret):
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        try:
            if self.mode == "reject":
                raise ProviderRejected()
            self.sent.append({"to": message["to"], "dedup": dedup_key, "secret": secret, "body": message["body"]})
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.mode == "lose_response":  # the provider accepted it, but we never hear back
                self.lost.append(dedup_key)
                raise TimeoutError()
            return {"provider_id": f"prov-{len(self.sent)}"}
        finally:
            self.concurrent -= 1

    async def lookup(self, account, dedup_key):
        return "prov-recuperado" if dedup_key in self.lost else None


class OutboundCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "team.sqlite3"
        self.repo = SqliteRepository(self.path)
        self.repo.migrate()
        self.clock = Clock()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases, clock=self.clock)
        self.approvals = ApprovalService(self.repo, self.access, self.cases, clock=self.clock)
        self.vault = MemoryVault()
        self.connections = ConnectionService(self.repo, self.access, self.vault,
                                             LauncherRegistry(Path(self.temp.name) / "launchers.json", self.access.operator_id))
        self.provider = FakeProvider()
        self.outbound = OutboundGateway(self.repo, self.access, self.cases, self.approvals, self.connections,
                                        {"email": self.provider}, handoffs=self.handoffs, clock=self.clock, send_timeout=2)
        self.queue = JobQueue(self.repo, self.access)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Atendimento", scope="x", status="ativo")
        self.suporte = self.agent("Suporte")
        self.financeiro = self.agent("Financeiro")
        for agent in (self.suporte, self.financeiro):
            self.team.create(self.ca, "access_grant", principal_id=agent["id"], resource_kind="case", resource_id=None,
                             actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        current = self.agents.get_agent(self.ca, self.suporte["id"])
        self.agents.update_agent(self.ca, self.suporte["id"], current["revision"], handoff_target_ids=[self.financeiro["id"]])
        self.connections.register_connector("smtp-demo")
        self.account = self.connections.create_connection(self.ca, label="SMTP", provider="smtp-demo", connection_kind="smtp")
        self.connections.set_credential(self.ca, self.account["id"], SECRET)
        contact = self.team.create(self.ca, "contact", display_name="Cliente", status="ativo")
        address = self.team.observe_address(self.ca, "email", CLIENT)
        self.team.link_address(self.ca, address["id"], contact["id"], address["revision"])
        self.case = self.working_case()

    def agent(self, name):
        return self.agents.create_agent(self.ca, department_id=self.dep["id"], name=name, **PROFILE)

    def working_case(self, owner=None):
        case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Atender", customer_ref="c1")
        case = self.cases.transition(self.ca, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], (owner or self.suporte)["id"])
        return self.cases.transition(self.ca, case["id"], case["revision"], "em_execucao")

    def agent_ctx(self, agent=None):
        run = self.agents.start_run(self.ca, (agent or self.suporte)["id"],
                                    budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 60})
        return self.access.agent_context(self.op, self.a["id"], run["id"])

    def draft(self, ctx=None, case=None, **kwargs):
        options = dict(account_id=self.account["id"], to=[CLIENT], body="Estamos verificando o seu site.", subject="Seu chamado")
        options.update(kwargs)
        message, _ = self.outbound.create_draft(ctx or self.agent_ctx(), (case or self.case)["id"], **options)
        return message

    def approved(self, **kwargs):
        message = self.outbound.submit(self.agent_ctx(), self.draft(**kwargs)["id"])
        approval = self.approvals.get(self.ca, message["approval_id"])
        self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])
        return self.outbound.get_message(self.ca, message["id"])

    def send(self, message):
        return asyncio.run(self.outbound.dispatch(self.a["id"], message["id"]))

    def case_now(self, case=None):
        return self.cases.get_case(self.ca, (case or self.case)["id"])

    def transfer_to_financeiro(self, case=None):
        case = case or self.case
        proposal, _ = self.handoffs.propose_transfer(self.agent_ctx(), case["id"], self.financeiro["id"],
                                                     reason="Passar para o financeiro", summary="Resumo")
        self.handoffs.accept(self.agent_ctx(self.financeiro), proposal["id"])


class DraftTests(OutboundCase):
    def test_a_draft_is_data_and_nothing_reaches_the_provider(self):
        message = self.draft()
        self.assertEqual((message["state"], message["owner_agent_id"], message["owner_version"], message["attempts"]),
                         ("rascunho", self.suporte["id"], 1, 0))
        self.assertEqual(message["to"], [CLIENT])
        edited = self.outbound.update_draft(self.agent_ctx(), message["id"], 1, body="Texto novo", to=["CLIENTE@exemplo.com "])
        self.assertNotEqual(edited["draft_hash"], message["draft_hash"])
        self.assertEqual(self.provider.sent, [])
        again, created = self.outbound.create_draft(self.agent_ctx(), self.case["id"], account_id=self.account["id"],
                                                    to=[CLIENT], body="x", dedup_key=message["dedup_key"])
        self.assertEqual((created, again["id"]), (False, message["id"]))

    def test_validation_and_no_path_from_notes_or_summaries_to_a_reply(self):
        ctx = self.agent_ctx()
        for bad in (dict(to=[]), dict(to=["sem-arroba"]), dict(to=[f"u{i}@exemplo.com" for i in range(6)]),
                    dict(body="   "), dict(body="<think>so raciocinio</think>"), dict(body="x" * 9000),
                    dict(channel="sms"), dict(account_id=new_id("connection"))):
            with self.assertRaises((OutboundError, NotFound, Exception), msg=str(bad)[:50]):
                self.outbound.create_draft(ctx, self.case["id"], **{"account_id": self.account["id"], "to": [CLIENT],
                                                                    "body": "ok", **bad})
        with self.assertRaises(ContractError):
            self.draft(body="use a chave sk-abcdefghijklmnopqrstuvwx para entrar")
        self.cases.add_note(self.ca, self.case["id"], "Nota interna: o cliente esta nervoso")
        self.cases.add_summary(self.ca, self.case["id"], "Resumo: enviar desculpas e reembolso")
        self.assertEqual(self.outbound.list_messages(self.ca), [])  # nothing is turned into a message automatically
        inactive = self.connections.create_connection(self.ca, label="Fora", provider="smtp-demo", connection_kind="smtp")
        self.connections.update_connection(self.ca, inactive["id"], 1, status="inativo")
        with self.assertRaisesRegex(OutboundError, "inativa"):
            self.draft(account_id=inactive["id"])
        mcp_account = self.connections.create_connection(self.ca, label="Outro", provider="x", connection_kind="http")
        with self.assertRaisesRegex(OutboundError, "incompativel"):
            self.draft(account_id=mcp_account["id"])

    def test_only_the_owner_drafts_and_other_companies_see_nothing(self):
        with self.assertRaises(NotFound):
            self.draft(ctx=self.agent_ctx(self.financeiro))
        message = self.draft()
        with self.assertRaises(NotFound):
            self.outbound.get_message(self.agent_ctx(self.financeiro), message["id"])
        with self.assertRaises(NotFound):
            self.outbound.get_message(self.cb, message["id"])
        self.assertEqual(self.outbound.list_messages(self.cb), [])
        self.assertEqual(self.outbound.list_messages(self.agent_ctx(self.financeiro)), [])
        queue_case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Sem dono")
        with self.assertRaisesRegex(OutboundError, "responsavel"):
            self.draft(ctx=self.ca, case=queue_case)


class ApprovalAndPolicyTests(OutboundCase):
    def test_recipients_must_be_linked_contacts_or_allowed_domains(self):
        stranger = self.draft(to=["estranho@outro.example"])
        with self.assertRaisesRegex(OutboundError, "fora da politica"):
            self.outbound.submit(self.agent_ctx(), stranger["id"])
        observed = self.team.observe_address(self.ca, "email", "remetente@visto.example")  # seen, never linked
        with self.assertRaisesRegex(OutboundError, "fora da politica"):
            self.outbound.submit(self.agent_ctx(), self.draft(to=["remetente@visto.example"])["id"])
        self.assertEqual(observed["link_state"], "observado")
        revision = self.connections.get_connection(self.ca, self.account["id"])["revision"]
        self.connections.update_connection(self.ca, self.account["id"], revision, label="SMTP")  # no policy change yet
        with self.repo.transaction(self.a["id"]) as session:  # allow the domain on this account
            account = session.get("connection", self.account["id"])
            session.save({**account, "allowed_recipient_domains": ["outro.example"], "revision": account["revision"] + 1})
        self.assertEqual(self.outbound.submit(self.agent_ctx(), stranger["id"])["state"], "aguardando_aprovacao")

    def test_nothing_is_sent_before_the_operator_approves(self):
        message = self.outbound.submit(self.agent_ctx(), self.draft()["id"])
        approval = self.approvals.get(self.ca, message["approval_id"])
        self.assertEqual((approval["action"], approval["recipient"], approval["resource"]),
                         ("enviar_mensagem", {"to": [CLIENT]}, self.account["id"]))
        self.assertEqual(self.outbound.submit(self.agent_ctx(), message["id"])["approval_id"], approval["id"])  # idempotent
        self.assertEqual(self.send(message)["state"], "aguardando_aprovacao")
        self.assertEqual(self.provider.sent, [])
        with self.assertRaises(OutboundError):
            self.send(self.draft(body="outro rascunho"))  # never submitted

    def test_full_path_sends_once_records_the_provider_id_and_the_history(self):
        message = self.approved()
        sent = self.send(message)
        self.assertEqual((sent["state"], sent["provider_id"], sent["attempts"]), ("enviada", "prov-1", 1))
        self.assertEqual(self.provider.sent[0]["dedup"], message["dedup_key"])
        self.assertEqual(self.provider.sent[0]["secret"], SECRET)  # given to the connector, never stored
        entries = [i["item"] for i in self.cases.timeline(self.ca, self.case["id"]) if i["tipo"] == "entrada"]
        outgoing = [e for e in entries if e["entry_type"] == "mensagem_externa"]
        self.assertEqual((outgoing[0]["direction"], outgoing[0]["author"]), ("saida", self.suporte["id"]))
        approval = self.approvals.get(self.ca, message["approval_id"])
        self.assertEqual((approval["state"], approval["effect_outcome"]), ("consumida", "confirmado"))
        self.assertEqual(self.send(message)["state"], "enviada")  # repeating does not resend
        self.assertEqual(len(self.provider.sent), 1)

    def test_a_changed_message_or_a_withdrawn_approval_never_goes_out(self):
        message = self.approved()
        with self.repo.transaction(self.a["id"]) as session:  # someone rewrites the body behind the gateway's back
            stored = session.get("outbound_message", message["id"])
            session.save({**stored, "body": "Texto trocado depois da aprovacao", "revision": stored["revision"] + 1})
        blocked = self.send(message)
        self.assertEqual((blocked["state"], blocked["last_error"]), ("bloqueada", "rascunho_alterado"))
        self.assertEqual(self.provider.sent, [])
        second = self.outbound.submit(self.agent_ctx(), self.draft(body="Outro texto")["id"])
        back = self.outbound.withdraw(self.agent_ctx(), second["id"])
        self.assertEqual((back["state"], back["approval_id"]), ("rascunho", None))
        self.assertEqual(self.approvals.get(self.ca, second["approval_id"])["state"], "cancelada")
        edited = self.outbound.update_draft(self.agent_ctx(), back["id"], back["revision"], body="Texto revisado")
        resubmitted = self.outbound.submit(self.agent_ctx(), edited["id"])
        self.assertNotEqual(resubmitted["approval_id"], second["approval_id"])  # a new approval for the new text
        with self.assertRaisesRegex(OutboundError, "rascunhos"):
            self.outbound.update_draft(self.agent_ctx(), resubmitted["id"], resubmitted["revision"], body="mudei de novo")

    def test_denied_expired_or_revoked_approvals_block(self):
        for how in ("deny", "revoke", "expire"):
            message = self.outbound.submit(self.agent_ctx(), self.draft(body=f"Mensagem {how}")["id"])
            approval = self.approvals.get(self.ca, message["approval_id"])
            if how == "deny":
                self.approvals.deny(self.ca, approval["id"], "nao")
            elif how == "revoke":
                self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])
                self.approvals.revoke(self.ca, approval["id"], "mudei de ideia")
            else:
                self.clock.now += timedelta(days=2)
                self.approvals.expire_due(self.a["id"])
            result = self.send(message)
            self.assertEqual(result["state"], "bloqueada", how)
            self.assertTrue(result["last_error"].startswith("aprovacao_"), result["last_error"])
        self.assertEqual(self.provider.sent, [])

    def test_a_deactivated_account_or_unlinked_recipient_is_caught_at_send_time(self):
        message = self.approved()
        revision = self.connections.get_connection(self.ca, self.account["id"])["revision"]
        self.connections.update_connection(self.ca, self.account["id"], revision, status="inativo")
        self.assertEqual(self.send(message)["last_error"], "conta_inativa")
        self.connections.update_connection(self.ca, self.account["id"], revision + 1, status="ativo")
        other = self.approved(body="Segunda mensagem")
        with self.repo.transaction(self.a["id"]) as session:  # the contact link is removed after approval
            for address in session.list("contact_address", limit=10):
                session.delete("contact_address", address["id"])
        self.assertEqual(self.send(other)["last_error"], "destinatario_fora_da_politica")
        self.assertEqual(self.provider.sent, [])


class OwnershipTests(OutboundCase):
    def test_a_transfer_blocks_the_previous_owners_pending_messages_and_the_new_owner_revalidates(self):
        queued, draft = self.approved(), self.draft(body="Rascunho ainda nao enviado")
        self.transfer_to_financeiro()
        for message_id in (queued["id"], draft["id"]):
            blocked = self.outbound.get_message(self.ca, message_id)
            self.assertEqual((blocked["state"], blocked["last_error"]), ("bloqueada", "dono_mudou"))
        self.assertEqual(self.send(queued)["state"], "bloqueada")
        self.assertEqual(self.provider.sent, [])
        notes = [i["item"]["body"] for i in self.cases.timeline(self.ca, self.case["id"]) if i["tipo"] == "entrada"]
        self.assertTrue(any("responsavel anterior bloqueada" in n for n in notes))
        with self.assertRaises(NotFound):  # the previous owner cannot revive or draft
            self.outbound.revalidate(self.agent_ctx(), queued["id"])
        with self.assertRaises(NotFound):
            self.draft(ctx=self.agent_ctx())
        new_ctx = self.agent_ctx(self.financeiro)
        adopted = self.outbound.revalidate(new_ctx, queued["id"])
        self.assertEqual((adopted["state"], adopted["owner_agent_id"], adopted["owner_version"], adopted["approval_id"]),
                         ("rascunho", self.financeiro["id"], 2, None))
        resubmitted = self.outbound.submit(new_ctx, adopted["id"])
        approval = self.approvals.get(self.ca, resubmitted["approval_id"])
        self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])
        sent = self.send(resubmitted)
        self.assertEqual(sent["state"], "enviada")
        outgoing = [i["item"] for i in self.cases.timeline(self.ca, self.case["id"])
                    if i["tipo"] == "entrada" and i["item"]["entry_type"] == "mensagem_externa"]
        self.assertEqual(outgoing[-1]["author"], self.financeiro["id"])

    def test_ownership_changing_after_the_approval_is_spent_still_stops_the_send(self):
        message = self.approved()
        original = self.approvals.consume
        done = []

        def consume_then_transfer(ctx, approval_id, request):
            ticket = original(ctx, approval_id, request)
            if not done:
                done.append(True)
                with self.repo.transaction(self.a["id"]) as session:  # the case changes hands right after step B
                    case = session.get("case", self.case["id"])
                    session.save({**case, "owner_agent_id": self.financeiro["id"], "owner_version": 2,
                                  "revision": case["revision"] + 1})
            return ticket

        self.approvals.consume = consume_then_transfer
        blocked = self.send(message)
        self.assertEqual((blocked["state"], blocked["last_error"]), ("bloqueada", "dono_mudou"))
        self.assertEqual(self.provider.sent, [])
        approval = self.approvals.get(self.ca, message["approval_id"])
        self.assertEqual((approval["state"], approval["effect_outcome"]), ("consumida", "falhou"))

    def test_operator_control_and_pauses_stop_sending(self):
        message = self.approved()
        case = self.case_now()
        self.cases.transition(self.ca, case["id"], case["revision"], "pausada", reason="aguardar")
        self.assertEqual(self.send(message)["last_error"], "demanda_sob_controle_do_operador")
        self.assertEqual(self.provider.sent, [])


class ReliabilityTests(OutboundCase):
    def test_duplicate_dispatches_and_double_clicks_send_one_message(self):
        message = self.approved()
        self.provider.delay = 0.2
        results, barrier = [], threading.Barrier(3)

        def click():
            barrier.wait()
            results.append(asyncio.run(self.outbound.dispatch(self.a["id"], message["id"]))["state"])

        threads = [threading.Thread(target=click) for _ in range(3)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(len(self.provider.sent), 1)
        self.assertEqual(sorted(results).count("enviada"), 1)
        self.assertEqual(self.outbound.get_message(self.ca, message["id"])["state"], "enviada")

    def test_messages_of_one_case_are_serialized_and_the_second_waits(self):
        first, second = self.approved(body="Primeira"), self.approved(body="Segunda")
        self.provider.delay = 0.3

        async def both():
            running = asyncio.ensure_future(self.outbound.dispatch(self.a["id"], first["id"]))
            await asyncio.sleep(0.1)
            with self.assertRaises(Busy):
                await self.outbound.dispatch(self.a["id"], second["id"])
            return await running

        self.assertEqual(asyncio.run(both())["state"], "enviada")
        self.assertEqual(self.send(second)["state"], "enviada")  # now it is its turn
        self.assertEqual(self.provider.peak, 1)
        self.assertEqual([s["body"] for s in self.provider.sent], ["Primeira", "Segunda"])

    def test_a_lost_response_is_unknown_never_delivered_and_waits_for_reconciliation(self):
        message = self.approved()
        self.provider.mode = "lose_response"
        result = self.send(message)
        self.assertEqual((result["state"], result["last_error"]), ("desconhecido", "sem_resposta_do_provedor"))
        self.assertIsNone(result["provider_id"])
        approval = self.approvals.get(self.ca, message["approval_id"])
        self.assertEqual(approval["effect_outcome"], "desconhecido")
        outgoing = [i for i in self.cases.timeline(self.ca, self.case["id"])
                    if i["tipo"] == "entrada" and i["item"]["entry_type"] == "mensagem_externa"]
        self.assertEqual(outgoing, [])  # no claim of delivery in the history
        self.assertEqual([m["id"] for m in self.outbound.needing_reconciliation(self.ca)], [message["id"]])
        with self.assertRaises(PermissionDenied):
            self.outbound.needing_reconciliation(self.agent_ctx())
        self.assertEqual(self.send(message)["state"], "desconhecido")  # never retried blindly
        self.assertEqual(len(self.provider.sent), 1)
        recovered = asyncio.run(self.outbound.auto_reconcile(self.a["id"], message["id"]))  # the provider knows the key
        self.assertEqual((recovered["state"], recovered["provider_id"]), ("enviada", "prov-recuperado"))

    def test_the_operator_can_settle_an_unknown_outcome_either_way(self):
        self.provider.mode = "lose_response"
        confirmed, denied = self.send(self.approved(body="A")), self.send(self.approved(body="B"))
        self.provider.lost.clear()
        self.assertEqual(asyncio.run(self.outbound.auto_reconcile(self.a["id"], confirmed["id"]))["state"], "desconhecido")
        for blocked in (lambda: self.outbound.resolve_unknown(self.agent_ctx(), confirmed["id"], "enviada", "p-1"),
                        lambda: self.outbound.resolve_unknown(self.ca, confirmed["id"], "enviada"),
                        lambda: self.outbound.resolve_unknown(self.ca, confirmed["id"], "talvez")):
            with self.assertRaises((PermissionDenied, OutboundError)):
                blocked()
        sent = self.outbound.resolve_unknown(self.ca, confirmed["id"], "enviada", "prov-visto-no-painel", "conferi na caixa de saida")
        self.assertEqual((sent["state"], sent["provider_id"]), ("enviada", "prov-visto-no-painel"))
        not_sent = self.outbound.resolve_unknown(self.ca, denied["id"], "nao_enviada")
        self.assertEqual(not_sent["state"], "falhou")
        self.assertEqual(self.outbound.needing_reconciliation(self.ca), [])
        with self.assertRaises(OutboundError):
            self.outbound.resolve_unknown(self.ca, sent["id"], "enviada", "x")

    def test_timeouts_rejections_and_credential_problems_have_distinct_outcomes(self):
        slow = self.approved(body="Lenta")
        self.provider.delay = 5
        self.assertEqual(self.send(slow)["state"], "desconhecido")  # timed out after the call started
        self.provider.delay = 0
        self.provider.mode = "reject"
        rejected = self.send(self.approved(body="Recusada"))
        self.assertEqual((rejected["state"], rejected["last_error"]), ("falhou", "recusada_pelo_provedor"))
        self.assertEqual(self.approvals.get(self.ca, rejected["approval_id"])["effect_outcome"], "falhou")
        self.provider.mode = "ok"
        before = len(self.provider.sent)
        self.connections.revoke_credential(self.ca, self.account["id"])
        revoked = self.send(self.approved(body="Sem credencial"))
        self.assertEqual((revoked["state"], revoked["last_error"]), ("falhou", "credencial_ou_conta_indisponivel"))
        self.assertEqual(len(self.provider.sent), before)

    def test_messages_stuck_in_flight_become_unknown(self):
        message = self.approved()
        with self.repo.transaction(self.a["id"]) as session:
            stored = session.get("outbound_message", message["id"])
            session.save({**stored, "state": "enviando", "revision": stored["revision"] + 1,
                          "updated_at": "2026-06-01T11:00:00Z"})
        self.assertEqual([m["state"] for m in self.outbound.mark_stuck(self.a["id"])], ["desconhecido"])

    def test_the_database_allows_one_sending_message_per_case_and_unique_keys(self):
        first, second = self.draft(body="A"), self.draft(body="B")
        conn = connect(self.path)
        self.addCleanup(conn.close)
        conn.execute("UPDATE outbound_messages SET state = 'enviando' WHERE id = ?", (first["id"],))
        with self.assertRaisesRegex(Exception, "UNIQUE"):
            conn.execute("UPDATE outbound_messages SET state = 'enviando' WHERE id = ?", (second["id"],))
        with self.assertRaisesRegex(Exception, "UNIQUE"):
            conn.execute("UPDATE outbound_messages SET dedup_key = ? WHERE id = ?", (first["dedup_key"], second["id"]))


class QueueIntegrationTests(OutboundCase):
    def worker(self):
        return Worker(self.queue, {"enviar_mensagem": outbound_handler(self.outbound)}, worker_id="w", lease_ms=30000,
                      heartbeat_s=0.05)

    def enqueue(self, message):
        job, _ = self.queue.enqueue(self.a["id"], "enviar_mensagem", {"message_id": message["id"]},
                                    idempotency_key=f"envio:{message['id']}", case_id=self.case["id"], max_attempts=5)
        return job

    def test_a_job_waits_for_the_approval_then_sends(self):
        message = self.outbound.submit(self.agent_ctx(), self.draft()["id"])
        job = self.enqueue(message)
        asyncio.run(self.worker().run_once())
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "pendente")  # not approved yet, nothing started
        self.assertEqual(self.provider.sent, [])
        approval = self.approvals.get(self.ca, message["approval_id"])
        self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])
        self.queue.clock = lambda: 10 ** 13  # skip the retry backoff
        asyncio.run(self.worker().run_once())
        done = self.queue.get(self.a["id"], job["id"])
        self.assertEqual((done["state"], done["result"]), ("concluido", {"provider_id": "prov-1"}))

    def test_a_crash_after_the_call_started_is_reconciled_not_resent(self):
        message = self.approved()
        job = self.enqueue(message)
        claimed = self.queue.claim("morreu", lease_ms=1000)
        self.queue.begin_effect(claimed["id"], "morreu", claimed["token"], f"enviar:{message['id']}")
        self.queue.clock = lambda: 10 ** 13  # the worker died holding the lease
        self.assertIsNone(self.queue.claim("outro"))
        stored = self.queue.get(self.a["id"], job["id"])
        self.assertEqual((stored["state"], stored["last_error"]), ("reconciliar", "efeito_externo_desconhecido"))
        self.assertEqual(self.provider.sent, [])


if __name__ == "__main__":
    unittest.main()
