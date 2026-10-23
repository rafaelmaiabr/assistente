import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.approvals import ApprovalService
from team.cases import CaseService
from team.handoffs import HandoffService
from team.ids import new_id
from team.jobs import JobQueue
from team.repository import RevisionConflict, SqliteRepository
from team.scheduler import (
    MAX_CONSECUTIVE_FAILURES, Notifier, ScheduleRejected, ScheduleService, SlaService, asset_check_handler, email_ingest_handler,
    following, tzinfo_for)
from team.schema import ContractError
from team.service import TeamService

PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SqliteRepository(Path(self.temp.name) / "team.sqlite3")
        self.repo.migrate()
        self.clock = Clock()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.handoffs = HandoffService(self.repo, self.access, self.cases, clock=self.clock)
        self.approvals = ApprovalService(self.repo, self.access, self.cases, clock=self.clock)
        self.queue = JobQueue(self.repo, self.access, clock=lambda: int(self.clock.now.timestamp() * 1000))
        self.notifier = Notifier(self.repo, clock=self.clock)
        self.scheduler = ScheduleService(self.repo, self.access, self.queue, self.notifier, clock=self.clock)
        self.sla = SlaService(self.repo, self.access, self.notifier, clock=self.clock)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])

    def create(self, **kwargs):
        options = dict(name="Checagem", job_kind="asset_check", payload={"hostname": "exemplo.com.br"}, timezone_name="UTC", frequency="intervalo",
                       interval_minutes=60, opt_in=True)
        options.update(kwargs)
        return self.scheduler.create(self.ca, **options)

    def jobs(self, state=None):
        return self.queue.list_jobs(self.a["id"], state=state, limit=1000)

    def get(self, schedule):
        return next(s for s in self.scheduler.list_schedules(self.ca) if s["id"] == schedule["id"])


class OccurrenceTests(unittest.TestCase):
    def test_intervals_daily_weekly_and_fixed_offsets(self):
        interval = {"frequency": "intervalo", "interval_minutes": 90, "timezone": "UTC"}
        self.assertEqual(following(interval, utc(2026, 6, 1, 10, 0)), utc(2026, 6, 1, 11, 30))
        daily = {"frequency": "diaria", "time_of_day": "08:00", "timezone": "UTC-03:00", "weekday": None}
        self.assertEqual(following(daily, utc(2026, 6, 1, 10, 0)), utc(2026, 6, 1, 11, 0))
        self.assertEqual(following(daily, utc(2026, 6, 1, 11, 0)), utc(2026, 6, 2, 11, 0))
        weekly = {"frequency": "semanal", "time_of_day": "09:30", "timezone": "UTC", "weekday": 0}  # Mondays
        self.assertEqual(following(weekly, utc(2026, 6, 1, 9, 30)), utc(2026, 6, 8, 9, 30))  # 2026-06-01 is a Monday
        self.assertEqual(following(weekly, utc(2026, 6, 3, 0, 0)), utc(2026, 6, 8, 9, 30))

    def test_time_zone_changes_and_daylight_saving(self):
        try:
            zone = tzinfo_for("America/New_York")
        except ScheduleRejected:
            self.skipTest("banco de fusos horarios (tzdata) indisponivel")
        self.assertIsNotNone(zone)
        schedule = {"frequency": "diaria", "time_of_day": "02:30", "timezone": "America/New_York", "weekday": None}
        # 2026-03-08: 02:30 does not exist locally; the run happens right after the gap (03:30 EDT = 07:30Z), once
        self.assertEqual(following(schedule, utc(2026, 3, 7, 12, 0)), utc(2026, 3, 8, 7, 30))
        self.assertEqual(following(schedule, utc(2026, 3, 8, 7, 30)), utc(2026, 3, 9, 6, 30))  # back to a normal 02:30 EDT
        # 2026-11-01: 01:30 happens twice; only the first (EDT) counts
        autumn = {**schedule, "time_of_day": "01:30"}
        first = following(autumn, utc(2026, 10, 31, 12, 0))
        self.assertEqual(first, utc(2026, 11, 1, 5, 30))
        self.assertEqual(following(autumn, first), utc(2026, 11, 2, 6, 30))
        brazil = {"frequency": "diaria", "time_of_day": "08:00", "timezone": "America/Sao_Paulo", "weekday": None}
        self.assertEqual(following(brazil, utc(2026, 6, 1, 0, 0)), utc(2026, 6, 1, 11, 0))

    def test_unknown_zones_and_offsets_are_refused_with_a_helpful_message(self):
        for bad in ("Marte/Olympus", "UTC+25:00", "UTC+03:99", "", "../../etc/passwd"):
            with self.assertRaises(ScheduleRejected, msg=bad):
                tzinfo_for(bad)


class ManagementTests(SchedulerCase):
    def test_a_schedule_must_be_explicit_known_and_secret_free(self):
        with self.assertRaises(ContractError):
            self.create(opt_in=False)
        for kind in ("agent_run", "enviar_mensagem", "pagar", "backup"):
            with self.assertRaises(ScheduleRejected):
                self.create(job_kind=kind)  # a schedule cannot trigger arbitrary work, sends or payments
        with self.assertRaises(ScheduleRejected):
            self.create(payload={"password": "abc", "token": "sk-abcdefghijklmnopqrstuvwxyz"})
        with self.assertRaises(ScheduleRejected):
            self.create(timezone_name="Marte/Olympus")
        for bad in (dict(frequency="diaria", interval_minutes=None), dict(frequency="semanal", time_of_day="08:00"),
                    dict(frequency="intervalo", interval_minutes=1), dict(max_runs_per_day=100000)):
            with self.assertRaises((ContractError, ScheduleRejected), msg=str(bad)):
                self.create(**bad)

    def test_validators_reject_payloads_that_point_nowhere_and_only_the_operator_manages(self):
        def need_host(tenant, payload):
            if payload.get("hostname") != "exemplo.com.br":
                raise ScheduleRejected("ativo nao autorizado")
        service = ScheduleService(self.repo, self.access, self.queue, self.notifier, clock=self.clock, validators={"asset_check": need_host})
        with self.assertRaisesRegex(ScheduleRejected, "nao autorizado"):
            service.create(self.ca, name="x", job_kind="asset_check", payload={"hostname": "outro.com"}, timezone_name="UTC",
                           frequency="intervalo", interval_minutes=60, opt_in=True)
        agent = self.agents.create_agent(self.ca, department_id=self.team.create(self.ca, "department", name="D", scope="x", status="ativo")["id"],
                                         name="A", **PROFILE)
        run = self.agents.start_run(self.ca, agent["id"], budget={"max_steps": 2, "token_budget": 100, "deadline_seconds": 60})
        agent_ctx = self.access.agent_context(self.op, self.a["id"], run["id"])
        with self.assertRaises(PermissionDenied):
            self.scheduler.create(agent_ctx, name="x", job_kind="asset_check", payload={}, timezone_name="UTC", frequency="intervalo",
                                  interval_minutes=60, opt_in=True)
        schedule = self.create()
        for call in (lambda: self.scheduler.set_state(agent_ctx, schedule["id"], 1, "pausado", "x"), lambda: self.scheduler.list_schedules(agent_ctx)):
            with self.assertRaises(PermissionDenied):
                call()
        with self.assertRaises(NotFound):
            self.scheduler.set_state(self.cb, schedule["id"], 1, "pausado", "x")
        self.assertEqual(self.scheduler.list_schedules(self.cb), [])


class TickTests(SchedulerCase):
    def test_a_due_schedule_enqueues_one_idempotent_job_and_advances(self):
        schedule = self.create()
        self.assertEqual(self.scheduler.tick(), [])  # nothing is due yet
        self.clock.advance(minutes=60)
        report = self.scheduler.tick()
        self.assertEqual([(r["acao"], r["puladas"]) for r in report], [("executada", 0)])
        job = self.jobs()[0]
        self.assertEqual((job["kind"], job["payload"]["hostname"], job["payload"]["schedule_id"]), ("asset_check", "exemplo.com.br", schedule["id"]))
        self.assertEqual(job["idempotency_key"], f"schedule:{schedule['id']}:{report[0]['ocorrencia']}")
        self.assertEqual(self.scheduler.tick(), [])
        stored = self.get(schedule)
        self.assertEqual((stored["runs_today"], stored["last_job_id"]), (1, job["id"]))
        self.assertGreater(stored["next_run_at"], report[0]["ocorrencia"])

    def test_a_restart_applies_the_catch_up_policy(self):
        once = self.create(name="uma vez", frequency="diaria", time_of_day="08:00", interval_minutes=None, catch_up="uma_vez")
        skip = self.create(name="pular", frequency="diaria", time_of_day="08:00", interval_minutes=None, catch_up="pular", payload={"hostname": "b.com.br"})
        self.clock.advance(days=3, hours=1)  # the app was off for three days
        actions = {r["schedule"]: r for r in self.scheduler.tick()}
        self.assertEqual((actions[once["id"]]["acao"], actions[once["id"]]["puladas"]), ("executada", 2))
        self.assertEqual((actions[skip["id"]]["acao"], actions[skip["id"]]["puladas"]), ("catch_up_pulado", 3))
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.get(skip)["catchup_skipped"], 3)
        self.assertEqual([n["topic"] for n in self.notifier.list(self.ca)], ["rotina-atrasada"])
        self.assertGreater(self.get(once)["next_run_at"], self.clock.now.strftime("%Y-%m-%dT%H:%M:%SZ"))  # nothing left overdue

    def test_a_slow_previous_job_skips_the_next_occurrence_instead_of_overlapping(self):
        schedule = self.create(interval_minutes=5)
        self.clock.advance(minutes=5)
        self.scheduler.tick()
        job = self.jobs()[0]
        for expected in range(1, 4):
            self.clock.advance(minutes=5)
            self.assertEqual(self.scheduler.tick()[0]["acao"], "sobreposicao")
            self.assertEqual(self.get(schedule)["overlaps"], expected)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual([n["topic"] for n in self.notifier.list(self.ca)], ["rotina-lenta"])  # one alert after repeated overlaps
        claimed = self.queue.claim("w1", tenant_id=self.a["id"])
        self.queue.complete(claimed["id"], "w1", claimed["token"], {})
        self.clock.advance(minutes=5)
        self.assertEqual(self.scheduler.tick()[0]["acao"], "executada")
        self.assertEqual(len(self.jobs()), 2)
        self.assertNotEqual(job["id"], self.get(schedule)["last_job_id"])

    def test_the_daily_budget_bounds_the_runs_and_resets_the_next_day(self):
        schedule = self.create(interval_minutes=60, max_runs_per_day=2)
        self.clock.now = self.clock.now.replace(hour=0, minute=0, second=0) + timedelta(days=1)
        self.clock.advance(minutes=-30)
        schedule = self.scheduler.reschedule(self.ca, schedule["id"], schedule["revision"], interval_minutes=60)
        actions = []
        for _ in range(5):
            self.clock.advance(minutes=60)
            for record in self.scheduler.tick():
                actions.append(record["acao"])
                job = self.queue.claim("w", tenant_id=self.a["id"])
                if job:
                    self.queue.complete(job["id"], "w", job["token"], {})
        self.assertEqual(actions[:2], ["executada", "executada"])
        self.assertEqual(set(actions[2:]), {"orcamento_esgotado"})
        self.assertEqual(len(self.jobs()), 2)
        alerts = [n for n in self.notifier.list(self.ca) if n["topic"] == "rotina-orcamento"]
        self.assertEqual((len(alerts), alerts[0]["count"]), (1, len(actions) - 2))  # repeated skips merge into one alert
        self.clock.advance(days=1)
        self.assertEqual(self.scheduler.tick()[0]["acao"], "executada")

    def test_two_instances_ticking_at_once_run_an_occurrence_exactly_once(self):
        schedule = self.create(interval_minutes=10)
        other = ScheduleService(SqliteRepository(self.repo.path), self.access, JobQueue(SqliteRepository(self.repo.path), self.access,
                                clock=lambda: int(self.clock.now.timestamp() * 1000)), self.notifier, clock=self.clock)
        self.clock.advance(minutes=10)
        results, barrier = [], threading.Barrier(2)

        def run(service):
            barrier.wait()
            results.append(service.tick())

        threads = [threading.Thread(target=run, args=(s,)) for s in (self.scheduler, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        flat = [r["acao"] for batch in results for r in batch]
        self.assertEqual(flat.count("executada"), 1, flat)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.get(schedule)["runs_today"], 1)

    def test_pause_resume_disable_and_automatic_pause_are_visible(self):
        schedule = self.create(interval_minutes=10)
        paused = self.scheduler.set_state(self.ca, schedule["id"], schedule["revision"], "pausado", "manutencao")
        self.assertEqual((paused["state"], paused["state_reason"]), ("pausado", "manutencao"))
        self.clock.advance(hours=2)
        self.assertEqual(self.scheduler.tick(), [])
        with self.assertRaises(RevisionConflict):
            self.scheduler.set_state(self.ca, schedule["id"], schedule["revision"], "ativo", "retomar")  # stale revision
        resumed = self.scheduler.set_state(self.ca, schedule["id"], paused["revision"], "ativo", "retomar")
        self.assertGreater(resumed["next_run_at"], self.clock.now.strftime("%Y-%m-%dT%H:%M:%SZ"))  # the pause is not replayed
        self.assertEqual(self.scheduler.tick(), [])
        # repeated failed jobs pause the schedule and tell the operator
        for _ in range(MAX_CONSECUTIVE_FAILURES + 1):
            self.clock.advance(minutes=10)
            records = self.scheduler.tick()
            job = self.queue.claim("w", tenant_id=self.a["id"])
            if job:
                self.queue.fail(job["id"], "w", job["token"], "boom", retry=False)
            if not records:
                break
        stored = self.get(schedule)
        self.assertEqual(stored["state"], "pausado")
        self.assertIn("falhas seguidas", stored["state_reason"])
        self.assertIn("rotina-pausada", [n["topic"] for n in self.notifier.list(self.ca)])
        disabled = self.scheduler.set_state(self.ca, schedule["id"], stored["revision"], "desativado", "nao e mais necessaria")
        self.clock.advance(days=1)
        self.assertEqual((disabled["state"], self.scheduler.tick()), ("desativado", []))

    def test_rescheduling_changes_the_next_run_without_replaying_the_past(self):
        schedule = self.create(frequency="diaria", time_of_day="08:00", interval_minutes=None, timezone_name="UTC")
        moved = self.scheduler.reschedule(self.ca, schedule["id"], schedule["revision"], timezone="UTC-03:00", time_of_day="09:00")
        self.assertEqual((moved["timezone"], moved["time_of_day"]), ("UTC-03:00", "09:00"))
        self.assertGreater(moved["next_run_at"], self.clock.now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        with self.assertRaises(ScheduleRejected):
            self.scheduler.reschedule(self.ca, schedule["id"], moved["revision"], job_kind="agent_run")
        with self.assertRaises(ScheduleRejected):
            self.scheduler.reschedule(self.ca, schedule["id"], moved["revision"], timezone="Marte/Olympus")

    def test_a_scheduled_job_never_runs_by_itself_and_carries_no_authority(self):
        self.create()
        self.clock.advance(hours=1)
        self.scheduler.tick()
        job = self.jobs()[0]
        self.assertEqual(job["state"], "pendente")  # only a worker with a handler for the kind can act on it
        self.assertEqual(set(job["payload"]), {"hostname", "schedule_id", "occurrence"})  # no credentials, grants or approvals
        with self.repo.transaction(self.a["id"]) as session:
            self.assertEqual(session.list("approval_request", limit=10), [])


class SlaTests(SchedulerCase):
    def setUp(self):
        super().setUp()
        self.dep = self.team.create(self.ca, "department", name="Atendimento", scope="x", status="ativo")
        self.sup = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Suporte", **PROFILE)
        self.fin = self.agents.create_agent(self.ca, department_id=self.dep["id"], name="Financeiro", **PROFILE)
        for source, target in ((self.sup, self.fin), (self.fin, self.sup)):
            current = self.agents.get_agent(self.ca, source["id"])
            self.agents.update_agent(self.ca, source["id"], current["revision"], handoff_target_ids=[target["id"]])
        self.team.create(self.ca, "access_grant", principal_id=self.sup["id"], resource_kind="case", resource_id=None,
                         actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)

    def owned_case(self):
        case = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Caso", origin="email")
        case = self.cases.transition(self.ca, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], self.sup["id"])
        return self.cases.transition(self.ca, case["id"], case["revision"], "em_execucao")

    def test_stale_handoffs_approvals_and_unattended_cases_escalate_in_limited_levels(self):
        case = self.owned_case()
        unattended = self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Sem dono", origin="email")
        sup_ctx = self.access.agent_context(self.op, self.a["id"], self.agents.start_run(
            self.ca, self.sup["id"], budget={"max_steps": 2, "token_budget": 100, "deadline_seconds": 60})["id"])
        handoff, _ = self.handoffs.delegate(sup_ctx, case["id"], self.fin["id"], reason="x", objective="y", expected_result="z", summary="w", ttl_seconds=86400)
        approval, _ = self.approvals.request(self.ca, case["id"], "enviar_email", resource="con_x", recipients=["a@b.com"], amount_cents=1, currency="brl")
        self.assertEqual(self.sla.sweep(self.a["id"]), [])  # everything is fresh
        self.clock.advance(minutes=31)
        first = {e["assunto"]: e["nivel"] for e in self.sla.sweep(self.a["id"])}
        self.assertEqual(first, {"encaminhamento": 1})
        self.assertEqual(self.sla.sweep(self.a["id"]), [])  # idempotent: no duplicate escalation
        self.clock.advance(minutes=100)  # 131 min: response SLA (120) for the unattended case, handoff level 2 (60)
        second = {(e["assunto"], e["nivel"]) for e in self.sla.sweep(self.a["id"])}
        self.assertEqual(second, {("encaminhamento", 2), ("demanda", 1)})
        self.clock.advance(minutes=120)  # 251 min: approval SLA (240)
        self.assertIn(("aprovacao", 1), {(e["assunto"], e["nivel"]) for e in self.sla.sweep(self.a["id"])})
        self.clock.advance(days=3)
        for _ in range(4):
            self.sla.sweep(self.a["id"])
        levels = self.sla.sweep(self.a["id"])
        self.assertEqual(levels, [])  # max two levels, then silence: nothing is redistributed forever
        with self.repo.transaction(self.a["id"]) as session:
            counts = session.conn.execute("SELECT subject_id, COUNT(*) FROM sla_escalations GROUP BY subject_id").fetchall()
        self.assertTrue(all(count <= 2 for _, count in counts))
        topics = {n["topic"] for n in self.notifier.list(self.ca)}
        self.assertTrue({"sla-aceite", "sla-aprovacao", "sla-resposta"} <= topics)
        self.assertTrue(handoff and approval and unattended)
        self.assertEqual(self.sla.sweep(self.b["id"]), [])  # another company has none of this

    def test_cases_with_an_owner_and_decided_items_are_not_escalated(self):
        case = self.owned_case()
        approval, _ = self.approvals.request(self.ca, case["id"], "enviar_email", resource="con_x", recipients=["a@b.com"], amount_cents=1, currency="brl")
        self.approvals.deny(self.ca, approval["id"], "nao")
        self.clock.advance(days=2)
        self.assertEqual(self.sla.sweep(self.a["id"]), [])


class NotificationTests(SchedulerCase):
    def test_repeats_inside_the_cool_down_merge_and_a_new_one_appears_afterwards(self):
        first, created = self.notifier.notify(self.a["id"], topic="teste", key="k1", message="algo aconteceu")
        again, merged = self.notifier.notify(self.a["id"], topic="teste", key="k1", message="algo aconteceu")
        self.assertEqual((created, merged, again["count"]), (True, False, 2))
        self.clock.advance(hours=7)
        later, created = self.notifier.notify(self.a["id"], topic="teste", key="k1", message="de novo")
        self.assertTrue(created)
        self.assertNotEqual(first["id"], later["id"])
        self.notifier.mark_read(self.ca, later["id"])
        self.assertTrue(self.notifier.notify(self.a["id"], topic="teste", key="k1", message="depois de lida")[1])
        self.assertEqual(len(self.notifier.list(self.ca, state="nova")), 2)
        self.assertEqual(self.notifier.list(self.cb), [])
        with self.assertRaises(NotFound):
            self.notifier.mark_read(self.cb, first["id"])

    def test_messages_are_cleaned_and_bounded(self):
        saved, _ = self.notifier.notify(self.a["id"], topic="teste", key="k2", message="<think>raciocinio</think>texto " + "x" * 3000)
        self.assertNotIn("raciocinio", saved["message"])
        self.assertLessEqual(len(saved["message"]), 1000)


class HandlerTests(SchedulerCase, unittest.IsolatedAsyncioTestCase):
    async def test_asset_check_notifies_only_when_the_situation_changes(self):
        answers = iter([{"categoria": "site_ok", "certeza": "confirmado", "resumo": "ok", "proximo_passo": "-"},
                        {"categoria": "dominio_expirado", "certeza": "confirmado", "resumo": "venceu", "proximo_passo": "renovar"},
                        {"categoria": "dominio_expirado", "certeza": "confirmado", "resumo": "venceu", "proximo_passo": "renovar"},
                        {"categoria": "site_ok", "certeza": "confirmado", "resumo": "voltou", "proximo_passo": "-"}])

        class Fake:
            def diagnose(self, host):
                return next(answers)

        handler = asset_check_handler(lambda tenant: Fake(), self.notifier)
        job = {"tenant_id": self.a["id"], "payload": {"hostname": "exemplo.com.br"}}
        results = [await handler(job, None) for _ in range(4)]
        self.assertEqual([r.result["categoria"] for r in results], ["site_ok", "dominio_expirado", "dominio_expirado", "site_ok"])
        notes = self.notifier.list(self.ca)
        self.assertEqual(sorted((n["severity"], n["dedupe_key"].rsplit(":", 1)[-1]) for n in notes), [("acao", "dominio_expirado"), ("info", "site_ok")])
        self.assertEqual([n["count"] for n in notes if "dominio_expirado" in n["dedupe_key"]], [1])  # the repeat did not even notify

    async def test_failures_become_failed_jobs_not_exceptions(self):
        class Broken:
            def diagnose(self, host):
                raise RuntimeError("rede caiu")

        handler = asset_check_handler(lambda tenant: Broken(), self.notifier)
        result = await handler({"tenant_id": self.a["id"], "payload": {"hostname": "x.com.br"}}, None)
        self.assertEqual(result.code, "diagnostico_indisponivel")

        class Ingest:
            def __init__(self, report):
                self.report = report

            def ingest(self, ctx, connection_id, folder):
                if isinstance(self.report, Exception):
                    raise self.report
                return self.report

        job = {"tenant_id": self.a["id"], "payload": {"connection_id": "c", "folder": "INBOX"}}
        ok = await email_ingest_handler(Ingest({"processadas": 2, "por_classificacao": {"nova_demanda": 2}, "erro": None, "parou_em": None}), lambda t: self.ca)(job, None)
        self.assertEqual(ok.result["processadas"], 2)
        for bad in ({"processadas": 0, "erro": "autenticacao_recusada", "parou_em": None}, {"processadas": 1, "erro": None, "parou_em": {"uid": 3}}, RuntimeError("x")):
            result = await email_ingest_handler(Ingest(bad), lambda t: self.ca)(job, None)
            self.assertTrue(result.code)


if __name__ == "__main__":
    unittest.main()
