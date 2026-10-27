import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from team.approvals import ApprovalService
from team.cases import CaseService
from team.connectors import ConnectorRegistry, DescriptorRejected
from team.connectors import erp_fake
from team.connectors.erp import ERP, ChargeRejected, emission_blockers, normalize_charge, verify_hmac_sha256
from team.connectors.erp_ledger import LedgerRejected, NotSent, Reconciler, UnknownResult, run_emission
from team.gateway import ApprovalRequired, Gateway
from tests.connector_contract import ConnectorContractMixin
from tests.team_fixtures import MANIFEST, GatewayCase
from tests.test_team_finance import FinanceCase

TENANT, OTHER, P = "empresa-a", "empresa-b", erp_fake.PROVIDER
CHARGE = {"documento": "NF-1", "cliente": "Acme Ltda", "beneficiario": "Minha Empresa", "valor_centavos": 150_00,
          "moeda": "BRL", "status": "emitida", "vencimento": "2026-12-10"}


def at(minute):
    return f"2026-11-01T10:{minute:02d}:00Z"


def event(event_id, status, minute, **changes):
    return {"event_id": event_id, "documento": "NF-1", "status": status, "occurred_at": at(minute), "cliente": "Acme Ltda",
            "valor_centavos": 150_00, "moeda": "brl", **changes}


class FakeErpContract(ConnectorContractMixin, unittest.IsolatedAsyncioTestCase):
    """The generic connector kit (#101) against the reference ERP driver."""

    def setUp(self):
        self.descriptor, self.transport = erp_fake.DESCRIPTOR, erp_fake.FakeErpTransport(Reconciler())

    def seed(self, tenant_id, marker):
        self.transport.charges[tenant_id] = {marker: {**CHARGE, "documento": marker}}

    def probe(self):
        return "consultar-cobrancas", {}

    def effects(self):
        return self.transport.issued


class CapabilityTests(unittest.TestCase):
    def registry(self):
        registry = ConnectorRegistry()
        registry.register_capability(ERP)
        return registry

    def test_reference_driver_fits_and_reports_what_it_does_not_support(self):
        registry = self.registry()
        registry.register_driver(erp_fake.DESCRIPTOR, erp_fake.FakeErpTransport(Reconciler()))
        self.assertTrue(registry.supports(P, "emitir-cobranca"))
        self.assertFalse(registry.supports(P, "alterar-cobranca"))
        self.assertEqual(registry.unsupported(P), ["alterar-cobranca", "consultar-pedidos"])
        self.assertEqual(registry.unsupported("nao-registrado"), [])

    def test_contract_violations_are_rejected(self):
        def broken(mutate):
            descriptor = copy.deepcopy(erp_fake.DESCRIPTOR)
            mutate(descriptor)
            with self.assertRaises(DescriptorRejected):
                self.registry().register_driver(descriptor, erp_fake.FakeErpTransport(Reconciler()))

        broken(lambda d: d["tools"].pop("status-cobranca"))                                          # required read missing
        broken(lambda d: d["tools"]["status-cobranca"].update(operation="write"))                    # wrong class
        broken(lambda d: d["tools"]["emitir-cobranca"].update(operation="send"))                     # optional write, wrong class
        broken(lambda d: d["tools"]["emitir-cobranca"].pop("amount_argument"))                       # approval would not show the amount
        broken(lambda d: d["tools"]["emitir-cobranca"].update(recipient_argument="beneficiario"))    # wrong binding
        broken(lambda d: d["tools"]["emitir-cobranca"]["arguments"]["valor_centavos"].update(type="string"))
        broken(lambda d: d["tools"]["emitir-cobranca"]["arguments"]["moeda"].pop("required"))        # currency must be mandatory
        broken(lambda d: d["tools"]["emitir-cobranca"]["arguments"].pop("beneficiario"))             # exact data includes beneficiary
        broken(lambda d: d["idempotency_arguments"].clear())                                         # no idempotency key
        broken(lambda d: d["tools"]["consultar-clientes"]["arguments"].update(busca={"type": "integer"}))


class ChargeModelTests(unittest.TestCase):
    def test_normalizes_money_currency_and_status_and_bounds_extras(self):
        charge = normalize_charge({**CHARGE, "status": "pendente", "extras": {"id_bling": 7, "token": "sk-test-ABCDEFGHIJKLMNOP1234"}})
        self.assertEqual((charge["status"], charge["moeda"], charge["valor_centavos"]), ("emitida", "brl", 150_00))
        self.assertNotIn("sk-test-ABCDEFGHIJKLMNOP1234", json.dumps(charge))
        for change in ({"valor_centavos": 1.5}, {"valor_centavos": True}, {"valor_centavos": -1}, {"moeda": "real"},
                       {"status": "quitada"}, {"vencimento": "10/12/2026"}, {"documento": ""}, {"extras": "texto"},
                       {"extras": {"a": "x" * 5000}}):
            with self.assertRaises(ChargeRejected, msg=str(change)):
                normalize_charge({**CHARGE, **change})
        with self.assertRaises(ChargeRejected):
            normalize_charge(None)

    def test_hmac_authentication_is_strict(self):
        body = b'{"a":1}'
        good = erp_fake.sign(body)
        self.assertTrue(verify_hmac_sha256(erp_fake.WEBHOOK_SECRET, body, good))
        self.assertTrue(verify_hmac_sha256(erp_fake.WEBHOOK_SECRET, body, "sha256=" + good.upper(), prefix="sha256="))
        for args in ((erp_fake.WEBHOOK_SECRET, body + b" ", good), (b"outro", body, good), (b"", body, good), (None, body, good),
                     (erp_fake.WEBHOOK_SECRET, "texto", good), (erp_fake.WEBHOOK_SECRET, body, None), (erp_fake.WEBHOOK_SECRET, body, "")):
            self.assertFalse(verify_hmac_sha256(*args))

    def test_finance_findings_that_block_a_proposal_keep_blocking(self):
        analysis = {"achados": [{"codigo": "fatura_duplicada", "bloqueia": True}, {"codigo": "orcamento_nao_e_divida", "bloqueia": False}]}
        self.assertEqual([f["codigo"] for f in emission_blockers(analysis)], ["fatura_duplicada"])
        self.assertEqual(emission_blockers({"achados": []}), [])
        self.assertEqual(emission_blockers(None), [])


class FinanceGateTests(FinanceCase):
    """The ERP layer reuses the finance analysis (#39) instead of repeating its rules."""

    def blockers(self, party, document):
        return [f["codigo"] for f in emission_blockers(self.analyze(party, document))]

    def test_findings_that_block_a_payment_proposal_block_the_issue_proposal_too(self):
        self.assertEqual(self.blockers("Hospedagem Alfa Ltda", "FAT-100"), [])                       # regular: may go to approval
        self.assertEqual(self.blockers("Hospedagem Alfa Ltda", "FAT-090"), ["possivel_pagamento_anterior"])
        self.assertEqual(self.blockers("Seguranca Gama Ltda", "FAT-301"), ["beneficiario_alterado"])
        self.assertIn("fatura_duplicada", self.blockers("Papelaria Delta", "FAT-400"))
        self.assertEqual(self.blockers("Consultoria Epsilon", "FAT-500"), ["cobranca_de_outra_empresa"])


class ReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.rec = Reconciler()
        self.rec.record_issued(TENANT, P, CHARGE)

    def state(self, tenant=TENANT):
        return self.rec.charge(tenant, P, "NF-1")

    def apply(self, raw, source="webhook", tenant=TENANT):
        return self.rec.apply(tenant, P, raw, source=source)

    def test_unauthenticated_webhook_changes_nothing_and_can_be_retried_signed(self):
        transport = erp_fake.FakeErpTransport(self.rec)
        body, signature = transport.webhook("NF-1", "paga", event_id="w1", occurred_at=at(1))
        for bad in ("0" * 64, "", signature[:-1] + ("0" if signature[-1] != "0" else "1")):
            self.assertEqual(self.rec.ingest_webhook(TENANT, P, body, bad, erp_fake.WEBHOOK_SECRET, erp_fake.parse_webhook),
                             {"resultado": "assinatura_invalida"})
        self.assertEqual(self.state()["status"], "emitida")
        ok = self.rec.ingest_webhook(TENANT, P, body, signature, erp_fake.WEBHOOK_SECRET, erp_fake.parse_webhook)
        self.assertEqual((ok["resultado"], ok["estado"]), ("aplicado", "paga"))
        garbage = b"nao e json"
        self.assertEqual(self.rec.ingest_webhook(TENANT, P, garbage, erp_fake.sign(garbage), erp_fake.WEBHOOK_SECRET,
                                                 erp_fake.parse_webhook), {"resultado": "evento_invalido"})

    def test_duplicate_events_never_duplicate_the_receipt(self):
        self.assertEqual(self.apply(event("e1", "paga", 1))["resultado"], "aplicado")
        self.assertEqual(self.apply(event("e1", "paga", 1))["resultado"], "duplicado")           # same webhook redelivered
        self.assertEqual(self.apply(event("e2", "paga", 2))["resultado"], "ja_aplicado")         # new id, same fact
        self.assertEqual(self.apply(event("poll-x", "paga", 3), source="polling")["resultado"], "ja_aplicado")
        receipts = self.state()["recibos"]
        self.assertEqual([(r["tipo"], r["comprovado"]) for r in receipts], [("paga", True)])
        self.assertEqual(receipts[0]["valor_centavos"], 150_00)

    def test_pending_status_is_not_a_payment(self):
        result = self.rec.apply_snapshot(TENANT, P, {**CHARGE, "status": "pendente"}, at(1))
        self.assertEqual((result["resultado"], result["estado"]), ("ja_aplicado", "emitida"))
        self.assertEqual(self.state()["recibos"], [])

    def test_polling_is_idempotent_and_applies_the_erp_view(self):
        paid = {**CHARGE, "status": "paga"}
        self.assertEqual(self.rec.apply_snapshot(TENANT, P, paid, at(1))["resultado"], "aplicado")
        self.assertEqual(self.rec.apply_snapshot(TENANT, P, paid, at(5))["resultado"], "duplicado")
        self.assertEqual(len(self.state()["recibos"]), 1)

    def test_out_of_order_refund_waits_for_the_payment(self):
        self.assertEqual(self.apply(event("r1", "estornada", 5))["resultado"], "aguardando_ordem")
        self.assertEqual(self.state()["status"], "emitida")
        self.assertEqual(self.state()["recibos"], [])
        self.assertEqual(self.apply(event("p1", "paga", 2))["resultado"], "aplicado")
        state = self.state()
        self.assertEqual(state["status"], "estornada")
        self.assertEqual([r["tipo"] for r in state["recibos"]], ["paga", "estornada"])
        self.assertEqual(self.state()["aguardando"], [])

    def test_stale_events_are_obsolete_and_a_refund_is_an_ordinary_transition(self):
        self.apply(event("p1", "paga", 2))
        self.assertEqual(self.apply(event("old", "emitida", 1))["resultado"], "obsoleto")
        self.assertEqual(self.apply(event("old2", "vencida", 1))["resultado"], "obsoleto")
        self.assertEqual(self.apply(event("r1", "estornada", 9))["resultado"], "aplicado")
        self.assertEqual(self.apply(event("late-paid", "paga", 3))["resultado"], "obsoleto")
        self.assertEqual(self.state()["status"], "estornada")

    def test_wrong_customer_amount_or_currency_is_flagged_not_applied(self):
        cases = (("cliente_incorreto", {"cliente": "Outra Empresa"}), ("valor_divergente", {"valor_centavos": 149_99}),
                 ("moeda_divergente", {"moeda": "usd"}))
        for number, (reason, change) in enumerate(cases):
            result = self.apply(event(f"d{number}", "paga", 1, **change))
            self.assertEqual((result["resultado"], result["motivo"], result["estado"]), ("divergente", reason, "emitida"))
        state = self.state()
        self.assertEqual((state["status"], state["recibos"], state["revisao_pendente"]), ("emitida", [], True))
        self.assertEqual([d["motivo"] for d in state["divergencias"]], [c[0] for c in cases])
        self.assertEqual(self.apply(event("ok", "paga", 2, cliente="  ACME   ltda "))["resultado"], "aplicado")  # same party, formatting only

    def test_contradictory_states_need_a_person(self):
        self.apply(event("c1", "cancelada", 1))
        result = self.apply(event("p1", "paga", 2))
        self.assertEqual((result["resultado"], result["motivo"]), ("divergente", "estado_contraditorio"))
        self.assertEqual((self.state()["status"], self.state()["recibos"]), ("cancelada", []))

    def test_unknown_documents_and_other_companies_are_not_touched(self):
        self.assertEqual(self.apply({**event("x1", "paga", 1), "documento": "NF-999"})["resultado"], "desconhecida")
        self.assertEqual(self.apply(event("o1", "paga", 1), tenant=OTHER)["resultado"], "desconhecida")  # NF-1 is not theirs
        self.assertIsNone(self.rec.charge(OTHER, P, "NF-1"))
        self.assertEqual(self.state()["status"], "emitida")
        self.rec.record_issued(OTHER, P, {**CHARGE, "cliente": "Beta"})
        self.assertEqual(self.apply(event("o2", "paga", 1, cliente="Beta"), tenant=OTHER)["resultado"], "aplicado")
        self.assertEqual(self.state()["status"], "emitida")

    def test_a_customer_message_is_a_claim_not_proof(self):
        claim = self.rec.note_customer_claim(TENANT, P, "NF-1", "Ja paguei ontem, segue comprovante")
        self.assertEqual((claim["resultado"], claim["estado"]), ("alegacao_registrada", "emitida"))
        self.assertEqual(self.rec.note_customer_claim(TENANT, P, "NF-404")["resultado"], "desconhecida")
        for source in ("mensagem_cliente", "email", "operador", "", None):
            self.assertEqual(self.apply(event(f"m-{source}", "paga", 1), source=source)["resultado"], "fonte_nao_prova_pagamento")
        self.assertEqual((self.state()["status"], self.state()["recibos"]), ("emitida", []))
        self.assertEqual(len(self.state()["alegacoes"]), 1)

    def test_malformed_events_are_rejected(self):
        for bad in (None, {}, {**event("a", "paga", 1), "occurred_at": "01/11/2026"}, {**event("a", "paga", 1), "status": "quitada"},
                    {**event("", "paga", 1)}, {**event("a", "paga", 1), "valor_centavos": 1.5}, {**event("a", "paga", 1), "moeda": "real"}):
            with self.assertRaises(LedgerRejected, msg=str(bad)):
                self.apply(bad)
        self.assertEqual(self.state()["status"], "emitida")


class EmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rec = Reconciler()
        self.payload = {"documento": "NF-2", "valor_centavos": 100_00}

    async def test_success_replay_and_key_reuse(self):
        calls = []

        async def call():
            calls.append(1)
            return normalize_charge({**CHARGE, "documento": "NF-2"})

        first = await run_emission(self.rec, TENANT, P, "k1", self.payload, call)
        again = await run_emission(self.rec, TENANT, P, "k1", self.payload, call)
        self.assertEqual((first, again), ({"repetida": False, "documento": "NF-2"}, {"repetida": True, "documento": "NF-2"}))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.rec.charge(TENANT, P, "NF-2")["status"], "emitida")
        with self.assertRaises(LedgerRejected):
            await run_emission(self.rec, TENANT, P, "k1", {**self.payload, "valor_centavos": 1}, call)

    async def test_a_lost_response_blocks_repeating_until_verified(self):
        async def lost():
            raise ConnectionError("sem resposta")

        with self.assertRaises(ConnectionError):
            await run_emission(self.rec, TENANT, P, "k2", self.payload, lost)
        self.assertEqual(self.rec.emission_state(TENANT, P, "k2"), "desconhecido")
        with self.assertRaises(UnknownResult):
            await run_emission(self.rec, TENANT, P, "k2", self.payload, lost)
        self.assertEqual(self.rec.resolve_emission(TENANT, P, "k2", found={**CHARGE, "documento": "NF-2"}), "emitida")
        self.assertEqual(self.rec.charge(TENANT, P, "NF-2")["documento"], "NF-2")
        self.assertEqual((await run_emission(self.rec, TENANT, P, "k2", self.payload, lost))["repetida"], True)

    async def test_verified_absence_releases_the_key_and_unsent_calls_never_block(self):
        async def lost():
            raise TimeoutError()

        async def never_left():
            raise NotSent()

        with self.assertRaises(TimeoutError):
            await run_emission(self.rec, TENANT, P, "k3", self.payload, lost)
        self.assertEqual(self.rec.resolve_emission(TENANT, P, "k3", found=None), "nao_emitida")
        with self.assertRaises(NotSent):
            await run_emission(self.rec, TENANT, P, "k3", self.payload, never_left)
        self.assertEqual(self.rec.emission_state(TENANT, P, "k3"), "nao_emitida")
        with self.assertRaises(LedgerRejected):
            self.rec.resolve_emission(TENANT, P, "k3", found=None)  # nothing left to resolve
        with self.assertRaises(LedgerRejected):
            self.rec.resolve_emission(TENANT, P, "nunca-vista")

    async def test_a_cancelled_call_is_treated_as_unknown(self):
        import asyncio
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(30)

        task = asyncio.ensure_future(run_emission(self.rec, TENANT, P, "k4", self.payload, slow))
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.rec.emission_state(TENANT, P, "k4"), "desconhecido")

    async def test_keys_do_not_cross_companies(self):
        async def call():
            return normalize_charge({**CHARGE, "documento": "NF-2"})

        await run_emission(self.rec, TENANT, P, "k5", self.payload, call)
        self.assertIsNone(self.rec.emission_state(OTHER, P, "k5"))
        self.assertIsNone(self.rec.charge(OTHER, P, "NF-2"))


IDENTITY = {"tenant_id": TENANT, "agent_id": "a", "run_id": "r", "connection_id": "c", "operation": "write"}
ISSUE = {"cliente": "Acme Ltda", "beneficiario": "Minha Empresa", "documento": "NF-7", "valor_centavos": 150_00, "moeda": "brl",
         "chave": "chave-7"}


class ReferenceDriverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rec = Reconciler()
        self.erp = erp_fake.FakeErpTransport(self.rec)

    async def call(self, tool, arguments, tenant=TENANT):
        return await self.erp.call_tool({}, tool, arguments, identity={**IDENTITY, "tenant_id": tenant}, secret=None)

    async def test_fixture_and_default_never_issue_a_real_charge(self):
        self.assertTrue((await self.call("emitir-cobranca", ISSUE))["dry_run"])
        self.assertTrue((await self.call("emitir-cobranca", {**ISSUE, "dry_run": True}))["dry_run"])
        self.assertEqual((self.erp.issued, self.erp.charges.get(TENANT, {})), (0, {}))

    async def test_real_issue_then_status_and_isolation(self):
        done = await self.call("emitir-cobranca", {**ISSUE, "dry_run": False})
        self.assertEqual(done, {"dry_run": False, "repetida": False, "documento": "NF-7"})
        self.assertEqual((await self.call("status-cobranca", {"documento": "NF-7"}))["cobranca"]["status"], "emitida")
        self.assertEqual((await self.call("status-cobranca", {"documento": "NF-7"}, tenant=OTHER))["erro"], "nao_encontrada")

    async def test_lost_response_does_not_double_issue_and_asks_for_verification(self):
        self.erp.lose_next_response = True
        with self.assertRaises(ConnectionError):
            await self.call("emitir-cobranca", {**ISSUE, "dry_run": False})
        retry = await self.call("emitir-cobranca", {**ISSUE, "dry_run": False})
        self.assertEqual(retry["erro"], "resultado_desconhecido")
        self.assertEqual(self.erp.issued, 1)  # the vendor did it once; the retry was refused locally
        found = (await self.call("status-cobranca", {"documento": "NF-7"}))["cobranca"]
        self.rec.resolve_emission(TENANT, P, "chave-7", found=found)
        again = await self.call("emitir-cobranca", {**ISSUE, "dry_run": False})
        self.assertEqual((again["repetida"], self.erp.issued), (True, 1))


class GatewayApprovalErpTests(GatewayCase):
    """Issuing through the real gateway: exact approval of customer, amount, currency, beneficiary and every other field."""

    def setUp(self):
        super().setUp()
        self.rec = Reconciler()
        self.erp = erp_fake.FakeErpTransport(self.rec)
        self.registry = ConnectorRegistry()
        self.registry.register_capability(ERP)
        self.registry.register_driver(erp_fake.DESCRIPTOR, self.erp)
        self.registry.attach(self.connections)
        self.cases = CaseService(self.repo, self.access)
        self.approvals = ApprovalService(self.repo, self.access, self.cases,
                                         clock=lambda: datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
        self.gateway = Gateway(self.repo, self.access, self.skills, self.connections, {"local": self.registry.local_transport()},
                               approvals=self.approvals)
        entry = self.registry.approve(self.connections, self.op, P)
        conn = self.connections.create_connection(self.ca, label="ERP", provider=P, connection_kind="mcp", catalog_entry_id=entry["id"])
        manifest = {**MANIFEST, "id": "teste.erp", "required_tools": [
            {"tool": f"{P}.emitir-cobranca", "operations": ["write"]}, {"tool": f"{P}.status-cobranca", "operations": ["read"]}]}
        folder = Path(self.temp.name) / "erpskill"
        folder.mkdir()
        (folder / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
        skill = self.skills.install(self.op, folder, self.skills.review_package(folder).content_hash)
        self.skills.grant_skill(self.ca, agent_id=self.agent["id"], skill_id=skill["id"], operations=["read", "write"],
                                connection_id=conn["id"], attach=True)
        dep = self.team.create(self.ca, "department", name="Fin2", scope="x", status="ativo")
        case = self.cases.open_case(self.ca, department_id=dep["id"], objective="Cobrar", customer_ref="c1")
        case = self.cases.transition(self.ca, case["id"], case["revision"], "triagem")
        case = self.cases.assign_owner(self.ca, case["id"], case["revision"], self.agent["id"])
        self.case = self.cases.transition(self.ca, case["id"], case["revision"], "em_execucao")
        self.alias = f"{P}.emitir-cobranca"
        self.call_args = {**ISSUE, "dry_run": False}

    def execution(self):
        run = self.agents.start_run(self.ca, self.agent["id"], case_id=self.case["id"],
                                    budget={"max_steps": 4, "token_budget": 100, "deadline_seconds": 60})
        return self.gateway.open_execution(self.access.agent_context(self.op, self.a["id"], run["id"]), run["id"])

    def approve_pending(self):
        pending = self.approvals.list_approvals(self.ca, state="pendente")
        self.assertEqual(len(pending), 1)
        return pending[0], self.approvals.approve(self.ca, pending[0]["id"], pending[0]["payload_hash"])

    async def test_issue_needs_an_exact_approval_and_runs_once(self):
        execution = self.execution()
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, self.call_args)
        self.assertEqual(self.erp.issued, 0)
        pending, _ = self.approve_pending()
        self.assertEqual((pending["amount_cents"], pending["currency"]), (150_00, "brl"))
        self.assertEqual(pending["recipient"], {"to": ["acme ltda"]})
        shown = json.dumps(pending, ensure_ascii=False)
        for exact in ("Minha Empresa", "NF-7", "chave-7"):  # beneficiary and the other exact data are part of what is approved
            self.assertIn(exact, shown)
        result = await self.gateway.call_tool(execution, self.alias, self.call_args)
        self.assertEqual((result.data["documento"], self.erp.issued), ("NF-7", 1))
        with self.assertRaises(ApprovalRequired):  # replaying needs a new approval
            await self.gateway.call_tool(execution, self.alias, self.call_args)
        self.assertEqual(self.erp.issued, 1)

    async def test_changing_any_exact_field_needs_a_new_approval(self):
        execution = self.execution()
        with self.assertRaises(ApprovalRequired):
            await self.gateway.call_tool(execution, self.alias, self.call_args)
        self.approve_pending()
        for change in ({"cliente": "Outra Ltda"}, {"valor_centavos": 150_01}, {"moeda": "usd"}, {"beneficiario": "Terceiro"},
                       {"documento": "NF-8"}, {"chave": "outra-chave"}):
            with self.assertRaises(ApprovalRequired, msg=str(change)):
                await self.gateway.call_tool(execution, self.alias, {**self.call_args, **change})
        self.assertEqual(self.erp.issued, 0)

    async def test_dry_run_is_a_write_too_and_reads_need_no_approval(self):
        execution = self.execution()
        with self.assertRaises(ApprovalRequired):  # even a simulation goes through the write path: nothing slips by
            await self.gateway.call_tool(execution, self.alias, {**ISSUE, "dry_run": True})
        result = await self.gateway.call_tool(execution, f"{P}.status-cobranca", {"documento": "NF-7"})
        self.assertEqual(result.data["erro"], "nao_encontrada")


if __name__ == "__main__":
    unittest.main()
