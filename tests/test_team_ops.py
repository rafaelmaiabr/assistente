import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.approvals import ApprovalService
from team.audit import AuditService, sanitize_text
from team.backup import BackupInvalid, BackupService
from team.cases import CaseService
from team.connections import ConnectionService, LauncherRegistry
from team.db import connect
from team.ids import new_id
from team.jobs import JobQueue
from team.repository import SqliteRepository
from team.retention import RetentionRejected, RetentionService
from team.service import TeamService
from team.vault import MemoryVault

CANARY_KEY = "sk-CANARIO0123456789abcdefghijkl"
CANARY_TEXT = "SEGREDO-DE-CORPO-XYZ"
CANARY_MAIL = "maria.canario@exemplo.com"
CANARY_CPF = "123.456.789-09"
PROFILE = dict(name="Financeiro", goals=["Cobrar"], personality={"tone": "claro", "initiative": "proativo"},
               scope="Cobrancas", out_of_scope="Outros", model_policy_ref="local-primeiro")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class OpsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "team-data"
        self.base.mkdir()
        self.path = self.base / "team.sqlite3"
        self.repo = SqliteRepository(self.path)
        self.repo.migrate()
        self.clock = Clock()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.approvals = ApprovalService(self.repo, self.access, self.cases, clock=self.clock)
        self.queue = JobQueue(self.repo, self.access)
        self.audit = AuditService(self.repo, self.access, self.base / "audit.json", clock=self.clock)
        self.retention = RetentionService(self.repo, self.access, self.base, clock=self.clock)
        self.backup = BackupService(self.repo, self.access.operator_id, self.base, clock=self.clock)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Financeiro", scope="x", status="ativo")
        self.agent = self.agents.create_agent(self.ca, department_id=self.dep["id"], **PROFILE)
        self.case = self.working_case(self.ca, self.dep, f"Cobrar Maria ({CANARY_MAIL}, CPF {CANARY_CPF})")
        self.run = self.agents.start_run(self.ca, self.agent["id"], case_id=self.case["id"],
                                         budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 60})
        self.fill(self.case, self.run)

    def working_case(self, ctx, dep, objective):
        case = self.cases.open_case(ctx, department_id=dep["id"], objective=objective, customer_ref="cli-1")
        case = self.cases.transition(ctx, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(ctx, case["id"], case["revision"], self.agent["id"])
        return self.cases.transition(ctx, case["id"], case["revision"], "em_execucao", reason=f"cliente {CANARY_MAIL} pediu")

    def fill(self, case, run):
        """A case with content, evidence, an approval, a job, tool decisions and token usage."""
        ctx = self.ca
        self.cases.add_note(ctx, case["id"], f"Nota privada {CANARY_TEXT}")
        self.cases.add_message(ctx, case["id"], "entrada", "email", f"Mensagem do cliente {CANARY_TEXT} {CANARY_MAIL}")
        self.cases.add_evidence(ctx, case["id"], source=f"fatura de {CANARY_MAIL}", summary=f"resumo {CANARY_TEXT}",
                                evidence_type="documento", reliability="declarada")
        approval, _ = self.approvals.request(ctx, case["id"], action="enviar_email", resource="con_x", recipients=[CANARY_MAIL],
                                             amount_cents=15000, currency="BRL", params={"assunto": CANARY_TEXT})
        self.approvals.approve(ctx, approval["id"], approval["payload_hash"])
        with self.repo.transaction(self.a["id"]) as session:
            session.record_tool_decision({"run_id": run["id"], "agent_id": self.agent["id"], "agent_version": 1, "tool": "erp.listar",
                                          "operation": "read", "decision": "allowed", "reason": f"ok {CANARY_MAIL} {CANARY_KEY}", "policy_version": "p1",
                                          "result_bytes": 120, "duration_ms": 40})
            session.record_tool_decision({"run_id": run["id"], "agent_id": self.agent["id"], "agent_version": 1, "tool": "erp.criar",
                                          "operation": "write", "decision": "denied", "reason": "sem_aprovacao", "policy_version": "p1",
                                          "duration_ms": 2})
        job, _ = self.queue.enqueue(self.a["id"], "agent_run", {"mensagem": CANARY_TEXT}, idempotency_key=f"k-{case['id']}",
                                    case_id=case["id"], run_id=run["id"])
        claimed = self.queue.claim("w1", tenant_id=self.a["id"])
        self.queue.complete(claimed["id"], "w1", claimed["lease_token"] if "lease_token" in claimed else claimed.get("token"),
                            {"run_id": run["id"], "tokens": 321, "steps": 3})
        return approval

    def count(self, table, tenant=None):
        conn = connect(self.path)
        self.addCleanup(conn.close)
        return conn.execute(f"SELECT COUNT(*) FROM {table}" + (" WHERE tenant_id = ?" if tenant else ""), (tenant,) if tenant else ()).fetchone()[0]


class TrailTests(OpsCase):
    def test_the_case_trail_lists_actors_policies_decisions_costs_and_evidence_references(self):
        trail = self.audit.trail(self.ca, case_id=self.case["id"])
        by_kind = {}
        for item in trail["itens"]:
            by_kind.setdefault(item["tipo"], []).append(item)
        self.assertEqual({"evento", "execucao", "ferramenta", "job", "aprovacao", "evidencia"}, set(by_kind))
        tools = {i["dados"]["ferramenta"]: i["dados"] for i in by_kind["ferramenta"]}
        self.assertEqual((tools["erp.listar"]["decisao"], tools["erp.listar"]["politica"], tools["erp.listar"]["duracao_ms"]), ("allowed", "p1", 40))
        self.assertEqual(tools["erp.criar"]["decisao"], "denied")
        self.assertEqual(by_kind["job"][0]["dados"]["tokens"], 321)
        evidence = by_kind["evidencia"][0]
        self.assertTrue(evidence["id"].startswith("evd_") or evidence["id"])
        self.assertEqual(by_kind["aprovacao"][0]["dados"]["estado"], "aprovada")
        self.assertEqual(by_kind["aprovacao"][0]["dados"]["valor_centavos"], 15000)
        self.assertIn(self.access.operator_id, {i["ator"] for i in trail["itens"]})
        self.assertEqual([i["em"] for i in trail["itens"]], sorted(i["em"] for i in trail["itens"]))

    def test_run_trail_is_scoped_to_the_run(self):
        trail = self.audit.trail(self.ca, run_id=self.run["id"])
        self.assertEqual({i["tipo"] for i in trail["itens"]}, {"execucao", "ferramenta", "job"})
        with self.assertRaises(NotFound):
            self.audit.trail(self.cb, run_id=self.run["id"])
        with self.assertRaises(NotFound):
            self.audit.trail(self.cb, case_id=self.case["id"])
        with self.assertRaises(ValueError):
            self.audit.trail(self.ca)
        with self.assertRaises(ValueError):
            self.audit.trail(self.ca, case_id=self.case["id"], run_id=self.run["id"])

    def test_canaries_never_reach_the_default_trail_metrics_or_exports(self):
        trail = json.dumps(self.audit.trail(self.ca, case_id=self.case["id"]), ensure_ascii=False)
        metrics = json.dumps(self.audit.metrics(self.ca), ensure_ascii=False)
        for canary in (CANARY_KEY, CANARY_TEXT, CANARY_MAIL, CANARY_CPF, "Cobrar Maria", "Nota privada"):
            self.assertNotIn(canary, trail, canary)
            self.assertNotIn(canary, metrics, canary)
        self.assertIn("[email]", trail)  # free-text reasons are masked, not dropped

    def test_raw_payload_is_off_by_default_and_still_masked_when_enabled(self):
        self.assertFalse(self.audit.raw_payload_enabled())
        self.assertFalse(self.audit.trail(self.ca, case_id=self.case["id"], include_raw=True)["payload_bruto"])
        with self.assertRaises(PermissionDenied):
            self.audit.set_raw_payload(self.ca, True)  # a company context is not the trusted operator
        self.audit.set_raw_payload(self.op, True)
        trail = self.audit.trail(self.ca, case_id=self.case["id"], include_raw=True)
        self.assertTrue(trail["payload_bruto"])
        self.assertTrue(any("payload_bruto" in i["dados"] for i in trail["itens"] if i["tipo"] == "evento"))
        text = json.dumps(trail, ensure_ascii=False)
        for canary in (CANARY_MAIL, CANARY_KEY, CANARY_CPF):
            self.assertNotIn(canary, text)
        self.assertEqual(self.audit.trail(self.ca, case_id=self.case["id"])["payload_bruto"], False)  # per call opt-in
        self.audit.set_raw_payload(self.op, False)
        self.assertFalse(self.audit.trail(self.ca, case_id=self.case["id"], include_raw=True)["payload_bruto"])

    def test_only_the_operator_reads_the_trail(self):
        agent_ctx = self.access.agent_context(self.op, self.a["id"], self.run["id"])
        for call in (lambda: self.audit.trail(agent_ctx, case_id=self.case["id"]), lambda: self.audit.metrics(agent_ctx)):
            with self.assertRaises(PermissionDenied):
                call()

    def test_sanitize_masks_secrets_and_personal_data(self):
        text = sanitize_text(f"{CANARY_MAIL} {CANARY_CPF} 12.345.678/0001-95 (11) 91234-5678 4111 1111 1111 1111 "
                             f"Bearer abcdef1234567890 https://x.test/cb?token=abc123&y=1 {CANARY_KEY} {'A' * 40}", limit=500)
        for leaked in (CANARY_MAIL, CANARY_CPF, "12.345.678/0001-95", "91234-5678", "4111 1111", "abcdef1234567890", "abc123", CANARY_KEY, "A" * 40):
            self.assertNotIn(leaked, text)
        self.assertLessEqual(len(sanitize_text("x" * 1000)), 200)


class MetricsTests(OpsCase):
    def test_queue_failures_human_wait_and_tokens(self):
        self.queue.enqueue(self.a["id"], "agent_run", {}, idempotency_key="k2", case_id=self.case["id"], max_attempts=2)
        job = self.queue.claim("w2", tenant_id=self.a["id"])
        self.queue.fail(job["id"], "w2", job.get("lease_token") or job.get("token"), "falha_x", retry=True)
        approval, _ = self.approvals.request(self.ca, self.case["id"], action="enviar_email", resource="con_y", recipients=["z@exemplo.com"],
                                             amount_cents=1, currency="BRL", params={"assunto": "outra"})
        self.clock.now += timedelta(hours=2)
        metrics = self.audit.metrics(self.ca)
        self.assertEqual(metrics["fila"]["por_estado"]["concluido"], 1)
        self.assertGreaterEqual(metrics["fila"]["tentativas_extras"], 0)
        self.assertEqual(metrics["falhas"]["ferramentas_negadas"], 1)
        self.assertEqual(metrics["execucoes"]["decisoes_de_ferramenta"], {"allowed": 1, "denied": 1})
        self.assertEqual(metrics["espera_humana"]["aprovacoes_pendentes"], 1)
        self.assertGreaterEqual(metrics["espera_humana"]["mais_antiga_pendente_segundos"], 0)
        self.assertEqual(metrics["tokens"]["total"], 321)
        self.assertIsNone(metrics["tokens"]["custo"])
        self.assertIn("nenhum custo", metrics["tokens"]["custo_observacao"])
        self.assertEqual(self.audit.metrics(self.cb)["tokens"]["total"], 0)  # another company's numbers are separate


class ExportErasureTests(OpsCase):
    def test_export_needs_scope_acknowledgement_and_never_carries_credentials(self):
        with self.assertRaises(RetentionRejected):
            self.retention.export_case(self.ca, self.case["id"])
        with self.assertRaises(NotFound):
            self.retention.export_case(self.cb, self.case["id"], acknowledge_sensitive=True)
        result = self.retention.export_case(self.ca, self.case["id"], acknowledge_sensitive=True)
        content = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("dados pessoais", result["aviso"])
        self.assertIn(CANARY_TEXT, content)  # the case content is the point of the export...
        self.assertNotIn(CANARY_KEY, content)  # ...but credentials are redacted
        self.assertEqual(result["itens"]["entradas"], 3)  # note, message and the approval record
        agent_ctx = self.access.agent_context(self.op, self.a["id"], self.run["id"])
        with self.assertRaises(PermissionDenied):
            self.retention.export_case(agent_ctx, self.case["id"], acknowledge_sensitive=True)

    def test_erasing_a_case_removes_it_everywhere_and_leaves_only_a_content_free_record(self):
        other = self.working_case(self.ca, self.dep, "Outra demanda preservada")
        self.cases.add_note(self.ca, other["id"], "nota que deve permanecer")
        export = self.retention.export_case(self.ca, self.case["id"], acknowledge_sensitive=True)
        with self.assertRaises(RetentionRejected):
            self.retention.erase_case(self.ca, self.case["id"], "errado")
        with self.assertRaises(NotFound):
            self.retention.erase_case(self.cb, self.case["id"], self.case["id"])
        result = self.retention.erase_case(self.ca, self.case["id"], self.case["id"])
        self.assertEqual((result["apagado"]["demanda"], result["apagado"]["entradas"], result["apagado"]["execucoes"], result["apagado"]["jobs"]), (1, 3, 1, 1))
        self.assertEqual(result["exportacoes_removidas"], 1)
        self.assertFalse(Path(export["path"]).exists())
        conn = connect(self.path)
        self.addCleanup(conn.close)
        for table in ("evidences", "approval_requests", "tool_decisions", "jobs", "agent_runs", "outbox", "events"):
            left = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ? AND " + (
                "run_id IN (SELECT id FROM agent_runs)" if table == "tool_decisions" else "1 = 1"), (self.a["id"],)).fetchone()[0]
            if table in ("events", "outbox"):  # the surviving case keeps its own events
                continue
            self.assertEqual(left, 0, table)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM case_entries WHERE case_id = ?", (self.case["id"],)).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE case_id = ?", (self.case["id"],)).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox WHERE event_seq IN (SELECT seq FROM events WHERE case_id = ?)",
                                      (self.case["id"],)).fetchone()[0], 0)
        remaining = conn.execute("SELECT doc FROM cases WHERE tenant_id = ?", (self.a["id"],)).fetchall()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(self.cases.get_case(self.ca, other["id"])["objective"], "Outra demanda preservada")
        self.assertTrue(any(item.get("body") == "nota que deve permanecer" or "nota que deve permanecer" in json.dumps(item) for item in self.cases.timeline(self.ca, other["id"])))
        with self.assertRaises(sqlite3.DatabaseError):  # the erasure record itself is append-only
            conn.execute("DELETE FROM erasure_log")
        log = self.retention.erasure_log(self.ca)
        self.assertEqual([entry["case_id"] for entry in log], [self.case["id"]])
        self.assertNotIn(CANARY_TEXT, json.dumps(log))
        with self.assertRaises(NotFound):
            self.cases.get_case(self.ca, self.case["id"])
        self.assertEqual(self.retention.erasure_log(self.cb), [])
        # the erased text is gone from every live table (the surviving case never contained it)
        for table, column in (("case_entries", "doc"), ("events", "doc"), ("evidences", "doc"), ("jobs", "payload"), ("cases", "doc")):
            hits = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?", (f"%{CANARY_TEXT}%",)).fetchone()[0]
            self.assertEqual(hits, 0, table)

    def test_append_only_tables_still_refuse_deletes_outside_a_controlled_erasure(self):
        conn = connect(self.path)
        self.addCleanup(conn.close)
        for sql in ("DELETE FROM case_entries", "DELETE FROM evidences", "UPDATE case_entries SET tenant_id = tenant_id",
                    ):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                conn.execute(sql)
        conn.execute("INSERT INTO erasure_guard (token) VALUES (1)")  # the guard alone does not allow updates
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE case_entries SET tenant_id = tenant_id")
        conn.execute("DELETE FROM erasure_guard")
        self.assertEqual(self.count("erasure_guard"), 0)

    def test_a_failed_erasure_changes_nothing(self):
        conn = connect(self.path)
        self.addCleanup(conn.close)
        # force the last step to fail: everything deleted before it must roll back, and the guard must not linger
        conn.execute("CREATE TRIGGER bloqueio_teste BEFORE DELETE ON cases BEGIN SELECT RAISE(ABORT, 'bloqueado'); END")
        before = {t: self.count(t, self.a["id"]) for t in ("case_entries", "events", "jobs", "agent_runs", "tool_decisions", "cases")}
        with self.assertRaises(RetentionRejected):
            self.retention.erase_case(self.ca, self.case["id"], self.case["id"])
        self.assertEqual({t: self.count(t, self.a["id"]) for t in before}, before)
        self.assertEqual(self.count("erasure_guard"), 0)
        self.assertEqual(self.retention.erasure_log(self.ca), [])
        conn.execute("DROP TRIGGER bloqueio_teste")
        self.retention.erase_case(self.ca, self.case["id"], self.case["id"])  # and it works once the obstacle is gone
        self.assertEqual(self.count("cases", self.a["id"]), 0)

    def test_retention_policy_erases_old_closed_cases_and_old_exports(self):
        self.clock.now = datetime.now(timezone.utc)  # case timestamps come from the real clock
        old = self.cases.get_case(self.ca, self.case["id"])
        old = self.cases.transition(self.ca, old["id"], old["revision"], "cancelada", reason="encerrada no teste")
        recent = self.working_case(self.ca, self.dep, "Recente e aberta")
        export = self.retention.export_case(self.ca, recent["id"], acknowledge_sensitive=True)
        with self.assertRaises(RetentionRejected):
            self.retention.set_policy(self.ca, closed_case_days=0)
        with self.assertRaises(RetentionRejected):
            self.retention.set_policy(self.ca, nao_existe=3)
        self.assertEqual(self.retention.policy()["closed_case_days"], None)
        self.assertEqual(self.retention.apply_policy(self.ca)["demandas_apagadas"], [])  # default: nothing is erased automatically
        self.retention.set_policy(self.ca, closed_case_days=30, export_days=10)
        self.clock.now += timedelta(days=9)
        self.assertEqual(self.retention.apply_policy(self.ca)["demandas_apagadas"], [])
        self.clock.now += timedelta(days=22)
        report = self.retention.apply_policy(self.ca)
        self.assertEqual(report["demandas_apagadas"], [old["id"]])
        self.assertEqual(report["exportacoes_removidas"], 1)
        self.assertFalse(Path(export["path"]).exists())
        self.assertEqual(self.cases.get_case(self.ca, recent["id"])["state"], "em_execucao")  # open cases are never touched


class BackupTests(OpsCase):
    def test_backup_is_consistent_checksummed_and_leaves_secrets_out(self):
        result = self.backup.create(self.op)
        manifest = json.loads((Path(result["pasta"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["segredos"], manifest["criptografado"], manifest["versao_do_esquema"] >= 15), ("nao incluidos", False, True))
        self.assertEqual(manifest["linhas"]["cases"], 1)
        self.assertIn("NAO inclui senhas", manifest["aviso"])
        self.assertEqual(self.backup.verify(Path(result["pasta"]))["sha256"], manifest["sha256"])
        self.assertEqual(len(self.backup.list_backups(self.op)), 1)
        with self.assertRaises(PermissionDenied):
            self.backup.create(self.ca)

    def test_tampered_corrupt_foreign_and_newer_backups_are_refused(self):
        folder = Path(self.backup.create(self.op)["pasta"])
        (folder / "team.sqlite3").write_bytes((folder / "team.sqlite3").read_bytes() + b"x")
        with self.assertRaisesRegex(BackupInvalid, "soma de verificacao"):
            self.backup.verify(folder)
        empty = Path(self.temp.name) / "vazio"
        empty.mkdir()
        with self.assertRaisesRegex(BackupInvalid, "manifesto"):
            self.backup.verify(empty)
        self.clock.now += timedelta(seconds=5)
        fresh = Path(self.backup.create(self.op)["pasta"])
        manifest = json.loads((fresh / "manifest.json").read_text(encoding="utf-8"))
        manifest["versao_do_esquema"] = 999
        (fresh / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(BackupInvalid, "mais nova"):
            self.backup.verify(fresh)
        self.clock.now += timedelta(seconds=5)
        junk = Path(self.backup.create(self.op)["pasta"])
        data = b"nao e um banco" * 100
        (junk / "team.sqlite3").write_bytes(data)
        manifest = json.loads((junk / "manifest.json").read_text(encoding="utf-8"))
        import hashlib
        manifest["sha256"] = hashlib.sha256(data).hexdigest()
        (junk / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(BackupInvalid, "banco valido|corrompido"):
            self.backup.verify(junk)

    def test_restore_brings_back_state_and_reconciles_jobs_and_messages_without_resending(self):
        # a job and a message are in flight when the backup is taken
        running, _ = self.queue.enqueue(self.a["id"], "agent_run", {}, idempotency_key="voo", case_id=self.case["id"], max_attempts=3)
        job = self.queue.claim("w9", tenant_id=self.a["id"])
        self.assertEqual(job["id"], running["id"])
        with self.repo.transaction(self.a["id"]) as session:
            conn = session.conn
            conn.execute("UPDATE jobs SET checkpoint = ? WHERE id = ?", (json.dumps({"effects": {"enviar": {"state": "iniciado"}}}), running["id"]))
        plain, _ = self.queue.enqueue(self.a["id"], "agent_run", {}, idempotency_key="voo2", max_attempts=3)
        job2 = self.queue.claim("w9", tenant_id=self.a["id"])
        folder = Path(self.backup.create(self.op)["pasta"])
        self.assertEqual(json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["em_andamento"]["jobs_em_execucao"], 2)
        # later the live database changes (new case), then it is lost and replaced by the backup
        extra = self.working_case(self.ca, self.dep, "Criada depois do backup")
        with self.assertRaises(BackupInvalid):
            self.backup.restore(self.op, folder)  # refuses to replace a live database without confirmation
        report = self.backup.restore(self.op, folder, overwrite=True)
        self.assertEqual(report["jobs_apos_restauracao"], {"reconciliar": 1, "pendente": 1})
        self.assertIn("nada foi reenviado", report["proximo_passo"])
        self.assertTrue(Path(report["banco_anterior"]).exists())
        fresh = SqliteRepository(self.path)
        restored_cases = CaseService(fresh, self.access)
        with self.assertRaises(NotFound):
            restored_cases.get_case(self.ca, extra["id"])
        self.assertEqual(restored_cases.get_case(self.ca, self.case["id"])["state"], "em_execucao")
        queue = JobQueue(fresh, self.access)
        states = {j["id"]: j["state"] for j in queue.list_jobs(self.a["id"])}
        self.assertEqual((states[running["id"]], states[plain["id"]]), ("reconciliar", "pendente"))
        self.assertEqual([j["id"] for j in queue.needs_attention(self.a["id"])], [running["id"]])
        self.assertTrue(job2)

    def test_messages_that_were_sending_become_unknown_for_an_operator_decision(self):
        vault = MemoryVault()
        account = ConnectionService(self.repo, self.access, vault, LauncherRegistry(self.base / "l.json", self.access.operator_id)) \
            .create_connection(self.ca, label="Caixa", provider="imap", connection_kind="imap")
        now = "2026-06-01T12:00:00Z"
        with self.repo.transaction(self.a["id"]) as session:
            message = {"kind": "outbound_message", "schema_version": 1, "id": new_id("outbound_message"), "tenant_id": self.a["id"],
                       "revision": 1, "created_at": now, "updated_at": now, "case_id": self.case["id"], "channel": "email",
                       "account_id": account["id"], "to": ["x@exemplo.com"], "subject": "s", "body": "b", "state": "enviando", "dedup_key": "dk1",
                       "owner_agent_id": self.agent["id"], "owner_version": 1, "draft_hash": "0" * 64, "approval_id": None,
                       "provider_id": None, "sent_at": None, "attempts": 1, "last_error": None, "created_by": self.op.principal_id}
            session.conn.execute(
                "INSERT INTO outbound_messages (id, tenant_id, revision, created_at, updated_at, case_id, account_id, owner_agent_id, "
                "approval_id, state, dedup_key, provider_id, doc) VALUES (?, ?, 1, ?, ?, ?, ?, ?, NULL, 'enviando', 'dk1', NULL, ?)",
                (message["id"], self.a["id"], now, now, self.case["id"], account["id"], self.agent["id"], json.dumps(message)))
        folder = Path(self.backup.create(self.op)["pasta"])
        report = self.backup.restore(self.op, folder, overwrite=True)
        self.assertEqual(report["mensagens_para_revisao"], 1)
        with SqliteRepository(self.path).transaction(self.a["id"]) as session:
            restored = session.get("outbound_message", message["id"])
        self.assertEqual((restored["state"], restored["last_error"]), ("desconhecido", "restaurado_do_backup"))

    def test_restore_into_a_fresh_location_and_pruning(self):
        folder = Path(self.backup.create(self.op)["pasta"])
        elsewhere = Path(self.temp.name) / "novo" / "team.sqlite3"
        service = BackupService(SqliteRepository(elsewhere), self.access.operator_id, elsewhere.parent, clock=self.clock)
        report = service.restore(self.op, folder)  # no live database: no confirmation needed
        self.assertIsNone(report["banco_anterior"])
        self.assertEqual(CaseService(SqliteRepository(elsewhere), self.access).get_case(self.ca, self.case["id"])["id"], self.case["id"])
        for _ in range(2):
            self.clock.now += timedelta(days=20)
            self.backup.create(self.op)
        self.assertEqual(len(self.backup.list_backups(self.op)), 3)
        removed = self.backup.prune(self.op, 30)
        self.assertEqual(len(removed), 1)
        self.assertEqual(len(self.backup.list_backups(self.op)), 2)
        self.clock.now += timedelta(days=400)
        self.backup.prune(self.op, 30)
        self.assertEqual(len(self.backup.list_backups(self.op)), 1)  # the newest is always kept
        with self.assertRaises(BackupInvalid):
            self.backup.prune(self.op, 0)


if __name__ == "__main__":
    unittest.main()
