"""Evidence-based diagnostics for "the site is down", limited to hosts the company has authorized.

A diagnosis separates what failed (DNS, TLS, HTTP, registration), says where each fact came from and
how sure it is, and never assumes that every error means an expired domain. It is read-only: it cannot
change DNS, a domain or hosting, and it does not pay or renew anything.

Network safety (SSRF):
* only hosts registered as company assets are probed (a redirect to another host is reported, not followed);
* every resolved address must be globally routable: private, loopback, link-local (including the cloud
  metadata address), CGNAT, multicast and reserved ranges are refused, and an IPv4 address embedded in
  IPv6 is checked as IPv4;
* the validated address is the one connected to (no second lookup, so DNS rebinding cannot swap it);
* ports 80/443 only, a few redirects, bounded time and bytes, a fixed number of requests per diagnosis.
"""
import http.client
import ipaddress
import json
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

MAX_REDIRECTS = 3
MAX_REQUESTS = 8
MAX_BODY = 256 * 1024
TIMEOUT = 8.0
SOON_DAYS = 14
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
RDAP_BOOTSTRAP = "rdap.org"
_EXPIRED_STATUS = ("pending delete", "pendingdelete", "redemption period", "redemptionperiod", "expired")
_HOLD_STATUS = ("client hold", "clienthold", "server hold", "serverhold")


class DiagnosticRefused(ValueError):
    """The request is not allowed (unauthorized or unsafe target); the message is safe to show."""


class DnsFailure(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind  # "nao_resolve" (NXDOMAIN or no data) | "falha_temporaria" | "tempo_esgotado"


class NetFailure(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind  # "tempo_esgotado" | "recusada" | "sem_rota" | "protocolo"


class TlsFailure(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind  # "vencido" | "ainda_nao_valido" | "nome_divergente" | "nao_confiavel" | "handshake"


@dataclass
class HttpReply:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    tls: dict | None = None  # {"not_after": iso, "issuer": str} when the connection used TLS


class SystemNetwork:
    """The real network: system resolver and standard-library sockets (all blocking; call from a thread)."""

    def resolve(self, host):
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as error:
            temporary = {getattr(socket, "EAI_AGAIN", -3), 11002}
            raise DnsFailure("falha_temporaria" if error.errno in temporary else "nao_resolve") from None
        except (TimeoutError, OSError):
            raise DnsFailure("tempo_esgotado") from None
        return list(dict.fromkeys(info[4][0] for info in infos))

    def http(self, ip, host, port, scheme, path, *, timeout=TIMEOUT, max_bytes=MAX_BODY):
        try:
            raw = socket.create_connection((ip, port), timeout=timeout)
        except (socket.timeout, TimeoutError):
            raise NetFailure("tempo_esgotado") from None
        except ConnectionRefusedError:
            raise NetFailure("recusada") from None
        except OSError:
            raise NetFailure("sem_rota") from None
        tls, sock = None, raw
        try:
            if scheme == "https":
                sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
                cert = sock.getpeercert() or {}
                expires = None
                if cert.get("notAfter"):
                    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                issuer = " ".join(value for rdn in cert.get("issuer", ()) for key, value in rdn if key == "organizationName")
                tls = {"not_after": expires, "issuer": issuer[:120] or None}
            request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: assistente-diagnostico/1\r\n"
                       "Accept: */*\r\nConnection: close\r\n\r\n").encode("ascii")
            sock.sendall(request)
            response = http.client.HTTPResponse(sock)
            response.begin()
            body = response.read(max_bytes)
            headers = {name.lower(): value[:300] for name, value in response.getheaders()}
            return HttpReply(response.status, headers, body, tls)
        except ssl.SSLCertVerificationError as error:
            kinds = {10: "vencido", 9: "ainda_nao_valido", 62: "nome_divergente", 64: "nome_divergente"}
            raise TlsFailure(kinds.get(error.verify_code, "nao_confiavel")) from None
        except ssl.SSLError:
            raise TlsFailure("handshake") from None
        except (socket.timeout, TimeoutError):
            raise NetFailure("tempo_esgotado") from None
        except (http.client.HTTPException, OSError):
            raise NetFailure("protocolo") from None
        finally:
            try:
                sock.close()
            except OSError:
                pass


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def is_public_address(text):
    """True only for globally routable addresses (IPv4-in-IPv6 is unwrapped first)."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv6Address) and (address.sixtofour or address.teredo):
        return False
    return address.is_global and not address.is_multicast


def _registrar(data):
    for entity in data.get("entities", []):
        if isinstance(entity, dict) and "registrar" in entity.get("roles", []):
            for item in (entity.get("vcardArray") or [None, []])[1]:
                if isinstance(item, list) and len(item) > 3 and item[0] == "fn":
                    return str(item[3])[:120]
    return None


def normalize_host(value):
    host = str(value or "").strip().lower().rstrip(".")
    labels = host.split(".")
    if not (4 <= len(host) <= 253 and len(labels) >= 2 and all(_LABEL.match(label) for label in labels) and not host.replace(".", "").isdigit()):
        raise DiagnosticRefused("Informe um nome de dominio valido (nao um endereco IP, URL ou caminho).")
    return host


class Diagnostics:
    """Runs the checks. ``authorized(host)`` says whether the company registered the host."""

    def __init__(self, network, authorized, clock=lambda: datetime.now(timezone.utc)):
        self.network, self.authorized, self.clock = network, authorized, clock

    # -- guards ------------------------------------------------------------------------------------------------
    def _host(self, value):
        host = normalize_host(value)
        if not self.authorized(host):
            raise DiagnosticRefused("Este dominio nao esta entre os ativos autorizados da empresa.")
        return host

    def _meta(self, source, **fields):
        return {"fonte": source, "consultado_em": _stamp(self.clock()), **fields}

    def _public_ips(self, host):
        """Resolve once and validate every address; the caller connects to the returned one."""
        ips = self.network.resolve(host)
        if not ips:
            raise DnsFailure("nao_resolve")
        if not all(is_public_address(ip) for ip in ips):
            raise DiagnosticRefused("O nome aponta para uma rede interna ou reservada; a consulta foi bloqueada.")
        return ips

    # -- individual checks ---------------------------------------------------------------------------------------
    def check_dns(self, value):
        host = self._host(value)
        try:
            ips = self._public_ips(host)
        except DnsFailure as failure:
            return self._meta("resolvedor-do-sistema", dominio=host, resultado=failure.kind, enderecos=[],
                              certeza="provavel" if failure.kind == "nao_resolve" else "incerto",
                              observacao="O resolvedor do sistema nao distingue NXDOMAIN de resposta sem dados; confirme no registro."
                              if failure.kind == "nao_resolve" else "Falha temporaria ou de tempo: repita antes de concluir.")
        except DiagnosticRefused as refused:
            return self._meta("resolvedor-do-sistema", dominio=host, resultado="bloqueado", enderecos=[], certeza="confirmado",
                              observacao=str(refused))
        return self._meta("resolvedor-do-sistema", dominio=host, resultado="resolve", enderecos=ips, certeza="confirmado")

    def _request(self, host, scheme, path, budget):
        """One validated request to an authorized host; returns the reply. Raises Net/Tls/Dns failures."""
        if budget["left"] <= 0:
            raise NetFailure("protocolo")
        budget["left"] -= 1
        ip = self._public_ips(host)[0]
        return self.network.http(ip, host, 443 if scheme == "https" else 80, scheme, path, timeout=TIMEOUT, max_bytes=MAX_BODY)

    def _follow(self, host, scheme, budget):
        """GET / following redirects only inside authorized hosts; returns (hops, final_reply, note)."""
        hops, path = [], "/"
        for _ in range(MAX_REDIRECTS + 1):
            reply = self._request(host, scheme, path, budget)
            hops.append({"url": f"{scheme}://{host}{path}", "status": reply.status})
            location = reply.headers.get("location")
            if reply.status not in (301, 302, 303, 307, 308) or not location:
                return hops, reply, None
            target = urlsplit(location if "//" in location or location.startswith("/") else "/" + location)
            if target.scheme and target.scheme not in ("http", "https"):
                return hops, reply, f"Redirecionamento para esquema nao permitido ({target.scheme}); nao seguido."
            next_host = (target.hostname or host).lower().rstrip(".")
            port_ok = target.port in (None, 80, 443)
            if not port_ok:
                return hops, reply, "Redirecionamento para porta nao permitida; nao seguido."
            try:
                is_ip = isinstance(ipaddress.ip_address(next_host), (ipaddress.IPv4Address, ipaddress.IPv6Address))
            except ValueError:
                is_ip = False
            if is_ip:
                return hops, reply, "Redirecionamento para um endereco IP; bloqueado."
            if next_host != host and not self.authorized(next_host):
                return hops, reply, f"Redireciona para {next_host}, fora dos ativos autorizados; nao seguido."
            host, scheme = next_host, target.scheme or scheme
            path = (target.path or "/") + (f"?{target.query}" if target.query else "")
        return hops, reply, "Redirecionamentos demais; parei."

    def check_http(self, value, _budget=None):
        host = self._host(value)
        budget = _budget or {"left": MAX_REQUESTS}
        out = {"dominio": host}
        try:
            hops, reply, note = self._follow(host, "https", budget)
        except TlsFailure as failure:
            return self._meta("sonda-https", **out, resultado="falha_tls", detalhe=failure.kind, certeza="confirmado", redirecionamentos=[])
        except NetFailure as failure:
            return self._meta("sonda-https", **out, resultado="sem_conexao", detalhe=failure.kind,
                              certeza="confirmado" if failure.kind in ("recusada", "tempo_esgotado") else "provavel", redirecionamentos=[])
        except DnsFailure as failure:
            return self._meta("sonda-https", **out, resultado="dns_falhou", detalhe=failure.kind, certeza="provavel", redirecionamentos=[])
        except DiagnosticRefused as refused:
            return self._meta("sonda-https", **out, resultado="bloqueado", detalhe=str(refused), certeza="confirmado", redirecionamentos=[])
        result = "ok" if reply.status < 400 else "erro_servidor" if reply.status >= 500 else "erro_cliente"
        return self._meta("sonda-https", **out, resultado=result, status=reply.status, redirecionamentos=hops, observacao=note,
                          certeza="confirmado", servidor=reply.headers.get("server"))

    def check_tls(self, value, _budget=None):
        host = self._host(value)
        budget = _budget or {"left": MAX_REQUESTS}
        try:
            reply = self._request(host, "https", "/", budget)
        except TlsFailure as failure:
            return self._meta("sonda-tls", dominio=host, resultado=failure.kind, certeza="confirmado")
        except (NetFailure, DnsFailure):
            return self._meta("sonda-tls", dominio=host, resultado="sem_conexao", certeza="incerto")
        except DiagnosticRefused as refused:
            return self._meta("sonda-tls", dominio=host, resultado="bloqueado", certeza="confirmado", observacao=str(refused))
        tls = reply.tls or {}
        expires = _parse(tls.get("not_after"))
        days = (expires - self.clock()).days if expires else None
        return self._meta("sonda-tls", dominio=host, resultado="valido" if expires is None or days >= 0 else "vencido",
                          vence_em=tls.get("not_after"), dias_restantes=days, emissor=tls.get("issuer"), certeza="confirmado",
                          aviso="vence em breve" if days is not None and 0 <= days <= SOON_DAYS else None)

    # -- registration (RDAP) -------------------------------------------------------------------------------------
    def check_registration(self, value, _budget=None):
        """RDAP lookup via the public bootstrap service; tries the host and then its parent names (max 4)."""
        host = self._host(value)
        budget = _budget or {"left": MAX_REQUESTS}
        labels, tried = host.split("."), []
        for start in range(0, min(len(labels) - 1, 4)):
            name = ".".join(labels[start:])
            tried.append(name)
            reply = self._rdap_get(name, budget)
            if reply == "indisponivel":
                return self._meta("rdap", dominio=host, resultado="indisponivel", certeza="incerto",
                                  observacao="O servico RDAP nao respondeu ou o TLD nao oferece RDAP; confirme com o registrador.")
            if reply is not None:
                return self._registration_facts(host, name, reply)
        return self._meta("rdap", dominio=host, resultado="nao_encontrado", consultados=tried, certeza="provavel",
                          observacao="Nenhum registro encontrado; pode ser dominio inexistente ou TLD sem RDAP.")

    def _rdap_get(self, name, budget):
        """Follow the bootstrap redirects (validated hops only); dict on success, None on 404, 'indisponivel' otherwise."""
        host, path = RDAP_BOOTSTRAP, f"/domain/{name}"
        for _ in range(MAX_REDIRECTS + 1):
            try:
                if budget["left"] <= 0:
                    return "indisponivel"
                budget["left"] -= 1
                ip = self._public_ips(host)[0]  # RDAP servers are public third parties: public address required, not an asset
                reply = self.network.http(ip, host, 443, "https", path, timeout=TIMEOUT, max_bytes=MAX_BODY)
            except (NetFailure, TlsFailure, DnsFailure, DiagnosticRefused):
                return "indisponivel"
            if reply.status in (301, 302, 303, 307, 308) and reply.headers.get("location"):
                target = urlsplit(reply.headers["location"])
                if target.scheme != "https" or not target.hostname or target.port not in (None, 443):
                    return "indisponivel"
                host, path = target.hostname.lower().rstrip("."), (target.path or "/") + (f"?{target.query}" if target.query else "")
                continue
            if reply.status == 404:
                return None
            if reply.status != 200:
                return "indisponivel"
            try:
                data = json.loads(reply.body.decode("utf-8", "replace"))
            except ValueError:
                return "indisponivel"
            return data if isinstance(data, dict) else "indisponivel"
        return "indisponivel"

    def _registration_facts(self, asked, name, data):
        events = {str(e.get("eventAction", "")).lower(): e.get("eventDate") for e in data.get("events", []) if isinstance(e, dict)}
        status = [str(s).lower() for s in data.get("status", []) if isinstance(s, str)]
        expires = events.get("expiration")
        registrar = _registrar(data)
        parsed = _parse(expires) if expires else None
        flags = {"expirado_por_data": bool(parsed and parsed <= self.clock()),
                 "status_de_expiracao": any(s in _EXPIRED_STATUS or "redemption" in s or "pending delete" in s for s in status),
                 "retido": any(s in _HOLD_STATUS for s in status)}
        return self._meta("rdap", dominio=asked, dominio_registrado=name, resultado="encontrado", expira_em=expires,
                          sem_data_de_expiracao=expires is None, status=status[:8], registrador=registrar, sinais=flags,
                          certeza="confirmado" if expires or status else "incerto")

    # -- diagnosis -----------------------------------------------------------------------------------------------
    def diagnose(self, value):
        host = self._host(value)
        budget = {"left": MAX_REQUESTS}
        dns = self.check_dns(host)
        evidence = [dns]
        tls = http = registration = None
        if dns["resultado"] == "resolve":
            http = self.check_http(host, budget)
            tls = self.check_tls(host, budget) if http["resultado"] != "falha_tls" else self._meta(
                "sonda-tls", dominio=host, resultado=http["detalhe"], certeza="confirmado")
            evidence += [http, tls]
        wrong = dns["resultado"] != "resolve" or (http and http["resultado"] not in ("ok", "erro_cliente")) or (
            tls and tls["resultado"] not in ("valido",)) or (tls and tls.get("aviso"))
        if wrong:
            registration = self.check_registration(host, budget)
            evidence.append(registration)
        conclusion = self._conclude(dns, http, tls, registration)
        return {"dominio": host, "diagnostico_em": _stamp(self.clock()), **conclusion, "evidencias": evidence,
                "somente_leitura": True}

    def _conclude(self, dns, http, tls, registration):
        def out(code, sure, text, step):
            return {"categoria": code, "certeza": sure, "resumo": text, "proximo_passo": step}

        if registration and registration["resultado"] == "encontrado":
            signals = registration["sinais"]
            if signals["expirado_por_data"] or signals["status_de_expiracao"]:
                return out("dominio_expirado", "confirmado",
                           "O registro (RDAP) indica dominio vencido ou em periodo de resgate/exclusao.",
                           "Falar com o titular/registrador sobre a renovacao. Renovar ou pagar exige aprovacao humana; este diagnostico nao altera nada.")
            if signals["retido"]:
                return out("dominio_retido", "confirmado", "O registro mostra o dominio retido (hold) pelo registrador/registro.",
                           "Consultar o registrador sobre o motivo do bloqueio (cobranca, verificacao de dados ou disputa).")
        if dns["resultado"] == "bloqueado":
            return out("alvo_bloqueado", "confirmado", "O nome aponta para uma rede interna ou reservada; nao foi consultado.",
                       "Conferir o registro DNS do ativo: ele nao deveria apontar para endereco interno.")
        if dns["resultado"] != "resolve":
            if dns["resultado"] in ("falha_temporaria", "tempo_esgotado"):
                return out("dns_instavel", "incerto", "O resolvedor falhou de forma temporaria; nao e possivel concluir.",
                           "Repetir a consulta em alguns minutos e, se persistir, consultar o provedor de DNS.")
            if registration and registration["resultado"] == "encontrado":
                return out("dns_sem_resposta_dominio_vigente", "provavel",
                           "O dominio consta como registrado e dentro da validade, mas o nome nao resolve. O problema esta na zona DNS ou nos servidores de nomes, nao na expiracao.",
                           "Verificar os nameservers e a zona no painel do provedor de DNS.")
            if registration and registration["resultado"] == "nao_encontrado":
                return out("dominio_nao_encontrado", "provavel", "O nome nao resolve e nao ha registro encontrado; pode nao estar registrado ou o TLD nao oferece RDAP.",
                           "Confirmar no site do registro do TLD (por exemplo Registro.br) e com quem administra o dominio.")
            return out("suspeita_de_expiracao", "incerto",
                       "O nome nao resolve e o servico de registro nao deu dados suficientes; expiracao e uma hipotese, nao um fato.",
                       "Confirmar a situacao do dominio diretamente no registrador antes de qualquer acao.")
        if tls and tls["resultado"] in ("vencido", "ainda_nao_valido", "nome_divergente", "nao_confiavel", "handshake"):
            names = {"vencido": "O certificado TLS esta vencido.", "ainda_nao_valido": "O certificado TLS ainda nao e valido (data do servidor ou do certificado).",
                     "nome_divergente": "O certificado nao corresponde ao nome do site.", "nao_confiavel": "O certificado nao e confiavel (autoassinado ou cadeia incompleta).",
                     "handshake": "A negociacao TLS falhou."}
            return out("certificado_" + tls["resultado"], "confirmado", names[tls["resultado"]] + " O dominio resolve.",
                       "Renovar ou corrigir o certificado no servidor/hospedagem (renovacao automatica falhou?).")
        if http and http["resultado"] == "sem_conexao":
            return out("servidor_inacessivel", "confirmado" if http["certeza"] == "confirmado" else "provavel",
                       f"O dominio resolve, mas o servidor nao respondeu ({http['detalhe']}).",
                       "Verificar se o servidor/hospedagem esta ativo e se a porta 443 esta aberta; contatar o provedor de hospedagem.")
        if http and http["resultado"] == "erro_servidor":
            return out("erro_do_servidor", "confirmado", f"O site respondeu com erro de servidor (HTTP {http['status']}).",
                       "Verificar logs e estado da aplicacao na hospedagem.")
        if http and http["resultado"] == "erro_cliente":
            return out("resposta_de_erro", "confirmado", f"O site respondeu HTTP {http['status']} na pagina inicial.",
                       "Confirmar se a pagina inicial deveria existir ou estar protegida.")
        if tls and tls.get("aviso"):
            return out("certificado_vence_em_breve", "confirmado", f"O site funciona, mas o certificado vence em {tls['dias_restantes']} dia(s).",
                       "Agendar a renovacao do certificado.")
        return out("site_ok", "confirmado", "DNS, TLS e HTTP responderam normalmente nesta consulta.",
                   "Se o usuario ainda ve falha, pedir captura do erro e testar a partir da rede dele (cache, DNS local ou bloqueio).")
