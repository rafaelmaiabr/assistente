import unittest
from datetime import timedelta

from team import a2a
from team.a2a_service import A2AService, A2ATaskRejected, callback_token
from team.access import NotFound, PermissionDenied
from team.cases import CaseRejected
from team.connections import ConnectionRejected, ConnectionService, CredentialError, LauncherRegistry
from team.vault import MemoryVault
from tests.a2a_fake import FakePeer, MemoryTransport
from tests.test_team_handoffs import HandoffCase

SECRET = "token-de-teste"
KEY = "sk-abcdefghijklmnopqrstuvwx1234"


class A2ACase(HandoffCase):
    def setUp(self):
        super().setUp()
        self.vault = MemoryVault()
        self.connections = ConnectionService(self.repo, self.access, self.vault, LauncherRegistry(
            __import__("pathlib").Path(self.temp.name) / "launchers.json", self.access.operator_id))
        self.peer = FakePeer(token=SECRET)
        self.online = True
        self.service = A2AService(self.repo, self.access, self.cases, self.connections, self.transport, clock=self.clock)
        self.conn = self.connections.create_connection(self.ca, label="Parceiro", provider="a2a", connection_kind="a2a",
                                                       endpoint="https://parceiro.invalid/a2a")
        self.connections.set_credential(self.ca, self.conn["id"], SECRET)

    def transport(self, view, secret):
        return MemoryTransport(self.peer, token=secret, online=self.online)

    def delegate(self, ctx=None, case=None, **kwargs):
        case = case or self.open_case()
        options = dict(objective="Analisar o contrato", expected_result="Resumo das clausulas", summary="Cliente quer renovar")
        return self.service.delegate(ctx or self.ca, case["id"], self.conn["id"], **{**options, **kwargs}), case

    def remote_id(self, task):
        return next(iter(self.peer.tasks))

    def events(self, case):
        return [e["type"] for e in super().events(case)]


class DelegationTests(A2ACase):
    def test_a_delegated_task_is_tracked_and_the_case_keeps_its_owner(self):
        task, case = self.delegate()
        self.assertEqual((task["state"], task["remote_state"]), ("pendente", a2a.SUBMITTED))
        self.assertEqual(task["remote_task_id"], "t-1")
        after = self.cases.get_case(self.ca, case["id"])
        self.assertEqual((after["owner_agent_id"], after["state"]), (self.secretaria["id"], "aguardando_subtarefa"))
        self.assertIn("a2a.delegated", self.events(case))
        sent = self.peer.calls[0][1]["message"]
        self.assertEqual(sorted(sent), ["contextId", "messageId", "parts", "role"])
        self.assertEqual(sent["contextId"], task["id"])

    def test_the_result_comes_back_as_an_internal_note_that_is_not_evidence_and_the_owner_resumes(self):
        task, case = self.delegate()
        self.peer.advance("t-1", a2a.WORKING)
        self.assertEqual(self.service.poll(self.ca, task["id"])["state"], "em_execucao")
        self.peer.advance("t-1", a2a.COMPLETED, f"Tres clausulas. api_key={KEY}")
        done = self.service.poll(self.ca, task["id"])
        self.assertEqual(done["state"], "concluida")
        self.assertNotIn(KEY, done["result_text"])
        after = self.cases.get_case(self.ca, case["id"])
        self.assertEqual((after["state"], after["owner_agent_id"]), ("em_execucao", self.secretaria["id"]))
        notes = [e for e in self.cases.timeline(self.ca, case["id"]) if "Resultado do agente externo" in str(e)]
        self.assertTrue(notes)
        self.assertEqual(self.cases.timeline(self.ca, case["id"]) and [e for e in self.cases.timeline(self.ca, case["id"]) if e["tipo"] == "evidencia"], [])

    def test_a_duplicate_request_is_the_same_task_and_sends_once(self):
        case = self.open_case()
        first, _ = self.delegate(case=case, idempotency_key="pedido-1")
        again, _ = self.delegate(case=case, idempotency_key="pedido-1")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.peer.tasks), 1)
        self.assertEqual(len([c for c in self.peer.calls if c[0] == "SendMessage"]), 1)

    def test_only_the_owner_or_the_operator_delegates_and_an_agent_needs_a_write_grant(self):
        case = self.open_case()
        other = self.ctx(self.suporte)
        self.grant_case_access(self.suporte)
        with self.assertRaises(PermissionDenied):
            self.service.delegate(other, case["id"], self.conn["id"], objective="x")
        owner = self.ctx(self.secretaria)
        with self.assertRaises((PermissionDenied, NotFound)):  # no grant on the connection
            self.service.delegate(owner, case["id"], self.conn["id"], objective="x")
        self.team.create(self.ca, "access_grant", principal_id=self.secretaria["id"], resource_kind="connection", resource_id=self.conn["id"],
                         actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        task = self.service.delegate(self.ctx(self.secretaria), case["id"], self.conn["id"], objective="Analisar")
        self.assertEqual(task["state"], "pendente")

    def test_a_secret_in_the_text_sends_nothing_and_stores_nothing(self):
        case = self.open_case()
        with self.assertRaises(A2ATaskRejected):
            self.service.delegate(self.ca, case["id"], self.conn["id"], objective="Analisar", summary=f"chave {KEY}")
        self.assertEqual((self.peer.calls, self.service.list_tasks(self.ca)), ([], []))

    def test_the_other_company_cannot_see_or_use_the_counterparty_or_the_task(self):
        task, case = self.delegate()
        with self.assertRaises(NotFound):
            self.service.view(self.cb, task["id"])
        self.assertEqual(self.service.list_tasks(self.cb), [])
        with self.assertRaises(NotFound):
            self.service.delegate(self.cb, case["id"], self.conn["id"], objective="x")

    def test_the_limit_of_open_tasks_per_case_and_a_closed_case(self):
        case = self.open_case()
        for number in range(3):
            self.delegate(case=case, idempotency_key=f"k{number}")
        with self.assertRaises(A2ATaskRejected):
            self.delegate(case=case, idempotency_key="k9")


class CounterpartyTests(A2ACase):
    def test_an_a2a_connection_needs_https_the_a2a_provider_and_no_catalog(self):
        for kwargs in (dict(endpoint="http://parceiro.invalid/a2a"), dict(endpoint=None), dict(endpoint="ftp://x.invalid"),
                       dict(endpoint="https://x.invalid", provider="outro")):
            options = dict(label="X", provider="a2a", connection_kind="a2a", endpoint="https://x.invalid")
            with self.assertRaises(ConnectionRejected, msg=str(kwargs)):
                self.connections.create_connection(self.ca, **{**options, **kwargs})
        self.connections.create_connection(self.ca, label="Local", provider="a2a", connection_kind="a2a", endpoint="http://127.0.0.1:9100/a2a")

    def test_a_revoked_counterparty_stops_every_later_call_and_the_task_ends_as_a_failure(self):
        task, case = self.delegate()
        self.connections.revoke_credential(self.ca, self.conn["id"])
        self.assertEqual(self.service.poll(self.ca, task["id"])["attention"], True)
        again, _ = self.delegate(case=case, idempotency_key="depois")
        self.assertEqual((again["state"], again["attention"]), ("falhou", True))
        self.assertEqual(len([c for c in self.peer.calls if c[0] == "SendMessage"]), 1)
        self.assertEqual(self.cases.get_case(self.ca, case["id"])["owner_agent_id"], self.secretaria["id"])

    def test_a_disabled_counterparty_is_not_called(self):
        current = self.connections.get_connection(self.ca, self.conn["id"])
        self.connections.update_connection(self.ca, self.conn["id"], current["revision"], status="inativo")
        task, _ = self.delegate()
        self.assertEqual(task["state"], "falhou")
        self.assertEqual(self.peer.calls, [])

    def test_a_wrong_credential_is_a_refusal_and_never_appears(self):
        self.connections.set_credential(self.ca, self.conn["id"], "credencial-errada-123")
        task, case = self.delegate()
        self.assertEqual((task["state"], task["attention"]), ("falhou", True))
        self.assertIn("Unauthenticated", task["detail"])
        self.assertNotIn("credencial-errada-123", repr(task) + repr(self.cases.timeline(self.ca, case["id"])))
        self.assertEqual(self.cases.get_case(self.ca, case["id"])["state"], "em_execucao")


class UnknownOutcomeTests(A2ACase):
    def test_a_lost_answer_is_unknown_blocks_a_blind_repeat_and_a_retry_keeps_the_same_message(self):
        self.online = False
        task, case = self.delegate(idempotency_key="p1")
        self.assertEqual((task["state"], task["attention"], task["remote_task_id"]), ("desconhecido", True, None))
        with self.assertRaises(A2ATaskRejected):
            self.service.poll(self.ca, task["id"])
        same, _ = self.delegate(case=case, idempotency_key="p1")
        self.assertEqual(same["id"], task["id"])
        self.assertEqual(self.cases.get_case(self.ca, case["id"])["state"], "aguardando_subtarefa")
        with self.assertRaises(PermissionDenied):
            self.service.retry(self.ctx(self.secretaria), task["id"])
        self.online = True
        retried = self.service.retry(self.ca, task["id"])
        self.assertEqual((retried["state"], retried["remote_task_id"]), ("pendente", "t-1"))
        self.assertEqual(len(self.peer.tasks), 1)

    def test_the_operator_can_close_an_unknown_task_after_checking(self):
        self.online = False
        task, case = self.delegate()
        closed = self.service.close_unknown(self.ca, task["id"], "confirmei com o parceiro: nao chegou")
        self.assertEqual((closed["state"], closed["attention"]), ("cancelada", False))
        self.assertEqual(self.cases.get_case(self.ca, case["id"])["state"], "em_execucao")
        with self.assertRaises(A2ATaskRejected):
            self.service.close_unknown(self.ca, task["id"], "de novo")

    def test_a_peer_that_stops_answering_while_polling_changes_nothing_but_flags_it(self):
        task, _ = self.delegate()
        self.online = False
        flagged = self.service.poll(self.ca, task["id"])
        self.assertEqual((flagged["state"], flagged["attention"]), ("pendente", True))


class TimeoutTests(A2ACase):
    def test_an_expired_task_is_closed_here_the_peer_is_asked_to_stop_and_the_owner_keeps_the_case(self):
        task, case = self.delegate(ttl_seconds=60)
        self.clock.now += timedelta(seconds=120)
        expired = self.service.expire_due(self.a["id"])
        self.assertEqual([t["state"] for t in expired], ["expirada"])
        self.assertEqual(self.peer.tasks["t-1"]["status"]["state"], a2a.CANCELED)
        after = self.cases.get_case(self.ca, case["id"])
        self.assertEqual((after["state"], after["owner_agent_id"]), ("em_execucao", self.secretaria["id"]))
        self.assertEqual(len(self.service.needing_attention(self.ca)), 1)
        self.assertEqual(self.service.expire_due(self.a["id"]), [])

    def test_an_unknown_task_past_its_deadline_is_only_flagged_never_assumed_failed(self):
        self.online = False
        task, case = self.delegate(ttl_seconds=60)
        self.service.resolve_attention(self.ca, task["id"], "visto")
        self.clock.now += timedelta(seconds=120)
        self.service.expire_due(self.a["id"])
        current = self.service.view(self.ca, task["id"])
        self.assertEqual((current["state"], current["attention"]), ("desconhecido", True))

    def test_a_task_that_finished_is_not_expired(self):
        task, _ = self.delegate(ttl_seconds=60)
        self.peer.advance("t-1", a2a.COMPLETED, "ok")
        self.service.poll(self.ca, task["id"])
        self.clock.now += timedelta(seconds=120)
        self.assertEqual(self.service.expire_due(self.a["id"]), [])


class CancelTests(A2ACase):
    def test_cancel_reflects_what_the_peer_answers(self):
        task, case = self.delegate()
        cancelled = self.service.cancel(self.ca, task["id"])
        self.assertEqual((cancelled["state"], cancelled["remote_state"], cancelled["attention"]), ("cancelada", a2a.CANCELED, False))
        self.assertEqual(self.cases.get_case(self.ca, case["id"])["state"], "em_execucao")

    def test_a_peer_that_already_finished_is_believed_over_the_cancel_request(self):
        task, _ = self.delegate()
        self.peer.advance("t-1", a2a.COMPLETED, "pronto")
        self.assertEqual(self.service.cancel(self.ca, task["id"])["state"], "concluida")

    def test_when_the_peer_cannot_be_reached_the_cancel_holds_here_but_is_flagged_as_unconfirmed(self):
        task, _ = self.delegate()
        self.online = False
        cancelled = self.service.cancel(self.ca, task["id"])
        self.assertEqual((cancelled["state"], cancelled["attention"]), ("cancelada", True))
        self.assertIn("nao confirmou", cancelled["detail"])


class IncompatibleAndRepeatedTests(A2ACase):
    def test_repeating_the_same_state_changes_nothing(self):
        task, case = self.delegate()
        self.peer.advance("t-1", a2a.WORKING)
        first = self.service.poll(self.ca, task["id"])
        again = self.service.poll(self.ca, task["id"])
        self.assertEqual(first["revision"], again["revision"])

    def test_input_required_waits_for_a_person_and_grants_nothing(self):
        task, case = self.delegate()
        self.peer.advance("t-1", a2a.INPUT_REQUIRED)
        waiting = self.service.poll(self.ca, task["id"])
        self.assertEqual((waiting["state"], waiting["attention"]), ("aguardando", True))
        self.assertEqual(self.cases.get_case(self.ca, case["id"])["state"], "aguardando_subtarefa")

    def test_a_task_of_another_context_is_refused(self):
        task, _ = self.delegate()
        self.peer.tasks["t-1"]["contextId"] = "ctx-de-outro"
        self.assertTrue(self.service.poll(self.ca, task["id"])["attention"])
        self.assertEqual(self.service.view(self.ca, task["id"])["state"], "pendente")


class CallbackTests(A2ACase):
    def push(self, task, state=a2a.WORKING, text=None, token=None, connection_id=None, remote="t-1"):
        self.peer.advance("t-1", state, text)
        payload = {**self.peer.tasks["t-1"], "id": remote}
        token = callback_token(SECRET, self.conn["id"], remote) if token is None else token
        return self.service.accept_callback(self.a["id"], connection_id or self.conn["id"], token, payload)

    def test_a_valid_callback_updates_the_task_and_a_repeat_changes_nothing(self):
        task, _ = self.delegate()
        first = self.push(task)
        again = self.push(task)
        self.assertEqual((first["state"], first["revision"]), ("em_execucao", again["revision"]))
        done = self.push(task, a2a.COMPLETED, "pronto")
        self.assertEqual(done["state"], "concluida")
        self.assertEqual(self.push(task, a2a.COMPLETED, "pronto")["revision"], done["revision"])

    def test_a_forged_callback_is_refused_whatever_is_wrong_and_changes_nothing(self):
        task, _ = self.delegate()
        for kwargs in ({"token": "token-forjado"}, {"token": ""}, {"token": callback_token("outro-segredo", self.conn["id"], "t-1")},
                       {"remote": "t-99", "token": callback_token(SECRET, self.conn["id"], "t-99")},
                       {"connection_id": "con_inexistente"}):
            with self.assertRaises(A2ATaskRejected, msg=str(kwargs)[:50]) as raised:
                self.push(task, **kwargs)
            self.assertEqual(str(raised.exception), "Notificacao recusada.")
        self.assertEqual(self.service.view(self.ca, task["id"])["state"], "pendente")

    def test_a_callback_for_a_revoked_counterparty_or_from_another_company_is_refused(self):
        task, _ = self.delegate()
        token = callback_token(SECRET, self.conn["id"], "t-1")
        with self.assertRaises(A2ATaskRejected):
            self.service.accept_callback(self.b["id"], self.conn["id"], token, {**self.peer.tasks["t-1"]})
        self.connections.revoke_credential(self.ca, self.conn["id"])
        with self.assertRaises(A2ATaskRejected):
            self.service.accept_callback(self.a["id"], self.conn["id"], token, {**self.peer.tasks["t-1"]})

    def test_a_callback_cannot_move_a_finished_task_back(self):
        task, _ = self.delegate()
        self.push(task, a2a.COMPLETED, "ok")
        with self.assertRaises(A2ATaskRejected):
            self.push(task, a2a.WORKING)


if __name__ == "__main__":
    unittest.main()
