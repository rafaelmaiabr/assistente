import json
import socket
import unittest
from datetime import datetime, timezone
from pathlib import Path

from team.access import NotFound, PermissionDenied
from team.diagnostics import (
    DiagnosticRefused, Diagnostics, DnsFailure, HttpReply, NetFailure, SystemNetwork, TlsFailure, is_public_address,
    normalize_host)
from team.diagnostics_mcp import CATALOG_KEY, TOOLS, AssetService, DiagnosticsTransport, approve_catalog
from team.gateway import Gateway, InvalidArguments
from tests.team_fixtures import GatewayCase

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
PUBLIC = "93.184.216.34"
RDAP_IP = "8.8.8.8"


def rdap_body(expires="2027-01-01T00:00:00Z", status=("active",), registrar="Registrar Exemplo"):
    events = [{"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"}]
    if expires:
        events.append({"eventAction": "expiration", "eventDate": expires})
    return json.dumps({"events": events, "status": list(status),
                       "entities": [{"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", registrar]]]}]}).encode()


class FakeNetwork:
    """Scripted DNS, HTTP and RDAP. Records every lookup and the exact address each connection used."""

    def __init__(self):
        self.dns = {"rdap.org": [RDAP_IP], "rdap.registro.exemplo": [RDAP_IP]}
        self.web = {}    # (host, scheme, path) -> HttpReply | Exception
        self.rdap = {}   # domain -> bytes (200) | None (404) | Exception
        self.calls = []

    def resolve(self, host):
        self.calls.append(("resolve", host))
        answer = self.dns.get(host, DnsFailure("nao_resolve"))
        if callable(answer):
            answer = answer()
        if isinstance(answer, Exception):
            raise answer
        return list(answer)

    def http(self, ip, host, port, scheme, path, *, timeout, max_bytes):
        self.calls.append(("http", ip, host, port, scheme, path))
        if host == "rdap.org":
            name = path.rsplit("/", 1)[-1]
            entry = self.rdap.get(name)
            if isinstance(entry, Exception):
                raise entry
            return HttpReply(404) if entry is None else HttpReply(200, {}, entry)
        entry = self.web.get((host, scheme, path), NetFailure("recusada"))
        if isinstance(entry, Exception):
            raise entry
        return entry

    def connections(self):
        return [c for c in self.calls if c[0] == "http"]


class DiagCase(unittest.TestCase):
    def setUp(self):
        self.net = FakeNetwork()
        self.hosts = {"exemplo.com.br"}
        self.diag = Diagnostics(self.net, lambda host: host in self.hosts or any(host.endswith("." + h) for h in self.hosts), lambda: NOW)

    def healthy(self, host="exemplo.com.br", tls=None):
        self.net.dns[host] = [PUBLIC]
        self.net.web[(host, "https", "/")] = HttpReply(200, {"server": "nginx"}, b"ok", tls or {"not_after": "2026-12-01T00:00:00Z", "issuer": "CA Exemplo"})


class ScenarioTests(DiagCase):
    def test_a_healthy_site_is_reported_ok_without_asking_the_registry(self):
        self.healthy()
        result = self.diag.diagnose("Exemplo.com.br.")
        self.assertEqual((result["categoria"], result["certeza"], result["somente_leitura"]), ("site_ok", "confirmado", True))
        self.assertEqual({e["fonte"] for e in result["evidencias"]}, {"resolvedor-do-sistema", "sonda-https", "sonda-tls"})
        self.assertTrue(all(e["consultado_em"] == "2026-06-15T12:00:00Z" for e in result["evidencias"]))
        self.assertFalse([c for c in self.net.connections() if c[2] == "rdap.org"])

    def test_an_expired_domain_is_confirmed_by_the_registry_not_by_dns_alone(self):
        self.net.rdap["exemplo.com.br"] = rdap_body(expires="2026-06-01T00:00:00Z", status=("client hold",))
        result = self.diag.diagnose("exemplo.com.br")
        self.assertEqual((result["categoria"], result["certeza"]), ("dominio_expirado", "confirmado"))
        self.assertIn("aprovacao humana", result["proximo_passo"])
        registry = next(e for e in result["evidencias"] if e["fonte"] == "rdap")
        self.assertEqual((registry["expira_em"], registry["registrador"]), ("2026-06-01T00:00:00Z", "Registrar Exemplo"))

    def test_nxdomain_with_a_valid_registration_is_a_dns_problem_not_an_expiration(self):
        self.net.rdap["exemplo.com.br"] = rdap_body(expires="2027-03-01T00:00:00Z")
        result = self.diag.diagnose("exemplo.com.br")
        self.assertEqual((result["categoria"], result["certeza"]), ("dns_sem_resposta_dominio_vigente", "provavel"))
        self.assertIn("nameservers", result["proximo_passo"])
        self.assertNotEqual(result["categoria"], "dominio_expirado")

    def test_rdap_without_an_expiration_date_stays_uncertain(self):
        self.net.rdap["exemplo.com.br"] = rdap_body(expires=None, status=("active",))
        registry = self.diag.check_registration("exemplo.com.br")
        self.assertTrue(registry["sem_data_de_expiracao"])
        result = self.diag.diagnose("exemplo.com.br")
        self.assertEqual(result["categoria"], "dns_sem_resposta_dominio_vigente")  # active status, no date: still no claim of expiry
        self.assertNotEqual(result["categoria"], "dominio_expirado")
        unavailable = Diagnostics(self.net, lambda h: True, lambda: NOW)
        self.net.rdap["exemplo.com.br"] = NetFailure("tempo_esgotado")
        out = unavailable.diagnose("exemplo.com.br")
        self.assertEqual((out["categoria"], out["certeza"]), ("suspeita_de_expiracao", "incerto"))
        self.assertIn("registrador", out["proximo_passo"])

    def test_unknown_registration_is_not_found_with_moderate_certainty(self):
        result = self.diag.diagnose("exemplo.com.br")  # rdap 404 for every candidate name
        self.assertEqual((result["categoria"], result["certeza"]), ("dominio_nao_encontrado", "provavel"))

    def test_registry_lookup_tries_the_parent_name_for_subdomains(self):
        self.net.rdap["exemplo.com.br"] = rdap_body()
        registry = self.diag.check_registration("www.exemplo.com.br")
        self.assertEqual((registry["resultado"], registry["dominio_registrado"]), ("encontrado", "exemplo.com.br"))

    def test_an_expired_certificate_is_a_tls_finding_on_a_resolving_domain(self):
        self.net.dns["exemplo.com.br"] = [PUBLIC]
        self.net.web[("exemplo.com.br", "https", "/")] = TlsFailure("vencido")
        self.net.rdap["exemplo.com.br"] = rdap_body()
        result = self.diag.diagnose("exemplo.com.br")
        self.assertEqual((result["categoria"], result["certeza"]), ("certificado_vencido", "confirmado"))
        for kind, expected in (("nome_divergente", "certificado_nome_divergente"), ("nao_confiavel", "certificado_nao_confiavel")):
            self.net.web[("exemplo.com.br", "https", "/")] = TlsFailure(kind)
            self.assertEqual(self.diag.diagnose("exemplo.com.br")["categoria"], expected)

    def test_certificate_nearing_expiry_and_server_errors(self):
        self.healthy(tls={"not_after": "2026-06-20T00:00:00Z", "issuer": "CA"})
        tls = self.diag.check_tls("exemplo.com.br")
        self.assertEqual((tls["resultado"], tls["dias_restantes"], tls["aviso"]), ("valido", 4, "vence em breve"))
        self.assertEqual(self.diag.diagnose("exemplo.com.br")["categoria"], "certificado_vence_em_breve")
        self.net.web[("exemplo.com.br", "https", "/")] = HttpReply(503, {}, b"", {"not_after": "2026-12-01T00:00:00Z"})
        self.assertEqual(self.diag.diagnose("exemplo.com.br")["categoria"], "erro_do_servidor")
        self.net.web[("exemplo.com.br", "https", "/")] = NetFailure("tempo_esgotado")
        self.assertEqual(self.diag.diagnose("exemplo.com.br")["categoria"], "servidor_inacessivel")

    def test_temporary_resolver_failure_is_not_a_conclusion(self):
        self.net.dns["exemplo.com.br"] = DnsFailure("falha_temporaria")
        result = self.diag.diagnose("exemplo.com.br")
        self.assertEqual((result["categoria"], result["certeza"]), ("dns_instavel", "incerto"))


class SsrfTests(DiagCase):
    def test_non_public_addresses_are_never_accepted(self):
        for blocked in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "192.168.1.1", "172.16.0.1", "0.0.0.0", "224.0.0.1",
                        "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::1", "not-an-ip", ""):
            self.assertFalse(is_public_address(blocked), blocked)
        for allowed in ("8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"):
            self.assertTrue(is_public_address(allowed), allowed)

    def test_a_host_that_resolves_to_an_internal_address_is_blocked_before_connecting(self):
        for ips in (["127.0.0.1"], ["169.254.169.254"], [PUBLIC, "10.0.0.5"], ["::ffff:192.168.0.9"]):
            self.net.dns["exemplo.com.br"] = ips
            self.net.calls.clear()
            self.assertEqual(self.diag.check_dns("exemplo.com.br")["resultado"], "bloqueado")
            result = self.diag.diagnose("exemplo.com.br")
            self.assertEqual(result["categoria"], "alvo_bloqueado")
            self.assertEqual([c for c in self.net.connections() if c[2] != "rdap.org"], [], ips)  # the registry is a public third party

    def test_the_connection_uses_the_validated_address_so_dns_rebinding_cannot_swap_it(self):
        answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])
        self.net.dns["exemplo.com.br"] = lambda: next(answers)
        self.net.web[("exemplo.com.br", "https", "/")] = HttpReply(200, {}, b"", {"not_after": "2026-12-01T00:00:00Z"})
        self.diag.check_http("exemplo.com.br")
        self.assertEqual([c[1] for c in self.net.connections()], [PUBLIC])  # connected to what was validated, resolved once
        self.assertEqual(self.net.calls.count(("resolve", "exemplo.com.br")), 1)
        again = self.diag.check_http("exemplo.com.br")  # the next lookup now answers with loopback: refused
        self.assertEqual(again["resultado"], "bloqueado")

    def test_redirects_to_internal_or_unauthorized_targets_are_reported_not_followed(self):
        self.healthy()
        for location, expected in (("http://169.254.169.254/latest/meta-data/", "endereco IP"), ("https://10.0.0.8/", "endereco IP"),
                                   ("https://[::1]/", "endereco IP"), ("https://outro-site.com/", "fora dos ativos"),
                                   ("ftp://exemplo.com.br/", "esquema nao permitido"), ("https://exemplo.com.br:8443/", "porta nao permitida"),
                                   ("gopher://exemplo.com.br/", "esquema nao permitido")):
            self.net.calls.clear()
            self.net.web[("exemplo.com.br", "https", "/")] = HttpReply(302, {"location": location})
            result = self.diag.check_http("exemplo.com.br")
            self.assertIn(expected, result["observacao"], location)
            self.assertEqual([c[2] for c in self.net.connections()], ["exemplo.com.br"], location)  # only the authorized host was contacted
        self.net.dns["internal.exemplo.com.br"] = ["10.1.2.3"]  # an authorized name pointing inside: blocked at resolution
        self.net.web[("exemplo.com.br", "https", "/")] = HttpReply(302, {"location": "https://internal.exemplo.com.br/"})
        self.assertEqual(self.diag.check_http("exemplo.com.br")["resultado"], "bloqueado")

    def test_redirect_chains_inside_authorized_hosts_are_followed_but_limited(self):
        self.healthy()
        self.net.dns["www.exemplo.com.br"] = [PUBLIC]
        self.net.web[("exemplo.com.br", "https", "/")] = HttpReply(301, {"location": "https://www.exemplo.com.br/inicio"})
        self.net.web[("www.exemplo.com.br", "https", "/inicio")] = HttpReply(200, {})
        result = self.diag.check_http("exemplo.com.br")
        self.assertEqual([h["status"] for h in result["redirecionamentos"]], [301, 200])
        self.net.web[("exemplo.com.br", "https", "/")] = HttpReply(302, {"location": "/"})  # a loop
        self.net.calls.clear()
        loop = self.diag.check_http("exemplo.com.br")
        self.assertIn("Redirecionamentos demais", loop["observacao"])
        self.assertLessEqual(len(self.net.connections()), 4)

    def test_every_diagnosis_has_a_request_budget(self):
        self.net.dns["exemplo.com.br"] = [PUBLIC]
        self.net.web[("exemplo.com.br", "https", "/")] = HttpReply(302, {"location": "/"})
        self.diag.diagnose("exemplo.com.br")
        self.assertLessEqual(len(self.net.connections()), 8)

    def test_only_authorized_domains_and_valid_names_are_probed(self):
        self.healthy("outro.com")
        for name in ("outro.com", "evil.exemplo.com.br.outro.com"):
            self.net.calls.clear()
            with self.assertRaises(DiagnosticRefused):
                self.diag.diagnose(name)
            self.assertEqual(self.net.calls, [], name)
        for bad in ("127.0.0.1", "10.0.0.1", "http://exemplo.com.br/", "exemplo.com.br/caminho", "exemplo.com.br:8080", "localhost",
                    "", "a b.com", "-x.com", "x" * 64 + ".com", "[::1]", "exemplo.com.br;x", "1.2.3.4"):
            with self.assertRaises(DiagnosticRefused, msg=repr(bad)):
                normalize_host(bad)
        self.hosts.add("loja.exemplo.com.br")
        self.healthy("www.exemplo.com.br")
        self.assertEqual(self.diag.check_dns("www.exemplo.com.br")["resultado"], "resolve")  # subdomain of an authorized asset


class SystemNetworkTests(unittest.TestCase):
    def test_the_real_resolver_maps_failures_and_never_needs_the_internet(self):
        import unittest.mock as mock
        network = SystemNetwork()
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(socket.EAI_NONAME, "x")):
            with self.assertRaises(DnsFailure) as raised:
                network.resolve("nao-existe.invalid")
            self.assertEqual(raised.exception.kind, "nao_resolve")
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(getattr(socket, "EAI_AGAIN", -3), "x")):
            with self.assertRaises(DnsFailure) as raised:
                network.resolve("x.invalid")
            self.assertEqual(raised.exception.kind, "falha_temporaria")
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("93.184.216.34", 0))]):
            self.assertEqual(network.resolve("x.example"), ["93.184.216.34"])

    def test_a_refused_local_connection_is_reported_as_such(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        with self.assertRaises(NetFailure) as raised:
            SystemNetwork().http("127.0.0.1", "x.example", port, "http", "/", timeout=2)
        self.assertIn(raised.exception.kind, ("recusada", "tempo_esgotado"))


class ToolTests(GatewayCase):
    def setUp(self):
        super().setUp()
        self.net = FakeNetwork()
        self.assets = AssetService(self.repo, self.access)
        self.gateway = Gateway(self.repo, self.access, self.skills, self.connections,
                               {"http": self.transport, "local": DiagnosticsTransport(self.assets, self.net, lambda: NOW)})
        self.entry = approve_catalog(self.connections, self.op)
        self.connections.register_connector("diagnostico")
        self.conn = self.connections.create_connection(self.ca, label="Diagnostico", provider="diagnostico", connection_kind="mcp",
                                                       catalog_entry_id=self.entry["id"])
        folder = Path(self.temp.name) / "pkg-diag"
        folder.mkdir()
        (folder / "skill.json").write_text(json.dumps({
            "manifest_version": 1, "id": "teste.diagnostico", "version": "1.0.0", "description": "Diagnostico de sites.",
            "instructions": "Investigue.", "required_tools": [{"tool": f"{CATALOG_KEY}.{t}", "operations": ["read"]} for t in TOOLS]}),
            encoding="utf-8")
        skill = self.skills.install(self.op, folder, self.skills.review_package(folder).content_hash)
        self.skills.grant_skill(self.ca, agent_id=self.agent["id"], skill_id=skill["id"], operations=["read"],
                                connection_id=self.conn["id"], attach=True)
        self.assets.add(self.ca, "exemplo.com.br", "site institucional")

    def alias(self, execution, tool):
        return next(t["name"] for t in self.gateway.list_tools(execution) if t["name"].endswith(tool))

    def test_assets_are_operator_only_unique_and_per_company(self):
        run = self.agents.start_run(self.ca, self.agent["id"], budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 60})
        agent_ctx = self.access.agent_context(self.op, self.a["id"], run["id"])
        with self.assertRaises(PermissionDenied):
            self.assets.add(agent_ctx, "evil.com")
        with self.assertRaises(PermissionDenied):
            self.assets.list(agent_ctx)
        with self.assertRaises(ValueError):
            self.assets.add(self.ca, "exemplo.com.br")
        with self.assertRaises(ValueError):
            self.assets.add(self.ca, "127.0.0.1")
        self.assertEqual(self.assets.list(self.cb), [])
        self.assertFalse(self.assets.authorized(self.b["id"], "exemplo.com.br"))
        asset = self.assets.list(self.ca)[0]
        self.assets.set_status(self.ca, asset["id"], asset["revision"], "inativo")
        self.assertFalse(self.assets.authorized(self.a["id"], "exemplo.com.br"))
        with self.assertRaises(NotFound):
            self.assets.set_status(self.cb, asset["id"], 1, "ativo")

    async def test_an_agent_diagnoses_an_authorized_domain_through_the_gateway(self):
        self.net.dns["exemplo.com.br"] = [PUBLIC]
        self.net.web[("exemplo.com.br", "https", "/")] = HttpReply(200, {"server": "nginx"}, b"", {"not_after": "2026-12-01T00:00:00Z"})
        execution, run = self.open()
        result = await self.gateway.call_tool(execution, self.alias(execution, "diagnosticar-site"), {"dominio": "exemplo.com.br"})
        self.assertEqual((result.data["categoria"], result.data["somente_leitura"]), ("site_ok", True))
        self.assertEqual({d["decision"] for d in self.decisions(run)}, {"allowed"})

    async def test_the_model_cannot_probe_hosts_outside_the_company_list(self):
        execution, _ = self.open()
        alias = self.alias(execution, "verificar-http")
        for target in ("outro.com", "169.254.169.254", "localhost", "exemplo.com.br.evil.com", "http://exemplo.com.br"):
            result = await self.gateway.call_tool(execution, alias, {"dominio": target})
            self.assertEqual(result.data["erro"], "nao_autorizado", target)
        self.assertEqual(self.net.calls, [])
        for bad in ({"dominio": "exemplo.com.br", "porta": 22}, {"dominio": "exemplo.com.br", "ip": "10.0.0.1"}, {}):
            with self.assertRaises(InvalidArguments):
                await self.gateway.call_tool(execution, alias, bad)

    def test_the_catalog_entry_is_local_and_read_only(self):
        self.assertEqual((self.entry["transport"], self.entry["url"], self.entry["launcher"]), ("local", None, None))
        self.assertEqual({s["operation"] for s in self.entry["tool_specs"].values()}, {"read"})
        self.assertFalse({"dns-update", "renovar", "pagar"} & set(self.entry["tools"]))


if __name__ == "__main__":
    unittest.main()
