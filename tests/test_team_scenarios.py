"""End-to-end scenarios for the whole team, against fictitious systems only (see ``tests/e2e_world.py``).

Every external system is a fake: IMAP and SMTP are local protocol servers, the web and the registry are scripted, the
registrar is a person who renews by hand, and the model is scripted. The clock is controlled. Nothing here proves that
a real mailbox, registrar or model behaves the same way: that is what the optional real-account script is for.
"""
import email
import email.policy
import json
import unittest

from team.access import NotFound
from team.renewal import FlowRejected
from team.schema import content_hash
from tests.e2e_world import CUSTOMER, DOMAIN, OWN, OTHER_CUSTOMER, World, mail
from tests.test_team_diagnostics import rdap_body


class Scenario(World):
    def start_site_case(self, text="Meu site esta fora do ar e acho que o dominio venceu.", mid="<a1@cliente.invalid>", uid=1):
        """Customer email -> case -> triage -> Suporte accepts and is ready to work."""
        self.put_mail(uid, mail("Meu site caiu", f"Cliente Um <{CUSTOMER}>", text, mid))
        self.receive()
        case = self.all_cases()[-1]
        self.run_triage(case, text)
        self.handoffs.accept(self.ctx(self.suporte), self.pending_transfer(case)["id"])
        case = self.case(case["id"])
        if case["state"] != "em_execucao":
            case = self.cases.transition(self.ctx(self.suporte), case["id"], case["revision"], "em_execucao")
        return case

    def delegate_to_financeiro(self, case):
        sup = self.ctx(self.suporte)
        diagnosis = self.flow.diagnose(sup, case["id"], DOMAIN)
        handoff = self.flow.open_financial_subtask(sup, case["id"], DOMAIN, self.financeiro["id"], diagnosis_evidence_id=diagnosis["evidencia"])
        self.handoffs.accept(self.ctx(self.financeiro), handoff["id"])
        return diagnosis

    def propose(self, case):
        proposal = self.flow.propose(self.ctx(self.financeiro), case["id"], DOMAIN, counterparty="Registro Exemplo", amount_cents=8990, currency="BRL")
        return self.approvals.get(self.ca, proposal["aprovacao"])

    def approve(self, approval):
        self.approvals.approve(self.ca, approval["id"], approval["payload_hash"])

    def renew_by_hand(self, case, approval):
        self.flow.record_manual_renewal(self.ca, case["id"], approval["id"], reference="REG-77", amount_cents=8990, currency="BRL",
                                        paid_at=self.clock.stamp(), registrar="Registro Exemplo")

    def financeiro_task(self, case):
        with self.repo.transaction(self.a["id"]) as session:
            return next(t for t in session.list("task", case_id=case["id"], limit=10) if t["assignee_agent_id"] == self.financeiro["id"])

    def outgoing(self, case):
        return [t["item"] for t in self.cases.timeline(self.ca, case["id"])
                if t["tipo"] == "entrada" and t["item"]["entry_type"] == "mensagem_externa" and t["item"]["direction"] == "saida"]


class ScenarioA(Scenario):
    """Customer -> Secretaria -> Suporte -> Financeiro (delegation) -> Suporte -> customer, site verified before resolving."""

    def test_the_site_down_story_from_the_first_email_to_a_verified_resolution(self):
        self.put_mail(1, mail("Meu site caiu", f"Cliente Um <{CUSTOMER}>", "Meu site esta fora do ar e acho que o dominio venceu.", "<a1@cliente.invalid>"))
        report = self.receive()
        self.assertEqual(report["por_classificacao"], {"nova_demanda": 1})
        case = self.all_cases()[0]
        self.assertEqual((case["state"], case["origin"], case["owner_agent_id"]), ("recebida", "email", None))
        decision = self.run_triage(case, "Meu site esta fora do ar e acho que o dominio venceu.")
        self.assertEqual((decision["decision"], decision["capabilities"], decision["method"]), ("rotear", ["suporte-tecnico"], "regra"))
        self.assertEqual(self.case(case["id"])["owner_agent_id"], self.secretaria["id"])  # until Suporte accepts, Secretaria owns it
        self.handoffs.accept(self.ctx(self.suporte), self.pending_transfer(case)["id"])
        case = self.case(case["id"])
        self.assertEqual(case["owner_agent_id"], self.suporte["id"])
        if case["state"] != "em_execucao":
            case = self.cases.transition(self.ctx(self.suporte), case["id"], case["revision"], "em_execucao")
        diagnosis = self.delegate_to_financeiro(case)
        self.assertEqual((diagnosis["categoria"], diagnosis["certeza"]), ("dominio_expirado", "confirmado"))
        self.assertEqual((self.case(case["id"])["owner_agent_id"], self.case(case["id"])["state"]), (self.suporte["id"], "aguardando_subtarefa"))
        approval = self.propose(case)
        self.approve(approval)
        self.renew_by_hand(case, approval)
        self.net.rdap[DOMAIN] = rdap_body(expires="2027-06-15T00:00:00Z")  # the registry knows; DNS still has not caught up
        self.assertEqual(self.flow.reverify(self.ca, case["id"], DOMAIN)["estado"], "em_propagacao")
        with self.assertRaises(FlowRejected):
            self.flow.close_if_verified(self.ca, case["id"], "cedo demais")
        self.clock.advance(hours=3)
        self.healthy_web()
        self.assertEqual(self.flow.reverify(self.ca, case["id"], DOMAIN)["estado"], "resolvido_tecnicamente")
        self.flow.return_to_support(self.ctx(self.financeiro), self.financeiro_task(case)["id"],
                                    "Renovacao confirmada por uma pessoa (REG-77); aguardando a validacao tecnica.")
        self.assertEqual(self.case(case["id"])["state"], "em_execucao")
        sent = self.reply_to_customer(self.suporte, case, "Seu dominio foi renovado e o site voltou a responder. Obrigado pela paciencia.")
        self.assertEqual((sent["state"], sent["last_error"]), ("enviada", None))
        delivered = self.smtp.delivered()
        self.assertEqual(len(delivered), 1)
        out = email.message_from_bytes(delivered[0]["data"], policy=email.policy.default)
        self.assertEqual((out["In-Reply-To"], out["To"], out["Message-ID"]), ("<a1@cliente.invalid>", CUSTOMER, sent["message_id"]))
        resolved = self.flow.close_if_verified(self.ca, case["id"], "Site verificado apos a renovacao e cliente informado")
        self.assertEqual((resolved["state"], resolved["owner_agent_id"]), ("resolvida", self.suporte["id"]))
        self.assertEqual(sorted(e["direction"] for e in [t["item"] for t in self.cases.timeline(self.ca, case["id"]) if t["tipo"] == "entrada"
                                                           and t["item"]["entry_type"] == "mensagem_externa"]), ["entrada", "saida"])
        self.assertEqual(self.cases.list_cases(self.cb, limit=100), [])
        self.assertEqual([s["message_id"] for s in self.ingest.sources_for_case(self.ca, case["id"])], ["<a1@cliente.invalid>"])


class ScenarioB(Scenario):
    """A purely financial request: transfer accepted by Financeiro, who answers the customer; Suporte is never involved."""

    def test_a_billing_request_is_transferred_answered_and_resolved_without_a_return_trip(self):
        text = "Preciso da segunda via da fatura de maio, por favor."
        self.put_mail(1, mail("Fatura de maio", f"Cliente Um <{CUSTOMER}>", text, "<b1@cliente.invalid>"))
        self.receive()
        case = self.all_cases()[0]
        decision = self.run_triage(case, text)
        self.assertEqual((decision["decision"], decision["capabilities"]), ("rotear", ["financeiro"]))
        fin = self.ctx(self.financeiro)
        self.handoffs.accept(fin, self.pending_transfer(case)["id"])
        case = self.case(case["id"])
        self.assertEqual(case["owner_agent_id"], self.financeiro["id"])
        if case["state"] != "em_execucao":
            case = self.cases.transition(fin, case["id"], case["revision"], "em_execucao")
        self.finance.import_csv(self.ca, "tipo,contraparte,beneficiario,pagador,documento,valor,moeda,vencimento,origem,data_da_fonte\n"
                                "cobranca_cliente,Cliente Um,,Cliente Um,COB-5,990.00,BRL,2026-05-20,erp,2026-05-01\n", "fixture do cenario B")
        analysis = self.finance.analyze(self.a["id"], "Cliente Um", document="COB-5")
        self.assertEqual([a["acao"] for a in analysis["propostas"]], ["propor_cobranca_ao_cliente"])
        self.assertFalse(analysis["executado"])
        self.cases.add_evidence(fin, case["id"], source="financeiro:analise", summary="Analise do registro COB-5 sem achados bloqueantes.",
                                evidence_type="resultado_ferramenta", reliability="verificada", content_hash=content_hash(analysis))
        sent = self.reply_to_customer(self.financeiro, case, "Segue a segunda via da fatura de maio, no valor de R$ 990,00.")
        self.assertEqual(sent["state"], "enviada")
        resolved = self.cases.transition(fin, case["id"], self.case(case["id"])["revision"], "resolvida", reason="Segunda via enviada ao cliente")
        self.assertEqual((resolved["state"], resolved["owner_agent_id"]), ("resolvida", self.financeiro["id"]))
        handoffs = self.handoffs.list_handoffs(self.ca, case_id=case["id"])
        self.assertEqual([(h["handoff_kind"], h["state"]) for h in handoffs], [("transferencia", "aceito")])  # no delegation, no return
        self.assertNotIn("renovacao:", self.timeline_text(case))
        self.assertEqual(len(self.smtp.delivered()), 1)


class DenialAndTakeover(Scenario):
    def test_a_denied_renewal_never_renews_never_resolves_and_still_informs_the_customer(self):
        case = self.start_site_case()
        self.delegate_to_financeiro(case)
        approval = self.propose(case)
        self.approvals.deny(self.ca, approval["id"], "Cliente nao autorizou o gasto")
        status = self.flow.status(self.ca, case["id"])
        self.assertEqual(status["etapa"], "aprovacao_negada")
        with self.assertRaises(FlowRejected):
            self.flow.record_manual_renewal(self.ca, case["id"], approval["id"], reference="X", amount_cents=8990, currency="BRL", paid_at=self.clock.stamp())
        self.flow.return_to_support(self.ctx(self.financeiro), self.financeiro_task(case)["id"], "Aprovacao negada pelo operador; nenhuma renovacao foi feita.")
        sent = self.reply_to_customer(self.suporte, case, "O dominio esta vencido e a renovacao ainda depende de autorizacao. Podemos ajudar a decidir?")
        self.assertEqual(sent["state"], "enviada")
        with self.assertRaises(FlowRejected):
            self.flow.close_if_verified(self.ca, case["id"], "tentativa")
        self.assertNotEqual(self.case(case["id"])["state"], "resolvida")
        self.assertEqual(self.approvals.get(self.ca, approval["id"])["effect_outcome"], None)  # nothing was spent

    def test_a_person_taking_over_stops_the_agents_and_withdraws_their_approvals(self):
        case = self.start_site_case()
        self.delegate_to_financeiro(case)
        approval = self.propose(case)
        taken = self.control.take_over(self.ca, case["id"], self.case(case["id"])["revision"], "Vou ligar para o cliente")
        self.assertTrue(taken["operator_control"])
        self.assertEqual(self.approvals.get(self.ca, approval["id"])["state"], "cancelada")
        with self.assertRaises(NotFound):
            self.outbound.create_draft(self.ctx(self.suporte), case["id"], account_id=self.smtp_conn["id"], to=[CUSTOMER], body="Nao devo enviar")
        with self.assertRaises(NotFound):
            self.flow.propose(self.ctx(self.financeiro), case["id"], DOMAIN, counterparty="Registro Exemplo", amount_cents=8990, currency="BRL")
        self.assertEqual(self.smtp.sessions, [])
        returned = self.control.return_to_team(self.ca, case["id"], self.case(case["id"])["revision"], "Cliente de acordo")
        self.assertFalse(returned["operator_control"])
        again = self.outbound.create_draft(self.ctx(self.suporte), case["id"], account_id=self.smtp_conn["id"], to=[CUSTOMER], body="Agora posso responder")
        self.assertEqual(again[0]["state"], "rascunho")


class AmbiguousCustomer(Scenario):
    def test_an_unclear_request_is_asked_back_and_the_reply_joins_the_same_case(self):
        text = "Preciso de ajuda com uma coisa aqui"
        self.classifier.answers[text] = {"capacidades": [{"nome": "suporte-tecnico", "confianca": 45}], "pergunta": "Qual e o assunto: cobranca, site ou outro?"}
        self.put_mail(1, mail("Ajuda", f"Cliente Um <{CUSTOMER}>", text, "<c1@cliente.invalid>"))
        self.receive()
        case = self.all_cases()[0]
        decision = self.run_triage(case, text)
        self.assertEqual((decision["decision"], decision["method"], decision["rounds"]), ("esclarecer", "llm", 1))
        self.assertEqual((self.case(case["id"])["state"], self.case(case["id"])["owner_agent_id"]), ("aguardando_cliente", self.secretaria["id"]))
        asked = self.reply_to_customer(self.secretaria, case, decision["question"])
        self.assertEqual(asked["state"], "enviada")
        self.put_mail(2, mail("Re: Ajuda", CUSTOMER, "E sobre o meu site, o dominio parece ter vencido.", "<c2@cliente.invalid>",
                              In_Reply_To=asked["message_id"], References=f"<c1@cliente.invalid> {asked['message_id']}"))
        report = self.receive()
        self.assertEqual(report["por_classificacao"], {"resposta": 1})
        self.assertEqual(len(self.all_cases()), 1)  # the reply joined the conversation by headers, not by subject
        second = self.run_triage(self.case(case["id"]), "E sobre o meu site, o dominio parece ter vencido.")
        self.assertEqual((second["decision"], second["capabilities"], second["rounds"]), ("rotear", ["suporte-tecnico"], 2))
        self.assertEqual(len(self.classifier.calls), 1)  # the model was asked once; the second round was decided by rules
        self.assertEqual(len(self.outgoing(case)), 1)


class LoopsAndDuplicates(Scenario):
    def test_our_own_mail_auto_replies_and_redelivered_emails_never_create_work(self):
        case = self.start_site_case()
        self.delegate_to_financeiro(case)
        approval = self.propose(case)
        self.approve(approval)
        self.renew_by_hand(case, approval)
        self.healthy_web()
        self.flow.reverify(self.ca, case["id"], DOMAIN)
        self.flow.return_to_support(self.ctx(self.financeiro), self.financeiro_task(case)["id"], "Renovacao confirmada por uma pessoa; aguardando validacao.")
        sent = self.reply_to_customer(self.suporte, case, "Seu dominio foi renovado e o site voltou.")
        before = (len(self.all_cases()), len(self.outgoing(case)), len(self.smtp.sessions))
        # the mailbox now receives: a copy of our own reply, the customer's out-of-office, and the original email delivered again
        self.put_mail(2, self.smtp.delivered()[0]["data"])
        self.put_mail(3, mail("Resposta automatica: ferias", CUSTOMER, "Estou fora ate segunda.", "<auto1@cliente.invalid>", Auto_Submitted="auto-replied",
                              In_Reply_To=sent["message_id"]))
        self.put_mail(4, mail("Meu site caiu", f"Cliente Um <{CUSTOMER}>", "Meu site esta fora do ar e acho que o dominio venceu.", "<a1@cliente.invalid>"))
        report = self.receive()
        self.assertEqual(report["por_classificacao"], {"ignorada_loop": 1, "ignorada_autoresposta": 1, "duplicada": 1})
        self.assertEqual((len(self.all_cases()), len(self.outgoing(case)), len(self.smtp.sessions)), before)  # no new case, no new reply
        self.assertEqual(self.receive()["processadas"], 0)

    def test_the_same_external_reply_delivered_twice_is_recorded_once(self):
        case = self.start_site_case()
        reply = mail("Re: Meu site caiu", CUSTOMER, "Ainda esta fora do ar.", "<a2@cliente.invalid>", In_Reply_To="<a1@cliente.invalid>", References="<a1@cliente.invalid>")
        self.put_mail(2, reply)
        self.put_mail(3, reply)  # the provider delivered it twice
        report = self.receive()
        self.assertEqual(report["por_classificacao"], {"resposta": 1, "duplicada": 1})
        incoming = [t["item"] for t in self.cases.timeline(self.ca, case["id"]) if t["tipo"] == "entrada" and t["item"]["direction"] == "entrada"]
        self.assertEqual(len(incoming), 2)  # the original and one reply


class Restarts(Scenario):
    def test_a_process_restart_at_every_stage_loses_nothing_and_duplicates_nothing(self):
        self.put_mail(1, mail("Meu site caiu", f"Cliente Um <{CUSTOMER}>", "Meu site esta fora do ar e acho que o dominio venceu.", "<r1@cliente.invalid>"))
        self.receive()
        self.restart()
        self.assertEqual(self.receive()["processadas"], 0)  # the cursor survived
        case = self.all_cases()[0]
        self.run_triage(case, "Meu site esta fora do ar e acho que o dominio venceu.")
        self.restart()
        self.handoffs.accept(self.ctx(self.suporte), self.pending_transfer(case)["id"])
        case = self.case(case["id"])
        if case["state"] != "em_execucao":
            case = self.cases.transition(self.ctx(self.suporte), case["id"], case["revision"], "em_execucao")
        self.restart()
        self.delegate_to_financeiro(case)
        self.restart()
        approval = self.propose(case)
        self.restart()
        self.assertEqual(self.flow.status(self.ca, case["id"])["etapa"], "aprovacao_pendente")
        self.approve(approval)
        self.restart()
        self.renew_by_hand(case, approval)
        self.restart()
        self.healthy_web()
        self.assertEqual(self.flow.reverify(self.ca, case["id"], DOMAIN)["estado"], "resolvido_tecnicamente")
        self.flow.return_to_support(self.ctx(self.financeiro), self.financeiro_task(case)["id"], "Renovacao confirmada por uma pessoa; aguardando validacao.")
        # draft and approve, then the process dies before sending; the restarted one sends exactly once
        draft, _ = self.outbound.create_draft(self.ctx(self.suporte), case["id"], account_id=self.smtp_conn["id"], to=[CUSTOMER], body="Tudo certo com o seu site.")
        submitted = self.outbound.submit(self.ctx(self.suporte), draft["id"])
        pending = self.approvals.get(self.ca, submitted["approval_id"])
        self.approvals.approve(self.ca, pending["id"], pending["payload_hash"])
        self.restart()
        import asyncio
        first = asyncio.run(self.outbound.dispatch(self.a["id"], draft["id"]))
        self.restart()
        second = asyncio.run(self.outbound.dispatch(self.a["id"], draft["id"]))
        self.assertEqual((first["state"], second["state"]), ("enviada", "enviada"))
        self.assertEqual(len(self.smtp.delivered()), 1)
        self.assertEqual(self.flow.close_if_verified(self.ca, case["id"], "Tudo verificado")["state"], "resolvida")
        self.assertEqual(len(self.all_cases()), 1)
        self.assertEqual(len(self.handoffs.list_handoffs(self.ca, case_id=case["id"])), 2)  # one transfer, one delegation


class Provenance(Scenario):
    def test_every_piece_of_evidence_comes_from_an_identified_fixture(self):
        case = self.start_site_case()
        self.delegate_to_financeiro(case)
        with self.repo.transaction(self.a["id"]) as session:
            sources = {e["source"].split(":")[0] + ":" for e in session.list("evidence", limit=1000)}
        self.assertEqual(sources, {"email:", "renovacao:"})
        text = self.timeline_text(case)
        for forbidden in ("@gmail.com", "@outlook.com", "@hotmail.com"):
            self.assertNotIn(forbidden, text)  # only reserved .invalid fixture addresses appear
        self.assertIn(".invalid", text)
        self.assertEqual(self.simulated.sent, [])  # the SMTP-configured account never fell back to the simulated transport


if __name__ == "__main__":
    unittest.main()
