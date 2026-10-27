import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web, ClientSession

from personal_mail import PersonalMail
from personal_outbox import PersonalOutbox
from team.app import TeamApp
from team.vault import MemoryVault
from tests.imap_case import build
from tests.imap_fake import FakeImap
from tests.smtp_fake import FakeSmtp
from tests.test_web import FakeAgent as BaseAgent, FakeSpeech
from web_server import ALLOWED_HOSTS, create_app

PASSWORD = "senha-de-app"
USER = "ana@exemplo.invalid"


class Agent(BaseAgent):
    refreshed = 0

    async def refresh(self):
        type(self).refreshed += 1


class WebMailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Agent.instances, Agent.refreshed = [], 0
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.imap = FakeImap({"INBOX": {"validity": 11, "messages": {
            1: build("Reuniao", sender="Bruno <bruno@exemplo.invalid>", body="Podemos conversar?").as_bytes()}}}, user=USER, password=PASSWORD)
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

    def configure_mail(self):
        mail = PersonalMail(self.root, self.vault)
        mail.configure(host="127.0.0.1", port=self.imap.port, username=USER, password=PASSWORD, tls=False, smtp={
            "host": "127.0.0.1", "port": self.smtp.port, "security": "plain", "from_address": USER, "from_name": None})
        return mail

    async def until(self, ws, predicate):
        try:
            async with asyncio.timeout(10):
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

    def backlog(self, kind):
        return [json.loads(line) for line in self.seen if json.loads(line).get("type") == kind]

    async def test_asking_to_configure_the_mailbox_opens_the_form_without_the_model(self):
        ws, session = await self.connect()
        self.assertEqual((session["mail"], session["reply"]), ("demo", False))
        await ws.send_json({"type": "message", "text": "Configurar caixa de email"})
        await self.until(ws, lambda e: e.get("type") == "setup")
        await self.until(ws, lambda e: e.get("state") == "ready")
        self.assertEqual(Agent.instances[0].inputs, [])  # a small model is not needed to open the form
        await ws.send_json({"type": "message", "text": "Qual a capital da França?"})
        await self.until(ws, lambda e: e.get("type") == "status" and e.get("state") == "processing")
        self.assertEqual(Agent.instances[0].inputs, ["Qual a capital da França?"])
        Agent.instances[0].gate.set()

    async def test_the_form_sets_the_skill_up_refreshes_the_agent_and_never_echoes_the_password(self):
        ws, _ = await self.connect()
        with patch.object(PersonalMail, "setup", return_value={"ok": True, "leitura": True, "envio": True, "servidor": "x", "usuario": USER}) as setup:
            await ws.send_json({"type": "setup_mail", "email": USER, "password": "senha-secreta-123", "read_only": False})
            result = await self.until(ws, lambda e: e.get("type") == "setup_result")
            skill = await self.until(ws, lambda e: e.get("type") == "skill")
        self.assertTrue(result["ok"])
        self.assertTrue(result["reply"])
        self.assertEqual((skill["id"], skill["state"]), ("email", "ativa"))
        self.assertEqual(Agent.refreshed, 1)
        self.assertEqual(setup.call_args.args[:2], (USER, "senha-secreta-123"))
        self.assertFalse([line for line in self.seen if "senha-secreta-123" in line])

    async def test_a_failed_setup_reports_and_does_not_refresh(self):
        ws, _ = await self.connect()
        with patch.object(PersonalMail, "setup", return_value={"erro": "O servidor recusou a senha. Nada foi guardado."}):
            await ws.send_json({"type": "setup_mail", "email": USER, "password": "senha-secreta-123"})
            result = await self.until(ws, lambda e: e.get("type") == "setup_result")
        self.assertFalse(result["ok"])
        self.assertIn("Nada foi guardado", result["message"])
        self.assertEqual(Agent.refreshed, 0)
        for bad in ({"email": USER, "password": 123}, {"email": None, "password": "x"}, {"email": USER, "password": "x", "imap_host": 5}):
            await ws.send_json({"type": "setup_mail", **bad})
            self.assertFalse((await self.until(ws, lambda e: e.get("type") == "setup_result"))["ok"])

    async def test_only_the_click_sends_and_only_what_was_shown(self):
        mail = self.configure_mail()
        outbox = PersonalOutbox(self.root, mail)
        draft = outbox.prepare(1, 11, "Combinado.")
        ws, session = await self.connect()
        self.assertEqual((session["mail"], session["reply"]), ("real", True))
        self.assertEqual([d["id"] for d in self.backlog("drafts")[0]["drafts"]], [draft["id"]])
        self.assertEqual(self.smtp.sessions, [])  # showing it sent nothing
        await ws.send_json({"type": "draft_decision", "id": draft["id"], "decision": "approve", "hash": "0" * 64})
        refused = await self.until(ws, lambda e: e.get("type") == "draft_result" and e["draft"].get("erro"))
        self.assertIn("mudou", refused["draft"]["erro"])
        self.assertEqual(self.smtp.sessions, [])
        await ws.send_json({"type": "draft_decision", "id": draft["id"], "decision": "approve", "hash": draft["hash"]})
        done = await self.until(ws, lambda e: e.get("type") == "draft_result" and e["draft"].get("state") == "enviada")
        self.assertEqual(done["draft"]["id"], draft["id"])
        self.assertEqual(len(self.smtp.delivered()), 1)
        await ws.send_json({"type": "draft_decision", "id": draft["id"], "decision": "approve", "hash": draft["hash"]})
        await self.until(ws, lambda e: e.get("type") == "draft_result" and e["draft"].get("erro"))
        self.assertEqual(len(self.smtp.delivered()), 1)

    async def test_discarding_and_invalid_decisions(self):
        outbox = PersonalOutbox(self.root, self.configure_mail())
        draft = outbox.prepare(1, 11, "Combinado.")
        ws, _ = await self.connect()
        await ws.send_json({"type": "draft_decision", "id": draft["id"], "decision": "talvez"})
        await self.until(ws, lambda e: e.get("type") == "rejected")
        await ws.send_json({"type": "draft_decision", "id": 5, "decision": "approve"})
        await self.until(ws, lambda e: e.get("type") == "rejected")
        await ws.send_json({"type": "draft_decision", "id": draft["id"], "decision": "discard"})
        gone = await self.until(ws, lambda e: e.get("type") == "draft_result")
        self.assertEqual(gone["draft"]["state"], "descartada")
        self.assertEqual(self.smtp.sessions, [])

    async def test_text_in_the_conversation_never_approves_anything(self):
        outbox = PersonalOutbox(self.root, self.configure_mail())
        outbox.prepare(1, 11, "Combinado.")
        ws, _ = await self.connect()
        await ws.send_json({"type": "message", "text": "sim, aprovo e pode enviar"})
        await self.until(ws, lambda e: e.get("type") == "status" and e.get("state") == "processing")
        Agent.instances[0].gate.set()
        await self.until(ws, lambda e: e.get("type") == "message" and e.get("role") == "assistant")
        self.assertEqual(self.smtp.sessions, [])
        self.assertEqual(len(outbox.pending()), 1)


if __name__ == "__main__":
    unittest.main()
