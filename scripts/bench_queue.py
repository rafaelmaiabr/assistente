"""Measure the durable queue (enqueue, claim, complete) on SQLite or PostgreSQL, to decide with numbers whether a broker is needed.

    python scripts/bench_queue.py sqlite --jobs 1500 --workers 4
    set ASSISTENTE_PG_DSN=postgresql://...   &   python scripts/bench_queue.py postgres --jobs 1500 --workers 6

It builds a throw-away company in a throw-away schema/file, enqueues the jobs, runs the workers as threads of this process (trivial
work: claim, complete) and prints jobs per second and the latency percentiles of a claim. It measures the QUEUE, not the model: an
agent run takes seconds, so the queue needs a small fraction of the numbers printed here (docs/TEAM-POSTGRES.md).
"""
import argparse
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from team.access import AccessService  # noqa: E402
from team.ids import new_id  # noqa: E402
from team.jobs import JobQueue  # noqa: E402
from team.repository import SqliteRepository  # noqa: E402
from team.service import TeamService  # noqa: E402


def build(backend):
    if backend == "sqlite":
        folder = tempfile.mkdtemp(prefix="assistente-bench-")
        repo = SqliteRepository(Path(folder) / "bench.sqlite3")
        cleanup = lambda: None  # noqa: E731
    else:
        from team.pgdb import PostgresRepository
        from tests.pg_support import PostgresSchema
        os.environ.setdefault("ASSISTENTE_TEST_POSTGRES_DSN", os.environ.get("ASSISTENTE_PG_DSN", ""))
        schema = PostgresSchema()
        repo, cleanup = PostgresRepository(schema.dsn, max_connections=32), schema.drop
    repo.migrate()
    access = AccessService(repo, new_id("user"))
    company = TeamService(repo, access).create_company(access.local_operator(), "Benchmark")
    return repo, access, company, cleanup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backend", choices=("sqlite", "postgres"))
    parser.add_argument("--jobs", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    repo, access, company, cleanup = build(args.backend)
    try:
        queue = JobQueue(repo, access)
        started = time.perf_counter()
        for n in range(args.jobs):
            queue.enqueue(company["id"], "bench", {"n": n}, idempotency_key=f"b{n}")
        enqueue_s = time.perf_counter() - started
        latencies, done, lock = [], [], threading.Lock()

        def work(name):
            while True:
                t0 = time.perf_counter()
                job = queue.claim(name, lease_ms=60_000)
                took = time.perf_counter() - t0
                if job is None:
                    return
                queue.complete(job["id"], name, job["token"])
                with lock:
                    latencies.append(took)
                    done.append(job["id"])

        started = time.perf_counter()
        threads = [threading.Thread(target=work, args=(f"w{n}",)) for n in range(args.workers)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        run_s = time.perf_counter() - started
        ordered = sorted(latencies)
        pct = lambda p: ordered[min(len(ordered) - 1, int(len(ordered) * p))] * 1000  # noqa: E731
        print(f"{args.backend}: {args.jobs} jobs, {args.workers} workers")
        print(f"  enqueue : {args.jobs / enqueue_s:8.0f} jobs/s")
        print(f"  claim+complete : {len(done) / run_s:8.0f} jobs/s   (todos distintos: {len(set(done)) == len(done) == args.jobs})")
        print(f"  claim latency  : p50 {statistics.median(latencies) * 1000:.1f} ms   p95 {pct(0.95):.1f} ms   max {ordered[-1] * 1000:.1f} ms")
        return 0
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
