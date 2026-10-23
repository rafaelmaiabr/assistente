"""A separate worker process for the multi-process queue test: claims, works and completes jobs; prints a JSON report."""
import json
import os
import sys
import time

from team.jobs import JobQueue
from team.pgdb import PostgresRepository

name = sys.argv[1]
identity = f"{name}-{os.getpid()}"
queue = JobQueue(PostgresRepository(os.environ["WORKER_DSN"], max_connections=2))
handled, repeated, idle = [], 0, 0
while idle < 25:
    job = queue.claim(identity, lease_ms=60_000)
    if job is None:
        idle += 1
        time.sleep(0.05)
        continue
    idle = 0
    if not queue.consume_once("efeito-externo", job["id"]):  # had the external effect of this job already happened?
        repeated += 1
    handled.append(job["id"])
    queue.complete(job["id"], identity, job["token"])
print(json.dumps({"worker": name, "jobs": handled, "effects_repeated": repeated}))
