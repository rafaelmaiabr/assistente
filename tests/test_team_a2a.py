import copy
from pathlib import Path
import unittest

from team import a2a
from team.a2a import A2AClient, A2AError, A2ARefused, A2AUnavailable
from tests.a2a_fake import FakePeer, MemoryTransport

CONTEXT = "ctx-caso-1"
KEY = "sk-abcdefghijklmnopqrstuvwx1234"


def request(**kwargs):
    return a2a.outbound_message(**{"message_id": a2a.message_id("caso-1", "subtarefa-1"), "context_id": CONTEXT,
                                   "objective": "Analisar o contrato", "expected_result": "Resumo das clausulas", **kwargs})


class Setup(unittest.TestCase):
    def setUp(self):
        self.peer = FakePeer()
        self.client = A2AClient(MemoryTransport(self.peer))


class VersionAndCardTests(Setup):
    def test_the_pinned_version_and_the_card_this_team_publishes(self):
        self.assertEqual(a2a.A2A_VERSION, "1.0")
        card = a2a.build_agent_card(name="Agente Contratos", description="Le contratos", url="https://equipe.invalid/a2a", version="1",
                                    skills=[{"id": "contratos", "name": "Contratos"}])
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertFalse(card["capabilities"]["pushNotifications"])
        self.assertNotIn("securitySchemes", card)
        with self.assertRaises(A2AError):
            a2a.build_agent_card(name="X", description=f"chave api_key={KEY}", url="https://e.invalid", version="1")

    def test_a_remote_card_is_information_and_never_permission(self):
        card = a2a.read_remote_card({**self.peer.card, "securitySchemes": {"x": 1}, "grants": ["tudo"], "name": "<b>Parceiro</b>"})
        self.assertFalse(card.grants_access)
        self.assertFalse(card.as_dict()["grants_access"])
        self.assertEqual(card.name, "&lt;b&gt;Parceiro&lt;/b&gt;")
        self.assertEqual(card.skills[0]["id"], "analise")
        self.assertNotIn("grants", card.as_dict())

    def test_a_card_without_required_fields_for_another_version_or_with_secrets_is_refused(self):
        broken = copy.deepcopy(self.peer.card)
        del broken["skills"]
        with self.assertRaises(A2AError):
            a2a.read_remote_card(broken)
        other = copy.deepcopy(self.peer.card)
        other["supportedInterfaces"][0]["protocolVersion"] = "0.3"
        with self.assertRaises(A2ARefused) as raised:
            a2a.read_remote_card(other)
        self.assertEqual(raised.exception.name, "VersionNotSupportedError")
        leaking = copy.deepcopy(self.peer.card)
        leaking["description"] = f"use api_key={KEY} aqui"
        with self.assertRaises(A2AError):
            a2a.read_remote_card(leaking)


class OutboundTests(Setup):
    def test_the_message_carries_only_the_envelope(self):
        message = request(summary="Cliente quer renovar", evidence=["Contrato assinado em 2025"])["message"]
        self.assertEqual(message["role"], "ROLE_USER")
        self.assertEqual(message["contextId"], CONTEXT)
        self.assertEqual(sorted(message), ["contextId", "messageId", "parts", "role"])
        self.assertEqual(len(message["parts"]), 4)

    def test_a_secret_or_an_oversized_message_sends_nothing(self):
        for kwargs in ({"summary": f"token: {KEY}"}, {"objective": "x" * 5000}, {"evidence": ["e"] * 6}, {"expected_result": f"chave {KEY}"}):
            with self.assertRaises(A2AError, msg=str(kwargs)[:40]):
                request(**kwargs)
        self.assertEqual(self.peer.calls, [])

    def test_the_message_id_is_stable_so_a_retry_is_the_same_message(self):
        self.assertEqual(a2a.message_id("a", 1), a2a.message_id("a", 1))
        self.assertNotEqual(a2a.message_id("a", 1), a2a.message_id("a", 2))


class ContinuityTests(Setup):
    def test_send_poll_and_complete_with_the_result_redacted(self):
        task = self.client.send(request())
        self.assertEqual((task["state"], task["local_state"], task["terminal"]), (a2a.SUBMITTED, "pendente", False))
        self.peer.advance(task["task_id"], a2a.WORKING)
        working = self.client.get(task["task_id"], context_id=CONTEXT, previous_state=task["state"])
        self.assertEqual(working["local_state"], "em_execucao")
        self.peer.advance(task["task_id"], a2a.COMPLETED, f"Resumo pronto. api_key={KEY}")
        done = self.client.get(task["task_id"], context_id=CONTEXT, previous_state=working["state"])
        self.assertEqual((done["local_state"], done["terminal"]), ("concluida", True))
        self.assertIn("Resumo pronto", done["text"])
        self.assertNotIn(KEY, done["text"])

    def test_a_retry_with_the_same_message_id_is_the_same_remote_task(self):
        first, again = self.client.send(request()), self.client.send(request())
        self.assertEqual(first["task_id"], again["task_id"])
        self.assertEqual(len(self.peer.tasks), 1)

    def test_input_required_and_auth_required_wait_for_a_person_and_a_rejection_is_a_failure(self):
        task = self.client.send(request())
        for state, local in ((a2a.INPUT_REQUIRED, "aguardando"), (a2a.AUTH_REQUIRED, "aguardando"), (a2a.REJECTED, "falhou")):
            self.peer.advance(task["task_id"], state)
            self.assertEqual(self.client.get(task["task_id"], context_id=CONTEXT)["local_state"], local)

    def test_cancel_is_best_effort_and_the_returned_state_is_what_counts(self):
        task = self.client.send(request())
        self.assertEqual(self.client.cancel(task["task_id"], context_id=CONTEXT)["local_state"], "cancelada")
        with self.assertRaises(A2AError) as raised:
            self.client.cancel(task["task_id"], context_id=CONTEXT)
        self.assertEqual(raised.exception.name, "TaskNotCancelableError")


class IncompatibleResponseTests(Setup):
    def test_a_terminal_task_never_goes_back_to_work(self):
        task = self.client.send(request())
        self.peer.advance(task["task_id"], a2a.COMPLETED, "ok")
        self.peer.advance(task["task_id"], a2a.WORKING)
        with self.assertRaises(A2AError):
            self.client.get(task["task_id"], context_id=CONTEXT, previous_state=a2a.COMPLETED)

    def test_a_task_of_another_context_or_another_id_or_an_unknown_state_is_refused(self):
        task = self.client.send(request())
        with self.assertRaises(A2AError):
            self.client.get(task["task_id"], context_id="ctx-de-outro-caso")
        for broken in ({"id": "t-9"}, {"contextId": "outro"}, {"status": {"state": "TASK_STATE_QUALQUER"}}, {"status": None}):
            with self.assertRaises(A2AError, msg=str(broken)):
                a2a.read_task({**self.peer.tasks[task["task_id"]], **broken}, expect_task_id=task["task_id"], expect_context_id=CONTEXT)

    def test_a_message_instead_of_a_task_or_both_is_not_accepted(self):
        class Odd(a2a.A2ATransport):
            def __init__(self, result):
                self.result = result

            def call(self, method, params, *, version=a2a.A2A_VERSION):
                return self.result
        for result in ({"message": {"parts": []}}, {"task": {}, "message": {}}, {}, []):
            with self.assertRaises(A2AError):
                A2AClient(Odd(result)).send(request())

    def test_the_result_is_capped(self):
        task = self.client.send(request())
        self.peer.advance(task["task_id"], a2a.COMPLETED, "a" * 200000)
        self.assertLess(len(self.client.get(task["task_id"], context_id=CONTEXT)["text"]), a2a.MAX_RESULT_BYTES + 50)


class RefusalAndOutageTests(Setup):
    def test_a_wrong_credential_is_a_refusal_that_never_shows_it(self):
        client = A2AClient(MemoryTransport(self.peer, token="credencial-errada-123"))
        with self.assertRaises(A2ARefused) as raised:
            client.send(request())
        self.assertEqual(raised.exception.name, "Unauthenticated")
        self.assertNotIn("credencial-errada-123", str(raised.exception))
        self.assertEqual(self.peer.tasks, {})

    def test_a_peer_that_does_not_answer_is_unknown_not_success_and_transport_bugs_show_only_the_kind(self):
        with self.assertRaises(A2AUnavailable):
            A2AClient(MemoryTransport(self.peer, online=False)).send(request())

        class Broken(a2a.A2ATransport):
            def call(self, method, params, *, version=a2a.A2A_VERSION):
                raise ConnectionResetError("https://parceiro.invalid/a2a?token=segredo-123")
        with self.assertRaises(A2AUnavailable) as raised:
            A2AClient(Broken()).send(request())
        self.assertIn("ConnectionResetError", str(raised.exception))
        self.assertNotIn("segredo-123", str(raised.exception))

    def test_the_adapter_imports_nothing_from_the_domain(self):
        import team.a2a as module
        imports = [line for line in Path(module.__file__).read_text(encoding="utf-8").splitlines() if line.startswith(("import ", "from "))]
        self.assertEqual(sorted(imports), ["from .prompting import escape", "from .schema import find_secrets, redact_secrets",
                                           "from abc import ABC, abstractmethod", "import hashlib", "import re"])


if __name__ == "__main__":
    unittest.main()
