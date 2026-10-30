"""Mailbox rules that outlive the conversation, created only with the person's click (#139).

A rule says: for this *account* and this *exact address or exact domain* (no implied subdomains), when this checkable condition holds
and no exception does, apply this authorised action to the triage. Nothing creates a rule by itself: not an example, not an earlier
cleaning, not silence, and not text inside an email. The model or a plain request only *proposes*; the card asks "create a rule or
apply it only this time?" and only the person's choice saves. Rules live in a local file (never in Git, the package or the team code).
This module logs nothing and never stores message content.
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mail_intent import _plain
from person_memory import clean

FILE = "regras-de-caixa.json"
MAX_VALUE = 80
MAX_EXCEPTIONS = 5
MAX_RULES = 100
MAX_PENDING = 10
PENDING_KEEP = timedelta(days=7)

ACTIONS = {"prioridade": "tratar como prioridade", "preservar": "preservar (nunca tratar como suspeita; conferir você)",
           "mover_lixeira": "sugerir mover para a Lixeira (nada é movido sem a sua aprovação de cada lote)"}
ACTIVE_ACTIONS = ("prioridade", "preservar", "mover_lixeira")  # a move rule only *suggests* in the triage: a move still needs the exact approval (#140)
CONDITIONS = {"qualquer": "qualquer mensagem", "assunto_contem": "o assunto contém “{}”", "texto_contem": "o texto contém “{}”",
              "lista": "é uma lista ou newsletter (tem cancelamento de inscrição)"}
NEEDS_VALUE = ("assunto_contem", "texto_contem")
STATUSES = ("ativa", "desativada", "substituida")

_ADDRESS = re.compile(r"^[a-z0-9._%+\-]{1,64}@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+$")
_DOMAIN = re.compile(r"^[a-z0-9\-]+(?:\.[a-z0-9\-]+)+$")
_LOCK = threading.Lock()
_REASONS = {
    "escopo": "O escopo precisa ser um endereço completo (ana@empresa.com) ou um domínio exato (empresa.com), sem curinga.",
    "condicao": "Condição desconhecida.", "valor": "A condição precisa de um texto curto.", "acao": "Ação desconhecida.",
    "conta": "A regra precisa de uma conta.", "texto": "O texto da regra foi recusado pela verificação.",
    "duplicada": "Já existe uma regra igual para esta conta.", "conflito": "Já existe uma regra com o mesmo escopo e condição e outra ação.",
    "inexistente": "Não há regra com esse identificador.", "limite": "Há regras demais.", "pendencias": "Há propostas demais esperando decisão.",
    "substituida": "Essa regra já foi substituída.", "excecoes": "Exceções demais ou inválidas.",
}


def _now():
    return datetime.now(timezone.utc)


def reason_text(code):
    return _REASONS.get(code, "A regra foi recusada.")


def account_of(mail):
    """``(key, label)`` of the mailbox in use: the personal one, or a company connection. A rule of one never applies to another."""
    source = getattr(mail, "source", None)
    if isinstance(source, dict) and source.get("company_id") and source.get("connection_id"):
        return f"empresa:{source['company_id']}:{source['connection_id']}", str(source.get("label") or "empresa")[:60]
    return "pessoal", "pessoal"


def _scope(kind, value):
    value = str(value or "").strip().lower()
    if kind == "endereco" and _ADDRESS.match(value):
        return {"tipo": "endereco", "valor": value}
    if kind == "dominio" and _DOMAIN.match(value):
        return {"tipo": "dominio", "valor": value}
    return None


def _condition(kind, value):
    if kind not in CONDITIONS:
        return None, "condicao"
    if kind not in NEEDS_VALUE:
        return {"tipo": kind, "valor": None}, None
    text, why = clean(str(value or ""))
    if why in ("vazio", "grande_demais"):
        return None, "valor"
    if why or len(text) > MAX_VALUE:
        return None, "texto"
    return {"tipo": kind, "valor": text}, None


def _exceptions(values):
    out = []
    for item in list(values or [])[: MAX_EXCEPTIONS + 1]:
        text, why = clean(str(item))
        if why or len(text) > MAX_VALUE:
            return None
        out.append(text)
    return out if len(out) <= MAX_EXCEPTIONS else None


def describe(rule):
    scope = rule["escopo"]
    where = f"o endereço {scope['valor']}" if scope["tipo"] == "endereco" else f"o domínio exato {scope['valor']} (sem subdomínios)"
    cond = rule["condicao"]
    when = CONDITIONS[cond["tipo"]].format(cond.get("valor"))
    text = f"mensagens de {where}, quando {when}"
    if rule.get("excecoes"):
        text += ", exceto se contiver " + " ou ".join(f"“{x}”" for x in rule["excecoes"])
    return f"{text}: {ACTIONS[rule['acao']]}"


def _examples(scope):
    value = scope["valor"]
    if scope["tipo"] == "endereco":
        domain = value.split("@", 1)[1]
        return {"positivos": [value], "negativos": [f"outra.pessoa@{domain}", f"{value.split('@', 1)[0]}@sub.{domain}"]}
    return {"positivos": [f"alguem@{value}"], "negativos": [f"alguem@sub.{value}", f"alguem@outro-{value}"]}


def matches(rule, message):
    """Whether an active rule applies to a parsed message of its account. Pure function of the rule and the message."""
    if rule.get("status") != "ativa":
        return False
    sender = str(message.get("sender") or "").lower()
    if "@" not in sender:
        return False
    scope = rule["escopo"]
    if scope["tipo"] == "endereco":
        if sender != scope["valor"]:
            return False
    elif sender.rsplit("@", 1)[1] != scope["valor"]:  # the whole host: sub.empresa.com is not empresa.com
        return False
    subject, text = _plain(message.get("subject") or ""), _plain(message.get("text") or "")
    cond = rule["condicao"]
    if cond["tipo"] == "assunto_contem" and _plain(cond["valor"]) not in subject:
        return False
    if cond["tipo"] == "texto_contem" and _plain(cond["valor"]) not in text:
        return False
    if cond["tipo"] == "lista" and not ((message.get("auto") or {}).get("list_unsubscribe") or (message.get("auto") or {}).get("list_id")):
        return False
    return not any(_plain(item) in subject or _plain(item) in text for item in rule.get("excecoes") or [])


def applied(rules, message):
    """``(actions, conflict)`` for a message: the actions of the matching rules; ``conflict`` when a move rule meets a preserve rule."""
    actions = []
    ids = []
    for rule in rules:
        if matches(rule, message):
            actions.append(rule["acao"])
            ids.append(rule["id"])
    conflict = "mover_lixeira" in actions and "preservar" in actions
    return actions, ids, conflict


class MailboxRules:
    def __init__(self, root, clock=_now):
        self.path, self.clock = Path(root) / FILE, clock

    # -- storage ------------------------------------------------------------------------------------------------------------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rules, pending = data.get("rules", []), data.get("pending", {})
            return [r for r in rules if isinstance(r, dict) and r.get("id") and r.get("acao") in ACTIONS], pending if isinstance(pending, dict) else {}
        except (OSError, ValueError, AttributeError):
            return [], {}

    def _save(self, rules, pending):
        limit = (self.clock() - PENDING_KEEP).isoformat()
        pending = {k: v for k, v in pending.items() if isinstance(v, dict) and v.get("criada_em", "") >= limit}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps({"rules": rules, "pending": pending}, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)

    def _state(self, rules, pending, **extra):
        return {"ok": "erro" not in extra, "rules": [self._view(r) for r in rules], "pending": [dict(p) for p in pending.values()], **extra}

    @staticmethod
    def _view(rule):
        return {**rule, "descricao": describe(rule)}

    def state(self):
        with _LOCK:
            return self._state(*self._load())

    def active_for(self, account):
        """The rules of triage for one account: active, of that account, and of an action that is in force."""
        rules, _ = self._load()
        return [r for r in rules if r.get("conta") == account and r.get("status") == "ativa" and r.get("acao") in ACTIVE_ACTIONS]

    def _refuse(self, rules, pending, why, **extra):
        return self._state(rules, pending, erro=reason_text(why), motivo=why, **extra)

    # -- building a rule (shared by proposals and the person's own edits) --------------------------------------------------
    def _build(self, rules, account, label, scope_kind, scope_value, cond_kind, cond_value, action, exceptions, replaces):
        if not account:
            return None, "conta"
        scope = _scope(scope_kind, scope_value)
        if scope is None:
            return None, "escopo"
        condition, why = _condition(cond_kind, cond_value)
        if why:
            return None, why
        if action not in ACTIONS:
            return None, "acao"
        extra = _exceptions(exceptions)
        if extra is None:
            return None, "excecoes"
        same = [r for r in rules if r["conta"] == account and r["status"] == "ativa" and r["escopo"] == scope and r["condicao"] == condition]
        if replaces:
            old = next((r for r in rules if r["id"] == replaces and r["conta"] == account), None)
            if old is None:
                return None, "inexistente"
            if old["status"] == "substituida":
                return None, "substituida"
        for other in same:
            if other["id"] == replaces:
                continue
            return None, "duplicada" if other["acao"] == action and sorted(other["excecoes"]) == sorted(extra) else "conflito"
        return {"conta": account, "conta_nome": label, "escopo": scope, "condicao": condition, "excecoes": extra, "acao": action,
                "substitui": replaces or None, "exemplos": _examples(scope)}, None

    # -- proposals: all the model (or a plain request) can do --------------------------------------------------------------
    def propose(self, account, label, scope_kind, scope_value, cond_kind, cond_value, action, exceptions=(), replaces=None):
        with _LOCK:
            rules, pending = self._load()
            built, why = self._build(rules, account, label, scope_kind, scope_value, cond_kind, cond_value, action, exceptions, replaces)
            if why:
                conflict = {}
                if why == "conflito":
                    scope = _scope(scope_kind, scope_value)
                    cond, _ = _condition(cond_kind, cond_value)
                    other = next((r for r in rules if r["conta"] == account and r["status"] == "ativa" and r["escopo"] == scope and r["condicao"] == cond), None)
                    conflict = {"regra_existente": self._view(other)} if other else {}
                return self._refuse(rules, pending, why, **conflict)
            if len([r for r in rules if r["status"] != "substituida"]) >= MAX_RULES:
                return self._refuse(rules, pending, "limite")
            if len(pending) >= MAX_PENDING:
                return self._refuse(rules, pending, "pendencias")
            proposal = {"id": "p-" + uuid.uuid4().hex[:8], **built, "criada_em": self.clock().isoformat()}
            proposal["pergunta"] = f"Criar regra para {describe(proposal)}, ou aplicar só desta vez?"
            pending[proposal["id"]] = proposal
            self._save(rules, pending)
            return {**self._state(rules, pending), "proposal": dict(proposal)}

    def decide(self, proposal_id, decision):
        """The person's choice on a card: ``criar`` saves the rule, ``uma_vez`` returns it without saving, ``recusar`` drops it."""
        with _LOCK:
            rules, pending = self._load()
            proposal = pending.pop(proposal_id, None) if isinstance(proposal_id, str) else None
            if proposal is None:
                return self._refuse(rules, pending, "inexistente")
            if decision == "recusar":
                self._save(rules, pending)
                return self._state(rules, pending, decidida="recusada")
            if decision == "uma_vez":  # applied once by the caller; nothing is stored
                self._save(rules, pending)
                return self._state(rules, pending, decidida="uma_vez", regra_temporaria={**proposal, "status": "ativa", "id": "temp-" + proposal["id"]})
            if decision != "criar":
                return self._refuse(rules, pending, "acao")
            built, why = self._build(rules, proposal["conta"], proposal["conta_nome"], proposal["escopo"]["tipo"], proposal["escopo"]["valor"],
                                     proposal["condicao"]["tipo"], proposal["condicao"]["valor"], proposal["acao"], proposal["excecoes"], proposal.get("substitui"))
            if why:  # the world changed since the question was asked
                self._save(rules, pending)
                return self._refuse(rules, pending, why)
            now = self.clock().isoformat()
            rule = {"id": "r-" + uuid.uuid4().hex[:8], "version": 1, "status": "ativa", **built, "substituida_por": None, "criada_em": now,
                    "atualizada_em": now, "aprovacao": {"em": now, "por": "pessoa", "via": "cartao"}}
            if built["substitui"]:
                for old in rules:
                    if old["id"] == built["substitui"]:
                        old.update(status="substituida", substituida_por=rule["id"], atualizada_em=now)
            rules.append(rule)
            self._save(rules, pending)
            return self._state(rules, pending, decidida="criada", regra=self._view(rule))

    # -- the person's own changes on the screen ----------------------------------------------------------------------------
    def set_status(self, rule_id, active):
        with _LOCK:
            rules, pending = self._load()
            rule = next((r for r in rules if r["id"] == rule_id), None)
            if rule is None:
                return self._refuse(rules, pending, "inexistente")
            if rule["status"] == "substituida":
                return self._refuse(rules, pending, "substituida")
            rule.update(status="ativa" if active else "desativada", atualizada_em=self.clock().isoformat())
            if active:  # turning it back on must not create a clash with another active rule
                clash = [r for r in rules if r is not rule and r["conta"] == rule["conta"] and r["status"] == "ativa" and r["escopo"] == rule["escopo"]
                         and r["condicao"] == rule["condicao"]]
                if clash:
                    rule["status"] = "desativada"
                    return self._refuse(rules, pending, "conflito")
            self._save(rules, pending)
            return self._state(rules, pending)

    def edit(self, rule_id, cond_value=None, exceptions=None, action=None):
        """Changes the condition text, the exceptions or the action of a rule: a new version of the same rule. Not the scope or account."""
        with _LOCK:
            rules, pending = self._load()
            rule = next((r for r in rules if r["id"] == rule_id), None)
            if rule is None:
                return self._refuse(rules, pending, "inexistente")
            if rule["status"] == "substituida":
                return self._refuse(rules, pending, "substituida")
            others = [r for r in rules if r["id"] != rule_id]
            built, why = self._build(others, rule["conta"], rule["conta_nome"], rule["escopo"]["tipo"], rule["escopo"]["valor"], rule["condicao"]["tipo"],
                                     rule["condicao"]["valor"] if cond_value is None else cond_value, action or rule["acao"],
                                     rule["excecoes"] if exceptions is None else exceptions, None)
            if why:
                return self._refuse(rules, pending, why)
            now = self.clock().isoformat()
            rule.update(condicao=built["condicao"], excecoes=built["excecoes"], acao=built["acao"], version=rule["version"] + 1, atualizada_em=now,
                        aprovacao={"em": now, "por": "pessoa", "via": "painel"})
            self._save(rules, pending)
            return self._state(rules, pending, regra=self._view(rule))


# -- plain requests ("sempre trate os emails de ana@x.com como prioridade") ---------------------------------------------------
_TARGET = r"(?P<alvo>[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}|(?:do\s+dom[ií]nio\s+|dom[ií]nio\s+)?(?P<dom>[a-z0-9\-]+(?:\.[a-z0-9\-]+)+))"
_PRIORITY = re.compile(
    rf"^\s*(?:(?:por favor|pode|poderia|quero que voc[eê])[,\s]+)*(?:sempre\s+)?(?:trate|considere|marque|classifique)\s+"
    rf"(?:os\s+|as\s+)?(?:e-?mails?|mensagens)\s+(?:de|do|da|vindos?\s+de)\s+(?:dom[ií]nio\s+)?{_TARGET}\s+como\s+(?:prioridade|priorit[aá]rios?|importantes?)\s*[.!]?\s*$",
    re.IGNORECASE)
_TRUSTED = re.compile(
    rf"^\s*(?:(?:por favor|pode|poderia|quero que voc[eê])[,\s]+)*(?:sempre\s+)?(?:trate|considere|marque|classifique)\s+"
    rf"(?:os\s+|as\s+)?(?:e-?mails?|mensagens)\s+(?:de|do|da|vindos?\s+de)\s+(?:dom[ií]nio\s+)?{_TARGET}\s+como\s+(?:confi[aá]ve(?:l|is)|de\s+clientes?|conhecidos?|seguros?)\s*[.!]?\s*$",
    re.IGNORECASE)


def parse_request(text):
    """``{"escopo": (tipo, valor), "condicao": ("qualquer", None), "acao": ...}`` for a short, plain request to create a rule, else ``None``."""
    if not isinstance(text, str) or len(text) > 200:
        return None
    for pattern, action in ((_PRIORITY, "prioridade"), (_TRUSTED, "preservar")):
        found = pattern.match(text)
        if found:
            target = found.group("alvo").strip().lower()
            if "@" in target:
                return {"escopo": ("endereco", target), "condicao": ("qualquer", None), "acao": action}
            domain = (found.group("dom") or target).strip().lower()
            return {"escopo": ("dominio", domain), "condicao": ("qualquer", None), "acao": action}
    return None
