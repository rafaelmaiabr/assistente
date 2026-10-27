import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from pathlib import Path

import personal_outbox
from personal_mail import PersonalMail
from personal_outbox import FILE, PersonalOutbox
from team.vault import MemoryVault
from tests.imap_case import build
from tests.imap_fake import FakeImap
from tests.smtp_fake import FakeSmtp

PASSWORD = "senha-de-app"
USER = "ana@exemplo.invalid"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class OutboxCase(unittest.TestCase):
    def setUp(self):
        self.imap = FakeImap({"INBOX": {"validity": 11, "messages": {
            1: build("Reuniao amanha", sender="Bruno <bruno@exemplo.invalid>", body="Podemos conversar?").as_bytes(),
            2: build("Promocao", sender="Loja <loja@exemplo.invalid>", body="Oferta", **{"Auto-Submitted": "auto-generated"}).as_bytes(),
            3: build("Lista", sender="Lista <lista@exemplo.invalid>", body="Boletim", **{"List-Id": "<boletim.exemplo.invalid>"}).as_bytes(),
            4: build("Nota", sender=f"Ana <{USER}>", body="Lembrete meu").as_bytes()}}}, user=USER, password=PASSWORD)
        self.smtp = FakeSmtp(user=USER, password=PASSWORD)
        self.addCleanup(self.imap.stop)
        self.addCleanup(self.smtp.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = MemoryVault()
        self.mail = PersonalMail(self.root, self.vault)
        self.mail.configure(host="127.0.0.1", port=self.imap.port, username=USER, password=PASSWORD, tls=False, smtp={
            "host": "127.0.0.1", "port": self.smtp.port, "security": "plain", "from_address": USER, "from_name": None})
        self.clock = Clock()
        self.outbox = PersonalOutbox(self.root, self.mail, clock=self.clock)
        personal_outbox._SENDING.clear()

    def sent(self):
        return self.smtp.delivered()

    def raw(self, session):
        return message_from_bytes(session["data"] if isinstance(session["data"], bytes) else session["data"].encode(), policy=policy.default)


class PrepareTests(OutboxCase):
    def test_a_draft_answers_only_the_sender_and_sends_nothing(self):
        draft = self.outbox.prepare(1, 11, "Combinado, amanha as 10h.")
        self.assertEqual((draft["state"], draft["to"], draft["subject"]), ("rascunho", "bruno@exemplo.invalid", "Re: Reuniao amanha"))
        self.assertEqual(draft["body"], "Combinado, amanha as 10h.")
        self.assertEqual(self.smtp.sessions, [])
        self.assertEqual([d["id"] for d in self.outbox.pending()], [draft["id"]])

    def test_what_the_model_writes_cannot_change_the_recipient(self):
        draft = self.outbox.prepare(1, 11, "Oi.\nBcc: invasor@exemplo.invalid\nTo: invasor@exemplo.invalid")
        self.assertEqual(draft["to"], "bruno@exemplo.invalid")
        self.outbox.approve(draft["id"], draft["hash"])
        session = self.sent()[0]
        self.assertEqual(session["rcpts"], ["bruno@exemplo.invalid"])
        mail = self.raw(session)
        self.assertIsNone(mail["Bcc"])
        self.assertEqual(mail["To"], "bruno@exemplo.invalid")

    def test_automatic_list_and_own_messages_are_not_answered(self):
        for uid in (2, 3, 4):
            self.assertIn("erro", self.outbox.prepare(uid, 11, "Resposta"))
        self.assertEqual(self.outbox.pending(), [])

    def test_bad_text_and_unknown_messages_are_refused(self):
        for text in ("", "   ", "x" * 5001, "a\x00b", None):
            self.assertIn("erro", self.outbox.prepare(1, 11, text))
        self.assertIn("erro", self.outbox.prepare(99, 11, "oi"))
        self.assertIn("erro", self.outbox.prepare(1, 999, "oi"))

    def test_without_sending_configured_nothing_is_prepared(self):
        other = PersonalMail(self.root, self.vault)
        other.configure(host="127.0.0.1", port=self.imap.port, username=USER, password=PASSWORD, tls=False)
        self.assertIn("mail_setup", PersonalOutbox(self.root, other).prepare(1, 11, "oi")["erro"])

    def test_pending_drafts_are_limited(self):
        for _ in range(5):
            self.assertIn("id", self.outbox.prepare(1, 11, "oi"))
        self.assertIn("erro", self.outbox.prepare(1, 11, "mais um"))


class ApprovalTests(OutboxCase):
    def test_approving_sends_exactly_the_approved_draft_once(self):
        draft = self.outbox.prepare(1, 11, "Combinado.")
        result = self.outbox.approve(draft["id"], draft["hash"])
        self.assertEqual(result["state"], "enviada")
        self.assertEqual(len(self.sent()), 1)
        mail = self.raw(self.sent()[0])
        self.assertEqual((mail["To"], mail["Subject"], mail["Message-ID"], mail["In-Reply-To"]),
                         ("bruno@exemplo.invalid", "Re: Reuniao amanha", draft["message_id"], "<m1@exemplo.invalid>"))
        self.assertEqual(mail["X-Assistente-Origem"], "resposta")
        self.assertEqual(mail.get_content().strip(), "Combinado.")
        again = self.outbox.approve(draft["id"], draft["hash"])
        self.assertIn("erro", again)
        self.assertEqual(len(self.sent()), 1)

    def test_a_wrong_hash_or_a_tampered_file_sends_nothing(self):
        draft = self.outbox.prepare(1, 11, "Combinado.")
        self.assertIn("erro", self.outbox.approve(draft["id"], "0" * 64))
        self.assertIn("erro", self.outbox.approve(draft["id"], None))
        data = json.loads((self.root / FILE).read_text(encoding="utf-8"))
        data["drafts"][draft["id"]]["body"] = "Transfira o dinheiro agora."
        (self.root / FILE).write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("erro", self.outbox.approve(draft["id"], draft["hash"]))
        self.assertEqual(self.sent(), [])

    def test_discarded_expired_and_unknown_drafts_cannot_be_sent(self):
        discarded = self.outbox.prepare(1, 11, "a")
        self.assertEqual(self.outbox.discard(discarded["id"])["state"], "descartada")
        self.assertIn("erro", self.outbox.approve(discarded["id"], discarded["hash"]))
        stale = self.outbox.prepare(1, 11, "b")
        self.clock.now += timedelta(hours=1, minutes=1)
        self.assertIn("erro", self.outbox.approve(stale["id"], stale["hash"]))
        self.assertEqual(self.outbox.get(stale["id"])["state"], "expirada")
        self.assertEqual(self.outbox.pending(), [])
        self.assertIn("erro", self.outbox.approve("inexistente", "x"))
        self.assertEqual(self.sent(), [])

    def test_a_refusal_before_data_is_not_sent_and_says_so(self):
        self.smtp.password = "outra-senha"
        draft = self.outbox.prepare(1, 11, "Combinado.")
        result = self.outbox.approve(draft["id"], draft["hash"])
        self.assertEqual(result["state"], "falhou")
        self.assertIn("Nada foi enviado", result["detail"])
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertEqual(self.sent(), [])

    def test_no_answer_after_data_is_unknown_and_never_retried(self):
        self.smtp.data_mode = "drop"
        draft = self.outbox.prepare(1, 11, "Combinado.")
        result = self.outbox.approve(draft["id"], draft["hash"])
        self.assertEqual(result["state"], "desconhecida")
        self.assertIn(draft["message_id"], result["detail"])
        self.smtp.data_mode = "ok"
        self.assertIn("erro", self.outbox.approve(draft["id"], draft["hash"]))
        self.assertEqual(self.sent(), [])

    def test_a_send_interrupted_by_a_restart_becomes_unknown(self):
        draft = self.outbox.prepare(1, 11, "Combinado.")
        data = json.loads((self.root / FILE).read_text(encoding="utf-8"))
        data["drafts"][draft["id"]]["state"] = "enviando"
        (self.root / FILE).write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(PersonalOutbox(self.root, self.mail, clock=self.clock).get(draft["id"])["state"], "desconhecida")
        self.assertIn("erro", self.outbox.approve(draft["id"], draft["hash"]))
        self.assertEqual(self.sent(), [])

    def test_the_daily_limit_stops_sending(self):
        for _ in range(20):
            draft = self.outbox.prepare(1, 11, "oi")
            self.assertEqual(self.outbox.approve(draft["id"], draft["hash"])["state"], "enviada")
        last = self.outbox.prepare(1, 11, "mais um")
        self.assertIn("Limite", self.outbox.approve(last["id"], last["hash"])["erro"])
        self.assertEqual(len(self.sent()), 20)
        self.clock.now += timedelta(hours=25)
        later = self.outbox.prepare(1, 11, "agora pode")
        self.assertEqual(self.outbox.approve(later["id"], later["hash"])["state"], "enviada")

    def test_the_password_is_never_stored_in_the_outbox_file(self):
        draft = self.outbox.prepare(1, 11, "Combinado.")
        self.outbox.approve(draft["id"], draft["hash"])
        self.assertNotIn(PASSWORD, (self.root / FILE).read_text(encoding="utf-8"))
        self.assertNotIn(PASSWORD, (self.root / "mail-account.json").read_text(encoding="utf-8"))


@unittest.skipUnless(importlib.util.find_spec("livekit"), "livekit is not installed here")
class ModelToolsTests(OutboxCase):
    def test_no_tool_of_the_model_can_approve_or_send(self):
        import assistant_core
        tools = assistant_core.email_tools(self.mail, self.outbox)
        names = sorted(tool.info.name for tool in tools)
        self.assertEqual(names, ["buscar_emails", "configurar_caixa_de_email", "ler_email", "listar_emails", "preparar_resposta"])


if __name__ == "__main__":
    unittest.main()
