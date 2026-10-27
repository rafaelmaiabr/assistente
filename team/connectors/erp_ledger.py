"""Charge reconciliation: what the ERP proved, applied once, in a sane order, for the right customer.

Rules (``Reconciler``):

* Only the ERP proves a state change: an HMAC-authenticated webhook or an authorized poll. A customer's message is
  recorded as a claim and changes nothing (``note_customer_claim``).
* Idempotent: an event id seen before changes nothing; the same transition again is ``ja_aplicado``; a duplicate never
  produces a second payment receipt.
* Out of order: an event that jumps ahead (a refund before the payment) waits until its predecessors arrive; an event
  older than the current state is ``obsoleto``.
* Wrong customer, amount or currency, or a state that contradicts money already moved (payment on a cancelled charge),
  is ``divergente``: nothing is applied and the charge is flagged for a person.
* A charge unknown to this company is ``desconhecida`` (also what another company's document looks like).
* A lost response to an issue request blocks repeating it until the result is verified (``UnknownResult``).

Storage is a small key-value interface (``MemoryStore`` here); the persistent store is a separate delivery (see
docs/TEAM-CONNECTORS-ERP.md). Nothing in this module moves money, issues anything or contacts a customer.
"""
import copy
import hashlib
import json
import re

from .erp import CHARGE_STATES, ChargeRejected, normalize_charge, verify_hmac_sha256

SOURCES = ("webhook", "polling")
_VALID = {"emitida": {"vencida", "paga", "cancelada"}, "vencida": {"paga", "cancelada"}, "paga": {"estornada"},
          "estornada": set(), "cancelada": set()}
_RANK = {"emitida": 0, "vencida": 1, "paga": 2, "estornada": 3, "cancelada": 3}
_CONFLICTS = {("cancelada", "paga"), ("cancelada", "estornada"), ("paga", "cancelada"), ("estornada", "cancelada")}
_MOMENT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
MAX_PARKED, MAX_HISTORY, MAX_NOTES = 20, 50, 20


class LedgerRejected(ValueError):
    """Invalid input; safe to show, never contains secrets."""


class UnknownResult(LedgerRejected):
    """An earlier attempt with this key may have gone through: verify in the ERP before repeating anything."""


class NotSent(Exception):
    """Raised by a driver's ``call`` when the request provably never left (connection refused before sending, bad
    input rejected locally): the key is released instead of marked unknown."""


class MemoryStore:
    """Namespaced key-value store (JSON-serializable values, copied in and out)."""

    def __init__(self):
        self._data = {}

    def get(self, namespace, key):
        value = self._data.get((namespace, key))
        return None if value is None else copy.deepcopy(value)

    def put(self, namespace, key, value):
        self._data[(namespace, key)] = copy.deepcopy(value)


def _key(*parts):
    return "\x1f".join(parts)


def _party(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _event(raw):
    if not isinstance(raw, dict):
        raise LedgerRejected("Evento invalido.")
    event_id, documento = raw.get("event_id"), raw.get("documento")
    status, moment = raw.get("status"), raw.get("occurred_at")
    if not (isinstance(event_id, str) and 1 <= len(event_id) <= 200 and isinstance(documento, str) and 1 <= len(documento) <= 80):
        raise LedgerRejected("Evento sem identificador ou documento.")
    status = "emitida" if status == "pendente" else status
    if status not in CHARGE_STATES or not (isinstance(moment, str) and _MOMENT.match(moment)):
        raise LedgerRejected("Evento com status ou data invalidos (use AAAA-MM-DDTHH:MM:SSZ em UTC).")
    valor, moeda = raw.get("valor_centavos"), raw.get("moeda")
    if valor is not None and (isinstance(valor, bool) or not isinstance(valor, int) or valor < 0):
        raise LedgerRejected("Valor do evento invalido.")
    if moeda is not None and not (isinstance(moeda, str) and re.match(r"^[A-Za-z]{3}$", moeda)):
        raise LedgerRejected("Moeda do evento invalida.")
    return {"event_id": event_id, "documento": documento, "status": status, "occurred_at": moment,
            "cliente": raw.get("cliente"), "valor_centavos": valor, "moeda": moeda.lower() if moeda else None}


class Reconciler:
    def __init__(self, store=None):
        self.store = store or MemoryStore()

    # -- charges -------------------------------------------------------------------------------------------------------
    def charge(self, tenant_id, provider, documento):
        return self.store.get("charge", _key(tenant_id, provider, documento))

    def _save(self, tenant_id, provider, charge):
        self.store.put("charge", _key(tenant_id, provider, charge["documento"]), charge)

    def record_issued(self, tenant_id, provider, snapshot):
        """Register a charge the ERP confirmed (an issue that succeeded, or an adopted one). Returns the ledger entry."""
        data = normalize_charge(snapshot)
        existing = self.charge(tenant_id, provider, data["documento"])
        if existing is not None:
            return existing
        entry = {**data, "extras": data["extras"], "historico": [], "recibos": [], "divergencias": [], "alegacoes": [],
                 "revisao_pendente": False, "aguardando": []}
        self._save(tenant_id, provider, entry)
        return entry

    # -- events --------------------------------------------------------------------------------------------------------
    def ingest_webhook(self, tenant_id, provider, body, signature, secret, parse, *, prefix=""):
        """Authenticate the raw body first; nothing about an unauthenticated request is parsed, stored or revealed."""
        if not verify_hmac_sha256(secret, body, signature, prefix=prefix):
            return {"resultado": "assinatura_invalida"}
        try:
            event = parse(body)
        except Exception:
            return {"resultado": "evento_invalido"}
        try:
            return self.apply(tenant_id, provider, event, source="webhook")
        except LedgerRejected:
            return {"resultado": "evento_invalido"}

    def apply_snapshot(self, tenant_id, provider, snapshot, observed_at, *, adopt=False):
        """An authorized poll: turn the ERP's current view of a charge into an event with a deterministic id."""
        data = normalize_charge(snapshot)
        if adopt and self.charge(tenant_id, provider, data["documento"]) is None:
            self.record_issued(tenant_id, provider, {**data, "status": "emitida"})
        digest = hashlib.sha256(json.dumps([provider, data["documento"], data["status"], data["valor_centavos"], data["moeda"],
                                            data["cliente"]], ensure_ascii=False).encode()).hexdigest()[:32]
        return self.apply(tenant_id, provider, {"event_id": f"poll:{digest}", "documento": data["documento"], "status": data["status"],
                                                "occurred_at": observed_at, "cliente": data["cliente"],
                                                "valor_centavos": data["valor_centavos"], "moeda": data["moeda"]}, source="polling")

    def note_customer_claim(self, tenant_id, provider, documento, text=None):
        """A customer says they paid. That is a claim, not proof: recorded for the person at Finance, state unchanged."""
        charge = self.charge(tenant_id, provider, documento) if isinstance(documento, str) else None
        if charge is None:
            return {"resultado": "desconhecida"}
        charge["alegacoes"] = (charge["alegacoes"] + [{"nota": str(text or "")[:300]}])[-MAX_NOTES:]
        self._save(tenant_id, provider, charge)
        return {"resultado": "alegacao_registrada", "estado": charge["status"],
                "aviso": "Mensagem do cliente nao comprova pagamento; aguarde confirmacao do ERP."}

    def apply(self, tenant_id, provider, raw_event, *, source):
        if source not in SOURCES:
            return {"resultado": "fonte_nao_prova_pagamento"}
        event = _event(raw_event)
        seen_key = _key(tenant_id, provider, event["event_id"])
        if self.store.get("event", seen_key) is not None:
            return {"resultado": "duplicado"}
        charge = self.charge(tenant_id, provider, event["documento"])
        if charge is None:
            self.store.put("event", seen_key, True)
            return {"resultado": "desconhecida"}
        result = self._decide(tenant_id, provider, charge, event, source)
        self.store.put("event", seen_key, True)
        self._save(tenant_id, provider, charge)
        return result

    def _decide(self, tenant_id, provider, charge, event, source):
        reason = None
        if event["cliente"] is not None and _party(event["cliente"]) != _party(charge["cliente"]):
            reason = "cliente_incorreto"
        elif event["moeda"] is not None and event["moeda"] != charge["moeda"]:
            reason = "moeda_divergente"
        elif event["valor_centavos"] is not None and event["valor_centavos"] != charge["valor_centavos"]:
            reason = "valor_divergente"
        elif (charge["status"], event["status"]) in _CONFLICTS:
            reason = "estado_contraditorio"
        if reason:
            charge["divergencias"] = (charge["divergencias"] + [{"event_id": event["event_id"], "motivo": reason}])[-MAX_NOTES:]
            charge["revisao_pendente"] = True
            return {"resultado": "divergente", "motivo": reason, "estado": charge["status"]}
        if event["status"] == charge["status"]:
            return {"resultado": "ja_aplicado", "estado": charge["status"]}
        if event["status"] in _VALID[charge["status"]]:
            self._transition(charge, event, source)
            self._drain(charge, source)
            return {"resultado": "aplicado", "estado": charge["status"]}
        if _RANK[event["status"]] <= _RANK[charge["status"]]:
            return {"resultado": "obsoleto", "estado": charge["status"]}
        parked = charge.setdefault("aguardando", [])
        if len(parked) < MAX_PARKED:
            parked.append(event)
        return {"resultado": "aguardando_ordem", "estado": charge["status"]}

    @staticmethod
    def _transition(charge, event, source):
        charge["status"] = event["status"]
        charge["historico"] = (charge["historico"] + [{"event_id": event["event_id"], "status": event["status"],
                                                       "occurred_at": event["occurred_at"], "fonte": source}])[-MAX_HISTORY:]
        if event["status"] in ("paga", "estornada"):
            charge["recibos"].append({"tipo": event["status"], "event_id": event["event_id"], "valor_centavos": charge["valor_centavos"],
                                      "moeda": charge["moeda"], "occurred_at": event["occurred_at"], "fonte": source, "comprovado": True})

    def _drain(self, charge, source):
        progress = True
        while progress and charge.get("aguardando"):
            progress = False
            for event in sorted(charge["aguardando"], key=lambda e: e["occurred_at"]):
                if event["status"] in _VALID[charge["status"]]:
                    charge["aguardando"].remove(event)
                    self._transition(charge, event, source)
                    progress = True
                    break

    # -- issue requests: a lost response blocks repeating ---------------------------------------------------------------
    def begin_emission(self, tenant_id, provider, chave, payload):
        """Reserve ``chave`` for one issue request. Replays return the charge; unknown outcomes block."""
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        record = self.store.get("emission", _key(tenant_id, provider, chave))
        if record is not None:
            if record["payload"] != digest:
                raise LedgerRejected("A chave de idempotencia ja foi usada com outros dados.")
            if record["state"] == "emitida":
                return {"repetida": True, "documento": record["documento"]}
            if record["state"] in ("em_andamento", "desconhecido"):
                raise UnknownResult("Uma tentativa anterior com esta chave nao teve resposta confirmada: verifique no ERP antes de repetir.")
        self.store.put("emission", _key(tenant_id, provider, chave), {"state": "em_andamento", "payload": digest, "documento": None})
        return {"repetida": False}

    def finish_emission(self, tenant_id, provider, chave, outcome, snapshot=None):
        record = self.store.get("emission", _key(tenant_id, provider, chave))
        if record is None or outcome not in ("emitida", "desconhecido"):
            raise LedgerRejected("Emissao desconhecida.")
        if outcome == "emitida":
            entry = self.record_issued(tenant_id, provider, snapshot)
            record.update(state="emitida", documento=entry["documento"])
        else:
            record["state"] = "desconhecido"
        self.store.put("emission", _key(tenant_id, provider, chave), record)

    def resolve_emission(self, tenant_id, provider, chave, found=None):
        """After checking the ERP: ``found`` is the charge it holds for this key, or None if it holds none."""
        record = self.store.get("emission", _key(tenant_id, provider, chave))
        if record is None or record["state"] not in ("em_andamento", "desconhecido"):
            raise LedgerRejected("Nada a resolver para esta chave.")
        if found is not None:
            self.finish_emission(tenant_id, provider, chave, "emitida", found)
        else:
            record["state"] = "nao_emitida"
            self.store.put("emission", _key(tenant_id, provider, chave), record)
        return self.store.get("emission", _key(tenant_id, provider, chave))["state"]

    def release_emission(self, tenant_id, provider, chave):
        record = self.store.get("emission", _key(tenant_id, provider, chave))
        if record is not None and record["state"] == "em_andamento":
            record["state"] = "nao_emitida"
            self.store.put("emission", _key(tenant_id, provider, chave), record)

    def emission_state(self, tenant_id, provider, chave):
        record = self.store.get("emission", _key(tenant_id, provider, chave))
        return None if record is None else record["state"]


async def run_emission(ledger, tenant_id, provider, chave, payload, call):
    """For drivers: guard a real issue call. ``call`` is an async function that talks to the ERP and returns the
    charge snapshot. If it fails after it may have left (any exception, including a cancel), the key is marked unknown."""
    state = ledger.begin_emission(tenant_id, provider, chave, payload)
    if state["repetida"]:
        return {"repetida": True, "documento": state["documento"]}
    try:
        snapshot = await call()
        ledger.finish_emission(tenant_id, provider, chave, "emitida", snapshot)
    except NotSent:
        ledger.release_emission(tenant_id, provider, chave)
        raise
    except BaseException:
        ledger.finish_emission(tenant_id, provider, chave, "desconhecido")
        raise
    return {"repetida": False, "documento": snapshot["documento"]}


__all__ = ["ChargeRejected", "LedgerRejected", "MemoryStore", "NotSent", "Reconciler", "UnknownResult", "run_emission"]
