import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from email.message import EmailMessage

from team.imap_client import ImapConfig, ImapError, ReadOnlyImap, parse_message
from tests.imap_case import build
from tests.imap_fake import FakeImap

PASSWORD = "senha-de-app"




class ImapCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeImap({
            "INBOX": {"validity": 11, "messages": {
                1: build("Fatura de maio", body="Segue a fatura de maio.").as_bytes(),
                2: build("Reuniao", sender="Bruno <bruno@exemplo.invalid>", body="Vamos conversar amanha.", date="Tue, 02 Jun 2026 10:00:00 +0000").as_bytes(),
                3: build("Acentuação — ação", body="Olá, órgão público. ✓", date="Wed, 03 Jun 2026 10:00:00 +0000").as_bytes()}},
            "Segredo": {"validity": 5, "messages": {1: build("Confidencial").as_bytes()}}})
        self.addCleanup(self.server.stop)

    def client(self, folders=("INBOX",), password=PASSWORD, **kwargs):
        config = ImapConfig(host="127.0.0.1", port=self.server.port, username="ana@exemplo.invalid", folders=tuple(folders), tls=False, **kwargs)
        return ReadOnlyImap(config, password)


class ReadTests(ImapCase):
    def test_lists_pages_searches_and_reads_without_touching_flags(self):
        with self.client() as imap:
            page = imap.list_messages("INBOX", limit=2)
            self.assertEqual((page["uidvalidity"], [m["uid"] for m in page["messages"]], page["next_before_uid"]), (11, [3, 2], 2))
            self.assertEqual(page["messages"][1]["from"], "Bruno <bruno@exemplo.invalid>")
            rest = imap.list_messages("INBOX", limit=2, before_uid=page["next_before_uid"])
            self.assertEqual(([m["uid"] for m in rest["messages"]], rest["next_before_uid"]), ([1], None))
            self.assertEqual([m["uid"] for m in imap.search("INBOX", sender="bruno")["messages"]], [2])
            self.assertEqual([m["uid"] for m in imap.search("INBOX", text="fatura")["messages"]], [1])
            self.assertEqual([m["uid"] for m in imap.list_messages("INBOX", since="2026-06-02")["messages"]], [3, 2])
            full = imap.read_message("INBOX", 3, 11)
            self.assertEqual(full["subject"], "Acentuação — ação")
            self.assertIn("órgão público", full["text"])
            self.assertTrue(full["untrusted"])
        self.assertEqual(self.server.seen, set())  # BODY.PEEK only: nothing marked as read
        self.assertEqual(set(self.server.commands()) - {"CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}, set())

    def test_only_authorized_folders_and_valid_uids_are_accepted(self):
        with self.client() as imap:
            with self.assertRaises(ImapError) as raised:
                imap.list_messages("Segredo")
            self.assertEqual(raised.exception.code, "pasta_nao_autorizada")
            for bad in (0, -1, "1", True, None):
                with self.assertRaises(ImapError) as raised:
                    imap.read_message("INBOX", bad, 11)
                self.assertEqual(raised.exception.code, "uid_invalido")
            with self.assertRaises(ImapError) as raised:
                imap.read_message("INBOX", 99, 11)
            self.assertEqual(raised.exception.code, "uid_nao_encontrado")
            with self.assertRaises(ImapError) as raised:
                imap.read_message("INBOX", 1, 999)  # stale UIDVALIDITY
            self.assertEqual(raised.exception.code, "uidvalidity_mudou")
            with self.assertRaises(ImapError):
                imap.search("INBOX")
            with self.assertRaises(ImapError):
                imap.search("INBOX", text="a\r\nb LOGOUT")
        self.assertNotIn("Segredo", " ".join(self.server.log))

    def test_forbidden_commands_cannot_be_issued_through_the_wrapper(self):
        with self.client() as imap:
            for items in ("(RFC822)", "(BODY[])", "(FLAGS)", "(BODY.PEEK[] RFC822)", "BODY.PEEK[]", "(BODY.PEEK[]); STORE 1"):
                with self.assertRaises(ImapError) as raised:
                    imap._fetch(1, items)
                self.assertEqual(raised.exception.code, "comando_proibido", items)
            self.assertFalse([name for name in dir(imap) if name.lstrip("_").lower() in ("store", "delete", "expunge", "append", "copy", "move", "select")])
        self.assertEqual(self.server.seen, set())

    def test_large_messages_are_cut_at_the_byte_limit(self):
        self.server.folders["INBOX"]["messages"][4] = build("Grande", body="linha de texto\n" * 40000).as_bytes()
        with self.client(max_bytes=2048) as imap:
            message = imap.read_message("INBOX", 4, 11, max_bytes=10 ** 9)
        self.assertTrue(message["truncated"])
        self.assertGreater(message["size"], 2048)
        self.assertLessEqual(len(message["text"]), 2048)

    def test_page_size_is_capped(self):
        for uid in range(10, 80):
            self.server.folders["INBOX"]["messages"][uid] = build(f"M{uid}").as_bytes()
        with self.client() as imap:
            page = imap.list_messages("INBOX", limit=10 ** 6)
        self.assertEqual(len(page["messages"]), 50)
        self.assertIsNotNone(page["next_before_uid"])

    def test_lists_which_authorized_folders_exist(self):
        with self.client(folders=("INBOX", "Inexistente")) as imap:
            self.assertEqual(imap.list_folders(), [{"name": "INBOX", "present": True}, {"name": "Inexistente", "present": False}])


class ConnectionTests(ImapCase):
    def test_actionable_errors_never_echo_credentials(self):
        with self.assertRaises(ImapError) as raised:
            self.client(password="errada").connect()
        self.assertEqual(raised.exception.code, "autenticacao_recusada")
        self.assertNotIn("errada", raised.exception.message)
        self.assertIn("senha de aplicativo", raised.exception.message)

    def test_tls_is_required_and_a_plain_server_is_rejected_by_a_tls_client(self):
        with self.assertRaises(ImapError) as raised:
            ImapConfig(host="mail.exemplo.invalid", username="a", folders=("INBOX",), tls=False).validate()
        self.assertEqual(raised.exception.code, "tls_obrigatorio")
        config = ImapConfig(host="127.0.0.1", port=self.server.port, username="ana@exemplo.invalid", folders=("INBOX",), timeout=3)
        with self.assertRaises(ImapError) as raised:
            ReadOnlyImap(config, PASSWORD).connect()  # TLS client against a server that only speaks plain IMAP
        self.assertIn(raised.exception.code, ("tls_invalido", "sem_conexao"))

    def test_timeout_and_refused_connection(self):
        self.server.delay = 1.5
        with self.assertRaises(ImapError) as raised:
            self.client(timeout=0.3).connect()
        self.assertEqual(raised.exception.code, "tempo_esgotado")
        config = ImapConfig(host="127.0.0.1", port=1, username="a", folders=("INBOX",), tls=False, timeout=2)
        with self.assertRaises(ImapError) as raised:
            ReadOnlyImap(config, "x").connect()
        self.assertIn(raised.exception.code, ("sem_conexao", "tempo_esgotado"))  # Windows may time out instead of refusing

    def test_config_validation(self):
        base = dict(host="mail.exemplo.invalid", username="a", folders=("INBOX",))
        for change in (dict(host="bad host"), dict(host="x\r\ny"), dict(port=0), dict(port=70000), dict(username="a\r\nb"),
                       dict(folders=()), dict(folders=("a\r\nb",)), dict(folders=tuple(str(i) for i in range(21)))):
            with self.assertRaises(ImapError, msg=str(change)):
                ImapConfig(**{**base, **change}).validate()
        ImapConfig(**base).validate()


@unittest.skipUnless(shutil.which("openssl"), "openssl nao encontrado")
class TlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cert, key = Path(self.temp.name, "c.pem"), Path(self.temp.name, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(self.cert), "-days", "2",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, key)
        self.server = FakeImap({"INBOX": {"validity": 3, "messages": {1: build().as_bytes()}}}, ssl_context=context)
        self.addCleanup(self.server.stop)

    def config(self, **kwargs):
        return ImapConfig(host=kwargs.pop("host", "127.0.0.1"), port=self.server.port, username="ana@exemplo.invalid", folders=("INBOX",),
                          timeout=5, **kwargs)

    def test_an_untrusted_certificate_is_refused_and_a_trusted_one_works(self):
        with self.assertRaises(ImapError) as raised:
            ReadOnlyImap(self.config(), PASSWORD).connect()
        self.assertEqual(raised.exception.code, "tls_invalido")
        self.assertIn("certificado", raised.exception.message)
        with ReadOnlyImap(self.config(ca_file=str(self.cert)), PASSWORD) as imap:
            self.assertEqual(imap.list_messages("INBOX")["uidvalidity"], 3)

    def test_the_host_name_must_match_the_certificate(self):
        other = ImapConfig(host="localhost", port=self.server.port, username="ana@exemplo.invalid", folders=("INBOX",), timeout=5,
                           ca_file=str(self.cert))
        with ReadOnlyImap(other, PASSWORD) as imap:  # DNS:localhost is in the certificate
            self.assertTrue(imap.list_folders()[0]["present"])
        import socket
        wrong = ssl.create_default_context(cafile=str(self.cert))
        with self.assertRaises(ssl.SSLCertVerificationError):  # the certificate has no name for this alias
            with socket.create_connection(("127.0.0.1", self.server.port)) as raw:
                wrong.wrap_socket(raw, server_hostname="outro.exemplo.invalid")


class MimeTests(unittest.TestCase):
    def test_html_is_reduced_to_text_without_loading_anything(self):
        msg = EmailMessage()
        msg["Subject"] = "H"
        msg.set_content("x")
        msg.clear_content()
        msg.add_alternative('<html><head><style>p{}</style></head><body><script>alert(1)</script><p>Ola <b>mundo</b></p>'
                            '<img src="http://rastreio.invalid/p.gif"><a href="http://x.invalid">link</a><!-- oculto --></body></html>', subtype="html")
        parsed = parse_message(msg.as_bytes())
        self.assertIn("Ola mundo", parsed["text"])
        for leaked in ("alert", "rastreio", "oculto", "<p>", "p{}"):
            self.assertNotIn(leaked, parsed["text"])

    def test_attachments_are_listed_but_never_opened(self):
        msg = build(body="corpo")
        msg.add_attachment(b"MZ\x90\x00 executavel", maintype="application", subtype="octet-stream", filename="../../fatura.exe")
        parsed = parse_message(msg.as_bytes())
        self.assertEqual(parsed["text"].strip(), "corpo")
        self.assertEqual([(a["content_type"], a["size"]) for a in parsed["attachments"]], [("application/octet-stream", 15)])
        self.assertNotIn("executavel", str(parsed))

    def test_deeply_nested_and_huge_part_counts_are_limited(self):
        raw = b"Subject: n\r\nContent-Type: text/plain\r\n\r\nfim"
        for i in range(20):
            raw = (f'Content-Type: multipart/mixed; boundary="b{i}"\r\n\r\n--b{i}\r\n'.encode() + raw + f"\r\n--b{i}--\r\n".encode())
        parsed = parse_message(raw)
        self.assertTrue(parsed["parts_limited"] and parsed["truncated"])
        many = b'Subject: m\r\nContent-Type: multipart/mixed; boundary="x"\r\n\r\n' + b"".join(
            b"--x\r\nContent-Type: text/plain\r\n\r\np\r\n" for _ in range(200)) + b"--x--\r\n"
        self.assertTrue(parse_message(many)["parts_limited"])

    def test_header_injection_and_odd_encodings_are_neutralized(self):
        raw = ("Subject: =?utf-8?B?T2zDoSBtdW5kbw==?=\r\nFrom: =?bogus-charset?Q?x?= <a@exemplo.invalid>\r\n"
               "Content-Type: text/plain; charset=nao-existe\r\n\r\ncorpo \xe9\r\n").encode("latin-1")
        parsed = parse_message(raw)
        self.assertEqual(parsed["subject"], "Olá mundo")
        self.assertIn("corpo", parsed["text"])
        self.assertNotIn("\n", parsed["subject"] + parsed["from"])

    def test_text_is_truncated(self):
        parsed = parse_message(build(body="a" * 50000).as_bytes(), max_chars=100)
        self.assertEqual((len(parsed["text"]), parsed["truncated"]), (100, True))


if __name__ == "__main__":
    unittest.main()
