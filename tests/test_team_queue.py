import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.cases import CaseService
from team.ids import new_id
from team.jobs import BudgetError, EffectAlreadyDone, JobQueue, LeaseLost, QueueError, backoff_ms
from team.repository import SqliteRepository
from team.service import TeamService
from team.worker import (
    Done, Fail, OutboxDispatcher, Retry, Stopped, Worker, agent_run_handler)
from tests.team_fixtures import BUDGET, RuntimeCase, TOOL, FakeLlm, tool_call
from team.llm import LlmReply, LlmUnavailable


class FakeClock:
    def __init__(self):
        self.ms = 1_000_000

    def __call__(self):
        return self.ms

    def advance(self, ms):
        self.ms += ms


class QueueCase(unittest.TestCase):
    def make_repo(self):
        """The backend under test; test_team_queue_limits.py overrides it for PostgreSQL."""
        self.path = Path(self.temp.name) / "team.sqlite3"
        return SqliteRepository(self.path)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = self.make_repo()
        self.repo.migrate()
        self.clock = FakeClock()
        self.access = AccessService(self.repo, new_id("user"))
        self.queue = JobQueue(self.repo, self.access, clock=self.clock)
        self.team = TeamService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.dep = self.team.create(self.ca, "department", name="Suporte", scope="x", status="ativo")

    def job(self, key="k1", **kwargs):
        job, _ = self.queue.enqueue(self.a["id"], "teste", {"n": key}, idempotency_key=key, **kwargs)
        return job

    def new_case(self):
        return self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Teste")["id"]

    def claim(self, worker="w1", **kwargs):
        return self.queue.claim(worker, **kwargs)


class EnqueueAndClaimTests(QueueCase):
    def test_idempotency_key_prevents_duplicates_per_company(self):
        first, created = self.queue.enqueue(self.a["id"], "teste", {}, idempotency_key="same")
        again, created_again = self.queue.enqueue(self.a["id"], "teste", {"outro": 1}, idempotency_key="same")
        self.assertEqual((created, created_again, again["id"]), (True, False, first["id"]))
        other, created_b = self.queue.enqueue(self.b["id"], "teste", {}, idempotency_key="same")
        self.assertTrue(created_b)
        self.assertNotEqual(other["id"], first["id"])
        for bad in ("", None, "x" * 201):
            with self.assertRaises(QueueError):
                self.queue.enqueue(self.a["id"], "teste", {}, idempotency_key=bad)
        with self.assertRaises(QueueError):  # unknown case reference
            self.queue.enqueue(self.a["id"], "teste", {}, idempotency_key="c", case_id=new_id("case"))

    def test_duplicate_command_adds_one_job_only(self):
        command = {"kind": "command", "schema_version": 1, "id": new_id("command"), "tenant_id": self.a["id"],
                   "type": "config.run", "case_id": None, "task_id": None, "correlation_id": new_id("correlation"),
                   "causation_id": None, "actor_id": self.op.principal_id, "expected_revision": None,
                   "idempotency_key": "msg-1", "issued_at": "2026-01-01T00:00:00Z", "payload": {}}
        job, created = self.queue.enqueue_command(self.a["id"], command, "teste", {"x": 1})
        duplicate = dict(command, id=new_id("command"))
        again, created_again = self.queue.enqueue_command(self.a["id"], duplicate, "teste", {"x": 1})
        self.assertEqual((created, created_again, again["id"]), (True, False, job["id"]))
        self.assertEqual(len(self.queue.list_jobs(self.a["id"])), 1)

    def test_claim_respects_order_availability_kinds_and_company(self):
        low = self.job("low", priority=0)
        high = self.job("high", priority=5)
        later = self.job("later", delay_ms=60_000)
        first, second = self.claim(), self.claim("w2")
        self.assertEqual((first["id"], second["id"]), (high["id"], low["id"]))
        self.assertIsNone(self.claim("w3"))  # `later` is not available yet
        self.queue.complete(first["id"], "w1", first["token"])
        self.queue.complete(second["id"], "w2", second["token"])
        self.clock.advance(60_000)
        self.assertEqual(self.claim("w3")["id"], later["id"])
        self.job("other-kind")
        self.assertIsNone(self.claim("w4", kinds=["nao-existe"]))
        self.assertIsNone(self.claim("w4", tenant_id=self.b["id"]))
        self.assertTrue(self.claim("w4", tenant_id=self.a["id"]))

    def test_two_workers_never_claim_the_same_job(self):
        ids = [self.job(f"j{i}")["id"] for i in range(30)]
        taken, lock = [], threading.Lock()

        def work(name):
            while True:
                job = self.queue.claim(name)
                if job is None:
                    return
                with lock:
                    taken.append(job["id"])
                self.queue.complete(job["id"], name, job["token"])

        threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(2)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        self.assertEqual(sorted(taken), sorted(ids))
        self.assertEqual(len(set(taken)), 30)
        self.assertEqual({j["state"] for j in self.queue.list_jobs(self.a["id"])}, {"concluido"})

    def test_only_one_active_job_per_case(self):
        case = self.new_case()
        first, second = self.job("c1", case_id=case), self.job("c2", case_id=case)
        claimed = self.claim()
        self.assertEqual(claimed["id"], first["id"])
        self.assertIsNone(self.claim("w2"))  # second job of the same case waits
        self.queue.complete(claimed["id"], "w1", claimed["token"])
        self.assertEqual(self.claim("w2")["id"], second["id"])
        with self.assertRaises(Exception):  # and the database itself refuses two active jobs for one case
            with self.repo._transaction() as conn:
                conn.execute("UPDATE jobs SET state = 'em_execucao' WHERE id = ?", (first["id"],))


class LeaseAndRecoveryTests(QueueCase):
    def test_expired_lease_returns_the_job_with_backoff_and_fences_the_old_worker(self):
        job = self.job()
        a = self.claim("a", lease_ms=10_000)
        self.queue.checkpoint(a["id"], "a", a["token"], {"passo": 2})
        self.clock.advance(10_001)  # worker a died: no heartbeat
        self.assertIsNone(self.claim("b"))  # recovered, but waiting out the backoff
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "pendente")
        self.clock.advance(backoff_ms(1))
        b = self.claim("b")
        self.assertEqual((b["id"], b["attempts"], b["checkpoint"]["passo"]), (job["id"], 2, 2))
        for call in (lambda: self.queue.complete(a["id"], "a", a["token"]),
                     lambda: self.queue.heartbeat(a["id"], "a", a["token"]),
                     lambda: self.queue.checkpoint(a["id"], "a", a["token"], {}),
                     lambda: self.queue.complete(a["id"], "b", a["token"])):  # right owner, stale token
            with self.assertRaises(LeaseLost):
                call()
        self.queue.complete(b["id"], "b", b["token"], {"ok": True})
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["result"], {"ok": True})

    def test_heartbeat_extends_the_lease_and_reports_control_signals(self):
        job = self.job()
        claimed = self.claim("a", lease_ms=10_000)
        self.clock.advance(9_000)
        self.assertIsNone(self.queue.heartbeat(claimed["id"], "a", claimed["token"], lease_ms=10_000))
        self.clock.advance(9_000)
        self.assertIsNone(self.claim("b"))  # still alive thanks to the heartbeat
        self.queue.pause(self.a["id"], job["id"])
        self.assertEqual(self.queue.heartbeat(claimed["id"], "a", claimed["token"]), "pausar")
        self.assertEqual(self.queue.acknowledge(claimed["id"], "a", claimed["token"]), "pausado")

    def test_checkpoint_size_and_shape_are_limited(self):
        job = self.job()
        claimed = self.claim()
        for bad in ({"x": "y" * 70_000}, ["lista"]):
            with self.assertRaises(QueueError):
                self.queue.checkpoint(job["id"], "w1", claimed["token"], bad)

    def test_retries_back_off_exponentially_then_become_visible_for_intervention(self):
        job = self.job(max_attempts=3)
        delays = []
        for attempt in (1, 2):
            claimed = self.claim()
            self.queue.fail(claimed["id"], "w1", claimed["token"], "llm_unavailable")
            stored = self.queue.get(self.a["id"], job["id"])
            delays.append(stored["available_at_ms"] - self.clock())
            self.assertEqual((stored["state"], stored["last_error"]), ("pendente", "llm_unavailable"))
            self.clock.advance(delays[-1])
        self.assertEqual(delays, [1000, 2000])
        last = self.claim()
        self.queue.fail(last["id"], "w1", last["token"], "llm_unavailable")
        stored = self.queue.get(self.a["id"], job["id"])
        self.assertEqual((stored["state"], stored["attempts"]), ("esgotado", 3))
        self.assertEqual([j["id"] for j in self.queue.needs_attention(self.a["id"])], [job["id"]])
        self.assertIsNone(self.claim())
        with self.assertRaises(PermissionDenied):
            self.queue.retry_exhausted(self.ca, self.a["id"], job["id"])
        again = self.queue.retry_exhausted(self.op, self.a["id"], job["id"])
        self.assertEqual((again["state"], again["attempts"]), ("pendente", 0))
        self.assertEqual(backoff_ms(20), 300_000)  # capped

    def test_lease_expiring_on_the_last_attempt_exhausts_the_job(self):
        job = self.job(max_attempts=1)
        self.claim(lease_ms=1000)
        self.clock.advance(1001)
        self.assertIsNone(self.claim("b"))
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "esgotado")

    def test_non_retryable_failure_stops_the_job(self):
        job = self.job()
        claimed = self.claim()
        self.queue.fail(claimed["id"], "w1", claimed["token"], "orcamento", retry=False)
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "falhou")
        self.assertEqual(len(self.queue.needs_attention(self.a["id"])), 1)

    def test_queue_survives_a_restart(self):
        job = self.job()
        claimed = self.claim("a", lease_ms=5000)
        self.queue.checkpoint(claimed["id"], "a", claimed["token"], {"feito": "metade"})
        reopened = JobQueue(SqliteRepository(self.path), self.access, clock=self.clock)
        self.clock.advance(5001 + backoff_ms(1))
        recovered = reopened.claim("novo")
        self.assertEqual((recovered["id"], recovered["checkpoint"]["feito"]), (job["id"], "metade"))


class ExternalEffectTests(QueueCase):
    def test_confirmed_effect_is_not_repeated_after_a_crash(self):
        job, sent = self.job(), []
        a = self.claim("a", lease_ms=5000)
        self.queue.begin_effect(a["id"], "a", a["token"], "enviar-1")
        sent.append("real-1")  # the external side effect happened ...
        self.queue.finish_effect(a["id"], "a", a["token"], "enviar-1", {"recibo": "R1"})
        self.clock.advance(5001 + backoff_ms(1))  # ... and the worker died before completing the job
        b = self.claim("b")
        with self.assertRaises(EffectAlreadyDone) as caught:
            self.queue.begin_effect(b["id"], "b", b["token"], "enviar-1")
        self.assertEqual(caught.exception.result, {"recibo": "R1"})
        self.assertEqual(len(sent), 1)
        self.queue.complete(b["id"], "b", b["token"])

    def test_unknown_outcome_goes_to_reconciliation_and_is_never_retried_blindly(self):
        case = self.new_case()
        job = self.job("pay", case_id=case)
        other = self.job("next", case_id=case)
        a = self.claim("a", lease_ms=5000)
        self.queue.begin_effect(a["id"], "a", a["token"], "renovar")  # the call went out, no answer recorded
        self.clock.advance(5001 + backoff_ms(1))
        self.assertIsNone(self.claim("b"))
        stored = self.queue.get(self.a["id"], job["id"])
        self.assertEqual((stored["state"], stored["last_error"]), ("reconciliar", "efeito_externo_desconhecido"))
        self.assertIsNone(self.claim("b"))  # nothing else of this case runs either
        self.assertEqual(self.queue.needs_attention(self.a["id"])[0]["id"], job["id"])
        for forged in (self.ca, None):
            with self.assertRaises(PermissionDenied):
                self.queue.resolve_reconciliation(forged, self.a["id"], job["id"], "confirmado")
        with self.assertRaises(QueueError):
            self.queue.resolve_reconciliation(self.op, self.a["id"], job["id"], "talvez")
        done = self.queue.resolve_reconciliation(self.op, self.a["id"], job["id"], "confirmado", {"recibo": "R9"})
        self.assertEqual(done["state"], "pendente")
        retry = self.claim("c")
        with self.assertRaises(EffectAlreadyDone) as caught:
            self.queue.begin_effect(retry["id"], "c", retry["token"], "renovar")
        self.assertEqual(caught.exception.result, {"recibo": "R9"})
        self.queue.complete(retry["id"], "c", retry["token"])
        self.assertEqual(self.claim("d")["id"], other["id"])  # the case moves on afterwards

    def test_reconciliation_can_declare_the_effect_never_happened_or_abandon(self):
        for decision, final in (("nao_ocorreu", "pendente"), ("abandonar", "falhou")):
            job = self.job(f"j-{decision}")
            claimed = self.claim("a", lease_ms=1000)
            self.queue.begin_effect(claimed["id"], "a", claimed["token"], "x")
            self.clock.advance(1001)
            self.claim("z")
            result = self.queue.resolve_reconciliation(self.op, self.a["id"], job["id"], decision)
            self.assertEqual(result["state"], final)
            if decision == "nao_ocorreu":
                retry = self.claim("c")
                self.queue.begin_effect(retry["id"], "c", retry["token"], "x")  # allowed to try again
                self.queue.finish_effect(retry["id"], "c", retry["token"], "x")
                self.queue.complete(retry["id"], "c", retry["token"])

    def test_effects_need_a_valid_lease(self):
        self.job()
        claimed = self.claim("a", lease_ms=1000)
        self.clock.advance(1001)
        with self.assertRaises(LeaseLost):
            self.queue.begin_effect(claimed["id"], "a", claimed["token"], "x")
        fresh = self.job("outro")
        c2 = self.claim("a", lease_ms=1000)
        with self.assertRaises(QueueError):
            self.queue.finish_effect(c2["id"], "a", c2["token"], "nunca-iniciado")
        self.assertTrue(fresh)


class ControlTests(QueueCase):
    def test_pause_resume_and_cancel_propagate_to_children(self):
        parent = self.job("parent")
        child, _ = self.queue.enqueue(self.a["id"], "teste", {}, idempotency_key="child", parent_job_id=parent["id"])
        grandchild, _ = self.queue.enqueue(self.a["id"], "teste", {}, idempotency_key="grand", parent_job_id=child["id"])
        self.queue.pause(self.a["id"], parent["id"])
        self.assertEqual({j["state"] for j in self.queue.list_jobs(self.a["id"])}, {"pausado"})
        self.assertIsNone(self.claim())
        self.queue.resume(self.a["id"], parent["id"])
        self.assertEqual({j["state"] for j in self.queue.list_jobs(self.a["id"])}, {"pendente"})
        running = self.claim("a")  # the parent runs; children are still queued
        self.queue.cancel(self.a["id"], parent["id"])
        states = {j["idempotency_key"]: j["state"] for j in self.queue.list_jobs(self.a["id"])}
        self.assertEqual(states, {"parent": "em_execucao", "child": "cancelado", "grand": "cancelado"})
        self.assertEqual(self.queue.heartbeat(running["id"], "a", running["token"]), "cancelar")
        self.assertEqual(self.queue.acknowledge(running["id"], "a", running["token"]), "cancelado")
        self.assertIsNone(self.claim("b"))

    def test_cancel_request_survives_the_death_of_the_worker(self):
        job = self.job()
        self.claim("a", lease_ms=1000)
        self.queue.cancel(self.a["id"], job["id"])
        self.clock.advance(1001)
        self.assertIsNone(self.claim("b"))
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "cancelado")

    def test_children_share_the_parent_budget_and_inherit_its_deadline(self):
        parent = self.job("p", budget={"max_steps": 10, "token_budget": 1000, "deadline_ms": 5_000_000})
        self.queue.enqueue(self.a["id"], "teste", {}, idempotency_key="c1", parent_job_id=parent["id"],
                           budget={"max_steps": 6, "token_budget": 400, "deadline_ms": 4_000_000})
        with self.assertRaises(BudgetError):
            self.queue.enqueue(self.a["id"], "teste", {}, idempotency_key="c2", parent_job_id=parent["id"],
                               budget={"max_steps": 5, "token_budget": 100, "deadline_ms": 4_000_000})  # steps left: 4
        with self.assertRaises(BudgetError):
            self.queue.enqueue(self.a["id"], "teste", {}, idempotency_key="c3", parent_job_id=parent["id"],
                               budget={"max_steps": 1, "token_budget": 100, "deadline_ms": 6_000_000})  # later than parent
        _, created = self.queue.enqueue(self.a["id"], "teste", {}, idempotency_key="c4", parent_job_id=parent["id"],
                                        budget={"max_steps": 4, "token_budget": 600, "deadline_ms": 5_000_000})
        self.assertTrue(created)

    def test_other_company_cannot_see_or_control_jobs(self):
        job = self.job()
        for action in (lambda: self.queue.get(self.b["id"], job["id"]), lambda: self.queue.pause(self.b["id"], job["id"]),
                       lambda: self.queue.cancel(self.b["id"], job["id"]), lambda: self.queue.resume(self.b["id"], job["id"])):
            with self.assertRaises(NotFound):
                action()
        self.assertEqual(self.queue.list_jobs(self.b["id"]), [])
        with self.assertRaises(NotFound):
            self.queue.enqueue(self.b["id"], "teste", {}, idempotency_key="x", parent_job_id=job["id"])


class OutboxTests(QueueCase):
    def emit(self, count=3):
        agents = []
        with self.repo.transaction(self.a["id"]) as session:
            for number in range(count):
                session.record_event({"kind": "event", "schema_version": 1, "id": new_id("event"),
                                      "tenant_id": self.a["id"], "type": "config.teste", "case_id": None,
                                      "correlation_id": new_id("correlation"), "causation_id": None,
                                      "actor_id": self.op.principal_id, "revision": number + 1,
                                      "occurred_at": "2026-01-01T00:00:00Z", "payload": {"n": number}}, topic="teste")
                agents.append(number)

    def test_delivers_in_order_exactly_the_pending_rows(self):
        self.emit(3)
        seen = []
        dispatcher = OutboxDispatcher(self.repo, {"teste": [lambda e: seen.append(e["payload"]["n"])]})
        self.assertEqual(dispatcher.dispatch_once(self.a["id"]), 3)
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(dispatcher.dispatch_once(self.a["id"]), 0)

    def test_failures_block_the_line_then_become_dead_letters(self):
        self.emit(2)
        seen, healthy = [], {"ok": False}

        def subscriber(event):
            if event["payload"]["n"] == 0 and not healthy["ok"]:
                raise RuntimeError("destino fora")
            seen.append(event["payload"]["n"])

        dispatcher = OutboxDispatcher(self.repo, {"teste": [subscriber]}, max_attempts=3)
        for _ in range(2):
            self.assertEqual(dispatcher.dispatch_once(self.a["id"]), 0)
        self.assertEqual(seen, [])  # order is kept: row 1 waits for row 0
        self.assertEqual(dispatcher.dispatch_once(self.a["id"]), 0)  # third failure reaches the limit
        self.assertEqual(len(dispatcher.dead_letters(self.a["id"])), 1)
        self.assertEqual(dispatcher.dispatch_once(self.a["id"]), 1)  # the dead letter is skipped, row 1 flows
        self.assertEqual(seen, [1])

    def test_crash_after_delivery_redelivers_and_consumers_deduplicate(self):
        self.emit(1)
        handled = []

        def consumer(event):
            if self.queue.consume_once("consumidor-a", event["id"]):
                handled.append(event["id"])

        class Crash(Exception):
            pass

        dispatcher = OutboxDispatcher(self.repo, {"teste": [consumer]})
        original = self.repo.transaction
        calls = {"n": 0}

        def flaky(tenant_id):  # die right after the subscriber ran, before the row is marked
            calls["n"] += 1
            if calls["n"] == 2:
                raise Crash()
            return original(tenant_id)

        self.repo.transaction = flaky
        with self.assertRaises(Crash):
            dispatcher.dispatch_once(self.a["id"])
        self.repo.transaction = original
        restarted = OutboxDispatcher(SqliteRepository(self.path), {"teste": [consumer]})
        self.assertEqual(restarted.dispatch_once(self.a["id"]), 1)  # delivered again after the restart ...
        self.assertEqual(len(handled), 1)  # ... but processed once
        self.assertFalse(self.queue.consume_once("consumidor-a", handled[0]))
        self.assertTrue(self.queue.consume_once("consumidor-b", handled[0]))


class WorkerTests(QueueCase, unittest.IsolatedAsyncioTestCase):
    def worker(self, handlers, name="w", **kwargs):
        return Worker(self.queue, handlers, worker_id=name, lease_ms=1000, heartbeat_s=0.02, **kwargs)

    async def test_outcomes_map_to_job_states(self):
        for key in ("ok", "retry", "fail", "boom"):
            self.job(key)

        async def handler(job, control):
            key = job["payload"]["n"]
            if key == "boom":
                raise RuntimeError("detalhe privado do cliente")
            return {"ok": Done({"r": 1}), "retry": Retry("llm_unavailable"), "fail": Fail("orcamento")}[key]

        self.assertEqual(await self.worker({"teste": handler}).run_until_idle(), 4)
        states = {j["payload"]["n"]: (j["state"], j["last_error"]) for j in self.queue.list_jobs(self.a["id"])}
        self.assertEqual(states, {"ok": ("concluido", None), "retry": ("pendente", "llm_unavailable"),
                                  "fail": ("falhou", "orcamento"), "boom": ("pendente", "erro_do_handler")})
        self.assertNotIn("privado", str(states))

    async def test_two_workers_run_each_job_once(self):
        for i in range(12):
            self.job(f"j{i}")
        runs = []

        async def handler(job, control):
            runs.append(job["id"])
            await asyncio.sleep(0.01)
            return Done()

        workers = [self.worker({"teste": handler}, name=f"w{i}") for i in range(2)]
        await asyncio.gather(*[w.run_until_idle() for w in workers])
        self.assertEqual(len(runs), 12)
        self.assertEqual(len(set(runs)), 12)

    async def test_cancel_and_pause_reach_a_running_handler(self):
        job = self.job("cancel")

        async def handler(job, control):
            while not control.stop_requested:
                await asyncio.sleep(0.01)
            return Stopped()

        running = asyncio.ensure_future(self.worker({"teste": handler}).run_once())
        await asyncio.sleep(0.1)
        self.queue.cancel(self.a["id"], job["id"])
        await asyncio.wait_for(running, 5)
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "cancelado")
        paused = self.job("pausar")
        running = asyncio.ensure_future(self.worker({"teste": handler}).run_once())
        await asyncio.sleep(0.1)
        self.queue.pause(self.a["id"], paused["id"])
        await asyncio.wait_for(running, 5)
        self.assertEqual(self.queue.get(self.a["id"], paused["id"])["state"], "pausado")
        self.queue.resume(self.a["id"], paused["id"])
        self.assertEqual(self.queue.get(self.a["id"], paused["id"])["state"], "pendente")

    async def test_a_worker_that_loses_its_lease_stops_and_cannot_settle(self):
        job = self.job()
        started = asyncio.Event()

        async def handler(job, control):
            started.set()
            await asyncio.sleep(30)
            return Done()

        running = asyncio.ensure_future(self.worker({"teste": handler}, name="lento").run_once())
        await started.wait()
        self.clock.advance(5000)  # the lease (1 s) expires while the handler is stuck
        await asyncio.wait_for(running, 5)
        self.assertEqual(self.queue.get(self.a["id"], job["id"])["state"], "em_execucao")  # nobody settled it
        recovered = self.claim("outro")  # the dead worker's lease ended long ago: the job is claimable again
        self.assertEqual((recovered["attempts"], recovered["lease_owner"]), (2, "outro"))
        with self.assertRaises(LeaseLost):  # and the slow worker, if it wakes up, cannot settle anything
            self.queue.complete(job["id"], "lento", 1)


class AgentRunWorkerTests(RuntimeCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.queue = JobQueue(self.repo, self.access, clock=self.clock)
        self.handler = agent_run_handler(self.runtime, self.agents, self.access)
        self.worker = Worker(self.queue, {"agent_run": self.handler}, worker_id="w", lease_ms=30_000, heartbeat_s=0.02)

    def enqueue(self, run=None, **payload):
        run = run or self.new_run()
        job, _ = self.queue.enqueue(self.a["id"], "agent_run", {"run_id": run["id"], "user_message": "oi", **payload},
                                    idempotency_key=f"run:{run['id']}", run_id=run["id"])
        return run, job

    def job(self, job):
        return self.queue.get(self.a["id"], job["id"])

    async def test_a_queued_run_completes_through_the_worker(self):
        self.llms["local"].script = [LlmReply("pronto")]
        run, job = self.enqueue()
        self.assertTrue(await self.worker.run_once())
        self.assertEqual(self.job(job)["state"], "concluido")
        self.assertEqual(self.state(run)["state"], "concluida")
        self.assertFalse(await self.worker.run_once())

    async def test_finished_runs_are_not_rerun_and_crashed_ones_are(self):
        self.llms["local"].script = [LlmReply("primeira")]
        run, _ = self.enqueue()
        await self.worker.run_once()
        calls = len(self.llms["local"].calls)
        job2, _ = self.queue.enqueue(self.a["id"], "agent_run", {"run_id": run["id"], "user_message": "oi"},
                                     idempotency_key="outra-chave", run_id=run["id"])
        await self.worker.run_once()
        self.assertEqual(len(self.llms["local"].calls), calls)  # no second model call for a finished run
        self.assertEqual(self.job(job2)["state"], "concluido")
        crashed = self.new_run()
        self.runtime._set_state(self.a["id"], crashed["id"], "em_execucao")  # a worker died mid-run
        self.llms["local"].script = [LlmReply("retomada")]
        _, job3 = self.enqueue(crashed)
        await self.worker.run_once()
        self.assertEqual((self.job(job3)["state"], self.state(crashed)["state"]), ("concluido", "concluida"))

    async def test_transient_model_outage_is_retried_with_backoff(self):
        self.llms["local"].script = [LlmUnavailable(), LlmReply("agora foi")]
        run, job = self.enqueue()
        await self.worker.run_once()
        stored = self.job(job)
        self.assertEqual((stored["state"], stored["last_error"]), ("pendente", "llm_unavailable"))
        self.assertEqual(self.state(run)["state"], "falhou")
        self.assertFalse(await self.worker.run_once())  # backoff not elapsed
        self.clock.advance(2000)
        run_after = self.state(run)
        self.assertEqual(run_after["state"], "falhou")  # a failed run is final: the job reports it
        await self.worker.run_once()
        self.assertEqual(self.job(job)["state"], "falhou")

    async def test_cancelling_the_job_stops_the_model_and_the_tool_call_in_flight(self):
        self.transport.delay = 5
        self.llms["local"].script = [tool_call(), LlmReply("nao deveria chegar")]
        run, job = self.enqueue()
        running = asyncio.ensure_future(self.worker.run_once())
        await asyncio.sleep(0.4)  # the model answered and the MCP call is in flight
        self.queue.cancel(self.a["id"], job["id"])
        await asyncio.wait_for(running, 5)
        self.assertEqual(self.job(job)["state"], "cancelado")
        self.assertEqual(self.state(run)["state"], "cancelada")
        self.assertTrue(self.transport.cancelled)

    async def test_pausing_and_resuming_starts_a_fresh_run_of_the_same_version(self):
        self.llms["local"].delay = 5
        run, job = self.enqueue()
        running = asyncio.ensure_future(self.worker.run_once())
        await asyncio.sleep(0.3)
        self.queue.pause(self.a["id"], job["id"])
        await asyncio.wait_for(running, 5)
        self.assertEqual((self.job(job)["state"], self.state(run)["state"]), ("pausado", "cancelada"))
        self.queue.resume(self.a["id"], job["id"])
        self.llms["local"].delay = 0
        self.llms["local"].script = [LlmReply("retomado")]
        await self.worker.run_once()
        done = self.job(job)
        self.assertEqual(done["state"], "concluido")
        self.assertNotEqual(done["checkpoint"]["run_id"], run["id"])
        new_run = self.state({"id": done["checkpoint"]["run_id"]})
        self.assertEqual((new_run["state"], new_run["agent_version"]), ("concluida", run["agent_version"]))


if __name__ == "__main__":
    unittest.main()
