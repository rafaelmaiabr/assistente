import itertools
import tempfile
import threading
import unittest
from pathlib import Path

from team.access import AccessService, NotFound, PermissionDenied
from team.agents import AgentService
from team.cases import CaseRejected, CaseService, InvalidTransition, clean_text
from team.connections import ConnectionService, LauncherRegistry
from team.db import connect
from team.enums import CASE_STATES, CASE_TRANSITIONS
from team.ids import new_id
from team.repository import RevisionConflict, SqliteRepository
from team.schema import ContractError
from team.service import TeamService
from team.vault import MemoryVault

PROFILE = dict(name="Suporte", goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"},
               scope="Suporte tecnico", out_of_scope="Outros", model_policy_ref="local-primeiro")


class CaseTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "team.sqlite3"
        self.repo = SqliteRepository(self.path)
        self.repo.migrate()
        self.access = AccessService(self.repo, new_id("user"))
        self.team = TeamService(self.repo, self.access)
        self.agents = AgentService(self.repo, self.access)
        self.cases = CaseService(self.repo, self.access)
        self.op = self.access.local_operator()
        self.a = self.team.create_company(self.op, "Empresa A")
        self.b = self.team.create_company(self.op, "Empresa B")
        self.ca = self.access.tenant_context(self.op, self.a["id"])
        self.cb = self.access.tenant_context(self.op, self.b["id"])
        self.dep = self.team.create(self.ca, "department", name="Suporte", scope="x", status="ativo")
        self.dep_b = self.team.create(self.cb, "department", name="Suporte B", scope="x", status="ativo")
        self.agent = self.agents.create_agent(self.ca, department_id=self.dep["id"], **PROFILE)
        self.agent_b = self.agents.create_agent(self.cb, department_id=self.dep_b["id"], **PROFILE)

    def open(self, **kwargs):
        return self.cases.open_case(self.ca, department_id=self.dep["id"], objective="Site fora do ar", **kwargs)

    def advance(self, case, *states, owner=True):
        """Walk a case through states using the service (assigning the owner on the way in)."""
        for state in states:
            kwargs = {"owner_agent_id": self.agent["id"]} if owner and state == "em_execucao" and not case["owner_agent_id"] else {}
            if state in ("pausada", "cancelada", "falhou"):
                kwargs["reason"] = "motivo de teste"
            case = self.cases.transition(self.ca, case["id"], case["revision"], state, **kwargs)
        return case

    def events(self, case):
        with self.repo.transaction(self.a["id"]) as session:
            return [event for _, event in session.events(case["id"])]


class StateMachineTests(CaseTestBase):
    def test_new_case_waits_in_the_initial_queue_with_an_event(self):
        case = self.open(customer_ref="cliente-1", priority="alta", origin="email")
        self.assertEqual((case["state"], case["owner_agent_id"], case["revision"], case["priority"], case["origin"]),
                         ("recebida", None, 1, "alta", "email"))
        [event] = self.events(case)
        self.assertEqual((event["type"], event["revision"], event["payload"]["origin"]), ("case.opened", 1, "email"))
        with self.repo.transaction(self.a["id"]) as session:
            self.assertEqual([p["event"]["type"] for p in session.pending_outbox()][-1], "case.opened")

    def test_happy_path_keeps_one_owner_and_numbers_every_change(self):
        case = self.open()
        case = self.cases.transition(self.ca, case["id"], 1, "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], 2, self.agent["id"])
        case = self.advance(case, "em_execucao", "aguardando_cliente", "em_execucao")
        self.cases.add_evidence(self.ca, case["id"], source="recibo-123", summary="Pagamento confirmado pelo provedor",
                                evidence_type="recibo", reliability="verificada", content_hash="a" * 64)
        case = self.cases.transition(self.ca, case["id"], case["revision"], "resolvida")
        self.assertEqual((case["state"], case["owner_agent_id"], case["revision"]), ("resolvida", self.agent["id"], 7))
        self.assertTrue(case["closed_at"])
        events = self.events(case)
        self.assertEqual([e["revision"] for e in events], list(range(1, 8)))
        self.assertEqual([e["payload"].get("to") for e in events if e["type"] == "case.state-changed"],
                         ["triagem", "em_execucao", "aguardando_cliente", "em_execucao", "resolvida"])

    def test_every_transition_outside_the_graph_is_refused(self):
        for source, target in itertools.product(CASE_STATES, CASE_STATES):
            if target in CASE_TRANSITIONS[source]:
                continue
            case = self.open()
            with self.repo.transaction(self.a["id"]) as session:  # place the case in `source` directly
                owner = None if source in ("recebida", "triagem", "cancelada", "falhou") else self.agent["id"]
                stored = session.get("case", case["id"])
                session.save({**stored, "state": source, "owner_agent_id": owner, "revision": 2})
            with self.assertRaises(InvalidTransition, msg=f"{source}->{target}"):
                self.cases.transition(self.ca, case["id"], 2, target, reason="x", owner_agent_id=self.agent["id"])
        with self.assertRaises(InvalidTransition):
            self.cases.transition(self.ca, self.open()["id"], 1, "inventado")

    def test_requirements_for_starting_work_and_for_reasons(self):
        case = self.cases.transition(self.ca, self.open()["id"], 1, "triagem")
        with self.assertRaisesRegex(CaseRejected, "responsavel"):
            self.cases.transition(self.ca, case["id"], 2, "em_execucao")
        for target in ("pausada", "cancelada", "falhou"):
            with self.assertRaisesRegex(CaseRejected, "motivo"):
                self.cases.transition(self.ca, case["id"], 2, target, owner_agent_id=self.agent["id"]) \
                    if target == "pausada" else self.cases.transition(self.ca, case["id"], 2, target)
        working = self.cases.transition(self.ca, case["id"], 2, "em_execucao", owner_agent_id=self.agent["id"])
        self.assertEqual(working["owner_agent_id"], self.agent["id"])
        with self.assertRaisesRegex(CaseRejected, "ja tem responsavel"):
            self.cases.assign_owner(self.ca, working["id"], working["revision"], self.agent["id"])
        other = self.agents.create_agent(self.ca, department_id=self.dep["id"], **{**PROFILE, "name": "Outro"})
        with self.assertRaisesRegex(CaseRejected, "transferencia"):
            self.cases.transition(self.ca, working["id"], working["revision"], "pausada", reason="x",
                                  owner_agent_id=other["id"])

    def test_owner_must_be_an_active_agent_of_the_same_company(self):
        case = self.cases.transition(self.ca, self.open()["id"], 1, "triagem")
        with self.assertRaises(NotFound):
            self.cases.assign_owner(self.ca, case["id"], 2, self.agent_b["id"])
        agent = self.agents.get_agent(self.ca, self.agent["id"])
        self.agents.update_agent(self.ca, self.agent["id"], agent["revision"], status="inativo")
        with self.assertRaisesRegex(CaseRejected, "desativado"):
            self.cases.assign_owner(self.ca, case["id"], 2, self.agent["id"])

    def test_stale_revision_is_rejected_and_concurrent_writers_have_one_winner(self):
        case = self.cases.transition(self.ca, self.open()["id"], 1, "triagem")
        with self.assertRaises(RevisionConflict):
            self.cases.transition(self.ca, case["id"], 1, "cancelada", reason="atrasado")
        wins, losses, barrier = [], [], threading.Barrier(2)

        def attempt(target):
            barrier.wait()
            try:
                wins.append(self.cases.transition(self.ca, case["id"], 2, target, reason="corrida",
                                                  owner_agent_id=self.agent["id"]))
            except RevisionConflict:
                losses.append(target)

        threads = [threading.Thread(target=attempt, args=(t,)) for t in ("em_execucao", "cancelada")]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual((len(wins), len(losses)), (1, 1))
        self.assertEqual([e["revision"] for e in self.events(case)], [1, 2, 3])  # no event lost or duplicated

    def test_reopening_needs_a_reason_and_survives_a_restart(self):
        case = self.advance(self.open(), "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], self.agent["id"])
        case = self.advance(case, "em_execucao", "cancelada")
        self.assertTrue(case["closed_at"])
        # the process "restarts": new repository object, new services, same database file
        repo = SqliteRepository(self.path)
        access = AccessService(repo, self.access.operator_id)
        service = CaseService(repo, access)
        ctx = access.tenant_context(access.local_operator(), self.a["id"])
        stored = service.get_case(ctx, case["id"])
        self.assertEqual((stored["state"], stored["owner_agent_id"]), ("cancelada", self.agent["id"]))
        with self.assertRaises(InvalidTransition):  # cancelled is final
            service.transition(ctx, case["id"], stored["revision"], "em_execucao", reason="x")
        done = self.advance(self.open(), "triagem")
        done = self.cases.assign_owner(self.ca, done["id"], done["revision"], self.agent["id"])
        done = self.advance(done, "em_execucao", "falhou")
        with self.assertRaisesRegex(CaseRejected, "motivo"):
            service.transition(ctx, done["id"], done["revision"], "em_execucao")
        reopened = service.transition(ctx, done["id"], done["revision"], "em_execucao", reason="retomar apos reinicio")
        self.assertEqual((reopened["state"], reopened["closed_at"]), ("em_execucao", None))
        self.assertEqual([e["type"] for e in self.events(done)][-1], "case.reopened")


class EvidenceTests(CaseTestBase):
    def working_case(self):
        case = self.advance(self.open(), "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], self.agent["id"])
        return self.advance(case, "em_execucao")

    def test_resolving_requires_verified_technical_evidence_not_summaries(self):
        case = self.working_case()
        with self.assertRaisesRegex(CaseRejected, "evidencia verificada"):
            self.cases.transition(self.ca, case["id"], case["revision"], "resolvida")
        self.cases.add_summary(self.ca, case["id"], "Resumo: o dominio foi renovado e tudo funciona.")
        self.cases.add_note(self.ca, case["id"], "Cliente confirmou por telefone que esta tudo certo.")
        self.cases.add_message(self.ca, case["id"], "entrada", "email", "Pronto, ja renovei.")
        self.cases.add_evidence(self.ca, case["id"], source="cliente", summary="Cliente afirma ter pago",
                                evidence_type="fato", reliability="declarada")
        self.cases.add_evidence(self.ca, case["id"], source="modelo", summary="Parece resolvido",
                                evidence_type="resultado_ferramenta", reliability="inferida")
        with self.assertRaisesRegex(CaseRejected, "evidencia verificada"):
            self.cases.transition(self.ca, case["id"], case["revision"], "resolvida")
        self.cases.add_evidence(self.ca, case["id"], source="consulta DNS", summary="Registro A presente e resolvendo",
                                evidence_type="resultado_ferramenta", reliability="verificada", content_hash="b" * 64)
        resolved = self.cases.transition(self.ca, case["id"], case["revision"], "resolvida")
        self.assertEqual(resolved["state"], "resolvida")

    def test_verified_evidence_from_an_agent_needs_technical_backing(self):
        case = self.working_case()
        for kwargs in ({"evidence_type": "fato"}, {"evidence_type": "recibo", "content_hash": None}):
            agent_ctx = self._agent_ctx(case)
            with self.assertRaisesRegex(CaseRejected, "Evidencia verificada"):
                self.cases.add_evidence(agent_ctx, case["id"], source="agente", summary="Confirmado", reliability="verificada",
                                        **{"content_hash": "c" * 64, **kwargs})
        ok = self.cases.add_evidence(self._agent_ctx(case), case["id"], source="provedor", summary="Recibo emitido",
                                     evidence_type="recibo", reliability="verificada", content_hash="d" * 64)
        self.assertEqual(ok["recorded_by"], self.agent["id"])

    def test_manual_resolution_is_operator_only_with_a_reason_and_flagged(self):
        case = self.working_case()
        with self.assertRaisesRegex(CaseRejected, "motivo"):
            self.cases.transition(self.ca, case["id"], case["revision"], "resolvida", manual_resolution=True)
        agent_ctx = self._agent_ctx(case)
        with self.assertRaisesRegex(CaseRejected, "operador"):
            self.cases.transition(agent_ctx, case["id"], case["revision"], "resolvida", reason="eu decidi",
                                  manual_resolution=True)
        resolved = self.cases.transition(self.ca, case["id"], case["revision"], "resolvida",
                                         reason="Cliente confirmou por telefone; sem comprovante tecnico",
                                         manual_resolution=True)
        self.assertTrue(self.events(resolved)[-1]["payload"]["manual_resolution"])

    def _agent_ctx(self, case):
        agent = self.agent
        run = self.agents.start_run(self.ca, agent["id"], budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 30},
                                    case_id=case["id"])
        self.team.create(self.ca, "access_grant", principal_id=agent["id"], resource_kind="case", resource_id=case["id"],
                         actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        return self.access.agent_context(self.op, self.a["id"], run["id"])


class HistoryTests(CaseTestBase):
    def test_notes_messages_facts_and_summaries_stay_distinct_in_the_timeline(self):
        case = self.open()
        note = self.cases.add_note(self.ca, case["id"], "Parece ser DNS.")
        message = self.cases.add_message(self.ca, case["id"], "saida", "email", "Estamos verificando.")
        summary = self.cases.add_summary(self.ca, case["id"], "Resumo da conversa ate agora.")
        fact = self.cases.add_evidence(self.ca, case["id"], source="whois", summary="Dominio expira em 2026-12-01",
                                       evidence_type="fato", reliability="declarada", collected_at="2026-10-22T10:00:00Z")
        self.assertEqual([n["entry_type"] for n in (note, message, summary)], ["nota_interna", "mensagem_externa", "resumo_ia"])
        self.assertEqual((message["direction"], message["channel"], note["direction"]), ("saida", "email", None))
        self.assertEqual((fact["source"], fact["collected_at"], fact["reliability"]), ("whois", "2026-10-22T10:00:00Z", "declarada"))
        kinds = [item["tipo"] for item in self.cases.timeline(self.ca, case["id"])]
        self.assertEqual(sorted(set(kinds)), ["entrada", "evento", "evidencia"])
        self.assertEqual(kinds.count("entrada"), 3)
        with self.assertRaises(ContractError):  # an external message without direction/channel is not a message
            self.cases._entry(self.ca, case["id"], "mensagem_externa", "sem direcao")

    def test_private_model_reasoning_never_reaches_the_history(self):
        case = self.open(origin="chat")
        self.assertEqual(clean_text("<think>plano secreto</think>Resposta final"), "Resposta final")
        self.assertEqual(clean_text("Antes <THINKING>\nmultilinha\n</THINKING> depois"), "Antes  depois")
        self.assertEqual(clean_text("Util <think>nunca fechado"), "Util")
        for text in ("<think>so raciocinio</think>", "   ", "<think>aberto"):
            with self.assertRaises(CaseRejected):
                self.cases.add_summary(self.ca, case["id"], text)
        self.cases.add_summary(self.ca, case["id"], "<think>raciocinio privado</think>Resumo limpo.")
        self.cases.add_evidence(self.ca, case["id"], source="s", summary="<think>x</think>Fato", evidence_type="fato",
                                reliability="declarada")
        advanced = self.cases.transition(self.ca, case["id"], 1, "triagem", reason="<think>oculto</think>fila")
        self.assertEqual(advanced["state"], "triagem")
        conn = connect(self.path)
        self.addCleanup(conn.close)
        for table in ("case_entries", "evidences", "events"):
            for (doc,) in conn.execute(f"SELECT doc FROM {table}"):
                self.assertNotIn("raciocinio privado", doc)
                self.assertNotIn("<think>", doc.lower())
        with self.assertRaises(CaseRejected):
            self.cases.open_case(self.ca, department_id=self.dep["id"], objective="<think>nada</think>")

    def test_history_and_evidence_are_append_only(self):
        case = self.open()
        entry = self.cases.add_note(self.ca, case["id"], "Nota original")
        evidence = self.cases.add_evidence(self.ca, case["id"], source="s", summary="Fato", evidence_type="fato",
                                           reliability="declarada")
        conn = connect(self.path)
        self.addCleanup(conn.close)
        for sql, args in (("UPDATE case_entries SET doc = '{}' WHERE id = ?", entry["id"]),
                          ("DELETE FROM case_entries WHERE id = ?", entry["id"]),
                          ("UPDATE evidences SET doc = '{}' WHERE id = ?", evidence["id"]),
                          ("DELETE FROM evidences WHERE id = ?", evidence["id"])):
            with self.assertRaisesRegex(Exception, "acrescimo"):
                conn.execute(sql, (args,))

    def test_closing_the_chat_changes_nothing_because_cases_do_not_depend_on_it(self):
        case = self.advance(self.open(origin="chat"), "triagem")
        self.cases.add_message(self.ca, case["id"], "entrada", "chat", "Cliente fechou a janela do chat.")
        stored = CaseService(SqliteRepository(self.path), self.access).get_case(self.ca, case["id"])
        self.assertEqual((stored["state"], stored["origin"]), ("triagem", "chat"))  # not resolved, not deleted


class IsolationAndPolicyTests(CaseTestBase):
    def test_other_company_sees_nothing_and_fails_like_a_missing_case(self):
        case = self.open()
        missing = new_id("case")
        for action in (lambda i: self.cases.get_case(self.cb, i),
                       lambda i: self.cases.transition(self.cb, i, 1, "triagem"),
                       lambda i: self.cases.assign_owner(self.cb, i, 1, self.agent_b["id"]),
                       lambda i: self.cases.add_note(self.cb, i, "x"),
                       lambda i: self.cases.add_message(self.cb, i, "entrada", "chat", "x"),
                       lambda i: self.cases.add_evidence(self.cb, i, source="s", summary="x", evidence_type="fato",
                                                         reliability="declarada"),
                       lambda i: self.cases.timeline(self.cb, i)):
            errors = []
            for value in (case["id"], missing):
                with self.assertRaises(NotFound) as caught:
                    action(value)
                errors.append(str(caught.exception))
            self.assertEqual(errors[0], errors[1])
        self.assertEqual(self.cases.list_cases(self.cb), [])
        with self.assertRaises(NotFound):
            self.cases.open_case(self.cb, department_id=self.dep["id"], objective="cruzado")

    def test_agents_need_grants_to_read_or_move_cases(self):
        case = self.advance(self.open(), "triagem")
        run = self.agents.start_run(self.ca, self.agent["id"], budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 30})
        agent_ctx = self.access.agent_context(self.op, self.a["id"], run["id"])
        with self.assertRaises(NotFound):
            self.cases.get_case(agent_ctx, case["id"])
        self.assertEqual(self.cases.list_cases(agent_ctx), [])
        with self.assertRaises(PermissionDenied):
            self.cases.open_case(agent_ctx, department_id=self.dep["id"], objective="x")
        self.team.create(self.ca, "access_grant", principal_id=self.agent["id"], resource_kind="case",
                         resource_id=case["id"], actions=["read"], granted_by=self.op.principal_id, expires_at=None)
        self.assertEqual(self.cases.get_case(agent_ctx, case["id"])["id"], case["id"])
        with self.assertRaises(NotFound):  # read is not write
            self.cases.add_note(agent_ctx, case["id"], "tentativa")
        with self.assertRaises(PermissionDenied):
            self.cases.get_case({"tenant_id": self.a["id"]}, case["id"])

    def test_list_filters(self):
        first = self.open(priority="baixa")
        second = self.advance(self.open(), "triagem")
        self.assertEqual({c["id"] for c in self.cases.list_cases(self.ca)}, {first["id"], second["id"]})
        self.assertEqual([c["id"] for c in self.cases.list_cases(self.ca, state="triagem")], [second["id"]])
        self.assertEqual(self.cases.list_cases(self.ca, owner_agent_id=self.agent["id"]), [])


if __name__ == "__main__":
    unittest.main()
