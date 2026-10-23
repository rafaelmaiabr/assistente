import tempfile
import unittest
from pathlib import Path

from evals.world import World
from team.cases import CaseService
from team.access import AccessService
from team.db import connect
from team.pgdb import PostgresRepository
from team.pgmigrate import MigrationMismatch, TargetNotEmpty, fingerprint, table_plan, to_postgres, to_sqlite, verify
from tests.pg_support import PostgresSchema, psycopg, requires_postgres


def populated_world():
    """A company with cases, approvals, messages, jobs, a disabled agent version that a run still points at, and people."""
    w = World()
    case = w.working_case()
    approval = w.request_approval(case)
    w.approvals.approve(w.ca, approval["id"], approval["payload_hash"])
    message = w.approved_message(w.working_case())
    w.dispatch(message)
    w.member("ana@exemplo.invalid", "atendente", [w.dep["id"]])
    w.cases.add_note(w.ca, case["id"], "Nota de teste")
    w.cases.add_evidence(w.ca, case["id"], source="fonte", summary="Prova", evidence_type="recibo", reliability="verificada", content_hash="a" * 64)
    run = w.agents.start_run(w.ca, w.suporte["id"], budget={"max_steps": 2, "token_budget": 10, "deadline_seconds": 5})
    version = w.agents.list_versions(w.ca, w.suporte["id"])[-1]
    w.agents.set_version_status(w.ca, w.suporte["id"], version["agent_version"], "inativo")   # history keeps pointing at it
    from team.jobs import JobQueue
    queue = JobQueue(w.repo, w.access)
    queue.enqueue(w.a["id"], "teste", {"n": 1}, idempotency_key="j1", case_id=case["id"])
    return w, case, run


def counts(conn):
    return {table: fingerprint(conn, table, columns) for table, columns in table_plan()}


@requires_postgres
class MigrationBetweenBackendsTests(unittest.TestCase):
    def setUp(self):
        self.world, self.case, self.run = populated_world()
        self.addCleanup(self.world.close)
        self.schema = PostgresSchema()
        self.addCleanup(self.schema.drop)
        self.pg = PostgresRepository(self.schema.dsn)
        self.sqlite_path = self.world.repo.path

    def test_everything_arrives_intact_and_the_application_reads_it_from_postgres(self):
        report = to_postgres(self.sqlite_path, self.pg)
        self.assertGreater(report["cases"], 0)
        self.assertGreater(report["events"], 0)
        self.assertGreater(report["auth_users"], 0)
        self.assertEqual(verify(self.sqlite_path, self.pg), report)
        # the same services, now on PostgreSQL, see the same company
        access = AccessService(self.pg, self.world.access.operator_id)
        ctx = access.tenant_context(access.local_operator(), self.world.a["id"])
        self.assertEqual(sorted(c["id"] for c in CaseService(self.pg, access).list_cases(ctx)),
                         sorted(c["id"] for c in self.world.cases.list_cases(self.world.ca)))
        with self.pg.transaction(self.world.a["id"]) as session:
            self.assertEqual(session.get("case", self.case["id"]), self.world.cases.get_case(self.world.ca, self.case["id"]))
            self.assertTrue(session.events(self.case["id"]))

    def test_the_way_back_reproduces_the_original_database(self):
        to_postgres(self.sqlite_path, self.pg)
        with tempfile.TemporaryDirectory() as folder:
            back = Path(folder) / "volta.sqlite3"
            to_sqlite(self.pg, back)
            original, copy = connect(self.sqlite_path), connect(back)
            try:
                self.assertEqual(counts(original), counts(copy))
                self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(copy.execute("PRAGMA foreign_key_check").fetchall(), [])
                triggers = {r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                self.assertEqual(triggers, {r[0] for r in original.execute("SELECT name FROM sqlite_master WHERE type='trigger'")})
            finally:
                original.close()
                copy.close()
            with self.assertRaises(TargetNotEmpty):
                to_sqlite(self.pg, back)                                                # never over an existing file

    def test_a_target_with_data_is_refused_and_a_difference_is_detected(self):
        to_postgres(self.sqlite_path, self.pg)
        with self.assertRaises(TargetNotEmpty):
            to_postgres(self.sqlite_path, self.pg)                                      # nothing is merged or overwritten
        with self.pg._transaction() as conn:
            conn.execute("ALTER TABLE cases DISABLE TRIGGER USER")
            conn.execute("UPDATE cases SET doc = jsonb_set(doc, '{objective}', '\"adulterado\"')")
        with self.assertRaises(MigrationMismatch):
            verify(self.sqlite_path, self.pg)

    def test_a_copy_that_fails_midway_leaves_the_target_empty_and_the_guards_in_place(self):
        broken = connect(self.sqlite_path)
        broken.execute("PRAGMA foreign_keys = OFF")
        broken.execute("DROP TRIGGER case_entries_append_only_update")
        broken.execute("UPDATE case_entries SET case_id = 'cas_inexistente'")
        broken.close()
        with self.assertRaises(psycopg.errors.IntegrityError):
            to_postgres(self.sqlite_path, self.pg)
        with self.pg._transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0], 0)    # rolled back as a whole
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0], 0)
            enabled = conn.execute("SELECT COUNT(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                                   "WHERE n.nspname = current_schema() AND NOT t.tgisinternal AND t.tgenabled = 'D'").fetchone()[0]
            self.assertEqual(enabled, 0)                                                        # no trigger left disabled

    def test_after_the_copy_the_guards_work_and_new_rows_continue_the_numbering(self):
        to_postgres(self.sqlite_path, self.pg)
        with self.assertRaises(psycopg.errors.IntegrityError):
            with self.pg._transaction() as conn:
                conn.execute("UPDATE case_entries SET created_at = 'x'")
        with self.pg._transaction() as conn:
            highest = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
        access = AccessService(self.pg, self.world.access.operator_id)
        ctx = access.tenant_context(access.local_operator(), self.world.a["id"])
        CaseService(self.pg, access).add_note(ctx, self.case["id"], "depois da migracao")
        CaseService(self.pg, access).open_case(ctx, department_id=self.world.dep["id"], objective="Nova demanda no PostgreSQL")
        with self.pg._transaction() as conn:
            self.assertGreater(conn.execute("SELECT MAX(seq) FROM events").fetchone()[0], highest)

    def test_a_missing_source_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            to_postgres(Path(tempfile.gettempdir()) / "nao-existe.sqlite3", self.pg)


class PlanTests(unittest.TestCase):
    def test_the_copy_plan_puts_parents_first_and_skips_what_belongs_to_other_modules(self):
        plan = [t for t, _ in table_plan()]
        for parent, child in (("tenants", "departments"), ("departments", "agent_definitions"), ("agent_definitions", "cases"), ("cases", "events"),
                              ("events", "outbox"), ("credentials", "connections"), ("auth_users", "auth_memberships"), ("agent_runs", "tool_decisions")):
            self.assertLess(plan.index(parent), plan.index(child), (parent, child))
        self.assertNotIn("schema_migrations", plan)
        self.assertFalse([t for t in plan if t.startswith("knowledge_index")])
        self.assertEqual(len(plan), len(set(plan)))


if __name__ == "__main__":
    unittest.main()
