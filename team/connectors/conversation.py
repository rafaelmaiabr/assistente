"""Connecting a system from the conversation (#107): intent -> check -> card -> secure form (or browser sign-in).

The model only asks for a card (``request``) by naming a capability or vendor; **the backend decides** what the person sees.
Nothing the model, a message, a page or a tool result says can open a form, pick a company or reach a secret: those steps are
messages that only the person's click produces, each tied to a card (``request_id``) this session created. The company comes
from the card's own list (every company of the local owner), never from text. Secrets travel only inside ``submit`` values,
straight to the vault through the connector catalog, and are never returned, stored in the session or put in an event.
"""
import secrets
import time
import unicodedata
from collections import OrderedDict
from urllib.parse import urlsplit

from ..access import AccessError, NotFound, PermissionDenied
from ..connections import ConnectionRejected

MAX_PENDING = 5
TTL_SECONDS = 3600
OAUTH_TIMEOUT = 180
STATE_TEXT = {"nao_configurada": "ainda nao foi configurada", "sem_credencial": "esta sem senha salva no cofre",
              "revogada": "teve a credencial revogada", "desativada": "esta desativada", "nao_testada": "ainda nao foi testada",
              "teste_falhou": "falhou no ultimo teste"}
CODE_TEXT = {"autenticacao_recusada": "O servidor recusou o usuario ou a senha (use senha de aplicativo, se o provedor exigir).",
             "tls_invalido": "Falha de TLS ou certificado nao aceito: confira servidor e porta.",
             "sem_conexao": "Nao foi possivel conectar: confira servidor, porta e rede.",
             "tempo_esgotado": "O servidor demorou demais para responder.", "cancelado": "A autorizacao foi cancelada.",
             "conta_nao_configurada": "Informe servidor, usuario e as demais configuracoes."}


def _norm(text):
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().casefold()
    return " ".join(plain.replace("-", " ").split())


def _names(driver, capability_label):
    d = driver.descriptor
    return {n for n in (_norm(driver.provider), _norm(d["label"]), *map(_norm, d["aliases"])) if n}, \
        {n for n in (_norm(driver.capability), _norm(capability_label)) if n}


def disabled_card():
    """The card when persistent attendance is off: there is no company to connect anything to."""
    return {"request_id": None, "state": "persistencia_desligada"}


def connection_state(view):
    """(state, reason): ``existente`` only when it was configured, has an active credential, is active and really tested OK."""
    if not view["configured"]:
        return "reconectar", "nao_configurada"
    if not view["credential"]:
        return "reconectar", "sem_credencial"
    if view["credential"]["state"] != "ativa":
        return "reconectar", "revogada"
    if view["status"] != "ativo":
        return "reconectar", "desativada"
    if not view["last_test"]:
        return "reconectar", "nao_testada"
    return ("existente", None) if view["last_test"]["ok"] else ("reconectar", "teste_falhou")


# Capabilities the chat itself reads through a connection. Empty on purpose: the chat's email tools read only the personal
# mailbox set up by the email skill, so a connection made from a card is tested and kept but not used by the chat (#117).
# Add a capability here only when the chat really uses it; the resume sentence and the notices depend on it.
CHAT_CAPABILITIES = frozenset()
CHAT_NOTE = ("O chat ainda nao usa esta conexao para ler: ela e testada e guardada, mas o assistente le so o email pessoal, "
             "configurado pela habilidade de email (peca para configurar a caixa de email).")


def _safe_url(url):
    parts = urlsplit(url) if isinstance(url, str) else None
    return bool(parts and parts.hostname and not parts.username and
                (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"))))


class IntegrationSession:
    """One per conversation. ``services`` is what ``TeamApp.services()`` returns (registry, connectors, team, access, operator)."""

    def __init__(self, services, clock=time.monotonic):
        self.services, self.clock = services, clock
        self.pending = OrderedDict()  # request_id -> {"provider", "created", "original", "flow"}

    # -- cards ----------------------------------------------------------------------------------------------------------
    def _drivers(self):
        registry = self.services.registry
        labels = {c.name: c.label for c in registry.capabilities()}
        return [(d, labels.get(d.capability, d.capability)) for d in registry.drivers() if d.setup is not None]

    def match(self, capability="", vendor=""):
        """Drivers the words may mean. The words only select among installed drivers; they never reach anything else."""
        found, vendor_key, capability_key = [], _norm(vendor), _norm(capability)
        drivers = self._drivers()
        if vendor_key:
            found = [d for d, cl in drivers if vendor_key in _names(d, cl)[0]] or [
                d for d, cl in drivers if any(len(n) >= 3 and (n in vendor_key or vendor_key in n) for n in _names(d, cl)[0])]
        if not found and capability_key:
            found = [d for d, cl in drivers if any(capability_key in n for n in _names(d, cl)[1]) or capability_key in _names(d, cl)[0]]
        return found

    def _remember(self, provider, original):
        now = self.clock()
        for key in [k for k, v in self.pending.items() if now - v["created"] > TTL_SECONDS]:
            self.pending.pop(key)
        request_id = secrets.token_hex(8)
        self.pending[request_id] = {"provider": provider, "created": now, "original": original, "flow": None}
        while len(self.pending) > MAX_PENDING:
            self.pending.popitem(last=False)
        return request_id

    def _driver(self, request_id):
        entry = self.pending.get(request_id) if isinstance(request_id, str) else None
        if entry is None or self.clock() - entry["created"] > TTL_SECONDS:
            raise NotFound()
        driver = self.services.registry.get(entry["provider"])
        if driver is None or driver.setup is None:
            raise NotFound()
        return entry, driver

    @staticmethod
    def _chat_uses(driver):
        return driver.capability in CHAT_CAPABILITIES

    def _done(self, entry, driver, message):
        """A success result. The earlier request resumes only when the chat really uses this connection; otherwise the
        message says so and nothing is resumed, so the person is not left waiting for a reading that will not happen."""
        if self._chat_uses(driver):
            return {"ok": True, "message": message, "original": entry["original"], "label": driver.descriptor["label"]}
        return {"ok": True, "message": f"{message} {CHAT_NOTE}", "label": driver.descriptor["label"]}

    def _companies(self):
        return self.services.team.list_companies(self.services.operator)

    def _ctx(self, company_id):
        if company_id not in {c["id"] for c in self._companies()}:
            raise NotFound()
        return self.services.access.tenant_context(self.services.operator, company_id)

    def _connections(self, ctx, driver):
        for capability in self.services.connectors.overview(ctx):
            for item in capability["drivers"]:
                if item["provider"] == driver.provider:
                    return item["connections"]
        return []

    def _row(self, company, driver):
        ctx = self.services.access.tenant_context(self.services.operator, company["id"])
        connections = self._connections(ctx, driver)
        if not connections:
            return {"company_id": company["id"], "company": company["name"], "state": "nova", "connection": None, "reason": None}
        ranked = sorted(connections, key=lambda c: connection_state(c)[0] != "existente")
        view = ranked[0]
        state, reason = connection_state(view)
        return {"company_id": company["id"], "company": company["name"], "state": state, "reason": STATE_TEXT.get(reason),
                "connection": {"id": view["id"], "label": view["label"]}}

    def card(self, request_id, driver):
        d = driver.descriptor
        companies = sorted(self._companies(), key=lambda c: (c["name"].casefold(), c["id"]))  # a stable order between refreshes
        return {"request_id": request_id, "state": "pronto" if companies else "sem_empresa", "provider": driver.provider,
                "label": d["label"], "mcp": d["protocol"] == "mcp", "auth": d["auth"]["type"],
                "notice": " ".join(filter(None, [
                    "" if d["protocol"] == "mcp" else f"Este conector nao usa MCP: {d['non_mcp_justification']}",
                    "" if self._chat_uses(driver) else CHAT_NOTE])),
                "rows": [self._row(c, driver) for c in companies]}

    def request(self, capability="", vendor="", original=None):
        """The card for "connect X". Unknown or ambiguous words give an honest card, never a guessed connector."""
        found = self.match(capability, vendor)
        if len(found) == 1:
            return self.card(self._remember(found[0].provider, original), found[0])
        wanted = vendor or capability
        request_id = self._remember(None, original)
        if found:
            return {"request_id": request_id, "state": "escolher", "options": [{"provider": d.provider, "label": d.descriptor["label"]} for d in found]}
        return {"request_id": request_id, "state": "indisponivel", "wanted": str(wanted)[:60],
                "available": [d.descriptor["label"] for d, _ in self._drivers()]}

    def refresh(self, request_id):
        """The same card with the rows as they are now (after a connection was made, changed or failed to test)."""
        entry, driver = self._driver(request_id)
        return self.card(request_id, driver)

    def pick(self, request_id, provider):
        entry = self.pending.get(request_id) if isinstance(request_id, str) else None
        driver = self.services.registry.get(provider) if isinstance(provider, str) else None
        if entry is None or driver is None or driver.setup is None:
            raise NotFound()
        return self.card(self._remember(provider, entry["original"]), driver)

    # -- the person's clicks ---------------------------------------------------------------------------------------------------
    def open(self, request_id, company_id):
        """The secure form for this card and company: fields from the driver's descriptor, stored non-secret values, no secrets."""
        entry, driver = self._driver(request_id)
        ctx = self._ctx(company_id)
        row = self._row({"id": company_id, "name": next(c["name"] for c in self._companies() if c["id"] == company_id)}, driver)
        values = {}
        if row["connection"]:
            for item in self._connections(ctx, driver):
                if item["id"] == row["connection"]["id"] and item["values"]:
                    values = {k: str(v) for k, v in item["values"].items()}
        d = driver.descriptor
        return {"request_id": request_id, "company_id": company_id, "company": row["company"], "provider": driver.provider,
                "label": d["label"], "auth": d["auth"]["type"], "steps": d["steps"], "fields": d["fields"],
                "editing": row["connection"] is not None, "connection_label": row["connection"] and row["connection"]["label"],
                "values": values}

    def use(self, request_id, company_id):
        entry, driver = self._driver(request_id)
        company = next((c for c in self._companies() if c["id"] == company_id), None)
        if company is None:
            raise NotFound()
        row = self._row(company, driver)
        if row["state"] != "existente":
            return {"ok": False, "message": "Esta conexao " + (row["reason"] or "ainda nao existe") + ". Use Reconectar."}
        return self._done(entry, driver, f"Usando a conexao existente: {row['connection']['label']}.")

    def submit(self, request_id, company_id, label, values):
        """Create or update the connection from the form and test it for real. The secret is not kept, returned or logged."""
        entry, driver = self._driver(request_id)
        ctx = self._ctx(company_id)
        catalog, operator = self.services.connectors, self.services.operator
        try:
            existing = self._connections(ctx, driver)
            if existing:
                connection_id = existing[0]["id"]
                catalog.reconfigure(operator, ctx, connection_id, values)
            else:
                connection_id = catalog.connect(operator, ctx, driver.provider, str(label or driver.descriptor["label"])[:120], values)["id"]
            if driver.descriptor["auth"]["type"] == "oauth":
                return {"ok": True, "next": "oauth", "connection_id": connection_id,
                        "message": "Configuracao salva. Falta autorizar no navegador."}
            outcome = catalog.test(ctx, connection_id)
        except (ConnectionRejected, NotFound) as error:
            return {"ok": False, "message": str(error) if isinstance(error, ConnectionRejected) else "Recurso nao encontrado."}
        except (PermissionDenied, AccessError):
            return {"ok": False, "message": "Acao nao permitida."}
        if not outcome["ok"]:
            return {"ok": False, "connection_id": connection_id,
                    "message": outcome["mensagem"] or CODE_TEXT.get(outcome["erro"], "O teste falhou. Confira as configuracoes.")}
        return {**self._done(entry, driver, f"{driver.descriptor['label']} conectado e testado."), "connection_id": connection_id}

    # -- browser sign-in -------------------------------------------------------------------------------------------------------
    def oauth_begin(self, request_id, company_id):
        entry, driver = self._driver(request_id)
        if driver.descriptor["auth"]["type"] != "oauth":
            raise NotFound()
        ctx = self._ctx(company_id)
        connections = self._connections(ctx, driver)
        if not connections:
            return {"ok": False, "message": "Salve a configuracao antes de autorizar no navegador."}
        started = driver.setup.oauth_begin(ctx, connections[0]["id"])
        if not _safe_url(started["url"]):
            driver.setup.oauth_cancel(ctx, started["flow_id"])
            return {"ok": False, "message": "O provedor devolveu um endereco de autorizacao inseguro."}
        entry["flow"] = {"flow_id": started["flow_id"], "company_id": company_id, "connection_id": connections[0]["id"]}
        return {"ok": True, "url": started["url"], "expires_at": started.get("expires_at")}

    def oauth_wait(self, request_id, timeout=OAUTH_TIMEOUT):
        """Blocking: run off the event loop. Waits for the browser step, then tests the connection for real."""
        entry, driver = self._driver(request_id)
        flow = entry["flow"]
        if flow is None:
            raise NotFound()
        ctx = self._ctx(flow["company_id"])
        result = driver.setup.oauth_complete(flow["flow_id"], timeout)
        entry["flow"] = None
        if not result["ok"]:
            return {"ok": False, "message": result.get("mensagem") or CODE_TEXT.get(result.get("erro"), "A autorizacao nao foi concluida.")}
        outcome = self.services.connectors.test(ctx, flow["connection_id"])
        if not outcome["ok"]:
            return {"ok": False, "message": outcome["mensagem"] or CODE_TEXT.get(outcome["erro"], "O teste falhou.")}
        return self._done(entry, driver, f"{driver.descriptor['label']} conectado e testado.")

    def oauth_cancel(self, request_id):
        entry, driver = self._driver(request_id)
        flow = entry["flow"]
        if flow is not None:
            driver.setup.oauth_cancel(self._ctx(flow["company_id"]), flow["flow_id"])
