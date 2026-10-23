"""Persistent cases: one owner, a validated state machine, append-only history and evidence.

A case survives closing the chat, the window or the process: state, owner, timeline and evidence
live in the database, and every change commits together with its event and outbox row. The history
keeps what was done and said, never a model's private reasoning, and an AI summary is a note, not
proof that anything was executed.
"""
import re
from datetime import datetime, timezone

from .access import AccessContext, AccessError, NotFound, PermissionDenied
from .enums import CASE_STATES, CASE_TRANSITIONS, can_transition
from .ids import new_id
from .repository import IntegrityViolation, RevisionConflict

_REASONING = re.compile(r"<\s*(think|thinking|reasoning)\s*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_OPEN_REASONING = re.compile(r"<\s*(think|thinking|reasoning)\s*>.*\Z", re.I | re.S)
TERMINAL_STATES = ("resolvida", "cancelada", "falhou")
_NEEDS_REASON = ("pausada", "falhou", "cancelada")
_TECHNICAL = ("resultado_ferramenta", "recibo", "documento")


class CaseRejected(ValueError):
    """The change is not allowed; the message is safe to show."""


class InvalidTransition(CaseRejected):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(text):
    """Drop model reasoning blocks (<think>…) before anything is stored; refuse text that is only reasoning."""
    if not isinstance(text, str):
        raise CaseRejected("Texto invalido.")
    cleaned = _OPEN_REASONING.sub("", _REASONING.sub("", text)).strip()
    if not cleaned:
        raise CaseRejected("O texto esta vazio ou continha apenas raciocinio interno do modelo.")
    return cleaned


class CaseService:
    def __init__(self, repository, access):
        self.repository, self.access = repository, access

    # -- helpers ---------------------------------------------------------------------------------------------
    def _ctx(self, ctx):
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()

    def _case(self, session, ctx, case_id, action):
        case = session.get("case", case_id) if isinstance(case_id, str) else None
        if case is None or not self.access.allowed(ctx, session, action, "case", case_id):
            raise NotFound()
        return case

    def _event(self, ctx, case, kind, payload):
        return {"kind": "event", "schema_version": 1, "id": new_id("event"), "tenant_id": ctx.tenant_id,
                "type": kind, "case_id": case["id"], "correlation_id": new_id("correlation"), "causation_id": None,
                "actor_id": ctx.principal_id, "revision": case["revision"], "occurred_at": case["updated_at"],
                "payload": payload}

    # -- opening and reading ---------------------------------------------------------------------------------
    def open_case(self, ctx, *, department_id, objective, customer_ref=None, priority="normal", origin="operador"):
        """Create a case in the initial queue (``recebida``, no owner)."""
        self._ctx(ctx)
        now = _now()
        case = {"kind": "case", "schema_version": 1, "id": new_id("case"), "tenant_id": ctx.tenant_id, "revision": 1,
                "created_at": now, "updated_at": now, "department_id": department_id, "state": "recebida",
                "owner_agent_id": None, "customer_ref": customer_ref, "objective": clean_text(objective),
                "closed_at": None, "priority": priority, "origin": origin}
        with self.repository.transaction(ctx.tenant_id) as session:
            if not self.access.allowed(ctx, session, "write", "case"):
                raise PermissionDenied()
            if session.get("department", department_id) is None:
                raise NotFound()
            session.save(case, event=self._event(ctx, case, "case.opened", {"origin": origin, "priority": priority}))
            return case

    def get_case(self, ctx, case_id):
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            return self._case(session, ctx, case_id, "read")

    def list_cases(self, ctx, state=None, owner_agent_id=None, department_id=None, limit=100):
        self._ctx(ctx)
        filters = {k: v for k, v in (("state", state), ("owner_agent_id", owner_agent_id),
                                     ("department_id", department_id)) if v is not None}
        with self.repository.transaction(ctx.tenant_id) as session:
            return [c for c in session.list("case", limit=limit, **filters)
                    if self.access.allowed(ctx, session, "read", "case", c["id"])]

    # -- state machine ----------------------------------------------------------------------------------------
    def assign_owner(self, ctx, case_id, expected_revision, agent_id):
        """Give a case in the initial queue its single owner. Later changes of owner are handoffs (T11)."""
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            case = self._case(session, ctx, case_id, "write")
            if case["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            if case["owner_agent_id"] is not None:
                raise CaseRejected("A demanda ja tem responsavel; use a transferencia de responsavel.")
            if case["state"] not in ("recebida", "triagem"):
                raise CaseRejected("So e possivel atribuir responsavel na fila inicial.")
            agent = session.get("agent_definition", agent_id) if isinstance(agent_id, str) else None
            if agent is None:
                raise NotFound()
            if agent["status"] != "ativo":
                raise CaseRejected("Agente desativado.")
            return self._save(ctx, session, case, {"owner_agent_id": agent_id,
                                                  "owner_version": (case.get("owner_version") or 0) + 1},
                              "case.owner-assigned", {"owner_agent_id": agent_id})

    def transition(self, ctx, case_id, expected_revision, target, *, reason=None, owner_agent_id=None,
                   manual_resolution=False):
        """Move a case along the allowed graph with the expected revision (optimistic concurrency)."""
        self._ctx(ctx)
        if target not in CASE_STATES:
            raise InvalidTransition("Estado desconhecido.")
        with self.repository.transaction(ctx.tenant_id) as session:
            case = self._case(session, ctx, case_id, "write")
            self._owner_only(ctx, case)
            if case["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            current = case["state"]
            if not can_transition(current, target):
                raise InvalidTransition(f"Transicao nao permitida: {current} -> {target}.")
            reason = clean_text(reason) if reason is not None else None
            if (target in _NEEDS_REASON or current in ("resolvida", "falhou")) and not reason:
                raise CaseRejected("Informe o motivo desta mudanca.")
            changes = {"state": target}
            if owner_agent_id is not None:
                if case["owner_agent_id"] not in (None, owner_agent_id):
                    raise CaseRejected("A demanda ja tem responsavel; use a transferencia de responsavel.")
                agent = session.get("agent_definition", owner_agent_id)
                if agent is None:
                    raise NotFound()
                if agent["status"] != "ativo":
                    raise CaseRejected("Agente desativado.")
                changes["owner_agent_id"] = owner_agent_id
            owner = changes.get("owner_agent_id", case["owner_agent_id"])
            if target not in ("recebida", "triagem", "cancelada", "falhou") and owner is None:
                raise CaseRejected("Atribua um responsavel antes de iniciar o atendimento.")
            payload = {"from": current, "to": target, "reason": reason}
            if target == "resolvida":
                if not manual_resolution and not self._has_technical_evidence(session, case_id):
                    raise CaseRejected("Resolver exige uma evidencia verificada (resultado de ferramenta, recibo "
                                       "ou documento). Um resumo do modelo nao e prova.")
                if manual_resolution:
                    if ctx.role != "operator" or not reason:
                        raise CaseRejected("Resolucao manual exige o operador e um motivo.")
                    payload["manual_resolution"] = True
            if target in ("resolvida", "cancelada", "falhou"):
                changes["closed_at"] = _now()
            elif current in ("resolvida", "cancelada", "falhou"):
                changes["closed_at"] = None
                payload["reopened"] = True
            kind = "case.reopened" if payload.get("reopened") else "case.state-changed"
            return self._save(ctx, session, case, changes, kind, payload)

    def commit_change(self, ctx, session, case, changes, event_type, payload):
        """Bump the case revision, save ``changes`` and record the event, all in the caller's transaction."""
        return self._save(ctx, session, case, changes, event_type, payload)

    def commit_note(self, ctx, session, case_id, body):
        """Append an internal note inside the caller's transaction (author: the acting principal)."""
        now = _now()
        return session.save({"kind": "case_entry", "schema_version": 1, "id": new_id("case_entry"),
                             "tenant_id": ctx.tenant_id, "revision": 1, "created_at": now, "updated_at": now,
                             "case_id": case_id, "entry_type": "nota_interna", "direction": None, "channel": None,
                             "author": ctx.principal_id, "body": clean_text(body)[:8000]})

    def _owner_only(self, ctx, case):
        """Agents act on a case only while they are its owner (a former owner loses that right at transfer)."""
        if ctx.role == "agent" and case.get("operator_control"):
            raise PermissionDenied("o operador assumiu esta demanda")
        if ctx.role == "agent" and case["owner_agent_id"] != ctx.principal_id:
            raise PermissionDenied("apenas o responsavel atual da demanda pode fazer isto")

    def _save(self, ctx, session, case, changes, event_type, payload):
        now = _now()
        updated = {**case, **changes, "revision": case["revision"] + 1, "updated_at": now}
        try:
            session.save(updated, event=self._event(ctx, updated, event_type, payload))
        except IntegrityViolation:
            raise CaseRejected("Nao foi possivel registrar a mudanca.") from None
        return updated

    @staticmethod
    def _has_technical_evidence(session, case_id):
        return any(e.get("reliability") == "verificada" and e.get("evidence_type") in _TECHNICAL
                   for e in session.list("evidence", case_id=case_id, limit=1000))

    # -- history: notes, messages, summaries (append-only) ----------------------------------------------------
    def _entry(self, ctx, case_id, entry_type, body, direction=None, channel=None):
        self._ctx(ctx)
        body = clean_text(body)
        if len(body) > 8000:
            raise CaseRejected("Texto longo demais (limite de 8000 caracteres).")
        now = _now()
        doc = {"kind": "case_entry", "schema_version": 1, "id": new_id("case_entry"), "tenant_id": ctx.tenant_id,
               "revision": 1, "created_at": now, "updated_at": now, "case_id": case_id, "entry_type": entry_type,
               "direction": direction, "channel": channel, "author": ctx.principal_id, "body": body}
        with self.repository.transaction(ctx.tenant_id) as session:
            case = self._case(session, ctx, case_id, "write")
            if direction == "saida":
                self._owner_only(ctx, case)  # only the current owner speaks to the customer
            return session.save(doc)

    def add_note(self, ctx, case_id, body):
        """Internal note: never sent to the customer."""
        return self._entry(ctx, case_id, "nota_interna", body)

    def add_message(self, ctx, case_id, direction, channel, body):
        """A message exchanged with the customer. Recording it does not send anything (see T14)."""
        return self._entry(ctx, case_id, "mensagem_externa", body, direction, channel)

    def add_summary(self, ctx, case_id, body):
        """A generated summary: helpful context, explicitly not evidence of execution."""
        return self._entry(ctx, case_id, "resumo_ia", body)

    def add_evidence(self, ctx, case_id, *, source, summary, evidence_type, reliability, collected_at=None,
                     content_hash=None):
        """A fact with source, date and reliability. ``verificada`` needs technical backing (or the operator)."""
        self._ctx(ctx)
        if reliability == "verificada" and ctx.role != "operator" and (
                evidence_type not in _TECHNICAL or content_hash is None):
            raise CaseRejected("Evidencia verificada exige resultado de ferramenta, recibo ou documento com hash.")
        now = _now()
        doc = {"kind": "evidence", "schema_version": 1, "id": new_id("evidence"), "tenant_id": ctx.tenant_id,
               "revision": 1, "created_at": now, "updated_at": now, "case_id": case_id, "source": source,
               "collected_at": collected_at or now, "summary": clean_text(summary), "content_hash": content_hash,
               "evidence_type": evidence_type, "reliability": reliability, "recorded_by": ctx.principal_id}
        with self.repository.transaction(ctx.tenant_id) as session:
            self._case(session, ctx, case_id, "write")
            return session.save(doc)

    def timeline(self, ctx, case_id):
        """Everything recorded for the case, oldest first, with each item's kind kept distinct."""
        self._ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            self._case(session, ctx, case_id, "read")
            items = [("entrada", e) for e in session.list("case_entry", case_id=case_id, limit=1000)]
            items += [("evidencia", e) for e in session.list("evidence", case_id=case_id, limit=1000)]
            items += [("evento", e) for _, e in session.events(case_id, 0, 1000)]
        key = {"entrada": "created_at", "evidencia": "created_at", "evento": "occurred_at"}
        return [{"tipo": kind, "em": doc[key[kind]], "item": doc}
                for kind, doc in sorted(items, key=lambda pair: (pair[1][key[pair[0]]], pair[1]["id"]))]
