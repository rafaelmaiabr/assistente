import asyncio
import importlib.util
import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from aiohttp import ClientSession, web

from mail_mover import MoveService
from personal_mail import PersonalMail
from team.app import TeamApp
from team.vault import MemoryVault
from tests.imap_fake_rw import FakeImapRW
from tests.smtp_fake import FakeSmtp
from tests.test_web import FakeAgent as BaseAgent, FakeSpeech
from web_server import ALLOWED_HOSTS, create_app

LIVEKIT = importlib.util.find_spec("livekit") is not None
USER, PASSWORD = "ana@exemplo.invalid", "senha-de-app"
PHISH = "Sua conta será bloqueada. Confirme sua senha agora em https://acesso-seguro.biz/login para evitar a suspensão."


def raw(number, subject, sender, body="Texto simples"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, USER, subject
    msg["Date"], msg["Message-ID"] = "Mon, 01 Jun 2026 10:00:00 +0000", f"<web{number}@exemplo.invalid>"
    msg.set_content(body)
    return msg.as_bytes()


class Agent(BaseAgent):
    async def refresh(self):
        pass


class WebMoveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Agent.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.imap = FakeImapRW({"INBOX": {"validity": 11, "messages": {
            1: raw(1, "Reuniao", "Bruno <bruno@exemplo.invalid>", "Podemos conversar?"),
            2: raw(2, "Sua conta", "Suporte <suporte@banco.com.br>", PHISH),
            3: raw(3, "Promocao", "Loja <loja@loja.com.br>", "Ofertas da semana")}},
            "Trash": {"validity": 21, "messages": {}}, "Junk": {"validity": 31, "messages": {}}}, user=USER, password=PASSWORD)
        self.smtp = FakeSmtp(user=USER, password=PASSWORD)
        self.vault = MemoryVault()
        self.runner = web.AppRunner(create_app(Agent, {"model": "test"}, FakeSpeech(), data_root=self.root,
                                               team_app=TeamApp(self.root, {"model": "test"}, vault=self.vault)))
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        port = self.runner.addresses[0][1]
        self.base, self.host = f"http://127.0.0.1:{port}", f"127.0.0.1:{port}"
        ALLOWED_HOSTS.add(self.host)
        self.client = ClientSession()
        self.seen = []

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        ALLOWED_HOSTS.discard(self.host)
        self.imap.stop()
        self.smtp.stop()
        self.tmp.cleanup()

    def configure_mail(self, send=True):
        smtp = {"host": "127.0.0.1", "port": self.smtp.port, "security": "plain", "from_address": USER, "from_name": None} if send else None
        PersonalMail(self.root, self.vault).configure(host="127.0.0.1", port=self.imap.port, username=USER, password=PASSWORD, tls=False, smtp=smtp)

    async def until(self, ws, predicate):
        try:
            async with asyncio.timeout(15):
                while True:
                    event = await ws.receive_json()
                    self.seen.append(json.dumps(event))
                    if predicate(event):
                        return event
        except TimeoutError:
            raise AssertionError("never arrived; saw: " + "; ".join(self.seen[-8:])) from None

    async def connect(self):
        ws = await self.client.ws_connect(self.base + "/ws", origin=self.base)
        session = await self.until(ws, lambda e: e.get("type") == "session")
        await self.until(ws, lambda e: e.get("state") == "ready")
        return ws, session

    async def say(self, ws, text):
        await ws.send_json({"type": "message", "text": text})
        reply = await self.until(ws, lambda e: e.get("type") == "message" and e.get("role") == "assistant")
        await self.until(ws, lambda e: e.get("state") == "ready")
        return reply["text"]

    def inbox(self):
        return sorted(self.imap.folders["INBOX"]["messages"])

    async def proposal(self, ws, text):
        await ws.send_json({"type": "message", "text": text})
        found = (await self.until(ws, lambda e: e.get("type") == "move_proposal"))["proposal"]
        await self.until(ws, lambda e: e.get("state") == "ready")
        return found

    async def test_a_plain_request_proposes_the_exact_list_and_moves_nothing(self):
        self.configure_mail()
        ws, _ = await self.connect()
        proposal = await self.proposal(ws, "Mova o email 1 para a lixeira")
        self.assertEqual((proposal["destino_pasta"], [i["uid"] for i in proposal["itens"]], proposal["itens"][0]["assunto"]), ("Trash", [1], "Reuniao"))
        self.assertEqual(len(proposal["hash"]), 64)
        self.assertEqual((self.inbox(), self.imap.writes), ([1, 2, 3], 0))
        self.assertEqual(Agent.instances[0].inputs, [])  # the model was never asked
        await ws.close()

    async def test_only_the_click_with_the_shown_hash_moves_and_the_result_is_confirmed(self):
        self.configure_mail()
        ws, _ = await self.connect()
        proposal = await self.proposal(ws, "Mova os emails 1 e 3 para o spam")
        await ws.send_json({"type": "move_decision", "id": proposal["id"], "decision": "approve", "hash": proposal["hash"]})
        done = await self.until(ws, lambda e: e.get("type") == "move_result" and e.get("estado") == "concluida")
        self.assertEqual((done["movidas"], done["destino"]), (2, "Junk"))
        text = (await self.until(ws, lambda e: e.get("type") == "message" and e.get("role") == "assistant" and "Movi" in e.get("text", "")))["text"]
        self.assertIn("Movi 2 mensagem(ns) para Junk", text)
        self.assertEqual((self.inbox(), len(self.imap.folders["Junk"]["messages"])), ([2], 2))
        self.assertEqual(self.imap.general_expunge, 0)
        self.assertEqual(self.imap.seen, set())
        await ws.close()

    async def test_another_hash_or_a_discard_moves_nothing(self):
        self.configure_mail()
        ws, _ = await self.connect()
        proposal = await self.proposal(ws, "Mova o email 1 para a lixeira")
        await ws.send_json({"type": "move_decision", "id": proposal["id"], "decision": "approve", "hash": "f" * 64})
        altered = await self.until(ws, lambda e: e.get("type") == "move_result" and e.get("estado") == "alterada")
        self.assertIn("Nada foi movido", altered["erro"])
        second = await self.proposal(ws, "Mova o email 2 para a lixeira")
        await ws.send_json({"type": "move_decision", "id": second["id"], "decision": "discard"})
        await self.until(ws, lambda e: e.get("type") == "move_result" and e.get("estado") == "descartada")
        await ws.send_json({"type": "move_decision", "id": second["id"], "decision": "approve", "hash": second["hash"]})
        await self.until(ws, lambda e: e.get("type") == "move_result" and e.get("estado") == "inexistente")
        await ws.send_json({"type": "move_decision", "id": "x", "decision": "talvez", "hash": "y"})
        self.assertEqual((await self.until(ws, lambda e: e.get("type") == "rejected"))["message"], "Decisão inválida.")
        self.assertEqual((self.inbox(), self.imap.writes), ([1, 2, 3], 0))
        await ws.close()

    async def test_move_the_suspects_means_exactly_what_the_last_triage_called_suspect(self):
        self.configure_mail()
        ws, _ = await self.connect()
        text = await self.say(ws, "Mova os suspeitos para o spam")
        self.assertIn("Nada foi movido", text)  # no triage yet: it does not guess
        triage = await self.say(ws, "Faça a triagem da minha caixa")
        self.assertIn("Suspeita (1)", triage)
        proposal = await self.proposal(ws, "Mova os suspeitos para o spam")
        self.assertEqual([i["uid"] for i in proposal["itens"]], [2])  # only the suspect one, not the newsletter or the colleague
        await ws.send_json({"type": "move_decision", "id": proposal["id"], "decision": "approve", "hash": proposal["hash"]})
        await self.until(ws, lambda e: e.get("type") == "move_result" and e.get("estado") == "concluida")
        self.assertEqual((self.inbox(), sorted(self.imap.folders["Junk"]["messages"])), ([1, 3], [1]))
        await ws.close()

    async def test_a_read_only_mailbox_does_not_move_and_says_so(self):
        self.configure_mail(send=False)
        ws, _ = await self.connect()
        text = await self.say(ws, "Mova o email 1 para a lixeira")
        self.assertIn("somente leitura", text)
        self.assertEqual(self.backlog("move_proposal"), [])
        self.assertEqual((self.inbox(), self.imap.writes), ([1, 2, 3], 0))
        refusal = await self.say(ws, "Apague meus emails de spam")
        self.assertIn("Não consigo mover, apagar", refusal)  # the old fixed answer: there is no way to move here
        await ws.close()

    async def test_with_a_movable_mailbox_the_refusal_to_delete_says_what_is_possible(self):
        self.configure_mail()
        ws, _ = await self.connect()
        text = await self.say(ws, "Apague meus emails de spam")
        self.assertIn("Não apago emails de vez", text)
        self.assertIn("propor mover", text)
        await ws.close()

    async def test_a_waiting_proposal_comes_back_after_a_reload(self):
        self.configure_mail()
        ws, _ = await self.connect()
        proposal = await self.proposal(ws, "Mova o email 3 para a lixeira")
        await ws.close()
        again, _ = await self.connect()
        listed = self.backlog("move_proposals")[-1]["proposals"]
        self.assertEqual([p["id"] for p in listed], [proposal["id"]])
        await again.close()

    def backlog(self, kind):
        return [json.loads(line) for line in self.seen if json.loads(line).get("type") == kind]


@unittest.skipUnless(LIVEKIT, "livekit is not installed here")
class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_tool_only_proposes_and_a_read_only_or_company_mailbox_has_no_tool(self):
        import assistant_core
        from unittest import mock
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        imap = FakeImapRW({"INBOX": {"validity": 11, "messages": {1: raw(1, "Oi", "Bruno <bruno@exemplo.invalid>")}},
                           "Trash": {"validity": 21, "messages": {}}, "Junk": {"validity": 31, "messages": {}}}, user=USER, password=PASSWORD)
        self.addCleanup(imap.stop)
        smtp = {"host": "127.0.0.1", "port": 25, "security": "plain", "from_address": USER, "from_name": None}
        mail = PersonalMail(Path(tmp.name), MemoryVault())
        mail.configure(host="127.0.0.1", port=imap.port, username=USER, password=PASSWORD, tls=False, smtp=smtp)
        service, seen = MoveService(Path(tmp.name)), []
        tool = assistant_core.mover_tools(mail, service, seen.append)[0]
        ok = await tool(uids="1", destino="lixeira")
        self.assertTrue(ok["proposta_criada"] and ok["aguardando_aprovacao_da_pessoa"])
        self.assertEqual((len(seen), imap.writes, sorted(imap.folders["INBOX"]["messages"])), (1, 0, [1]))
        self.assertIn("diga_a_pessoa", await tool(uids="99", destino="lixeira"))
        self.assertIn("diga_a_pessoa", await tool(uids="abc", destino="lixeira"))
        self.assertEqual(len(seen), 1)
        mail.configure(host="127.0.0.1", port=imap.port, username=USER, password=PASSWORD, tls=False, smtp=None)  # set up as read-only
        self.assertEqual(assistant_core.mover_tools(mail, service, seen.append), [])
        self.assertEqual(assistant_core.mover_tools(mock.Mock(spec=[]), service, seen.append), [])  # no write side at all (a company mailbox)
        from honesty import capabilities_text
        self.assertIn("e nunca se apaga de vez", capabilities_text({"propor_mover_emails"}))
        self.assertIn("apagar de vez, esvaziar a Lixeira", capabilities_text({"propor_mover_emails"}))


if __name__ == "__main__":
    unittest.main()
