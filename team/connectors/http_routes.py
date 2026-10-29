"""Routes the operator allows the assistant to read (GET, public, no credentials) and the one call that uses them.

A route is a URL template with exactly one ``{valor}`` of a known kind. The model picks a route by name and gives a
value; it never sends an address. The value is validated and encoded before it goes into the URL, and the final URL
passes the same checks as any other (``http_get.parse_url``). Routes are changed only by the operator
(``scripts/http_routes.py``), never from the conversation.
"""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .http_get import HttpFetchError, parse_url

MAX_CUSTOM = 20
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")
ROUTES_FILE = "http-routes.json"  # in the data root, next to the other local files
KINDS = {
    "dominio": ("um dominio, por exemplo exemplo.com.br", lambda v: v.strip().lower().rstrip("."), lambda v: bool(_DOMAIN.match(v))),
    "cnpj": ("um CNPJ com 14 digitos", lambda v: re.sub(r"[.\-/\s]", "", v), lambda v: bool(re.fullmatch(r"\d{14}", v))),
    "cep": ("um CEP com 8 digitos", lambda v: re.sub(r"[.\-\s]", "", v), lambda v: bool(re.fullmatch(r"\d{8}", v))),
    "numero": ("um numero inteiro", lambda v: v.strip(), lambda v: bool(re.fullmatch(r"\d{1,18}", v))),
}


class RouteRejected(ValueError):
    """Invalid route or value; the message is safe to show."""


@dataclass(frozen=True)
class Route:
    name: str
    label: str
    template: str
    kind: str
    builtin: bool = False

    def url_for(self, value):
        """The final address for ``value``, or ``RouteRejected``. Only the value changes; the host and path are the operator's."""
        if not isinstance(value, str) or not value.strip() or len(value) > 253:
            raise RouteRejected(f"Informe {KINDS[self.kind][0]}.")
        clean = KINDS[self.kind][1](value)
        if not KINDS[self.kind][2](clean):
            raise RouteRejected(f"Valor invalido: informe {KINDS[self.kind][0]}.")
        url = self.template.replace("{valor}", quote(clean, safe=""))
        parse_url(url)  # the same checks as any address
        return url


# Reviewed in code, off until the operator turns them on.
BUILTIN = {
    "registro-dominio": Route("registro-dominio", "Registro de dominio .br (BrasilAPI)", "https://brasilapi.com.br/api/registrobr/v1/{valor}",
                              "dominio", builtin=True),
}


def validate_route(name, label, template, kind):
    """A normalized ``Route`` or ``RouteRejected``. The template must be an https name with one ``{valor}`` outside the host."""
    if not (isinstance(name, str) and _NAME.match(name)):
        raise RouteRejected("Nome da rota invalido (use a-z, 0-9 e hifen, de 2 a 40 caracteres).")
    if not (isinstance(label, str) and 1 <= len(label.strip()) <= 80):
        raise RouteRejected("Descricao da rota invalida (1 a 80 caracteres).")
    if kind not in KINDS:
        raise RouteRejected(f"Tipo de valor invalido: use {', '.join(KINDS)}.")
    rest = template.replace("{valor}", "") if isinstance(template, str) else ""
    if not isinstance(template, str) or template.count("{valor}") != 1 or "{" in rest or "}" in rest:
        raise RouteRejected("O endereco deve ter exatamente um {valor}.")
    authority = re.match(r"^[a-z]+://([^/?#]*)", template)
    if authority is None or "{valor}" in authority.group(1):
        raise RouteRejected("O {valor} nao pode estar no nome do servidor.")
    try:
        parse_url(template.replace("{valor}", "exemplo"))
    except HttpFetchError as error:
        raise RouteRejected(error.message) from None
    return Route(name, label.strip(), template, kind)


class RouteStore:
    """The operator's routes: the reviewed ones plus the ones they added, and which are on. A plain local file, no secrets."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        stored = raw.get("custom") if isinstance(raw.get("custom"), dict) else {}
        custom = {}
        for name, item in stored.items():
            try:
                custom[name] = validate_route(name, item.get("label"), item.get("template"), item.get("kind"))
            except (RouteRejected, AttributeError):
                continue  # a hand-edited entry that no longer validates is ignored, never used
        enabled = raw.get("enabled") if isinstance(raw.get("enabled"), list) else []
        return custom, {name for name in enabled if isinstance(name, str)}

    def _save(self, custom, enabled):
        data = {"custom": {n: {"label": r.label, "template": r.template, "kind": r.kind} for n, r in sorted(custom.items())},
                "enabled": sorted(enabled)}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def routes(self):
        """Every route with whether it is on."""
        custom, enabled = self._load()
        return [(route, route.name in enabled) for route in sorted({**BUILTIN, **custom}.values(), key=lambda r: r.name)]

    def enabled_routes(self):
        return [route for route, on in self.routes() if on]

    def enabled(self, name):
        return next((r for r in self.enabled_routes() if r.name == name), None) if isinstance(name, str) else None

    def enable(self, name, on=True):
        custom, enabled = self._load()
        if name not in {**BUILTIN, **custom}:
            raise RouteRejected("Rota desconhecida.")
        enabled.add(name) if on else enabled.discard(name)
        self._save(custom, enabled)

    def add(self, name, label, template, kind):
        custom, enabled = self._load()
        route = validate_route(name, label, template, kind)
        if name in BUILTIN:
            raise RouteRejected("Ja existe uma rota revisada com este nome.")
        if name not in custom and len(custom) >= MAX_CUSTOM:
            raise RouteRejected(f"No maximo {MAX_CUSTOM} rotas proprias.")
        custom[name] = route
        self._save(custom, enabled)
        return route

    def remove(self, name):
        custom, enabled = self._load()
        if name not in custom:
            raise RouteRejected("Somente rotas proprias podem ser removidas.")
        del custom[name]
        enabled.discard(name)
        self._save(custom, enabled)


def consult(store, client, name, value):
    """Read one enabled route. Always a dict: the answer, or ``{"erro", "mensagem"}``. The answer is marked untrusted."""
    route = store.enabled(name)
    if route is None:
        return {"erro": "rota_nao_liberada", "mensagem": "Esta rota nao esta liberada pelo operador.",
                "disponiveis": [r.name for r in store.enabled_routes()]}
    try:
        url = route.url_for(value)
        answer = client.get(url)
    except RouteRejected as error:
        return {"erro": "valor_invalido", "mensagem": str(error)}
    except HttpFetchError as error:
        return {"erro": error.code, "mensagem": error.message}
    text = answer.body.decode("utf-8", errors="replace")
    content, kind = text, "texto"
    if answer.content_type == "application/json":
        try:
            content, kind = json.loads(text), "json"
        except ValueError:
            pass
    return {"dados_nao_confiaveis": True, "rota": route.name, "status": answer.status, "formato": kind, "conteudo": content}
