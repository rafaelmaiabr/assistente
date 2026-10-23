"""Workers for the durable queue, the agent-run handler and the outbox dispatcher.

A worker claims one job at a time, keeps its lease alive with a heartbeat, and settles the job as
done, retry or failed. Pause and cancel arrive as signals on the heartbeat; for agent runs they
cancel the in-flight model call and the tool calls through the runtime.
"""
import asyncio
import uuid
from dataclasses import dataclass

from .jobs import DEFAULT_LEASE_MS, LeaseLost, QueueError


@dataclass
class Done:
    result: dict | None = None


@dataclass
class Retry:
    code: str = "tentar_novamente"


@dataclass
class Fail:
    code: str = "falha"


@dataclass
class Stopped:
    """The handler stopped because of a pause/cancel signal (or a lost lease)."""


class JobControl:
    """What a handler may do while it holds the lease."""

    def __init__(self, queue, job, worker_id, lease_ms):
        self.queue, self.job, self.worker_id, self.lease_ms = queue, job, worker_id, lease_ms
        self.signal = None  # None | "pausar" | "cancelar", refreshed by the heartbeat
        self.lost = False

    @property
    def stop_requested(self):
        return self.signal is not None or self.lost

    def checkpoint(self, data):
        self.queue.checkpoint(self.job["id"], self.worker_id, self.job["token"], data)

    def begin_effect(self, key):
        self.queue.begin_effect(self.job["id"], self.worker_id, self.job["token"], key)

    def finish_effect(self, key, result=None):
        self.queue.finish_effect(self.job["id"], self.worker_id, self.job["token"], key, result)


class Worker:
    def __init__(self, queue, handlers, worker_id=None, lease_ms=DEFAULT_LEASE_MS, heartbeat_s=None):
        self.queue, self.handlers = queue, handlers
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_ms = lease_ms
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else lease_ms / 3000

    async def run_once(self, tenant_id=None) -> bool:
        """Process at most one job; False when the queue had nothing runnable."""
        job = self.queue.claim(self.worker_id, kinds=list(self.handlers), lease_ms=self.lease_ms, tenant_id=tenant_id)
        if job is None:
            return False
        control = JobControl(self.queue, job, self.worker_id, self.lease_ms)
        task = asyncio.ensure_future(self.handlers[job["kind"]](job, control))
        beat = asyncio.ensure_future(self._heartbeat(control, task))
        try:
            outcome = await task
        except asyncio.CancelledError:
            outcome = Stopped() if control.lost else None
            if outcome is None:
                raise
        except Exception:
            outcome = Retry("erro_do_handler")  # details stay out of the queue: they may hold private data
        finally:
            beat.cancel()
        self._settle(job, control, outcome)
        return True

    async def _heartbeat(self, control, task):
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                control.signal = self.queue.heartbeat(control.job["id"], self.worker_id, control.job["token"], self.lease_ms)
            except LeaseLost:
                control.lost = True
                task.cancel()
                return

    def _settle(self, job, control, outcome):
        jid, wid, token = job["id"], self.worker_id, job["token"]
        try:
            if control.lost:
                return  # another worker owns the job now
            if control.signal in ("pausar", "cancelar") and isinstance(outcome, Stopped):
                self.queue.acknowledge(jid, wid, token)
            elif isinstance(outcome, Done):
                self.queue.complete(jid, wid, token, outcome.result)
            elif isinstance(outcome, Fail):
                self.queue.fail(jid, wid, token, outcome.code, retry=False)
            else:
                self.queue.fail(jid, wid, token, getattr(outcome, "code", "tentar_novamente"))
        except (LeaseLost, QueueError):
            pass

    async def run_until_idle(self, tenant_id=None, limit=1000):
        count = 0
        while count < limit and await self.run_once(tenant_id):
            count += 1
        return count

    async def run_forever(self, stop: asyncio.Event, poll_s=0.5, tenant_id=None):
        while not stop.is_set():
            if not await self.run_once(tenant_id):
                try:
                    await asyncio.wait_for(stop.wait(), poll_s)
                except asyncio.TimeoutError:
                    pass


def agent_run_handler(runtime, agents, access):
    """Handler for ``agent_run`` jobs. Payload: ``run_id``, ``user_message`` and optional ``hints``/``policy_text``.

    Finished runs are never re-run; a run left ``em_execucao`` by a crashed worker is executed again; a run
    cancelled by a pause request is replaced by a fresh run of the same agent version when the job resumes.
    """
    operator = access.local_operator()

    async def handler(job, control):
        payload, tenant_id = job["payload"], job["tenant_id"]
        run_id = job["checkpoint"].get("run_id") or payload["run_id"]
        with runtime.repository.transaction(tenant_id) as session:
            run = session.get("agent_run", run_id)
        if run is None:
            return Fail("execucao_inexistente")
        if run["state"] == "concluida":
            return Done({"run_id": run_id, "reaproveitada": True})
        if run["state"] in ("falhou", "expirada"):
            return Fail(f"execucao_{run['state']}")
        if run["state"] == "cancelada":
            if not job["checkpoint"].get("paused"):
                return Fail("execucao_cancelada")
            ctx = access.tenant_context(operator, tenant_id)
            run = agents.start_run(ctx, run["agent_id"], version=run["agent_version"], budget=run["budget"],
                                   case_id=run["case_id"], task_id=run["task_id"], skill_versions=run["skill_versions"])
            run_id = run["id"]
        control.checkpoint({"run_id": run_id})
        task = asyncio.ensure_future(runtime.run(
            tenant_id, run_id, payload["user_message"], hints=payload.get("hints"),
            policy_text=payload.get("policy_text", "")))
        while not task.done():
            await asyncio.wait({task}, timeout=0.05)
            if control.stop_requested and not task.done():
                runtime.cancel(run_id)
                if control.signal == "pausar":
                    control.checkpoint({"run_id": run_id, "paused": True})
        outcome = task.result()
        if control.stop_requested:
            return Stopped()
        if outcome.status == "concluida":
            return Done({"run_id": run_id, "tokens": outcome.tokens, "steps": outcome.steps})
        if outcome.reason in ("llm_unavailable", "llm_timeout"):
            return Retry(outcome.reason)
        return Fail(outcome.reason or outcome.status)

    return handler


class OutboxDispatcher:
    """Delivers committed events to subscribers at least once, in order, per company.

    A row is marked dispatched only after every subscriber of its topic returned; a crash in between
    redelivers it, so subscribers must deduplicate (``JobQueue.consume_once``). A row that keeps failing
    stops the line until it reaches ``max_attempts``; then it is visible in ``dead_letters`` and skipped.
    """

    def __init__(self, repository, subscribers, max_attempts=5):
        self.repository, self.subscribers, self.max_attempts = repository, subscribers, max_attempts

    def dispatch_once(self, tenant_id, limit=50) -> int:
        delivered = 0
        with self.repository.transaction(tenant_id) as session:
            rows = session.pending_outbox(limit)
        for row in rows:
            if row["attempts"] >= self.max_attempts:
                continue  # dead letter
            try:
                for subscriber in self.subscribers.get(row["topic"], []):
                    subscriber(row["event"])
            except Exception:
                with self.repository.transaction(tenant_id) as session:
                    session.record_outbox_failure([row["id"]])
                break  # keep order: later rows wait for this one
            with self.repository.transaction(tenant_id) as session:
                session.mark_dispatched([row["id"]])
            delivered += 1
        return delivered

    def dead_letters(self, tenant_id, limit=100):
        with self.repository.transaction(tenant_id) as session:
            return [row for row in session.pending_outbox(1000) if row["attempts"] >= self.max_attempts][:limit]
