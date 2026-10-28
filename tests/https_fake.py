"""A local HTTPS server with a certificate made for the test, and a stand-in for the network (names -> addresses).

``FakeWeb`` answers different paths the way a public API, a hostile one or a broken one would. The client under test
resolves names through ``resolver`` (so a public-looking address can stand for the local server) and dials through
``dial`` (so the connection really reaches it); ``dialed`` records which addresses were ever connected to.
"""
import datetime
import http.server
import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PUBLIC = "93.184.216.34"
HOSTS = {
    "api.exemplo.test": [PUBLIC], "outro.exemplo.test": ["93.184.216.35"],
    "interno.exemplo.test": ["10.0.0.5"], "loopback.exemplo.test": ["127.0.0.1"], "misto.exemplo.test": [PUBLIC, "192.168.1.5"],
    "metadados.exemplo.test": ["169.254.169.254"], "v6.exemplo.test": ["::1"], "mapeado.exemplo.test": ["::ffff:10.0.0.1"],
    "cgnat.exemplo.test": ["100.64.0.1"],
}


def certificate(name="api.exemplo.test"):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def reply(self, status=200, kind="application/json", body=b'{"ok": true}', headers=()):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        for name, value in headers:
            self.send_header(name, value)
        if "Content-Length" not in dict(headers):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append({"path": self.path, "headers": dict(self.headers)})
        path = self.path.split("?", 1)[0]
        if path == "/json":
            return self.reply(body=b'{"dominio": "exemplo.com.br", "situacao": "ativo"}')
        if path == "/texto":
            return self.reply(kind="text/plain; charset=utf-8", body="ola".encode())
        if path == "/json-quebrado":
            return self.reply(body=b"{nao e json")
        if path == "/nao-existe":
            return self.reply(404, body=b'{"message": "dominio nao encontrado"}')
        if path == "/erro":
            return self.reply(503, body=b'{"erro": "x"}')
        if path == "/redir-mesmo":
            return self.reply(302, headers=[("Location", "/json")])
        if path == "/redir-sempre":
            return self.reply(302, headers=[("Location", "/redir-sempre")])
        if path == "/redir-outro":
            return self.reply(302, headers=[("Location", "https://outro.exemplo.test/json")])
        if path == "/redir-interno":
            return self.reply(302, headers=[("Location", "https://interno.exemplo.test/json")])
        if path == "/redir-ip":
            return self.reply(302, headers=[("Location", "https://127.0.0.1/json")])
        if path == "/redir-http":
            return self.reply(302, headers=[("Location", "http://api.exemplo.test/json")])
        if path == "/grande":
            return self.reply(body=b'{"x": "' + b"a" * 200_000 + b'"}')
        if path == "/grande-sem-tamanho":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            for _ in range(100):
                self.wfile.write(b"a" * 4096)
            return
        if path == "/html":
            return self.reply(kind="text/html", body=b"<script>alert(1)</script>")
        if path == "/comprimido":
            return self.reply(headers=[("Content-Encoding", "gzip")])
        if path == "/lento":
            time.sleep(1.5)
            return self.reply()
        if path == "/gotejo":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            for _ in range(30):
                self.wfile.write(b"a")
                self.wfile.flush()
                time.sleep(0.1)
            return
        self.reply(404, body=b'{"message": "rota desconhecida"}')


class _Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # the client under test closes on purpose (too big, too slow, bad certificate): that is not a failure of the fake


class FakeWeb:
    """``resolver``, ``dial`` and ``context`` to build a ``SafeHttp`` that reaches this server and nothing else."""

    def __init__(self):
        self.cert, key = certificate()
        self.folder = tempfile.TemporaryDirectory()
        cert_file, key_file = Path(self.folder.name) / "cert.pem", Path(self.folder.name) / "key.pem"
        cert_file.write_bytes(self.cert)
        key_file.write_bytes(key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.server.requests = []
        self.port = self.server.server_address[1]
        self.dialed, self.resolved = [], []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def requests(self):
        return self.server.requests

    def trusting(self):
        return ssl.create_default_context(cadata=self.cert.decode())

    def resolver(self, host, port):
        self.resolved.append(host)
        if host == "sem.exemplo.test":
            raise socket.gaierror("no such host")
        return list(HOSTS.get(host, []))

    def dial(self, ip, port, timeout):
        self.dialed.append(ip)
        return socket.create_connection(("127.0.0.1", self.port), timeout=timeout)

    def client(self, **options):
        from team.connectors.http_get import SafeHttp
        return SafeHttp(resolver=self.resolver, dial=self.dial, context=options.pop("context", self.trusting()), **options)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()
