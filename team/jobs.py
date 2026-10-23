"""Durable job queue: atomic claim by lease, heartbeat, checkpoint, retries with backoff and recovery.

All state lives in SQLite, so a crash loses nothing: a job whose worker stops heartbeating returns to
the queue when its lease expires. Every worker action carries a *fencing token* (the attempt number)
so a worker that lost its lease can no longer change the job. At most one job per case runs at a time
(enforced by a partial unique index). An external effect that started but never confirmed is not
retried: the job goes to ``reconciliar`` for a person or a reconciler to decide.

Contract for a future remote worker (same operations over an authenticated channel, see T32/T34):
``claim(worker_id) -> job`` | ``heartbeat(job, worker_id, token)`` (returns the control signal) |
``checkpoint`` | ``begin_effect``/``finish_effect`` | ``complete``/``fail``/``acknowledge``.
"""
import json
import sqlite3
import time

from .access import AccessError, NotFound, OperatorContext, PermissionDenied
from .ids import new_id

STATES = ("pendente", "em_execucao", "concluido", "falhou", "pausado", "cancelado", "esgotado", "reconciliar")
ATTENTION = ("esgotado", "reconciliar", "falhou")  # visible for human intervention
MAX_CHECKPOINT = 64 * 1024
DEFAULT_LEASE_MS = 30_000
BACKOFF_BASE_MS = 1_000
BACKOFF_CAP_MS = 300_000


class QueueError(AccessError):
    pass


class LeaseLost(QueueError):
    """The caller no longer owns the job (lease expired, another worker claimed it, or it was reassigned)."""

    def __init__(self):
        super().__init__("lease perdido")


class EffectAlreadyDone(QueueError):
    """The effect was confirmed by an earlier attempt: reuse ``.result`` instead of repeating it."""

    def __init__(self, result):
        super().__init__("efeito ja confirmado")
        self.result = result


class BudgetError(QueueError):
    pass


def _now_ms():
    return int(time.time() * 1000)


def backoff_ms(attempts, base=BACKOFF_BASE_MS, cap=BACKOFF_CAP_MS):
    return min(base * 2 ** max(attempts - 1, 0), cap)


_COLUMNS = ("id", "tenant_id", "kind", "case_id", "run_id", "parent_job_id", "idempotency_key", "state", "priority",
            "attempts", "max_attempts", "available_at_ms", "lease_owner", "lease_expires_ms", "heartbeat_ms",
            "control", "payload", "checkpoint", "result", "budget", "last_error", "created_ms", "updated_ms")
_JSON = ("payload", "checkpoint", "result", "budget")


def _job(row):
    job = dict(zip(_COLUMNS, row))
    for key in _JSON:
        job[key] = json.loads(job[key]) if job[key] is not None else None
    job["token"] = job["attempts"]  # fencing token
    return job


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class JobQueue:
    def __init__(self, repository, access=None, clock=_now_ms, backoff=backoff_ms):
        self.repository, self.access, self.clock, self.backoff = repository, access, clock, backoff

    # -- plumbing ----------------------------------------------------------------------------------------------
    def _tx(self):
        return self.repository._transaction()  # BEGIN IMMEDIATE: one writer at a time

    def _one(self, conn, job_id):
        row = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    def _owned(self, conn, job_id, worker_id, token):
        job = self._one(conn, job_id)
        now = self.clock()
        if job is None or job["state"] != "em_execucao" or job["lease_owner"] != worker_id \
                or job["token"] != token or job["lease_expires_ms"] <= now:
            raise LeaseLost()
        return job

    def _touch(self, conn, job_id, **changes):
        changes["updated_ms"] = self.clock()
        conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                     [*(_dump(v) if k in _JSON and v is not None else v for k, v in changes.items()), job_id])

    # -- producing ---------------------------------------------------------------------------------------------
    def enqueue(self, tenant_id, kind, payload, *, idempotency_key, case_id=None, run_id=None, max_attempts=3,
                delay_ms=0, priority=0, budget=None, parent_job_id=None):
        """Add a job; repeating an idempotency key returns the existing job (``created`` is False)."""
        if not (isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= 200):
            raise QueueError("idempotency_key invalida")
        now = self.clock()
        with self._tx() as conn:
            existing = conn.execute("SELECT id FROM jobs WHERE tenant_id = ? AND idempotency_key = ?",
                                    (tenant_id, idempotency_key)).fetchone()
            if existing:
                return self._one(conn, existing[0]), False
            if parent_job_id is not None:
                budget = self._reserve(conn, tenant_id, parent_job_id, budget)
            job_id = new_id("job")
            try:
                conn.execute(
                    "INSERT INTO jobs (id, tenant_id, kind, case_id, run_id, parent_job_id, idempotency_key, state, "
                    "priority, attempts, max_attempts, available_at_ms, payload, checkpoint, budget, created_ms, updated_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pendente', ?, 0, ?, ?, ?, '{}', ?, ?, ?)",
                    (job_id, tenant_id, kind, case_id, run_id, parent_job_id, idempotency_key, priority, max_attempts,
                     now + delay_ms, _dump(payload), _dump(budget) if budget is not None else None, now, now))
            except sqlite3.IntegrityError as error:
                raise QueueError("referencia invalida para o job") from error
            return self._one(conn, job_id), True

    def enqueue_command(self, tenant_id, command, kind, payload, **options):
        """Register a command once and queue its job in the same transaction: a duplicate message adds nothing."""
        key = f"cmd:{command['idempotency_key']}"
        with self.repository.transaction(tenant_id) as session:
            registered = session.register_command(command)
        job, created = self.enqueue(tenant_id, kind, payload, idempotency_key=key, **options)
        return job, created and registered

    def _reserve(self, conn, tenant_id, parent_id, budget):
        """A child may only spend what its parent still has unreserved (steps, tokens, deadline)."""
        parent = self._one(conn, parent_id)
        if parent is None or parent["tenant_id"] != tenant_id:
            raise NotFound()
        pool = parent["checkpoint"].get("unreserved", parent["budget"])
        if budget is None or pool is None:
            return budget
        if any(budget.get(key, 0) > pool.get(key, 0) for key in ("max_steps", "token_budget")) \
                or ("deadline_ms" in budget and "deadline_ms" in pool and budget["deadline_ms"] > pool["deadline_ms"]):
            raise BudgetError("o filho excede o orcamento restante do pai")
        remaining = {**pool, **{k: pool[k] - budget[k] for k in ("max_steps", "token_budget") if k in budget and k in pool}}
        checkpoint = {**parent["checkpoint"], "unreserved": remaining}
        self._touch(conn, parent_id, checkpoint=checkpoint)
        return budget

    # -- consuming ---------------------------------------------------------------------------------------------
    def claim(self, worker_id, *, kinds=None, lease_ms=DEFAULT_LEASE_MS, tenant_id=None):
        """Atomically take the next runnable job. Expired leases are recovered first."""
        with self._tx() as conn:
            now = self.clock()
            self._reap(conn, now)
            where, args = ["j.state = 'pendente'", "j.available_at_ms <= ?"], [now]
            if kinds:
                where.append(f"j.kind IN ({', '.join('?' * len(kinds))})")
                args += list(kinds)
            if tenant_id:
                where.append("j.tenant_id = ?")
                args.append(tenant_id)
            row = conn.execute(
                f"SELECT j.id FROM jobs j WHERE {' AND '.join(where)} AND (j.case_id IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM jobs r WHERE r.tenant_id = j.tenant_id AND r.case_id = j.case_id "
                "AND r.state IN ('em_execucao', 'reconciliar'))) "
                "ORDER BY j.priority DESC, j.created_ms, j.rowid LIMIT 1", args).fetchone()
            if row is None:
                return None
            current = self._one(conn, row[0])
            self._touch(conn, row[0], state="em_execucao", attempts=current["attempts"] + 1, lease_owner=worker_id,
                        lease_expires_ms=now + lease_ms, heartbeat_ms=now, last_error=None)
            return self._one(conn, row[0])

    def _reap(self, conn, now):
        """Return jobs of dead workers to the queue (or to reconciliation / exhaustion)."""
        rows = conn.execute("SELECT id FROM jobs WHERE state = 'em_execucao' AND lease_expires_ms <= ?", (now,)).fetchall()
        for (job_id,) in rows:
            job = self._one(conn, job_id)
            effects = (job["checkpoint"].get("effects") or {})
            lease = {"lease_owner": None, "lease_expires_ms": None}
            if any(e.get("state") == "iniciado" for e in effects.values()):
                self._touch(conn, job_id, state="reconciliar", last_error="efeito_externo_desconhecido", **lease)
            elif job["control"] == "cancelar":
                self._touch(conn, job_id, state="cancelado", control=None, **lease)
            elif job["control"] == "pausar":
                self._touch(conn, job_id, state="pausado", control=None, **lease)
            elif job["attempts"] >= job["max_attempts"]:
                self._touch(conn, job_id, state="esgotado", last_error="lease_expirado", **lease)
            else:
                # the backoff starts when the lease ran out, not when somebody noticed
                self._touch(conn, job_id, state="pendente", last_error="lease_expirado",
                            available_at_ms=job["lease_expires_ms"] + self.backoff(job["attempts"]), **lease)

    def heartbeat(self, job_id, worker_id, token, lease_ms=DEFAULT_LEASE_MS):
        """Extend the lease; returns the control signal (``None``, ``pausar`` or ``cancelar``)."""
        with self._tx() as conn:
            job = self._owned(conn, job_id, worker_id, token)
            now = self.clock()
            self._touch(conn, job_id, lease_expires_ms=now + lease_ms, heartbeat_ms=now)
            return job["control"]

    def checkpoint(self, job_id, worker_id, token, data):
        if len(_dump(data)) > MAX_CHECKPOINT or not isinstance(data, dict):
            raise QueueError("checkpoint invalido ou grande demais")
        with self._tx() as conn:
            job = self._owned(conn, job_id, worker_id, token)
            keep = {k: job["checkpoint"][k] for k in ("effects", "unreserved") if k in job["checkpoint"]}
            self._touch(conn, job_id, checkpoint={**data, **keep})

    def begin_effect(self, job_id, worker_id, token, effect_key):
        """Mark an external effect as started *before* doing it; confirmed effects are never repeated."""
        with self._tx() as conn:
            job = self._owned(conn, job_id, worker_id, token)
            effects = dict(job["checkpoint"].get("effects") or {})
            known = effects.get(effect_key)
            if known and known["state"] == "confirmado":
                raise EffectAlreadyDone(known.get("result"))
            if known and known["state"] == "iniciado":
                raise QueueError("efeito iniciado sem confirmacao: aguarde a reconciliacao")
            effects[effect_key] = {"state": "iniciado", "at_ms": self.clock()}
            self._touch(conn, job_id, checkpoint={**job["checkpoint"], "effects": effects})

    def finish_effect(self, job_id, worker_id, token, effect_key, result=None):
        with self._tx() as conn:
            job = self._owned(conn, job_id, worker_id, token)
            effects = dict(job["checkpoint"].get("effects") or {})
            if effect_key not in effects:
                raise QueueError("efeito nao iniciado")
            effects[effect_key] = {"state": "confirmado", "at_ms": self.clock(), "result": result}
            self._touch(conn, job_id, checkpoint={**job["checkpoint"], "effects": effects})

    def complete(self, job_id, worker_id, token, result=None):
        with self._tx() as conn:
            self._owned(conn, job_id, worker_id, token)
            self._touch(conn, job_id, state="concluido", result=result, lease_owner=None, lease_expires_ms=None,
                        control=None)

    def fail(self, job_id, worker_id, token, error_code, *, retry=True):
        """Record a failure: retry with exponential backoff until attempts run out, then ``esgotado``."""
        with self._tx() as conn:
            job = self._owned(conn, job_id, worker_id, token)
            lease = {"lease_owner": None, "lease_expires_ms": None, "last_error": str(error_code)[:80]}
            if not retry:
                self._touch(conn, job_id, state="falhou", **lease)
            elif job["attempts"] >= job["max_attempts"]:
                self._touch(conn, job_id, state="esgotado", **lease)
            else:
                self._touch(conn, job_id, state="pendente",
                            available_at_ms=self.clock() + self.backoff(job["attempts"]), **lease)

    def acknowledge(self, job_id, worker_id, token):
        """The worker saw a pause/cancel request and stopped: settle the job in the requested state."""
        with self._tx() as conn:
            job = self._owned(conn, job_id, worker_id, token)
            state = {"pausar": "pausado", "cancelar": "cancelado"}.get(job["control"])
            if state is None:
                raise QueueError("nenhum pedido de pausa ou cancelamento")
            self._touch(conn, job_id, state=state, control=None, lease_owner=None, lease_expires_ms=None)
            return state

    # -- controls (operator / backend) -------------------------------------------------------------------------
    def _tree(self, conn, job_id):
        found, frontier = [], [job_id]
        while frontier:
            found += frontier
            frontier = [r[0] for parent in frontier for r in conn.execute(
                "SELECT id FROM jobs WHERE parent_job_id = ?", (parent,)).fetchall()]
        return found

    def _control(self, tenant_id, job_id, action):
        with self._tx() as conn:
            root = self._one(conn, job_id)
            if root is None or root["tenant_id"] != tenant_id:
                raise NotFound()
            for current in (self._one(conn, i) for i in self._tree(conn, job_id)):
                if current["state"] in ("concluido", "cancelado", "falhou", "esgotado"):
                    continue
                if current["state"] == "em_execucao":  # the worker settles it at its next heartbeat
                    self._touch(conn, current["id"], control=action)
                elif action == "cancelar" or current["state"] == "pendente":
                    self._touch(conn, current["id"], state="cancelado" if action == "cancelar" else "pausado")
            return self._one(conn, job_id)

    def pause(self, tenant_id, job_id):
        """Pause the job and its descendants; running ones stop at their next heartbeat."""
        return self._control(tenant_id, job_id, "pausar")

    def cancel(self, tenant_id, job_id):
        """Cancel the job and its descendants; running ones (and their model/tool calls) stop at the next heartbeat."""
        return self._control(tenant_id, job_id, "cancelar")

    def resume(self, tenant_id, job_id):
        with self._tx() as conn:
            for current in (self._one(conn, i) for i in self._tree(conn, job_id)):
                if current["tenant_id"] != tenant_id:
                    raise NotFound()
                if current["state"] == "pausado":
                    self._touch(conn, current["id"], state="pendente", available_at_ms=self.clock())
                elif current["state"] == "em_execucao" and current["control"] == "pausar":
                    self._touch(conn, current["id"], control=None)
            job = self._one(conn, job_id)
            if job is None:
                raise NotFound()
            return job

    def _case_job_ids(self, tenant_id, case_id, states):
        marks = ", ".join("?" * len(states))
        with self._tx() as conn:
            return [r[0] for r in conn.execute(
                f"SELECT id FROM jobs WHERE tenant_id = ? AND case_id = ? AND state IN ({marks}) ORDER BY rowid",
                (tenant_id, case_id, *states))]

    def pause_case(self, tenant_id, case_id):
        """Pause every live job of a case (operator took over); running ones stop at their next heartbeat."""
        for job_id in self._case_job_ids(tenant_id, case_id, ("pendente", "em_execucao")):
            self.pause(tenant_id, job_id)

    def resume_case(self, tenant_id, case_id):
        for job_id in self._case_job_ids(tenant_id, case_id, ("pausado", "em_execucao")):
            self.resume(tenant_id, job_id)

    def cancel_case(self, tenant_id, case_id):
        for job_id in self._case_job_ids(tenant_id, case_id, ("pendente", "em_execucao", "pausado")):
            self.cancel(tenant_id, job_id)

    def retry_exhausted(self, operator, tenant_id, job_id):
        """Give an exhausted/failed job another round of attempts (explicit human intervention)."""
        self._operator(operator)
        with self._tx() as conn:
            job = self._one(conn, job_id)
            if job is None or job["tenant_id"] != tenant_id or job["state"] not in ("esgotado", "falhou"):
                raise NotFound()
            self._touch(conn, job_id, state="pendente", attempts=0, available_at_ms=self.clock(), last_error=None)
            return self._one(conn, job_id)

    def _operator(self, operator):
        if self.access is None or not isinstance(operator, OperatorContext) \
                or operator.principal_id != self.access.operator_id:
            raise PermissionDenied()

    def resolve_reconciliation(self, operator, tenant_id, job_id, decision, result=None):
        """Settle an effect of unknown outcome: it ``confirmado`` (happened), ``nao_ocorreu`` or ``abandonar``."""
        self._operator(operator)
        with self._tx() as conn:
            job = self._one(conn, job_id)
            if job is None or job["tenant_id"] != tenant_id or job["state"] != "reconciliar":
                raise NotFound()
            effects = dict(job["checkpoint"].get("effects") or {})
            pending = [k for k, v in effects.items() if v["state"] == "iniciado"]
            if decision == "abandonar":
                self._touch(conn, job_id, state="falhou", last_error="abandonado_na_reconciliacao")
                return self._one(conn, job_id)
            for key in pending:
                if decision == "confirmado":
                    effects[key] = {"state": "confirmado", "at_ms": self.clock(), "result": result}
                elif decision == "nao_ocorreu":
                    del effects[key]
                else:
                    raise QueueError("decisao desconhecida")
            self._touch(conn, job_id, state="pendente", available_at_ms=self.clock(), last_error=None,
                        checkpoint={**job["checkpoint"], "effects": effects})
            return self._one(conn, job_id)

    # -- reading -----------------------------------------------------------------------------------------------
    def get(self, tenant_id, job_id):
        with self._tx() as conn:
            job = self._one(conn, job_id) if isinstance(job_id, str) else None
        if job is None or job["tenant_id"] != tenant_id:
            raise NotFound()
        return job

    def list_jobs(self, tenant_id, state=None, limit=100):
        query, args = f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE tenant_id = ?", [tenant_id]
        if state:
            query += " AND state = ?"
            args.append(state)
        with self._tx() as conn:
            return [_job(r) for r in conn.execute(query + " ORDER BY created_ms, id LIMIT ?", [*args, min(limit, 1000)])]

    def needs_attention(self, tenant_id):
        """Jobs that need a person: exhausted retries, unknown external effects and hard failures."""
        return [j for j in self.list_jobs(tenant_id, limit=1000) if j["state"] in ATTENTION]

    # -- at-least-once consumers --------------------------------------------------------------------------------
    def consume_once(self, consumer, event_id):
        """True the first time ``(consumer, event_id)`` is seen. Consumers call it before acting, so a
        redelivered event (the outbox is at-least-once) is ignored."""
        with self._tx() as conn:
            try:
                conn.execute("INSERT INTO processed_events (consumer, event_id, processed_ms) VALUES (?, ?, ?)",
                             (consumer, event_id, self.clock()))
                return True
            except sqlite3.IntegrityError:
                return False
