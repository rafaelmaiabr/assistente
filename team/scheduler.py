"""Opt-in schedules, SLA escalation and low-noise operator notifications.

* A **schedule** is an explicit routine of one company: job kind, payload, time zone, frequency, catch-up policy and
  a daily run budget. It only *enqueues* a job in the durable queue; the worker's handler for that kind keeps its own
  rules, and a schedule never grants a permission or an approval (nor does it approve anything financial).
* ``tick`` is safe to run from several instances: the job idempotency key is derived from ``(schedule, occurrence)``
  and the schedule advances with a revision check, so an occurrence runs once. A restart applies the catch-up policy,
  a still-running previous job skips the next occurrence (no overlap), and the daily budget bounds the work.
* **SLA** sweeps look for stale hand-offs, approvals and unattended cases and escalate to the operator with a limited
  number of levels; they never re-assign work forever.
* **Notifications** merge repeats inside a cool-down into one record with a counter.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from .access import AccessContext, NotFound, PermissionDenied
from .cases import clean_text
from .ids import new_id
from .repository import IntegrityViolation, RevisionConflict
from .schema import find_secrets, validate

GRACE_SECONDS = 120
MAX_SCAN = 2000
MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_HOURS = 6
DEFAULT_KINDS = ("email_ingest", "asset_check", "sla_sweep", "retention_sweep")
_OFFSET = re.compile(r"^UTC([+-])(\d{2}):(\d{2})$")
_TERMINAL_JOB = ("concluido", "cancelado", "falhou", "esgotado")
SLA_DEFAULTS = {"accept_minutes": 30, "approval_minutes": 240, "response_minutes": 120, "max_escalations": 2}


class ScheduleRejected(ValueError):
    """The request is not valid; the message is safe to show."""


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def tzinfo_for(name):
    """``UTC``, a fixed ``UTC+03:00`` offset, or an IANA name (needs the ``tzdata`` package on Windows)."""
    if name == "UTC":
        return timezone.utc
    match = _OFFSET.match(str(name))
    if match:
        sign = 1 if match.group(1) == "+" else -1
        if int(match.group(2)) > 14 or int(match.group(3)) > 59:
            raise ScheduleRejected("Deslocamento de fuso invalido.")
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception:
        raise ScheduleRejected("Fuso horario desconhecido ou sem banco de fusos instalado (use UTC, UTC-03:00 ou instale o pacote tzdata).") from None


def following(schedule, moment):
    """The first occurrence strictly after ``moment`` (UTC)."""
    if schedule["frequency"] == "intervalo":
        return moment + timedelta(minutes=schedule["interval_minutes"])
    zone = tzinfo_for(schedule["timezone"])
    hour, minute = (int(part) for part in schedule["time_of_day"].split(":"))
    day = moment.astimezone(zone).date()
    for _ in range(15):
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)  # a skipped local time runs right after the gap
        utc = candidate.astimezone(timezone.utc)
        if utc > moment and (schedule["frequency"] == "diaria" or day.weekday() == schedule["weekday"]):
            return utc
        day += timedelta(days=1)
    raise ScheduleRejected("Nao foi possivel calcular a proxima execucao.")


class Notifier:
    """Operator notifications with a cool-down: the same situation raises a counter, not a new alert."""

    def __init__(self, repository, clock=lambda: datetime.now(timezone.utc), cooldown_hours=COOLDOWN_HOURS):
        self.repository, self.clock, self.cooldown = repository, clock, timedelta(hours=cooldown_hours)

    def notify(self, tenant_id, *, topic, key, message, severity="atencao", subject_ref=None):
        now = self.clock()
        text = clean_text(message)[:1000]
        with self.repository.transaction(tenant_id) as session:
            for existing in sorted(session.list("notification", dedupe_key=key, limit=50), key=lambda n: n["last_seen_at"], reverse=True):
                if existing["state"] == "nova" and now - _parse(existing["last_seen_at"]) < self.cooldown:
                    return session.save({**existing, "count": existing["count"] + 1, "last_seen_at": _stamp(now),
                                         "revision": existing["revision"] + 1, "updated_at": _stamp(now)}), False
                break
            doc = {"kind": "notification", "schema_version": 1, "id": new_id("notification"), "tenant_id": tenant_id, "revision": 1,
                   "created_at": _stamp(now), "updated_at": _stamp(now), "severity": severity, "topic": topic, "dedupe_key": key,
                   "message": text, "subject_ref": subject_ref, "state": "nova", "count": 1, "last_seen_at": _stamp(now)}
            return session.save(doc), True

    def list(self, ctx, *, state=None):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator":
            raise PermissionDenied()
        with self.repository.transaction(ctx.tenant_id) as session:
            items = session.list("notification", limit=1000, **({"state": state} if state else {}))
        return sorted(items, key=lambda n: n["last_seen_at"], reverse=True)

    def mark_read(self, ctx, notification_id):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator":
            raise PermissionDenied()
        with self.repository.transaction(ctx.tenant_id) as session:
            item = session.get("notification", notification_id) if isinstance(notification_id, str) else None
            if item is None:
                raise NotFound()
            return session.save({**item, "state": "lida", "revision": item["revision"] + 1, "updated_at": _stamp(self.clock())})

    def latest(self, tenant_id, topic, subject_ref):
        with self.repository.transaction(tenant_id) as session:
            items = [n for n in session.list("notification", topic=topic, limit=1000) if n["subject_ref"] == subject_ref]
        return max(items, key=lambda n: n["created_at"], default=None)


class ScheduleService:
    def __init__(self, repository, access, queue, notifier, clock=lambda: datetime.now(timezone.utc), allowed_kinds=DEFAULT_KINDS,
                 validators=None):
        """``validators``: ``kind -> callable(tenant_id, payload)`` raising ``ScheduleRejected`` for a payload that points nowhere."""
        self.repository, self.access, self.queue, self.notifier, self.clock = repository, access, queue, notifier, clock
        self.allowed_kinds, self.validators = tuple(allowed_kinds), validators or {}

    def _operator(self, ctx):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied()

    # -- management (operator only) ----------------------------------------------------------------------------------
    def create(self, ctx, *, name, job_kind, payload, timezone_name, frequency, time_of_day=None, weekday=None, interval_minutes=None,
               catch_up="uma_vez", max_runs_per_day=24, opt_in=False):
        self._operator(ctx)
        if job_kind not in self.allowed_kinds:
            raise ScheduleRejected("Tipo de rotina nao permitido: " + ", ".join(self.allowed_kinds))
        if not isinstance(payload, dict) or len(json.dumps(payload)) > 4000 or find_secrets(payload):
            raise ScheduleRejected("O payload deve ser um objeto pequeno e sem segredos.")
        tzinfo_for(timezone_name)
        validator = self.validators.get(job_kind)
        if validator:
            validator(ctx.tenant_id, payload)
        now = self.clock()
        doc = {"kind": "schedule", "schema_version": 1, "id": new_id("schedule"), "tenant_id": ctx.tenant_id, "revision": 1,
               "created_at": _stamp(now), "updated_at": _stamp(now), "name": clean_text(name)[:120], "job_kind": job_kind, "payload": payload,
               "timezone": timezone_name, "frequency": frequency, "time_of_day": time_of_day, "weekday": weekday,
               "interval_minutes": interval_minutes, "catch_up": catch_up, "max_runs_per_day": max_runs_per_day, "state": "ativo",
               "state_reason": None, "next_run_at": _stamp(now), "last_run_at": None, "last_job_id": None, "runs_day": None, "runs_today": 0,
               "overlaps": 0, "budget_skips": 0, "catchup_skipped": 0, "failures": 0, "opt_in": opt_in, "created_by": ctx.principal_id}
        validate("schedule", doc)  # field combinations first: ``following`` assumes a coherent schedule
        doc["next_run_at"] = _stamp(following(doc, now))
        with self.repository.transaction(ctx.tenant_id) as session:
            return session.save(doc)

    def _change(self, ctx, schedule_id, expected_revision, **changes):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("schedule", schedule_id) if isinstance(schedule_id, str) else None
            if current is None:
                raise NotFound()
            if current["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            return session.save({**current, **changes, "revision": current["revision"] + 1, "updated_at": _stamp(self.clock())})

    def set_state(self, ctx, schedule_id, expected_revision, state, reason):
        """Pause, resume or disable. A resumed schedule continues from the next future occurrence (no catch-up of the pause)."""
        if state not in ("ativo", "pausado", "desativado"):
            raise ScheduleRejected("Estado invalido.")
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("schedule", schedule_id) if isinstance(schedule_id, str) else None
        if current is None:
            raise NotFound()
        changes = {"state": state, "state_reason": clean_text(reason)[:300]}
        if state == "ativo":
            changes.update(failures=0, next_run_at=_stamp(following(current, self.clock())))
        return self._change(ctx, schedule_id, expected_revision, **changes)

    def reschedule(self, ctx, schedule_id, expected_revision, **fields):
        """Change time zone, time, weekday or interval. Old occurrences are not replayed: the next one is computed from now."""
        allowed = {"timezone": "timezone", "time_of_day": "time_of_day", "weekday": "weekday", "interval_minutes": "interval_minutes"}
        if set(fields) - set(allowed):
            raise ScheduleRejected("Campos permitidos: " + ", ".join(allowed))
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("schedule", schedule_id) if isinstance(schedule_id, str) else None
        if current is None:
            raise NotFound()
        candidate = {**current, **fields}
        tzinfo_for(candidate["timezone"])
        candidate["next_run_at"] = _stamp(following(candidate, self.clock()))
        return self._change(ctx, schedule_id, expected_revision, **fields, next_run_at=candidate["next_run_at"])

    def list_schedules(self, ctx):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            return session.list("schedule", limit=1000)

    # -- the loop body ---------------------------------------------------------------------------------------------
    def tick(self, now=None):
        """Run every due schedule once. Returns one record per schedule that was looked at."""
        now = now or self.clock()
        with self.repository._transaction() as conn:
            due = conn.execute("SELECT id, tenant_id FROM schedules WHERE state = 'ativo' AND next_run_at <= ? ORDER BY next_run_at LIMIT 200",
                               (_stamp(now),)).fetchall()
        return [self._run_one(tenant, schedule_id, now) for schedule_id, tenant in due]

    def _run_one(self, tenant_id, schedule_id, now):
        with self.repository.transaction(tenant_id) as session:
            schedule = session.get("schedule", schedule_id)
        if schedule is None or schedule["state"] != "ativo" or _parse(schedule["next_run_at"]) > now:
            return {"schedule": schedule_id, "acao": "nada"}
        # occurrences that fell due since the last run
        due, cursor = [], _parse(schedule["next_run_at"])
        while cursor <= now and len(due) < MAX_SCAN:
            due.append(cursor)
            cursor = following(schedule, cursor)
        latest = due[-1]
        on_time = now - latest <= timedelta(seconds=GRACE_SECONDS)
        skipped_catchup = len(due) - 1 if (on_time or schedule["catch_up"] == "uma_vez") else len(due)
        run_it = on_time or schedule["catch_up"] == "uma_vez"
        changes = {"next_run_at": _stamp(cursor), "catchup_skipped": schedule["catchup_skipped"] + max(skipped_catchup, 0)}
        outcome = "executada"
        day = latest.astimezone(tzinfo_for(schedule["timezone"])).date().isoformat()
        runs_today = schedule["runs_today"] if schedule["runs_day"] == day else 0
        job_id = None
        if not run_it:
            outcome = "catch_up_pulado"
        else:
            previous = self._previous_job(tenant_id, schedule)
            if previous is not None and previous["state"] not in _TERMINAL_JOB:
                outcome = "sobreposicao"
                changes["overlaps"] = schedule["overlaps"] + 1
            elif runs_today >= schedule["max_runs_per_day"]:
                outcome = "orcamento_esgotado"
                changes["budget_skips"] = schedule["budget_skips"] + 1
            else:
                if previous is not None and previous["state"] in ("falhou", "esgotado"):
                    changes["failures"] = schedule["failures"] + 1
                elif previous is not None:
                    changes["failures"] = 0
                job, _ = self.queue.enqueue(tenant_id, schedule["job_kind"], {**schedule["payload"], "schedule_id": schedule["id"],
                                                                           "occurrence": _stamp(latest)},
                                            idempotency_key=f"schedule:{schedule['id']}:{_stamp(latest)}", max_attempts=2)
                job_id = job["id"]
                changes.update(last_run_at=_stamp(now), last_job_id=job_id, runs_day=day, runs_today=runs_today + 1)
        if changes.get("failures", schedule["failures"]) >= MAX_CONSECUTIVE_FAILURES:
            changes.update(state="pausado", state_reason=f"pausada automaticamente apos {MAX_CONSECUTIVE_FAILURES} falhas seguidas")
        try:
            with self.repository.transaction(tenant_id) as session:
                fresh = session.get("schedule", schedule_id)
                if fresh["revision"] != schedule["revision"]:
                    return {"schedule": schedule_id, "acao": "outra_instancia"}  # another instance advanced it first
                session.save({**fresh, **changes, "revision": fresh["revision"] + 1, "updated_at": _stamp(now)})
        except (RevisionConflict, IntegrityViolation):
            return {"schedule": schedule_id, "acao": "outra_instancia"}
        self._alert(tenant_id, schedule, outcome, changes, day)
        return {"schedule": schedule_id, "acao": outcome, "job": job_id, "ocorrencia": _stamp(latest),
                "puladas": max(skipped_catchup, 0)}

    def _previous_job(self, tenant_id, schedule):
        if not schedule["last_job_id"]:
            return None
        try:
            return self.queue.get(tenant_id, schedule["last_job_id"])
        except NotFound:
            return None

    def _alert(self, tenant_id, schedule, outcome, changes, day):
        name, key = schedule["name"], f"schedule:{schedule['id']}"
        if outcome == "orcamento_esgotado":
            self.notifier.notify(tenant_id, topic="rotina-orcamento", key=f"{key}:orcamento:{day}", severity="atencao", subject_ref=schedule["id"],
                                 message=f"A rotina '{name}' atingiu o limite diario de {schedule['max_runs_per_day']} execucoes; as demais ficam para amanha.")
        elif outcome == "sobreposicao" and changes["overlaps"] >= 3:
            self.notifier.notify(tenant_id, topic="rotina-lenta", key=f"{key}:sobreposicao", severity="atencao", subject_ref=schedule["id"],
                                 message=f"A rotina '{name}' esta pulando execucoes porque a anterior ainda nao terminou.")
        elif outcome == "catch_up_pulado":
            self.notifier.notify(tenant_id, topic="rotina-atrasada", key=f"{key}:catchup", severity="info", subject_ref=schedule["id"],
                                 message=f"O aplicativo ficou parado e a rotina '{name}' pulou {changes['catchup_skipped'] - schedule['catchup_skipped']} execucao(oes) atrasada(s).")
        if changes.get("state") == "pausado":
            self.notifier.notify(tenant_id, topic="rotina-pausada", key=f"{key}:pausada", severity="acao", subject_ref=schedule["id"],
                                 message=f"A rotina '{name}' foi pausada apos falhas seguidas. Corrija a causa e retome-a.")


class SlaService:
    """Escalate stale hand-offs, approvals and unattended cases to the operator, a limited number of times."""

    def __init__(self, repository, access, notifier, clock=lambda: datetime.now(timezone.utc), config=None):
        self.repository, self.access, self.notifier, self.clock = repository, access, notifier, clock
        self.config = {**SLA_DEFAULTS, **(config or {})}

    def sweep(self, tenant_id):
        now, escalations = self.clock(), []
        cfg = self.config
        with self.repository.transaction(tenant_id) as session:
            subjects = [("encaminhamento", h["id"], h["created_at"], cfg["accept_minutes"], "O encaminhamento aguarda aceite",
                         "sla-aceite", "handoff") for h in session.list("handoff", state="proposto", limit=1000)]
            subjects += [("aprovacao", a["id"], a["created_at"], cfg["approval_minutes"], f"A aprovacao de '{a['action']}' aguarda decisao",
                          "sla-aprovacao", "approval") for a in session.list("approval_request", state="pendente", limit=1000)]
            subjects += [("demanda", c["id"], c["created_at"], cfg["response_minutes"], "A demanda recebida ainda nao tem responsavel nem resposta",
                          "sla-resposta", "case") for c in session.list("case", limit=1000)
                         if c["state"] in ("recebida", "triagem") and c["owner_agent_id"] is None]
            for kind, subject_id, created, minutes, message, topic, label in subjects:
                age = now - _parse(created)
                done = {row[0] for row in session.conn.execute(
                    "SELECT level FROM sla_escalations WHERE tenant_id = ? AND subject_id = ?", (tenant_id, subject_id))}
                for level in range(1, int(cfg["max_escalations"]) + 1):
                    if level in done or age < timedelta(minutes=minutes * level):
                        continue
                    session.conn.execute("INSERT INTO sla_escalations (tenant_id, subject_kind, subject_id, level, escalated_at) VALUES (?, ?, ?, ?, ?)",
                                         (tenant_id, kind, subject_id, level, _stamp(now)))
                    escalations.append((topic, kind, subject_id, level, message, int(age.total_seconds() // 60), label))
                    break  # one level per sweep: the next waits for the next window
        for topic, kind, subject_id, level, message, minutes, _ in escalations:
            self.notifier.notify(tenant_id, topic=topic, key=f"sla:{subject_id}:{level}", severity="acao" if level >= 2 else "atencao",
                                 subject_ref=subject_id, message=f"{message} ha {minutes} min (nivel {level} de {self.config['max_escalations']}).")
        return [{"assunto": kind, "id": subject_id, "nivel": level, "minutos": minutes} for _, kind, subject_id, level, _, minutes, _ in escalations]


def asset_check_handler(diagnostics_factory, notifier):
    """Queue handler for ``asset_check`` jobs. Payload: ``hostname``. Notifies only when the diagnosis category changes."""
    from .worker import Done, Fail

    async def handler(job, control):
        import asyncio
        tenant_id, host = job["tenant_id"], job["payload"].get("hostname")
        try:
            result = await asyncio.to_thread(diagnostics_factory(tenant_id).diagnose, host)
        except Exception:
            return Fail("diagnostico_indisponivel")
        category = result["categoria"]
        previous = notifier.latest(tenant_id, "ativo-status", host)
        known = previous["dedupe_key"].rsplit(":", 1)[-1] if previous else None
        if category != known and not (known is None and category == "site_ok"):
            notifier.notify(tenant_id, topic="ativo-status", key=f"ativo:{host}:{category}", subject_ref=host,
                            severity="info" if category == "site_ok" else "acao",
                            message=f"{host}: {result['resumo']} Proximo passo: {result['proximo_passo']}")
        return Done({"categoria": category, "certeza": result["certeza"]})

    return handler


def email_ingest_handler(ingest, context_for):
    """Queue handler for ``email_ingest`` jobs. Payload: ``connection_id``, ``folder``. ``context_for(tenant)`` gives the operator context."""
    from .worker import Done, Fail

    async def handler(job, control):
        import asyncio
        payload = job["payload"]
        try:
            report = await asyncio.to_thread(ingest.ingest, context_for(job["tenant_id"]), payload["connection_id"], payload["folder"])
        except Exception:
            return Fail("ingestao_falhou")
        if report.get("erro") or report.get("parou_em"):
            return Fail(report.get("erro") or "ingestao_parcial")
        return Done({"processadas": report["processadas"], "por_classificacao": report["por_classificacao"]})

    return handler
