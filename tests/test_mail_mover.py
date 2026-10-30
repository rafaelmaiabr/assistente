import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import mail_mover as mm
from mail_mover import FILE, ImapMover, MoveService, PROPOSAL_TTL, parse_request, render
from personal_mail import PersonalMail
from team.imap_client import ImapConfig, ImapError
from team.vault import MemoryVault
from tests.imap_fake_rw import FakeImapRW

USER, PASSWORD = "ana@exemplo.invalid", "senha-de-app"
SMTP = {"host": "127.0.0.1", "port": 25, "security": "plain", "from_address": USER, "from_name": None}
ALLOWED = {"CAPABILITY", "LOGIN", "LOGOUT", "LIST", "EXAMINE", "SELECT", "UID SEARCH", "UID FETCH", "UID MOVE", "UID COPY", "UID STORE", "UID EXPUNGE"}


def raw(number, subject=None, sender="Golpe <golpe@exemplo.invalid>"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, USER, subject or f"Assunto secreto {number}"
    msg["Date"], msg["Message-ID"] = "Mon, 01 Jun 2026 10:00:00 +0000", f"<msg{number}@exemplo.invalid>"
    msg.set_content(f"Corpo confidencial {number}")
    return msg.as_bytes()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Case(unittest.TestCase):
    capabilities = ("MOVE", "UIDPLUS")
    special_use = True

    def setUp(self):
        self.imap = FakeImapRW({"INBOX": {"validity": 11, "messages": {1: raw(1), 2: raw(2), 3: raw(3), 4: raw(4)}},
                                "Trash": {"validity": 21, "messages": {}}, "Junk": {"validity": 31, "messages": {}}},
                               user=USER, password=PASSWORD, capabilities=self.capabilities, special_use=self.special_use)
        self.addCleanup(self.imap.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.mail = PersonalMail(self.root, MemoryVault())
        self.mail.configure(host="127.0.0.1", port=self.imap.port, username=USER, password=PASSWORD, tls=False, smtp=SMTP)
        self.clock = Clock()
        self.service = MoveService(self.root, clock=self.clock)

    def inbox(self):
        return sorted(self.imap.folders["INBOX"]["messages"])

    def folder(self, name):
        return sorted(self.imap.folders[name]["messages"])

    def propose(self, uids=(1,), destination="lixeira"):
        result = self.service.propose(self.mail, uids, destination)
        self.assertTrue(result["ok"], result)
        return result["proposal"]

    def approve(self, proposal, presented=None):
        return self.service.approve(self.mail, proposal["id"], proposal["hash"] if presented is None else presented)


class Proposal(Case):
    def test_a_proposal_moves_nothing_and_writes_nothing_to_the_server(self):
        proposal = self.propose([1, 2], "spam")
        self.assertEqual((self.inbox(), self.folder("Junk")), ([1, 2, 3, 4], []))
        self.assertEqual(self.imap.writes, 0)
        self.assertNotIn("SELECT", self.imap.trace)  # read-only EXAMINE only
        self.assertEqual((proposal["destino_pasta"], proposal["pasta"], [i["uid"] for i in proposal["itens"]]), ("Junk", "INBOX", [1, 2]))
        self.assertEqual(proposal["itens"][0]["message_id"], "<msg1@exemplo.invalid>")
        self.assertEqual(len(proposal["hash"]), 64)
        self.assertEqual([p["id"] for p in self.service.pending()], [proposal["id"]])

    def test_the_destination_is_found_by_its_special_use_attribute_or_by_a_well_known_name_and_never_created(self):
        self.assertEqual(self.propose(destination="lixeira")["destino_pasta"], "Trash")
        self.imap.special_use = False
        self.assertEqual(self.propose(destination="spam")["destino_pasta"], "Junk")  # by name
        del self.imap.folders["Junk"]
        refused = self.service.propose(self.mail, [1], "spam")
        self.assertEqual((refused["ok"], refused["motivo"]), (False, "destino_inexistente"))
        self.assertEqual(sorted(self.imap.folders), ["INBOX", "Trash"])  # nothing was created

    def test_a_message_that_does_not_exist_or_a_bad_request_is_refused_as_a_whole(self):
        self.assertEqual(self.service.propose(self.mail, [1, 99], "lixeira")["motivo"], "nao_encontrada")
        self.assertEqual(self.service.propose(self.mail, [], "lixeira")["motivo"], "lote")
        self.assertEqual(self.service.propose(self.mail, list(range(1, 30)), "lixeira")["motivo"], "lote")
        self.assertEqual(self.service.propose(self.mail, [1], "arquivo")["ok"], False)
        self.assertEqual(self.service.pending(), [])

    def test_a_read_only_mailbox_and_a_company_mailbox_are_never_moved(self):
        self.mail.configure(host="127.0.0.1", port=self.imap.port, username=USER, password=PASSWORD, tls=False, smtp=None)  # set up as read-only
        refused = self.service.propose(self.mail, [1], "lixeira")
        self.assertEqual((refused["ok"], refused["motivo"]), (False, "somente_leitura"))
        company = type("Company", (), {})()  # a company mailbox has no write side at all
        refused = self.service.propose(company, [1], "lixeira")
        self.assertEqual((refused["ok"], refused["motivo"]), (False, "somente_leitura"))
        self.assertEqual(self.imap.writes, 0)


class Approval(Case):
    def test_without_the_click_or_with_another_hash_nothing_moves(self):
        proposal = self.propose([1, 2])
        self.assertEqual(self.inbox(), [1, 2, 3, 4])  # the proposal alone moved nothing
        altered = self.approve(proposal, presented="0" * 64)
        self.assertEqual((altered["ok"], altered["estado"]), (False, "alterada"))
        self.assertEqual((self.inbox(), self.imap.writes), ([1, 2, 3, 4], 0))
        self.assertEqual(self.service.pending(), [])  # a tampered approval spends the proposal

    def test_an_unknown_used_or_expired_proposal_moves_nothing(self):
        self.assertEqual(self.service.approve(self.mail, "m-nao-existe", "x")["estado"], "inexistente")
        proposal = self.propose([1])
        self.assertTrue(self.approve(proposal)["ok"])
        self.assertEqual(self.approve(proposal)["estado"], "inexistente")  # single use
        old = self.propose([2])
        self.clock.now += PROPOSAL_TTL + timedelta(seconds=1)
        self.assertEqual(self.approve(old)["estado"], "expirada")
        self.assertEqual(self.inbox(), [2, 3, 4])

    def test_a_proposal_can_be_discarded(self):
        proposal = self.propose([1])
        self.assertEqual(self.service.discard(proposal["id"])["estado"], "descartada")
        self.assertEqual(self.approve(proposal)["estado"], "inexistente")
        self.assertEqual(self.inbox(), [1, 2, 3, 4])


class Moving(Case):
    def test_moving_one_message_confirms_origin_and_destination_and_gives_the_new_uid(self):
        result = self.approve(self.propose([2], "lixeira"))
        self.assertTrue(result["ok"])
        self.assertEqual([(r["uid"], r["estado"]) for r in result["resultados"]], [(2, "movida")])
        self.assertIsNotNone(result["resultados"][0]["novo_uid"])
        self.assertEqual((self.inbox(), self.folder("Trash")), ([1, 3, 4], [result["resultados"][0]["novo_uid"]]))
        self.assertIn("Movi 1 mensagem", render(result))

    def test_moving_a_batch(self):
        result = self.approve(self.propose([1, 3, 4], "spam"))
        self.assertEqual([r["estado"] for r in result["resultados"]], ["movida"] * 3)
        self.assertEqual((self.inbox(), len(self.folder("Junk"))), ([2], 3))
        self.assertEqual(result["movidas"], 3)

    def test_the_journal_records_before_and_after_per_message_without_content(self):
        self.approve(self.propose([1, 2]))
        journal = self.service.journal()
        self.assertEqual([(e["fase"], e["uid"]) for e in journal], [("antes", 1), ("depois", 1), ("antes", 2), ("depois", 2)])
        self.assertEqual(journal[1]["estado"], "movida")
        text = json.dumps(journal) + (self.root / FILE).read_text(encoding="utf-8")
        for secret in ("Assunto secreto", "Corpo confidencial", "golpe@exemplo.invalid"):
            self.assertNotIn(secret, text)  # ids and folders only: no subject, sender or body once the proposal is spent
        self.assertEqual({"antes", "depois"}, {e["fase"] for e in journal})

    def test_only_the_approved_messages_move_and_the_rest_stay_untouched(self):
        self.approve(self.propose([2]))
        self.assertEqual(self.inbox(), [1, 3, 4])
        self.assertEqual(self.imap.seen, set())  # headers were read with BODY.PEEK: nothing was marked as read

    def test_the_whole_vocabulary_is_the_closed_list_and_there_is_no_general_expunge(self):
        self.approve(self.propose([1, 2], "lixeira"))
        self.assertLessEqual(set(self.imap.commands()), ALLOWED)
        self.assertNotIn("EXPUNGE", self.imap.commands())  # the bare command, which expunges everything marked
        self.assertEqual(self.imap.general_expunge, 0)

    def test_the_client_cannot_even_be_made_to_send_a_general_expunge_or_a_delete(self):
        config = ImapConfig(host="127.0.0.1", port=self.imap.port, username=USER, folders=("INBOX",), tls=False)
        with ImapMover(config, PASSWORD) as mover:
            for label in ("EXPUNGE", "DELETE", "APPEND", "CREATE", "STORE", "UID STORE ", "RENAME"):
                with self.assertRaises(ImapError, msg=label):
                    mover._write(label, mover._conn.expunge)
        self.assertEqual(self.imap.general_expunge, 0)
        self.assertEqual(self.imap.writes, 0)


class WithoutMove(Case):
    capabilities = ("UIDPLUS",)

    def test_without_move_it_copies_verifies_and_removes_only_that_uid(self):
        result = self.approve(self.propose([1, 2]))
        self.assertEqual([r["estado"] for r in result["resultados"]], ["movida", "movida"])
        self.assertEqual((self.inbox(), len(self.folder("Trash"))), ([3, 4], 2))
        commands = self.imap.commands()
        for needed in ("UID COPY", "UID STORE", "UID EXPUNGE"):
            self.assertIn(needed, commands)
        self.assertNotIn("EXPUNGE", commands)
        self.assertEqual(self.imap.general_expunge, 0)

    def test_if_the_copy_cannot_be_confirmed_the_original_is_kept(self):
        proposal = self.propose([1])
        self.imap.folders["Trash"]["validity"] = 21
        original_put = self.imap.put
        self.imap.put = lambda folder, data: original_put("Junk", data)  # the copy "lands" somewhere else, so it is not at the destination
        result = self.approve(proposal)
        self.assertEqual((result["estado"], result["motivo_da_parada"]), ("parada", "copia_nao_verificada"))
        self.assertIn(1, self.inbox())  # nothing was removed from the source
        self.assertNotIn("UID STORE", self.imap.commands())


class WithNeither(Case):
    capabilities = ()

    def test_without_move_or_uidplus_it_refuses_instead_of_improvising(self):
        result = self.approve(self.propose([1, 2]))
        self.assertEqual((result["estado"], result["motivo_da_parada"]), ("parada", "sem_suporte"))
        self.assertEqual(self.inbox(), [1, 2, 3, 4])
        self.assertEqual(self.imap.general_expunge, 0)
        self.assertEqual([r["estado"] for r in result["resultados"]], ["parou", "nao_executada"])


class WhenSomethingGoesWrong(Case):
    def test_a_failure_in_the_middle_stops_the_batch_and_is_not_repeated(self):
        proposal = self.propose([1, 2, 3])
        self.imap.fail_on = 2  # the second write command fails
        result = self.approve(proposal)
        self.assertEqual([r["estado"] for r in result["resultados"]], ["movida", "falhou", "nao_executada"])
        self.assertEqual((result["estado"], self.imap.writes), ("parada", 2))  # no retry of the failed command, none of the third
        self.assertEqual(self.inbox(), [2, 3, 4])
        self.assertIn("Parei o lote", render(result))

    def test_a_timeout_is_never_repeated_and_the_state_is_unknown(self):
        proposal = self.propose([1, 2])
        self.imap.hang_on = 1
        config = ImapConfig(host="127.0.0.1", port=self.imap.port, username=USER, folders=("INBOX",), tls=False, timeout=1.0)
        self.mail.open_mover = lambda: ImapMover(config, PASSWORD)
        result = self.approve(proposal)
        self.assertEqual([r["estado"] for r in result["resultados"]], ["desconhecida", "nao_executada"])
        self.assertEqual((result["motivo_da_parada"], self.imap.writes), ("tempo_esgotado", 1))
        self.assertIn("confira a pasta", render(result))

    def test_if_uidvalidity_changed_nothing_is_moved(self):
        proposal = self.propose([1, 2])
        self.imap.folders["INBOX"]["validity"] = 99  # the server reorganised the folder
        result = self.approve(proposal)
        self.assertEqual((result["estado"], result["motivo_da_parada"]), ("parada", "uidvalidity_mudou"))
        self.assertEqual((self.inbox(), self.imap.writes), ([1, 2, 3, 4], 0))

    def test_a_message_already_moved_is_reported_and_the_batch_goes_on(self):
        proposal = self.propose([1, 2])
        self.imap.put("Trash", self.imap.folders["INBOX"]["messages"].pop(1))  # someone moved it meanwhile
        result = self.approve(proposal)
        self.assertEqual([r["estado"] for r in result["resultados"]], ["ja_movida", "movida"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.folder("Trash")), 2)

    def test_a_message_that_is_not_the_approved_one_stops_the_batch(self):
        proposal = self.propose([1, 2])
        self.imap.folders["INBOX"]["messages"][1] = raw(77)  # the same UID now holds another message
        result = self.approve(proposal)
        self.assertEqual((result["estado"], result["motivo_da_parada"]), ("parada", "mudou"))
        self.assertEqual((self.inbox(), self.folder("Trash")), ([1, 2, 3, 4], []))

    def test_a_message_that_vanished_stops_the_batch(self):
        proposal = self.propose([1, 2])
        del self.imap.folders["INBOX"]["messages"][1]
        result = self.approve(proposal)
        self.assertEqual((result["estado"], result["motivo_da_parada"]), ("parada", "nao_encontrada"))
        self.assertEqual(self.inbox(), [2, 3, 4])

    def test_a_server_that_says_ok_but_leaves_the_message_is_caught_by_the_check_after(self):
        proposal = self.propose([1])
        self.imap.keep_on_move = True  # MOVE copies to the destination but the original stays in the source
        result = self.approve(proposal)
        self.assertEqual((result["estado"], result["motivo_da_parada"]), ("parada", "verificacao_origem"))


class PlainRequests(unittest.TestCase):
    def test_short_clear_requests_are_recognised(self):
        cases = {
            "Mova o email 12 para a lixeira": ([12], False, "lixeira"),
            "mova os emails 12, 15 e 18 para o spam": ([12, 15, 18], False, "spam"),
            "Mova os suspeitos para o spam": (None, True, "spam"),
            "Por favor, mova os suspeitos para a lixeira": (None, True, "lixeira"),
            "mova o email 7 para o lixo eletrônico": ([7], False, "spam"),
        }
        for text, (uids, suspects, destination) in cases.items():
            with self.subTest(text):
                self.assertEqual(parse_request(text), {"uids": uids, "suspeitos": suspects, "destino": destination})

    def test_everything_else_is_not_a_request_to_move(self):
        for text in ("Apague o email 12", "Mova o email 12 para a pasta Projetos", "mova tudo para o spam", "Mover emails para o spam?",
                     "Liste meus emails", "", None):
            with self.subTest(text):
                self.assertIsNone(parse_request(text))


class WhereItStays(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_the_journal_is_out_of_git_and_the_package_and_the_team_never_touches_it(self):
        self.assertIn("movimentos-de-email.json", (self.ROOT / ".gitignore").read_text(encoding="utf-8"))
        build = (self.ROOT / "desktop" / "build.cjs").read_text(encoding="utf-8")
        self.assertIn("movimentos-de-email", build.split("PRIVATE_NAMES=")[1].split("\n")[0])
        for source in (self.ROOT / "team").rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            self.assertNotIn("mail_mover", text, source.name)
            self.assertNotIn("movimentos-de-email", text, source.name)

    def test_the_read_only_client_still_has_no_write_command_and_this_module_logs_nothing(self):
        client = (self.ROOT / "team" / "imap_client.py").read_text(encoding="utf-8")
        for word in ("UID MOVE", "UID COPY", "UID STORE", "EXPUNGE\"", "APPEND"):
            self.assertNotIn(word, client)
        text = (self.ROOT / "mail_mover.py").read_text(encoding="utf-8")
        self.assertNotIn("logging", text)
        self.assertNotIn("print(", text)
        self.assertEqual(text.count('"EXPUNGE"'), 1)  # only as the argument of UID EXPUNGE, which carries one UID


if __name__ == "__main__":
    unittest.main()
