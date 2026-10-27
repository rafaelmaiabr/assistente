"""Tasks handed to agents outside this process (A2A), tracked on the case without moving its responsibility.

A counterparty is a ``connection`` of kind ``a2a`` for one company: its endpoint is fixed when the operator creates it, its credential
lives in the vault, and turning it off or revoking the credential stops every later call at once. What the peer's Agent Card says
changes none of that (``team/a2a.py``). Everything here runs under the caller's own access: the owner of the case (or the operator)
delegates, and an agent also needs a ``write`` grant on that connection (the grant that lets it use the connection to send).

* **The case owner stays responsible.** A remote task never becomes the owner, never gets permission to speak to the customer and its
  result is an internal note marked as data from outside, not evidence and not an instruction. A failure, a refusal, an expiry and a
  lost connection leave the case with its owner and flag the task for the operator (as the local handoffs do).
* **Unknown is not success.** The record is saved *before* the call; if the answer is lost the task stays ``desconhecido``, blocks a
  blind repeat and waits for a person (``retry`` re-sends the same ``messageId`` so the peer deduplicates; ``close_unknown`` records
  that the person verified it did not happen).
* **Repeating changes nothing.** One ``messageId`` is at most one task, a remote task belongs to one connection, and a callback or a
  poll that repeats a state already seen is ignored; a terminal task never changes again.
* **Callbacks are checked.** A callback is accepted only with the token derived from the connection's secret for that very remote task,
  and only for a task this company really delegated; anything else is refused without saying why.
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from . import a2a
from .access import AccessContext, AccessError, NotFound, PermissionDenied
from .cases import TERMINAL_STATES, CaseRejected, clean_text
from .ids import new_id
from .repository import IntegrityViolation, RevisionConflict

CONNECTOR = "a2a"
DEFAULT_TTL = 3600
MAX_TTL = 7 * 24 * 3600
MAX_OPEN_PER_CASE = 3
OPEN = ("pendente", "em_execucao", "aguardando")
FINAL = ("concluida", "falhou", "cancelada", "expirada")


class A2ATaskRejected(ValueError):
    """The request is refused; the message is safe to show."""


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def callback_token(secret, connection_id, remote_task_id):
    """The token a peer must present when it calls back about one remote task: derived from the connection's secret, never the secret."""
    return hmac.new(secret.encode("utf-8"), f"a2a-callback:{connection_id}:{remote_task_id}".encode("utf-8"), hashlib.sha256).hexdigest()


def _race_safe(method):
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except RevisionConflict:
            raise A2ATaskRejected("A tarefa mudou ao mesmo tempo; confira o estado atual.") from None
    return wrapper


class A2AService:
    def __init__(self, repository, access, cases, connections, transport_factory, clock=lambda: datetime.now(timezone.utc)):
        """``transport_factory(connection_view, secret)`` returns the ``A2ATransport`` for one counterparty (trusted wiring)."""
        self.repository, self.access, self.cases, self.connections = repository, access, cases, connections
        self.transport_factory, self.clock = transport_factory, clock
        connections.register_connector(CONNECTOR)

    # -- helpers -----------------------------------------------------------------------------------------------------
    def _ctx(self, ctx):
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()

    def _operator(self, ctx):
        self._ctx(ctx)
        if not self.access.is_operator(ctx, "admin"):
            raise PermissionDenied("apenas o operador confiavel ou um administrador faz isto")

    def _now(self):
        return self.clock()

    def _note(self, ctx, session, case_id, body):
        self.cases.commit_note(ctx, session, case_id, body)

    def _open_count(self, session, case_id):
        return len([t for t in session.list("a2a_task", case_id=case_id, limit=1000) if t["state"] in OPEN + ("desconhecido",)])

    def _bump(self, ctx, session, case, event_type, payload, waiting=None):
        """Record the event on the case; hold it while an external task is open, resume the owner when none is."""
        changes = {}
        if waiting is True and case["state"] == "em_execucao":
            changes["state"] = "aguardando_subtarefa"
        if waiting is False and case["state"] == "aguardando_subtarefa" and not self._open_count(session, case["id"]) and not [
                t for t in session.list("task", case_id=case["id"], limit=1000) if t["state"] in ("pendente", "em_execucao", "aguardando")]:
            changes["state"] = "em_execucao"
        return self.cases.commit_change(ctx, session, case, changes, event_type, payload)

    def _evidence_lines(self, session, case_id, evidence_ids):
        lines = []
        for evidence_id in evidence_ids:
            item = session.get("evidence", evidence_id) if isinstance(evidence_id, str) else None
            if item is None or item["case_id"] != case_id:
                raise NotFound()
            lines.append(item["summary"][:500])
        return lines

    def _a2a_connection(self, ctx, session, connection_id):
        connection = session.get("connection", connection_id) if isinstance(connection_id, str) else None
        if connection is None or connection["connection_kind"] != "a2a" or not self.access.allowed(ctx, session, "read", "connection", connection_id):
            raise NotFound()
        if ctx.role == "agent" and not self.access.allowed(ctx, session, "write", "connection", connection_id):  # "write" = use it to send
            raise PermissionDenied("este agente nao tem permissao para enviar tarefas por esta conexao")
        return connection

    # -- the remote call (outside any transaction) ------------------------------------------------------------------
    def _remote(self, ctx, connection_id, action):
        """Run ``action(client)`` through the vault. Peer failures come back as data, so the vault wrapper cannot blur them."""
        def run(secret):
            view = self.connections.get_connection(ctx, connection_id)
            client = a2a.A2AClient(self.transport_factory(view, secret))
            try:
                return ("ok", action(client))
            except a2a.A2AError as error:
                return ("peer", error)
        try:
            return self.connections.use_credential(ctx, connection_id, CONNECTOR, run)
        except AccessError as error:  # revoked/disabled counterparty or vault down: refused before any call
            return ("blocked", error)

    # -- delegating ---------------------------------------------------------------------------------------------------
    @_race_safe
    def delegate(self, ctx, case_id, connection_id, *, objective, expected_result=None, summary=None, evidence_ids=(),
                 ttl_seconds=DEFAULT_TTL, idempotency_key=None):
        """Hand a task to the counterparty. The caller must be the case owner (or the operator); the case keeps its owner."""
        self._ctx(ctx)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= MAX_TTL:
            raise A2ATaskRejected("O prazo deve ficar entre 60 segundos e 7 dias.")
        objective = clean_text(objective)
        now = self._now()
        key = idempotency_key or new_id("correlation")
        with self.repository.transaction(ctx.tenant_id) as session:
            case = session.get("case", case_id) if isinstance(case_id, str) else None
            if case is None or not self.access.allowed(ctx, session, "read", "case", case_id):
                raise NotFound()
            self.cases._owner_only(ctx, case)
            if case["state"] in TERMINAL_STATES:
                raise A2ATaskRejected("A demanda ja foi encerrada.")
            self._a2a_connection(ctx, session, connection_id)
            message_id = a2a.message_id(ctx.tenant_id, case_id, connection_id, key)
            known = next(iter(session.list("a2a_task", message_id=message_id, limit=1)), None)
            if known is not None:
                return known  # the same request again: the same task, nothing is sent twice
            if self._open_count(session, case_id) >= MAX_OPEN_PER_CASE:
                raise A2ATaskRejected(f"No maximo {MAX_OPEN_PER_CASE} tarefas externas abertas por demanda.")
            lines = self._evidence_lines(session, case_id, list(evidence_ids))
            try:  # validated before anything is stored or sent: a secret or an oversized text refuses it here
                a2a.outbound_message(message_id=message_id, context_id="ctx-0", objective=objective, expected_result=expected_result,
                                     summary=summary, evidence=lines)
            except a2a.A2AError as error:
                raise A2ATaskRejected(str(error)) from None
            stamp = _stamp(now)
            task_id = new_id("a2a_task")
            record = session.save({
                "kind": "a2a_task", "schema_version": 1, "id": task_id, "tenant_id": ctx.tenant_id, "revision": 1, "created_at": stamp,
                "updated_at": stamp, "case_id": case_id, "connection_id": connection_id, "requested_by": ctx.principal_id,
                "message_id": message_id, "remote_task_id": None, "remote_state": None, "state": "desconhecido", "objective": objective,
                "expected_result": expected_result, "expires_at": _stamp(now + timedelta(seconds=ttl_seconds)), "checked_at": None,
                "result_text": None, "attention": False, "detail": "enviando"})
            self._bump(ctx, session, case, "a2a.delegated", {"a2a_task_id": task_id, "connection_id": connection_id}, waiting=True)
        request = a2a.outbound_message(message_id=message_id, context_id=task_id, objective=objective, expected_result=expected_result,
                                       summary=summary, evidence=lines)
        return self._settle_send(ctx, record["id"], request)

    def _settle_send(self, ctx, task_id, request):
        kind, value = self._remote(ctx, self._get(ctx, task_id)["connection_id"], lambda client: client.send(request))
        with self.repository.transaction(ctx.tenant_id) as session:
            record = session.get("a2a_task", task_id)
            if kind == "ok":
                return self._apply(ctx, session, record, value)
            return self._fail_send(ctx, session, record, kind, value)

    def _fail_send(self, ctx, session, record, kind, error):
        """The call did not produce a task. A lost answer is unknown (stays blocked); a refusal or a revoked counterparty is a failure."""
        case = session.get("case", record["case_id"])
        if kind == "peer" and not isinstance(error, a2a.A2ARefused):  # anything but a clear refusal may have reached the peer
            saved = session.save({**record, "attention": True, "detail": "sem resposta valida do agente externo; o resultado e desconhecido",
                                  "revision": record["revision"] + 1, "updated_at": _stamp(self._now())})
            self._note(ctx, session, case["id"], "Nao se sabe se o agente externo recebeu a tarefa (sem resposta ou resposta invalida). O operador deve verificar.")
            self._bump(ctx, session, case, "a2a.unknown", {"a2a_task_id": record["id"]})
            return saved
        reason = error.name if isinstance(error, a2a.A2AError) else type(error).__name__
        saved = session.save({**record, "state": "falhou", "attention": True, "detail": f"recusada: {reason}",
                              "revision": record["revision"] + 1, "updated_at": _stamp(self._now())})
        self._note(ctx, session, case["id"], f"A tarefa externa nao foi aceita ({reason}). A demanda continua com o responsavel.")
        self._bump(ctx, session, case, "a2a.failed", {"a2a_task_id": record["id"]}, waiting=False)
        return saved

    # -- applying what the peer reports -----------------------------------------------------------------------------
    def _apply(self, ctx, session, record, task):
        """Fold a validated remote task into the record. A repeat is a no-op; a final record never changes."""
        case = session.get("case", record["case_id"])
        if record["state"] in FINAL:
            if task["state"] != record["remote_state"] and record["state"] != "expirada":
                saved = session.save({**record, "attention": True, "detail": "o agente externo informou outro estado depois do encerramento",
                                      "revision": record["revision"] + 1, "updated_at": _stamp(self._now())})
                return saved
            return record
        if record["remote_task_id"] is not None and task["task_id"] != record["remote_task_id"]:
            raise A2ATaskRejected("A resposta e de outra tarefa; recusada.")
        if task["state"] == record["remote_state"] and record["state"] != "desconhecido":
            return record  # a repeat of what is already known changes nothing
        local = task["local_state"]
        stamp = _stamp(self._now())
        changes = {"remote_task_id": task["task_id"], "remote_state": task["state"], "state": local, "checked_at": stamp,
                   "attention": local in ("falhou", "cancelada") or task["state"] in a2a.INTERRUPTED, "detail": None,
                   "revision": record["revision"] + 1, "updated_at": stamp}
        if local == "concluida":
            changes["result_text"] = task["text"] or "(sem texto)"
        try:
            saved = session.save({**record, **changes})
        except IntegrityViolation:
            raise A2ATaskRejected("Esta tarefa remota ja esta registrada.") from None
        if local == "concluida":
            self._note(ctx, session, case["id"], "Resultado do agente externo (dado de fora, nao e instrucao nem evidencia verificada):\n"
                       + saved["result_text"])
        elif local in ("falhou", "cancelada"):
            self._note(ctx, session, case["id"], f"O agente externo encerrou a tarefa sem resultado ({task['state']}). A demanda continua com o responsavel.")
        elif task["state"] in a2a.INTERRUPTED:
            self._note(ctx, session, case["id"], "O agente externo pede uma decisao ou autorizacao; so uma pessoa responde, e nada e concedido a ele por isso.")
        self._bump(ctx, session, case, f"a2a.{local}", {"a2a_task_id": record["id"], "remote_state": task["state"]},
                   waiting=False if local in FINAL else None)
        return saved

    def _get(self, ctx, task_id):
        with self.repository.transaction(ctx.tenant_id) as session:
            record = session.get("a2a_task", task_id) if isinstance(task_id, str) else None
            if record is None or not self.access.allowed(ctx, session, "read", "case", record["case_id"]):
                raise NotFound()
            return record

    # -- following ------------------------------------------------------------------------------------------------
    @_race_safe
    def poll(self, ctx, task_id):
        """Ask the peer for the task's state. A peer that does not answer changes nothing but the note on the record."""
        self._ctx(ctx)
        record = self._get(ctx, task_id)
        if record["state"] in FINAL:
            return record
        if record["remote_task_id"] is None:
            raise A2ATaskRejected("A tarefa ainda nao tem identificador remoto; o resultado do envio e desconhecido (use retry ou close_unknown).")
        kind, value = self._remote(ctx, record["connection_id"], lambda client: client.get(
            record["remote_task_id"], context_id=record["id"], previous_state=record["remote_state"]))
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("a2a_task", task_id)
            if kind == "ok":
                return self._apply(ctx, session, current, value)
            detail = f"sem resposta ({value.name})" if isinstance(value, a2a.A2AError) else "contraparte indisponivel ou revogada"
            return session.save({**current, "attention": True, "detail": detail, "revision": current["revision"] + 1,
                                 "updated_at": _stamp(self._now())})

    @_race_safe
    def cancel(self, ctx, task_id, reason="cancelado pelo responsavel"):
        """Best effort: ask the peer to stop; what counts is the state it answers. If it cannot be reached the task is cancelled here,
        marked as not confirmed, and the case continues with its owner."""
        self._ctx(ctx)
        record = self._get(ctx, task_id)
        with self.repository.transaction(ctx.tenant_id) as session:
            case = session.get("case", record["case_id"])
            self.cases._owner_only(ctx, case)
        if record["state"] in FINAL:
            raise A2ATaskRejected("A tarefa ja foi encerrada.")
        reason = clean_text(reason)[:500]
        answered = None
        if record["remote_task_id"] is not None:
            def ask(client):
                try:
                    return client.cancel(record["remote_task_id"], context_id=record["id"], previous_state=record["remote_state"])
                except a2a.A2AError as error:
                    if error.name != "TaskNotCancelableError":
                        raise
                    return client.get(record["remote_task_id"], context_id=record["id"], previous_state=record["remote_state"])
            kind, value = self._remote(ctx, record["connection_id"], ask)
            answered = value if kind == "ok" else None
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("a2a_task", task_id)
            if answered is not None and answered["state"] != a2a.CANCELED:
                return self._apply(ctx, session, current, answered)  # the peer says otherwise (already done): believe the peer
            case = session.get("case", current["case_id"])
            stamp = _stamp(self._now())
            saved = session.save({**current, "state": "cancelada", "remote_state": a2a.CANCELED if answered else current["remote_state"],
                                  "attention": answered is None, "revision": current["revision"] + 1, "updated_at": stamp,
                                  "detail": f"cancelada: {reason}" if answered else f"cancelada aqui; o agente externo nao confirmou ({reason})"})
            self._note(ctx, session, case["id"], f"Tarefa externa cancelada: {saved['detail']}")
            self._bump(ctx, session, case, "a2a.cancelled", {"a2a_task_id": task_id}, waiting=False)
            return saved

    # -- the unknown --------------------------------------------------------------------------------------------------
    @_race_safe
    def retry(self, ctx, task_id, summary=None, evidence_ids=()):
        """Operator only: re-send a task whose outcome is unknown, with the *same* messageId so the peer deduplicates it."""
        self._operator(ctx)
        record = self._get(ctx, task_id)
        if record["state"] != "desconhecido":
            raise A2ATaskRejected("So uma tarefa de resultado desconhecido pode ser reenviada.")
        with self.repository.transaction(ctx.tenant_id) as session:
            lines = self._evidence_lines(session, record["case_id"], list(evidence_ids))
        request = a2a.outbound_message(message_id=record["message_id"], context_id=record["id"], objective=record["objective"],
                                       expected_result=record["expected_result"], summary=summary, evidence=lines)
        return self._settle_send(ctx, task_id, request)

    @_race_safe
    def close_unknown(self, ctx, task_id, note):
        """Operator only: the person verified with the counterparty that the task did not happen (or does not matter): close it."""
        self._operator(ctx)
        note = clean_text(note)[:500]
        with self.repository.transaction(ctx.tenant_id) as session:
            record = session.get("a2a_task", task_id) if isinstance(task_id, str) else None
            if record is None:
                raise NotFound()
            if record["state"] != "desconhecido":
                raise A2ATaskRejected("Somente uma tarefa de resultado desconhecido pode ser encerrada assim.")
            case = session.get("case", record["case_id"])
            saved = session.save({**record, "state": "cancelada", "attention": False, "detail": f"verificado pelo operador: {note}",
                                  "revision": record["revision"] + 1, "updated_at": _stamp(self._now())})
            self._note(ctx, session, case["id"], f"Operador encerrou a tarefa externa de resultado desconhecido: {note}")
            self._bump(ctx, session, case, "a2a.cancelled", {"a2a_task_id": task_id}, waiting=False)
            return saved

    @_race_safe
    def resolve_attention(self, ctx, task_id, note):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            record = session.get("a2a_task", task_id) if isinstance(task_id, str) else None
            if record is None:
                raise NotFound()
            self._note(ctx, session, record["case_id"], f"Operador tratou a tarefa externa: {clean_text(note)[:500]}")
            return session.save({**record, "attention": False, "revision": record["revision"] + 1, "updated_at": _stamp(self._now())})

    # -- sweeping and callbacks ----------------------------------------------------------------------------------
    def expire_due(self, tenant_id, now=None):
        """Backend sweep (scheduler): an open task past its deadline expires and the owner keeps the case; a task of unknown outcome is
        only flagged (it is never assumed to have failed or succeeded)."""
        ctx = self.access.service_context(tenant_id, "expiracao")
        limit, changed = now or self._now(), []
        with self.repository.transaction(tenant_id) as session:
            due = [t for t in session.list("a2a_task", limit=1000) if t["state"] in OPEN + ("desconhecido",) and _parse(t["expires_at"]) <= limit]
        for record in due:
            if record["state"] == "desconhecido":
                with self.repository.transaction(tenant_id) as session:
                    current = session.get("a2a_task", record["id"])
                    if not current["attention"]:
                        changed.append(session.save({**current, "attention": True, "detail": "prazo esgotado com resultado desconhecido",
                                                     "revision": current["revision"] + 1, "updated_at": _stamp(self._now())}))
                continue
            if record["remote_task_id"] is not None:  # best effort: ask the peer to stop; its answer does not change that we gave up
                self._remote(ctx, record["connection_id"], lambda client, r=record: client.cancel(
                    r["remote_task_id"], context_id=r["id"], previous_state=r["remote_state"]))
            with self.repository.transaction(tenant_id) as session:
                current = session.get("a2a_task", record["id"])
                if current["state"] not in OPEN:
                    continue
                case = session.get("case", current["case_id"])
                changed.append(session.save({**current, "state": "expirada", "attention": True, "detail": "prazo esgotado",
                                             "revision": current["revision"] + 1, "updated_at": _stamp(self._now())}))
                self._note(ctx, session, case["id"], "A tarefa externa passou do prazo e foi encerrada aqui. A demanda continua com o responsavel.")
                self._bump(ctx, session, case, "a2a.expired", {"a2a_task_id": current["id"]}, waiting=False)
        return changed

    def accept_callback(self, tenant_id, connection_id, presented_token, task):
        """A peer's push about one remote task. Accepted only with the token derived from the connection's secret for that remote task
        and only for a task this company delegated through that connection; a repeat changes nothing. Any refusal says nothing about why."""
        refusal = A2ATaskRejected("Notificacao recusada.")
        ctx = self.access.service_context(tenant_id, "worker")
        remote_id = task.get("id") if isinstance(task, dict) else None
        if not (isinstance(presented_token, str) and isinstance(connection_id, str) and isinstance(remote_id, str)):
            raise refusal
        with self.repository.transaction(tenant_id) as session:
            known = next(iter(session.list("a2a_task", connection_id=connection_id, remote_task_id=remote_id, limit=1)), None)
        try:
            expected = self.connections.use_credential(ctx, connection_id, CONNECTOR, lambda secret: callback_token(secret, connection_id, remote_id))
        except AccessError:
            raise refusal from None
        if not hmac.compare_digest(str(presented_token), expected) or known is None:
            raise refusal
        try:
            parsed = a2a.read_task(task, expect_task_id=known["remote_task_id"], expect_context_id=known["id"], previous_state=known["remote_state"])
        except a2a.A2AError:
            raise refusal from None
        with self.repository.transaction(tenant_id) as session:
            try:
                return self._apply(ctx, session, session.get("a2a_task", known["id"]), parsed)
            except RevisionConflict:
                raise refusal from None

    # -- reading ---------------------------------------------------------------------------------------------------------
    def view(self, ctx, task_id):
        return self._get(ctx, task_id)

    def list_tasks(self, ctx, case_id=None):
        self._ctx(ctx)
        filters = {"case_id": case_id} if case_id else {}
        with self.repository.transaction(ctx.tenant_id) as session:
            return [t for t in session.list("a2a_task", limit=1000, **filters) if self.access.allowed(ctx, session, "read", "case", t["case_id"])]

    def needing_attention(self, ctx):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            return [t for t in session.list("a2a_task", limit=1000) if t["attention"]]
