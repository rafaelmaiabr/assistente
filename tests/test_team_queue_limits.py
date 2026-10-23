import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from team.jobs import Backpressure, JobQueue, LeaseLost, QueueLimits
from team.pgdb import DatabaseSaturated, DatabaseUnavailable, PostgresRepository
from team.worker import OutboxDispatcher
from tests.pg_support import PostgresSchema, requires_postgres
from tests.test_team_queue import QueueCase

ROOT = Path(__file__).resolve().parent.parent


class LimitsContract:
    """Quotas, fairness and backpressure behave the same on SQLite and on PostgreSQL."""

    def limited(self, **limits):
        return JobQueue(self.repo, self.access, clock=self.clock, limits=QueueLimits(**limits))

    def enqueue(self, queue, tenant, key, **kwargs):
        return queue.enqueue(tenant["id"], "teste", {"n": key}, idempotency_key=key, **kwargs)[0]

    def test_no_limits_is_how_the_queue_always_behaved(self):
        queue = self.limited()
        for n in range(30):
            self.enqueue(queue, self.a, f"k{n}")
        self.assertEqual(len([queue.claim(f"w{n}") for n in range(30)]), 30)
        self.assertIsNone(queue.claim("w-extra"))

    def test_backpressure_stops_new_work_per_company_and_in_total_but_not_repeats(self):
        queue = self.limited(max_pending_per_tenant=2, max_pending_total=3)
        first = self.enqueue(queue, self.a, "a1")
        self.enqueue(queue, self.a, "a2")
        with self.assertRaises(Backpressure) as full:
            self.enqueue(queue, self.a, "a3")
        self.assertEqual(full.exception.scope, "empresa")
        self.assertEqual(self.enqueue(queue, self.a, "a1")["id"], first["id"])      # a repeat is not new work
        self.enqueue(queue, self.b, "b1")                                           # another company is unaffected...
        with self.assertRaises(Backpressure) as total:                              # ...until the overall limit
            self.enqueue(queue, self.b, "b2")
        self.assertEqual(total.exception.scope, "total")
        queue.claim("w1")                                                            # claiming frees room
        self.enqueue(queue, self.a, "a3")

    def test_one_company_cannot_run_more_than_its_quota(self):
        queue = self.limited(max_running_per_tenant=2)
        for n in range(5):
            self.enqueue(queue, self.a, f"a{n}")
        running = [queue.claim(f"w{n}") for n in range(3)]
        self.assertEqual([j is not None for j in running], [True, True, False])
        queue.complete(running[0]["id"], "w0", running[0]["token"])
        self.assertIsNotNone(queue.claim("w3"))                                      # a slot was freed

    def test_a_busy_company_does_not_starve_the_others(self):
        queue = self.limited()
        for n in range(10):
            self.enqueue(queue, self.a, f"a{n}")
        self.enqueue(queue, self.b, "b0", priority=-5)                               # even with the lowest priority
        order = [queue.claim(f"w{n}")["tenant_id"] for n in range(2)]
        self.assertEqual(sorted(order), sorted([self.a["id"], self.b["id"]]))        # the second claim goes to the idle company

    def test_total_and_per_kind_caps_keep_the_model_server_within_its_capacity(self):
        queue = self.limited(max_running_total=3, max_running_per_kind={"agent_run": 1})
        for n in range(3):
            queue.enqueue(self.a["id"], "agent_run", {"n": n}, idempotency_key=f"m{n}")
            queue.enqueue(self.b["id"], "outro", {"n": n}, idempotency_key=f"o{n}")
        claimed = [queue.claim(f"w{n}") for n in range(6)]
        kinds = [j["kind"] for j in claimed if j]
        self.assertEqual(kinds.count("agent_run"), 1)                                # one model call in flight, whatever the workers
        self.assertEqual(len(kinds), 3)                                              # and never more than the total cap

    def test_the_one_active_job_per_case_still_holds_with_limits(self):
        queue = self.limited(max_running_per_tenant=5)
        case = self.new_case()
        for n in range(3):
            self.enqueue(queue, self.a, f"c{n}", case_id=case)
        self.assertEqual([queue.claim(f"w{n}") is not None for n in range(3)], [True, False, False])


class LimitsSqliteTests(LimitsContract, QueueCase):
    pass


class PostgresQueueCase(QueueCase):
    def make_repo(self):
        self.schema = PostgresSchema()
        self.addCleanup(self.schema.drop)
        return PostgresRepository(self.schema.dsn)


@requires_postgres
class LimitsPostgresTests(LimitsContract, PostgresQueueCase):
    pass


@requires_postgres
class ConcurrentWorkersTests(PostgresQueueCase):
    def test_many_workers_in_threads_run_every_job_exactly_once(self):
        queue = JobQueue(self.repo, self.access)  # the real clock: leases are long enough not to expire in a test
        total = 60
        for n in range(total):
            queue.enqueue(self.a["id"], "teste", {"n": n}, idempotency_key=f"j{n}")
        done, errors = [], []

        def work(name):
            try:
                while True:
                    job = queue.claim(name, lease_ms=60_000)
                    if job is None:
                        return
                    done.append((name, job["id"]))
                    queue.complete(job["id"], name, job["token"], {"por": name})
            except Exception as error:  # surfaced below
                errors.append(error)

        threads = [threading.Thread(target=work, args=(f"w{n}",)) for n in range(6)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(done), total)
        self.assertEqual(len({job_id for _, job_id in done}), total)               # nothing ran twice
        self.assertGreater(len({name for name, _ in done}), 1)                     # and the work was really shared
        self.assertEqual({j["state"] for j in queue.list_jobs(self.a["id"], limit=1000)}, {"concluido"})

    def test_workers_on_the_same_case_never_overlap(self):
        queue = JobQueue(self.repo, self.access)
        case, errors, running, peak = self.new_case(), [], [0], [0]
        lock = threading.Lock()
        for n in range(12):
            queue.enqueue(self.a["id"], "teste", {"n": n}, idempotency_key=f"c{n}", case_id=case)

        def work(name):
            try:
                for _ in range(400):
                    job = queue.claim(name, lease_ms=60_000)
                    if job is None:
                        if not [j for j in queue.list_jobs(self.a["id"], limit=100) if j["state"] in ("pendente", "em_execucao")]:
                            return
                        continue
                    with lock:
                        running[0] += 1
                        peak[0] = max(peak[0], running[0])
                    with lock:
                        running[0] -= 1
                    queue.complete(job["id"], name, job["token"])
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=work, args=(f"w{n}",)) for n in range(5)]
        [t.start() for t in threads]
        [t.join(90) for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual({j["state"] for j in queue.list_jobs(self.a["id"], limit=100)}, {"concluido"})
        self.assertEqual(peak[0], 1)

    def test_a_lease_lost_to_another_worker_cannot_be_used_any_more(self):
        queue = JobQueue(self.repo, self.access, clock=self.clock)
        queue.enqueue(self.a["id"], "teste", {}, idempotency_key="x")
        first = queue.claim("lento", lease_ms=1000)
        self.clock.advance(5000)
        second = queue.claim("rapido", lease_ms=1000)
        self.assertEqual((second["id"], second["token"]), (first["id"], first["token"] + 1))
        with self.assertRaises(LeaseLost):
            queue.complete(first["id"], "lento", first["token"])                      # the old worker was fenced out
        queue.complete(second["id"], "rapido", second["token"])

    def test_separate_worker_processes_share_one_queue_without_duplicates(self):
        queue = JobQueue(self.repo, self.access)
        total = 40
        for n in range(total):
            queue.enqueue(self.a["id"], "teste", {"n": n}, idempotency_key=f"p{n}")
        env = {**os.environ, "WORKER_DSN": self.schema.dsn, "PYTHONPATH": str(ROOT)}
        procs = [subprocess.Popen([sys.executable, str(ROOT / "tests" / "pg_worker_process.py"), name], env=env, cwd=str(ROOT),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("proc-a", "proc-b", "proc-c")]
        reports = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err[-500:])
            reports.append(json.loads(out.strip().splitlines()[-1]))
        handled = [job for r in reports for job in r["jobs"]]
        self.assertEqual(len(handled), total)
        self.assertEqual(len(set(handled)), total)                                  # each job handled by exactly one process
        self.assertTrue(all(r["effects_repeated"] == 0 for r in reports))
        self.assertGreater(len([r for r in reports if r["jobs"]]), 1)
        jobs = queue.list_jobs(self.a["id"], limit=1000)
        self.assertEqual({j["state"] for j in jobs}, {"concluido"})
        self.assertTrue(all(j["lease_owner"] is None for j in jobs))


@requires_postgres
class DatabaseFailureTests(PostgresQueueCase):
    def test_an_unreachable_database_is_reported_and_recovery_needs_nothing_special(self):
        broken = JobQueue(PostgresRepository("postgresql://x:y@127.0.0.1:1/z", acquire_timeout=1), self.access)
        with self.assertRaises(DatabaseUnavailable):
            broken.claim("w")
        queue = JobQueue(self.repo, self.access)
        job = queue.enqueue(self.a["id"], "teste", {}, idempotency_key="depois")[0]
        self.assertEqual(queue.claim("w")["id"], job["id"])                         # the healthy database simply works

    def test_all_connections_busy_is_backpressure_not_a_pile_up(self):
        small = PostgresRepository(self.schema.dsn, max_connections=1, acquire_timeout=0.3)
        started, release = threading.Event(), threading.Event()

        def hold():
            with small._transaction() as conn:
                conn.execute("SELECT 1")
                started.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(started.wait(5))
        try:
            with self.assertRaises(DatabaseSaturated):
                with small._transaction():
                    pass
        finally:
            release.set()
            holder.join(5)
        with small._transaction() as conn:                                           # and it recovers when the slot is free
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

    def test_a_job_whose_worker_died_returns_to_the_queue(self):
        queue = JobQueue(self.repo, self.access, clock=self.clock)
        queue.enqueue(self.a["id"], "teste", {}, idempotency_key="morreu")
        lost = queue.claim("morto", lease_ms=1000)
        self.clock.advance(60_000)                                                   # the worker never came back
        again = queue.claim("vivo")
        self.assertEqual((again["id"], again["token"]), (lost["id"], lost["token"] + 1))


@requires_postgres
class OutboxRecoveryTests(PostgresQueueCase):
    def make_events(self):
        case = self.new_case()
        return case

    def test_a_delivery_that_fails_waits_in_order_and_is_redelivered_and_two_dispatchers_cannot_double_the_effect(self):
        self.make_events()
        queue = JobQueue(self.repo, self.access)
        seen, crashed = [], []

        def subscriber(event):
            if not crashed:
                crashed.append(1)
                raise RuntimeError("caiu no meio da entrega")
            if queue.consume_once("assinante", event["id"]):
                seen.append(event["id"])

        dispatcher = OutboxDispatcher(self.repo, {"domain": [subscriber]}, max_attempts=5)
        self.assertEqual(dispatcher.dispatch_once(self.a["id"]), 0)                  # the first delivery failed: the row waits
        with self.repo.transaction(self.a["id"]) as session:
            self.assertEqual(len(session.pending_outbox()), 1)
        self.assertEqual(dispatcher.dispatch_once(self.a["id"]), 1)                  # redelivered
        self.assertEqual(len(seen), 1)
        with self.repo.transaction(self.a["id"]) as session:
            self.assertEqual(session.pending_outbox(), [])
        # a crash after the effect and before the mark: the event comes again, the consumer's own dedup keeps one effect
        self.make_events()
        marks = []

        class Flaky(OutboxDispatcher):
            def dispatch_once(inner, tenant_id, limit=50):
                result = None
                with self.repo.transaction(tenant_id) as session:
                    rows = session.pending_outbox(limit)
                for row in rows:
                    for sub in inner.subscribers.get(row["topic"], []):
                        sub(row["event"])        # the effect happens...
                    marks.append(row["id"])      # ...and the process dies before marking it dispatched
                return result
        Flaky(self.repo, {"domain": [subscriber]}).dispatch_once(self.a["id"])
        before = len(seen)
        healthy = OutboxDispatcher(self.repo, {"domain": [subscriber]})
        self.assertEqual(healthy.dispatch_once(self.a["id"]), len(marks))            # delivered again
        self.assertEqual(len(seen), before)                                          # but the effect was not repeated


if __name__ == "__main__":
    unittest.main()
