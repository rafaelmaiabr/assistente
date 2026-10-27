import asyncio
import datetime
import hashlib
import http.server
import ipaddress
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:  # the CI job installs only the lightweight test dependencies; these tests need the full runtime set
    import cryptography  # noqa: F401
    import httpx2  # noqa: F401
    import mcp  # noqa: F401
    import psutil
except ImportError as missing:
    raise unittest.SkipTest(f"dependencia ausente: {missing.name}") from None

from team.gateway import ResultTooLarge, ToolDenied, ToolUnavailable
from team.mcp_transport import McpClientTransport, check_url
from tests.team_fixtures import SECRET, GatewayCase

SERVER = str(Path(__file__).with_name("mcp_test_server.py"))
IDENTITY = {"tenant_id": "tenant-a", "agent_id": "agent-1", "run_id": "run-1", "connection_id": "conn-1", "operation": "read"}


def stdio_server(mode="-", pidfile="-"):
    return {"argv": [sys.executable, SERVER, "stdio", "-", mode, pidfile]}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Pinned(McpClientTransport):
    """Gateway tests: the approved entry says http://127.0.0.1:9000; point it at the real test server instead."""

    def __init__(self, kind, **pin):
        super().__init__(kind)
        self.pin = pin

    async def describe(self, server, tool):
        return await super().describe({**server, **self.pin}, tool)

    async def call_tool(self, server, tool, arguments, **kwargs):
        return await super().call_tool({**server, **self.pin}, tool, arguments, **kwargs)


class StdioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.transport = McpClientTransport("stdio")
        self.server = stdio_server()

    async def test_describe_reports_annotations_and_no_claim_without_one(self):
        self.assertEqual(await self.transport.describe(self.server, "eco"), {"read_only": True})
        self.assertEqual(await self.transport.describe(self.server, "escreve"), {"read_only": False})
        self.assertEqual(await self.transport.describe(self.server, "sem_anotacao"), {})
        self.assertIsNone(await self.transport.describe(self.server, "nao_existe"))

    async def test_call_returns_structured_and_json_text_results(self):
        self.assertEqual(await self.transport.call_tool(self.server, "eco", {"texto": "oi"}, identity=IDENTITY, secret=None), {"eco": "oi"})
        listed = await self.transport.call_tool(self.server, "texto_livre", {}, identity=IDENTITY, secret=None)
        self.assertEqual(listed, {"result": '{"lista": [1, 2, 3]}'})

    async def test_identity_and_secret_reach_the_server_only_through_the_backend_channel(self):
        seen = await self.transport.call_tool(self.server, "segredo", {}, identity=IDENTITY, secret="sk-test-segredo-injetado")
        self.assertEqual((seen["sha256"], seen["tenant"]), (sha("sk-test-segredo-injetado"), "tenant-a"))
        none = await self.transport.call_tool(self.server, "segredo", {}, identity=IDENTITY, secret=None)
        self.assertEqual(none["sha256"], sha(""))

    async def test_server_errors_become_a_fixed_message_without_server_text(self):
        result = await self.transport.call_tool(self.server, "falha", {}, identity=IDENTITY, secret=None)
        self.assertEqual(result["erro"], "erro_do_servidor")
        self.assertNotIn("SEGREDO-INTERNO", str(result))

    async def test_server_that_dies_mid_call_is_unavailable(self):
        with self.assertRaises(ToolUnavailable):
            await self.transport.call_tool(self.server, "cai", {}, identity=IDENTITY, secret=None)

    async def test_oversized_results_are_refused_before_reaching_the_gateway(self):
        small = McpClientTransport("stdio", max_result_bytes=10_000)
        with self.assertRaises(ResultTooLarge):
            await small.call_tool(self.server, "enorme", {"bytes_": 50_000}, identity=IDENTITY, secret=None)

    async def test_only_an_absolute_registered_argv_runs(self):
        for server in ({}, {"argv": None}, {"argv": []}, {"argv": ["python", SERVER]}, {"argv": [sys.executable, 7]},
                       {"argv": ["C:/nao/existe/mcp.exe"]}):
            with self.assertRaises(ToolUnavailable):
                await self.transport.describe(server, "eco")

    def test_unknown_kind_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            McpClientTransport("websocket")

    async def test_cancelling_a_call_ends_the_server_process(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "pid"
            task = asyncio.ensure_future(self.transport.call_tool(
                stdio_server(pidfile=str(pidfile)), "lento", {"segundos": 60}, identity=IDENTITY, secret=None))
            for _ in range(100):
                if pidfile.exists() and pidfile.read_text().strip():
                    break
                await asyncio.sleep(0.1)
            pid = int(pidfile.read_text())
            await asyncio.sleep(0.5)
            self.assertTrue(psutil.pid_exists(pid))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for _ in range(100):
                if not psutil.pid_exists(pid):
                    break
                await asyncio.sleep(0.1)
            self.assertFalse(psutil.pid_exists(pid), "o processo do servidor ficou orfao")


class NetworkServerCase(unittest.IsolatedAsyncioTestCase):
    kind = None

    def setUp(self):
        self.port = free_port()
        self.process = subprocess.Popen([sys.executable, SERVER, self.kind, str(self.port)], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        self.fail("servidor de teste nao subiu")

    def stop(self):
        self.process.terminate()
        self.process.wait(10)


class StreamableHttpTests(NetworkServerCase):
    kind = "streamable-http"

    async def test_describe_and_call_over_streamable_http(self):
        transport = McpClientTransport("http")
        server = {"url": f"http://127.0.0.1:{self.port}/mcp"}
        self.assertEqual(await transport.describe(server, "eco"), {"read_only": True})
        self.assertEqual(await transport.call_tool(server, "eco", {"texto": "http"}, identity=IDENTITY, secret="s"), {"eco": "http"})

    async def test_a_redirect_to_another_origin_is_not_followed(self):
        hits = []

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(handler):
                hits.append(handler.path)
                handler.send_response(307)
                handler.send_header("Location", f"http://127.0.0.1:{self.port}/mcp")
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            do_GET = do_POST

            def log_message(handler, *args):
                pass

        redirector = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=redirector.serve_forever, daemon=True).start()
        self.addCleanup(redirector.shutdown)
        with self.assertRaises(ToolUnavailable):
            await McpClientTransport("http").describe({"url": f"http://127.0.0.1:{redirector.server_address[1]}/mcp"}, "eco")
        self.assertTrue(hits)


class SseTests(NetworkServerCase):
    kind = "sse"

    async def test_describe_and_call_over_sse(self):
        transport = McpClientTransport("sse")
        server = {"url": f"http://127.0.0.1:{self.port}/sse"}
        self.assertEqual(await transport.describe(server, "escreve"), {"read_only": False})
        self.assertEqual(await transport.call_tool(server, "eco", {"texto": "sse"}, identity=IDENTITY, secret=None), {"eco": "sse"})


class TlsAndUrlTests(unittest.IsolatedAsyncioTestCase):
    def test_urls_need_tls_unless_loopback_and_carry_no_credentials(self):
        for good in ("https://erp.example.com/mcp", "http://127.0.0.1:9000/mcp", "http://localhost/mcp"):
            self.assertEqual(check_url(good, "http"), good)
        for bad in ("http://erp.example.com/mcp", "ftp://x/y", "https://user:pw@erp.example.com/mcp", "https://", "", None, 7):
            with self.assertRaises(ToolUnavailable, msg=str(bad)):
                check_url(bad, "http")

    def test_certificates_are_verified_by_default_and_a_private_ca_is_explicit(self):
        self.assertIs(McpClientTransport("http").verify, True)
        with tempfile.TemporaryDirectory() as folder:
            cert, _ = self.certificate(Path(folder))
            self.assertIsInstance(McpClientTransport("http", ca_file=str(cert)).verify, ssl.SSLContext)

    async def test_a_self_signed_certificate_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            cert, key = self.certificate(Path(folder))

            class Quiet(http.server.BaseHTTPRequestHandler):
                def do_POST(handler):
                    handler.send_response(200)
                    handler.send_header("Content-Length", "0")
                    handler.end_headers()

                do_GET = do_POST

                def log_message(handler, *args):
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            # check_url allows https on any host; the failure here is certificate verification (default context)
            with self.assertRaises(ToolUnavailable):
                await McpClientTransport("http").describe({"url": f"https://127.0.0.1:{server.server_address[1]}/mcp"}, "eco")

    @staticmethod
    def certificate(folder):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = folder / "cert.pem", folder / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                               serialization.NoEncryption()))
        return cert_path, key_path

    def test_identity_and_secret_headers_come_from_the_backend(self):
        headers = McpClientTransport._headers(IDENTITY, "sk-test-123")
        self.assertEqual(headers["Authorization"], "Bearer sk-test-123")
        self.assertEqual(headers["X-Assistente-Tenant-Id"], "tenant-a")
        self.assertNotIn("Authorization", McpClientTransport._headers(IDENTITY, None))


class GatewayWithRealServerTests(GatewayCase):
    """The gateway's rules (#22) with a real MCP server over stdio instead of the fake."""

    def use(self, mode="-"):
        self.gateway.transports["http"] = Pinned("stdio", argv=stdio_server(mode)["argv"])

    async def test_listing_call_decisions_and_secret_injection_end_to_end(self):
        self.use()
        execution, run = self.open()
        self.assertEqual([t["name"] for t in self.gateway.list_tools(execution)], ["erp-demo.listar-cobrancas"])
        result = await self.gateway.call_tool(execution, "erp-demo.listar-cobrancas", {"cliente": "Acme", "limite": 3})
        self.assertEqual(result.data["cobrancas"][0]["cliente"], "Acme")
        self.assertEqual(result.data["segredo_sha256"], sha(SECRET))  # injected by the backend, from the vault
        self.assertEqual(result.data["tenant"], self.a["id"])
        self.assertNotIn(SECRET, str(result.data))
        decisions = self.decisions(run)
        self.assertEqual([(d["decision"], d["reason"]) for d in decisions], [("allowed", "ok")])

    async def test_a_server_that_contradicts_the_approved_read_classification_is_denied(self):
        self.use("mentiroso")
        execution, run = self.open()
        with self.assertRaises(ToolDenied):
            await self.gateway.call_tool(execution, "erp-demo.listar-cobrancas", {"cliente": "Acme"})
        self.assertEqual(self.decisions(run)[-1]["decision"], "denied")

    async def test_unreachable_server_is_unavailable_not_a_crash(self):
        self.gateway.transports["http"] = Pinned("stdio", argv=["C:/nao/existe/mcp.exe"])
        execution, run = self.open()
        with self.assertRaises(ToolUnavailable):
            await self.gateway.call_tool(execution, "erp-demo.listar-cobrancas", {"cliente": "Acme"})
        self.assertEqual(self.decisions(run)[-1]["decision"], "error")


if __name__ == "__main__":
    unittest.main()
