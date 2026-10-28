import ssl
import unittest

from team.connectors.http_get import MAX_BYTES, HttpFetchError, SafeHttp, parse_url, public_address
try:
    from tests.https_fake import FakeWeb
except ImportError:  # the fake server needs ``cryptography``, which the minimal CI image does not install
    FakeWeb = None


PUBLIC = "93.184.216.34"  # a documentation-style public address; also what tests/https_fake.py resolves api.exemplo.test to


class AddressTests(unittest.TestCase):
    def test_only_global_unicast_addresses_are_public(self):
        for ip in (PUBLIC, "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            with self.subTest(ip=ip):
                self.assertTrue(public_address(ip))
        for ip in ("127.0.0.1", "127.1.2.3", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                   "224.0.0.1", "255.255.255.255", "192.0.2.1", "::1", "::", "fe80::1", "fc00::1", "ff02::1", "::ffff:10.0.0.1",
                   "::ffff:127.0.0.1", "2002:7f00:1::1", "64:ff9b::a00:1", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "no-es-ip", "", None, "10.0.0.1%eth0"):
            with self.subTest(ip=ip):
                self.assertFalse(public_address(ip))

    def test_the_url_must_be_https_a_name_on_443_without_credentials(self):
        self.assertEqual(parse_url("https://brasilapi.com.br/registrobr/v1/exemplo.com.br"), ("brasilapi.com.br", "/registrobr/v1/exemplo.com.br"))
        self.assertEqual(parse_url("https://API.Exemplo.com:443/x?a=1"), ("api.exemplo.com", "/x?a=1"))
        self.assertEqual(parse_url("https://exemplo.com"), ("exemplo.com", "/"))
        for url in ("http://exemplo.com/x", "ftp://exemplo.com/x", "file:///etc/passwd", "//exemplo.com/x", "exemplo.com/x", "https://",
                    "https://user:pass@exemplo.com/x", "https://exemplo.com@evil.example/x", "https://evil.example\\@exemplo.com/x",
                    "https://exemplo.com:8443/x", "https://exemplo.com:80/x", "https://127.0.0.1/x", "https://[::1]/x", "https://2130706433/x",
                    "https://localhost/x", "https://exemplo/x", "https://exemplo.com./x", "https://exemplo .com/x", "https://exemplo.com/x\ny",
                    "https://exemplo.com/" + "a" * 3000, "", None, 5):
            with self.subTest(url=url), self.assertRaises(HttpFetchError):
                parse_url(url)


@unittest.skipUnless(FakeWeb, "cryptography is not installed here")
class FetchTests(unittest.TestCase):
    def setUp(self):
        self.web = FakeWeb()
        self.addCleanup(self.web.stop)
        self.http = self.web.client()

    def code(self, url, client=None):
        with self.assertRaises(HttpFetchError) as raised:
            (client or self.http).get(url)
        return raised.exception.code

    def test_a_public_json_api_is_read(self):
        answer = self.http.get("https://api.exemplo.test/json?d=exemplo.com.br")
        self.assertEqual((answer.status, answer.content_type), (200, "application/json"))
        self.assertIn(b"exemplo.com.br", answer.body)
        self.assertEqual(self.web.dialed, [PUBLIC])  # it connected to the address that was checked

    def test_plain_text_and_an_error_status_with_a_body_are_returned(self):
        self.assertEqual(self.http.get("https://api.exemplo.test/texto").content_type, "text/plain")
        missing = self.http.get("https://api.exemplo.test/nao-existe")
        self.assertEqual(missing.status, 404)
        self.assertIn(b"nao encontrado", missing.body)

    def test_nothing_that_identifies_the_person_is_sent(self):
        self.http.get("https://api.exemplo.test/json")
        headers = {k.lower(): v for k, v in self.web.requests[0]["headers"].items()}
        self.assertEqual(set(headers), {"host", "accept", "accept-encoding", "user-agent"})
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(headers["host"], "api.exemplo.test")

    def test_names_that_resolve_to_non_public_addresses_are_blocked_before_any_connection(self):
        for host in ("interno", "loopback", "misto", "metadados", "v6", "mapeado", "cgnat"):
            with self.subTest(host=host):
                self.assertEqual(self.code(f"https://{host}.exemplo.test/json"), "endereco_bloqueado")
        self.assertEqual(self.web.dialed, [])  # not one connection was opened
        self.assertEqual(self.web.requests, [])

    def test_ip_literals_and_odd_schemes_never_reach_the_network(self):
        for url in ("https://127.0.0.1/json", "https://169.254.169.254/latest/meta-data", "http://api.exemplo.test/json",
                    "https://api.exemplo.test:8443/json", "https://user@api.exemplo.test/json"):
            with self.subTest(url=url):
                self.assertIn(self.code(url), ("endereco_invalido", "esquema_nao_permitido"))
        self.assertEqual((self.web.resolved, self.web.dialed), ([], []))

    def test_an_unknown_name_is_a_dns_error(self):
        self.assertEqual(self.code("https://sem.exemplo.test/json"), "dns")
        self.assertEqual(self.code("https://naoexiste.exemplo.test/json"), "endereco_bloqueado")  # resolves to nothing

    def test_a_redirect_inside_the_same_origin_is_followed_and_a_loop_is_not(self):
        self.assertEqual(self.http.get("https://api.exemplo.test/redir-mesmo").status, 200)
        self.assertEqual(self.code("https://api.exemplo.test/redir-sempre"), "redirecionamento_bloqueado")

    def test_a_redirect_to_another_origin_an_internal_address_or_plain_http_is_never_followed(self):
        for path in ("redir-outro", "redir-interno", "redir-ip", "redir-http"):
            with self.subTest(path=path):
                self.assertIn(self.code(f"https://api.exemplo.test/{path}"),
                              ("redirecionamento_bloqueado", "esquema_nao_permitido", "endereco_invalido"))
        self.assertNotIn("10.0.0.5", self.web.dialed)
        self.assertEqual(set(self.web.dialed), {PUBLIC})  # only the approved origin was ever contacted

    def test_the_certificate_is_checked_for_the_name_and_against_trusted_authorities(self):
        self.assertEqual(self.code("https://outro.exemplo.test/json"), "tls")  # a valid certificate, but for another name
        self.assertEqual(self.code("https://api.exemplo.test/json", self.web.client(context=ssl.create_default_context())), "tls")  # not trusted

    def test_big_answers_are_cut_with_or_without_a_declared_size(self):
        self.assertEqual(self.code("https://api.exemplo.test/grande"), "grande_demais")
        self.assertEqual(self.code("https://api.exemplo.test/grande-sem-tamanho"), "grande_demais")
        self.assertEqual(self.http.get("https://api.exemplo.test/json", max_bytes=MAX_BYTES).status, 200)
        self.assertEqual(self.code_with_limit(10), "grande_demais")

    def code_with_limit(self, limit):
        with self.assertRaises(HttpFetchError) as raised:
            self.http.get("https://api.exemplo.test/json", max_bytes=limit)
        return raised.exception.code

    def test_html_compressed_and_server_errors_are_refused(self):
        self.assertEqual(self.code("https://api.exemplo.test/html"), "tipo_inesperado")
        with self.assertRaises(HttpFetchError) as html:
            self.http.get("https://api.exemplo.test/html")
        self.assertIn("HTTP 200", html.exception.message)  # the status helps tell a wrong address from a refusal
        self.assertEqual(self.code("https://api.exemplo.test/comprimido"), "tipo_inesperado")
        self.assertEqual(self.code("https://api.exemplo.test/erro"), "servico_com_erro")

    def test_a_slow_server_and_a_slow_drip_run_out_of_time(self):
        quick = self.web.client(timeout=0.4)
        self.assertEqual(self.code("https://api.exemplo.test/lento", quick), "tempo_esgotado")
        self.assertEqual(self.code("https://api.exemplo.test/gotejo", quick), "tempo_esgotado")  # the total time counts, not each read

    def test_errors_carry_no_internal_detail(self):
        for path in ("grande", "html", "erro"):
            with self.assertRaises(HttpFetchError) as raised:
                self.http.get(f"https://api.exemplo.test/{path}")
            text = raised.exception.message
            self.assertNotIn("Traceback", text)
            self.assertNotIn("127.0.0.1", text)
            self.assertNotIn(str(self.web.port), text)


if __name__ == "__main__":
    unittest.main()
