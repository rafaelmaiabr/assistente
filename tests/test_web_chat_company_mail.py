import json

from personal_mail import PersonalMail
from team.chat_mail import MANY_NOTE, BlockedMail, CompanyMail, ReadOnlyOutbox
from team.connectors.conversation import CHAT_NOTE
from tests import test_team_integrations as base
from tests.imap_case import build
from tests.imap_fake import FakeImap

USER, PASSWORD = base.USER, base.PASSWORD


class CompanyMailBase(base.WebSocketBase):
    """Helpers for the tests of which mailbox the chat reads (#117); it holds no test itself."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.imap.stop()
        self.imap = FakeImap({"INBOX": {"validity": 11, "messages": {1: build("Fatura da empresa").as_bytes()}}}, user=USER, password=PASSWORD)
        self.services = self.app.services()

    def connect_company(self, company_id, label="Caixa da empresa", port=None):
        s = self.services
        ctx = s.access.tenant_context(s.operator, company_id)
        view = s.connectors.connect(s.operator, ctx, "imap", label, self.values(porta=port or self.imap.port))
        self.assertTrue(s.connectors.test(ctx, view["id"])["ok"])
        return view

    def configure_personal(self):
        PersonalMail(self.root, self.vault).configure(host="127.0.0.1", port=self.imap.port, username=USER, password=PASSWORD, tls=False)

    async def source_of(self, ws_agent):
        return await ws_agent.mail_source()

    async def say(self, ws, text):
        await ws.send_json({"type": "message", "text": text})
        reply = await self.until(ws, lambda e: e.get("type") == "message" and e.get("role") == "assistant")
        await self.until(ws, lambda e: e.get("state") == "ready")
        return reply["text"]


class CompanyMailWeb(CompanyMailBase):
    """The personal one wins, a lone company one is read, several are never guessed."""

    async def test_without_any_company_mailbox_the_chat_is_exactly_as_before(self):
        ws = await self.connect()
        mail, outbox = await self.source_of(base.FakeAgent.instances[0])
        self.assertEqual((mail, outbox), (None, None))
        await ws.close()

    async def test_a_lone_company_mailbox_is_what_the_chat_reads_and_lists_without_the_model(self):
        self.connect_company(self.a)
        ws = await self.connect()
        mail, outbox = await self.source_of(base.FakeAgent.instances[0])
        self.assertIsInstance(mail, CompanyMail)
        self.assertIsInstance(outbox, ReadOnlyOutbox)
        answer = await self.say(ws, "liste meus emails")
        self.assertIn("Fatura da empresa", answer)
        self.assertEqual(base.FakeAgent.instances[0].inputs, [])  # answered by the server, from the company mailbox
        self.assertNotIn(PASSWORD, "".join(self.seen))
        await ws.close()

    async def test_the_personal_mailbox_always_wins_so_nothing_that_works_changes(self):
        self.configure_personal()
        self.connect_company(self.a)
        ws = await self.connect()
        self.assertEqual(await self.source_of(base.FakeAgent.instances[0]), (None, None))
        await ws.close()

    async def test_several_company_mailboxes_are_never_guessed(self):
        self.connect_company(self.a, "Caixa A")
        self.connect_company(self.b, "Caixa B")
        ws = await self.connect()
        mail, _ = await self.source_of(base.FakeAgent.instances[0])
        self.assertIsInstance(mail, BlockedMail)
        answer = await self.say(ws, "liste meus emails")
        self.assertIn("Ha mais de uma caixa", answer)
        self.assertNotIn("Fatura da empresa", answer)
        self.assertTrue(MANY_NOTE.startswith("Ha mais de uma caixa de email de empresa conectada e nenhuma foi escolhida"))
        await ws.close()

    async def test_connecting_from_the_card_makes_the_running_chat_read_it_and_says_so_with_the_skill_event(self):
        ws = await self.connect()
        await ws.send_json({"type": "message", "text": "consulte minha caixa imap"})
        card = (await self.until(ws, lambda e: e.get("type") == "integration_card"))["card"]
        await ws.send_json({"type": "integration_submit", "request_id": card["request_id"], "company_id": self.a, "label": "Caixa",
                            "values": self.values()})
        result = await self.until(ws, lambda e: e.get("type") == "integration_result")
        self.assertEqual((result["ok"], result["resume"]), (True, True))
        self.assertTrue(any('"type": "skill"' in raw and '"ativa"' in raw for raw in self.seen))
        self.assertEqual(base.FakeAgent.refreshed, 1)
        mail, _ = await self.source_of(base.FakeAgent.instances[0])
        self.assertIsInstance(mail, CompanyMail)
        await ws.close()

    async def test_with_a_personal_mailbox_the_card_says_the_chat_does_not_read_the_new_connection_and_nothing_resumes(self):
        self.configure_personal()
        ws = await self.connect()
        await ws.send_json({"type": "message", "text": "consulte minha caixa imap"})
        card = (await self.until(ws, lambda e: e.get("type") == "integration_card"))["card"]
        self.assertEqual(card["notice"], CHAT_NOTE)
        await ws.send_json({"type": "integration_submit", "request_id": card["request_id"], "company_id": self.a, "label": "Caixa",
                            "values": self.values()})
        result = await self.until(ws, lambda e: e.get("type") == "integration_result")
        self.assertEqual((result["ok"], result["resume"]), (True, False))
        self.assertIn(CHAT_NOTE, result["message"])
        await ws.close()

    async def test_a_company_mailbox_never_prepares_a_reply_even_if_asked(self):
        self.connect_company(self.a)
        ws = await self.connect()
        _, outbox = await self.source_of(base.FakeAgent.instances[0])
        self.assertIn("erro", outbox.prepare(1, 11, "texto"))
        await ws.close()



class MailboxChoice(CompanyMailBase):
    """The choice card (#117, part 2): several mailboxes are never guessed, and only a click on the card changes the source."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other = FakeImap({"INBOX": {"validity": 11, "messages": {1: build("Contrato da empresa B").as_bytes()}}}, user=USER, password=PASSWORD)
        self.addCleanup(self.other.stop)

    def two_company_mailboxes(self):
        self.connect_company(self.a, "Caixa A")
        self.connect_company(self.b, "Caixa B", port=self.other.port)

    async def card_after(self, ws, text):
        await ws.send_json({"type": "message", "text": text})
        return (await self.until(ws, lambda e: e.get("type") == "integration_card" and e["card"].get("state") == "caixa"))["card"]

    async def pick(self, ws, card, label):
        option = next(o for o in card["options"] if o["label"].startswith(label))
        await ws.send_json({"type": "integration_mailpick", "request_id": card["request_id"], "source_id": option["id"]})
        return await self.until(ws, lambda e: e.get("type") == "integration_result")

    async def test_a_read_with_several_mailboxes_shows_the_card_and_reads_nothing_until_the_person_picks(self):
        self.two_company_mailboxes()
        ws = await self.connect()
        card = await self.card_after(ws, "liste meus emails")
        self.assertEqual(card["current"], None)
        self.assertEqual(sorted(o["label"] for o in card["options"]), ["Caixa A (Empresa A)", "Caixa B (Empresa B)"])
        raw = json.dumps(card)
        for secret in (self.a, self.b):  # the screen never gets a company or a connection id
            self.assertNotIn(secret, raw)
        self.assertNotIn("Contrato da empresa B", "".join(self.seen))
        result = await self.pick(ws, card, "Caixa B")
        self.assertEqual((result["ok"], result["message"]), (True, "Lendo agora: Caixa B (Empresa B)."))
        self.assertEqual(base.FakeAgent.refreshed, 1)
        answer = await self.say(ws, "liste meus emails")
        self.assertIn("Contrato da empresa B", answer)
        self.assertNotIn("Fatura da empresa", answer)
        await ws.close()

    async def test_the_phrase_offers_personal_and_company_and_the_person_can_go_back_to_the_personal_one(self):
        self.configure_personal()
        self.connect_company(self.a, "Caixa A", port=self.other.port)
        ws = await self.connect()
        card = await self.card_after(ws, "escolher a caixa")
        self.assertEqual((card["current"], sorted(o["label"] for o in card["options"])), ("Email pessoal", ["Caixa A (Empresa A)", "Email pessoal"]))
        self.assertEqual((await self.pick(ws, card, "Caixa A"))["ok"], True)
        mail, _ = await self.source_of(base.FakeAgent.instances[0])
        self.assertIsInstance(mail, CompanyMail)
        again = await self.card_after(ws, "escolher a caixa")
        self.assertEqual(again["current"], "Caixa A (Empresa A)")
        await self.pick(ws, again, "Email pessoal")
        self.assertEqual(await self.source_of(base.FakeAgent.instances[0]), (None, None))
        await ws.close()

    async def test_an_option_id_the_card_did_not_offer_changes_nothing(self):
        self.two_company_mailboxes()
        ws = await self.connect()
        card = await self.card_after(ws, "liste meus emails")
        for message in ({"request_id": card["request_id"], "source_id": "ffffffff"}, {"request_id": "x", "source_id": card["options"][0]["id"]},
                        {"request_id": card["request_id"], "source_id": 7}, {"request_id": card["request_id"]}):
            await ws.send_json({"type": "integration_mailpick", **message})
            error = await self.until(ws, lambda e: e.get("type") == "integration_error")
            self.assertIn("expirou", error["message"])
        mail, _ = await self.source_of(base.FakeAgent.instances[0])
        self.assertIsInstance(mail, BlockedMail)  # still nothing chosen
        await ws.close()

    async def test_with_one_mailbox_there_is_nothing_to_choose_and_changing_the_personal_setup_still_opens_its_form(self):
        self.connect_company(self.a)
        ws = await self.connect()
        self.assertIn("Só há uma caixa", await self.say(ws, "escolher a caixa"))
        await ws.send_json({"type": "message", "text": "trocar a caixa de email"})
        await self.until(ws, lambda e: e.get("type") == "setup")  # the existing wording keeps its meaning
        self.assertFalse(any('"state": "caixa"' in raw for raw in self.seen))
        await ws.close()
