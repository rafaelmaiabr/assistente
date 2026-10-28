"""A GET that is safe to point at an address somebody else chose: public JSON/text APIs only.

The address is checked before and after every redirect, the name is resolved once and the connection goes to the
checked address (not to a second resolution), the certificate is verified against the original name, and the answer is
bounded in time, size and type. Nothing is sent that identifies the person or the installation: no credentials, no
cookies, no proxy. Errors are typed and never carry internal detail.
"""
import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 64 * 1024
MAX_REDIRECTS = 2
MAX_URL = 2048
TIMEOUT = 10.0  # for the whole request, redirects included
ALLOWED_TYPES = ("application/json", "text/plain")
_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")
# Ranges that ``is_global`` alone does not rule out because they carry an IPv4 address inside (6to4, Teredo, NAT64).
_EMBEDDING = tuple(ipaddress.ip_network(net) for net in ("2002::/16", "2001::/32", "64:ff9b::/96", "64:ff9b:1::/48"))


class HttpFetchError(Exception):
    """A typed failure with a message that is safe to show: never contains a body, a header or a stack."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes


def public_address(text) -> bool:
    """True only for a globally routable unicast address (never loopback, private, link-local, metadata, CGNAT...)."""
    try:
        ip = ipaddress.ip_address(str(text).split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not any(ip in net for net in _EMBEDDING if ip.version == net.version)


def parse_url(url):
    """``(host, target)`` for an acceptable address, else ``HttpFetchError``. https, port 443, a name (not an IP), no credentials."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL or re.search(r"[\s\x00-\x1f\x7f]", url):
        raise HttpFetchError("endereco_invalido", "Endereco invalido.")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise HttpFetchError("endereco_invalido", "Endereco invalido.") from None
    if parts.scheme != "https":
        raise HttpFetchError("esquema_nao_permitido", "Somente https e permitido.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise HttpFetchError("endereco_invalido", "Endereco com usuario ou senha nao e permitido.")
    if port not in (None, 443):
        raise HttpFetchError("endereco_invalido", "Somente a porta 443 e permitida.")
    if not host or not _HOST.match(host):
        raise HttpFetchError("endereco_invalido", "O endereco deve usar um nome de dominio.")  # also rejects IP literals
    return host, (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _resolve(host, port):
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _dial(ip, port, timeout):
    return socket.create_connection((ip, port), timeout=timeout)


class _Pinned(http.client.HTTPSConnection):
    """HTTPS to an address already checked, with the certificate verified for the original host name."""

    def __init__(self, host, ip, timeout, context, dial):
        super().__init__(host, 443, timeout=timeout, context=context)
        self._ip, self._dial = ip, dial

    def connect(self):
        sock = self._dial(self._ip, 443, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class SafeHttp:
    """``get(url)`` for public JSON or text. ``resolver``, ``dial`` and ``context`` exist so tests can stand in for the network."""

    def __init__(self, *, resolver=_resolve, dial=_dial, context=None, timeout=TIMEOUT, clock=time.monotonic):
        self.resolver, self.dial, self.timeout, self.clock = resolver, dial, timeout, clock
        self.context = context or ssl.create_default_context()

    def get(self, url, *, max_bytes=MAX_BYTES) -> Response:
        deadline, origin = self.clock() + self.timeout, None
        for hop in range(MAX_REDIRECTS + 1):
            host, target = parse_url(url)
            if origin is not None and host != origin:
                raise HttpFetchError("redirecionamento_bloqueado", "O servico redirecionou para outro endereco; nada foi seguido.")
            origin = host
            response = self._once(host, target, deadline, max_bytes)
            if isinstance(response, str):  # a redirect: the next address goes through every check again
                url = urljoin(f"https://{host}{target}", response)
                continue
            return response
        raise HttpFetchError("redirecionamento_bloqueado", "Redirecionamentos demais.")

    def _remaining(self, deadline):
        left = deadline - self.clock()
        if left <= 0:
            raise HttpFetchError("tempo_esgotado", "O servico demorou demais para responder.")
        return left

    def _address(self, host):
        try:
            found = self.resolver(host, 443)
        except OSError:
            raise HttpFetchError("dns", "Nao foi possivel resolver o endereco.") from None
        if not found or not all(public_address(ip) for ip in found):
            raise HttpFetchError("endereco_bloqueado", "O endereco aponta para uma rede que nao e publica; a consulta foi bloqueada.")
        return found[0]

    def _once(self, host, target, deadline, max_bytes):
        ip = self._address(host)
        conn = _Pinned(host, ip, min(self._remaining(deadline), self.timeout), self.context, self.dial)
        try:
            conn.request("GET", target, headers={"Accept": "application/json, text/plain", "Accept-Encoding": "identity",
                                                 "User-Agent": "assistente-local/1.0"})
            sock = conn.sock  # kept: for a response that closes the connection, getresponse() lets go of it
            response = conn.getresponse()
            return self._read(response, sock, deadline, max_bytes)
        except HttpFetchError:
            raise
        except (ssl.SSLError, ssl.CertificateError):
            raise HttpFetchError("tls", "O certificado do servico nao e confiavel.") from None
        except (TimeoutError, socket.timeout):
            raise HttpFetchError("tempo_esgotado", "O servico demorou demais para responder.") from None
        except (OSError, http.client.HTTPException):
            raise HttpFetchError("sem_conexao", "Nao foi possivel conectar ao servico.") from None
        finally:
            conn.close()

    def _read(self, response, sock, deadline, max_bytes):
        status = response.status
        location = response.getheader("Location")
        if status in (301, 302, 303, 307, 308) and location:
            return location
        if status >= 500:
            raise HttpFetchError("servico_com_erro", f"O servico respondeu com erro (HTTP {status}).")
        if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
            raise HttpFetchError("tipo_inesperado", "Resposta comprimida nao e aceita.")
        kind = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
        if kind not in ALLOWED_TYPES:
            raise HttpFetchError("tipo_inesperado", f"O servico respondeu (HTTP {status}) com um tipo de conteudo que nao e aceito (somente JSON ou texto).")
        declared = response.getheader("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise HttpFetchError("grande_demais", "A resposta e grande demais.")
        chunks, size = [], 0
        while not response.isclosed():  # closed once the declared length was read: the socket is gone, nothing left to wait for
            sock.settimeout(self._remaining(deadline))  # raises when the total time is up, however slowly the bytes arrive
            chunk = response.read1(4096)  # what has arrived, so the clock is checked between chunks (read() would wait for all 4096)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HttpFetchError("grande_demais", "A resposta e grande demais.")
            chunks.append(chunk)
        return Response(status, kind, b"".join(chunks))
