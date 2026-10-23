"""Domain renewal as a tracked flow with manual execution (Support -> Financeiro -> human -> verification).

This module orchestrates services that already exist; it buys and renews nothing:

1. **Support diagnoses** the site (T23) and records the result as technical evidence.
2. Support **delegates a subtask to Financeiro** (T11) with the evidence and a proposal request.
3. **Financeiro asks for approval** (T13) of one exact renewal, or asks for information when the finance records
   show a blocking problem (T24). Nothing it writes claims that a purchase happened.
4. A **person renews outside this system** (no registrar connector yet) and the operator records the confirmation:
   reference, amount, currency and date. The approval is consumed only if amount and currency match what was approved.
5. The site and the registration are **checked again**, telling DNS propagation from a persistent failure.
6. The result goes **back to Support** (the handoff's ``return_to``) for technical validation, or Financeiro takes the
   case over when only billing remains. The case is closed only after a verification that is newer than the
   confirmation; denial, failure or timeout never closes it as resolved.

Stage, blockers and next step are derived from records already stored (approvals, handoffs, tagged evidence), so there
is no extra state to drift.
"""
import re
from datetime import datetime, timedelta, timezone

from .access import AccessContext, NotFound, PermissionDenied
from .cases import CaseRejected, clean_text
from .diagnostics import DiagnosticRefused, normalize_host
from .schema import content_hash

ACTION = "renovar_dominio"
PROPAGATION_HOURS = 24
# Categories a fresh renewal can still explain by propagation; HTTP/TLS/server problems are not renewal problems.
PROPAGATING = ("dns_sem_resposta_dominio_vigente", "suspeita_de_expiracao", "dominio_expirado", "dominio_retido",
               "dominio_nao_encontrado", "dns_instavel")
RENEWAL_CATEGORIES = ("dominio_expirado", "dominio_retido", "suspeita_de_expiracao", "dns_sem_resposta_dominio_vigente", "dominio_nao_encontrado")
TAGS = {"diagnostico": "renovacao:diagnostico", "recibo": "renovacao:recibo", "verificacao": "renovacao:verificacao"}
NOTICE_MANUAL = ("Nao ha conta de registrador configurada: a renovacao e feita manualmente por uma pessoa. "
                 "Este sistema nao comprou nem renovou nada.")
NOTICE_REGISTRAR = ("Existe uma conexao de registrador, mas esta entrega nao executa renovacoes: a execucao continua manual "
                    "(o conector e a T26). Este sistema nao comprou nem renovou nada.")


class FlowRejected(ValueError):
    """The step is not allowed in the current situation; the message is safe to show."""


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _tags(summary):
    return dict(part.split("=", 1) for part in summary.split(";") if "=" in part)


class RenewalFlow:
    def __init__(self, repository, access, cases, handoffs, approvals, diagnostics, finance=None,
                 clock=lambda: datetime.now(timezone.utc)):
        """``diagnostics`` is a callable ``tenant_id -> Diagnostics`` so authorization and the network stay per company."""
        self.repository, self.access, self.cases, self.handoffs, self.approvals = repository, access, cases, handoffs, approvals
        self.diagnostics, self.finance, self.clock = diagnostics, finance, clock

    def _ctx(self, ctx):
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()

    def _operator(self, ctx):
        self._ctx(ctx)
        if ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied()

    @staticmethod
    def _host(domain):
        try:
            return normalize_host(domain)
        except DiagnosticRefused as refused:
            raise FlowRejected(str(refused)) from None

    def _evidence(self, ctx, case_id, tag, summary, payload):
        # collected_at orders the flow: never let two records of the same kind share a second, or "latest" would be a coin toss
        previous = self._latest(ctx, case_id, tag)
        moment = self.clock()
        if previous is not None and _parse(previous["collected_at"]) >= moment:
            moment = _parse(previous["collected_at"]) + timedelta(seconds=1)
        return self.cases.add_evidence(ctx, case_id, source=TAGS[tag], summary=summary, evidence_type="resultado_ferramenta",
                                       reliability="verificada", content_hash=content_hash(payload), collected_at=_stamp(moment))

    def registrar_notice(self, tenant_id):
        with self.repository.transaction(tenant_id) as session:
            configured = [c for c in session.list("connection", limit=1000) if c["provider"] == "registrador" and c["status"] == "ativo"]
        return NOTICE_REGISTRAR if configured else NOTICE_MANUAL

    # -- 1. Support: diagnose and open the financial subtask ----------------------------------------------------------------
    def diagnose(self, ctx, case_id, domain):
        """Run the read-only diagnosis (T23) and keep it as technical evidence of the case."""
        self._ctx(ctx)
        host = self._host(domain)
        try:
            result = self.diagnostics(ctx.tenant_id).diagnose(host)
        except DiagnosticRefused as refused:
            raise FlowRejected(str(refused)) from None
        summary = (f"categoria={result['categoria']};certeza={result['certeza']};dominio={host};em={result['diagnostico_em']};"
                   f"resumo={clean_text(result['resumo'])[:300]}")
        evidence = self._evidence(ctx, case_id, "diagnostico", summary, result)
        return {"categoria": result["categoria"], "certeza": result["certeza"], "resumo": result["resumo"],
                "proximo_passo": result["proximo_passo"], "evidencia": evidence["id"],
                "renovacao_em_questao": result["categoria"] in RENEWAL_CATEGORIES}

    def open_financial_subtask(self, ctx, case_id, domain, financeiro_agent_id, *, diagnosis_evidence_id):
        """Support asks Financeiro to evaluate a renewal. The case keeps its owner; the result returns to Support."""
        self._ctx(ctx)
        host = self._host(domain)
        with self.repository.transaction(ctx.tenant_id) as session:
            evidence = session.get("evidence", diagnosis_evidence_id) if isinstance(diagnosis_evidence_id, str) else None
            if evidence is None or evidence["case_id"] != case_id or evidence["source"] != TAGS["diagnostico"]:
                raise FlowRejected("Registre o diagnostico tecnico do dominio antes de pedir a avaliacao financeira.")
            facts = _tags(evidence["summary"])
            case = session.get("case", case_id)
        if facts.get("dominio") != host:
            raise FlowRejected("O diagnostico registrado e de outro dominio.")
        if facts.get("categoria") not in RENEWAL_CATEGORIES:
            raise FlowRejected("O diagnostico nao aponta problema de registro do dominio; a renovacao nao esta em questao.")
        text = (f"Avaliar a renovacao do dominio {host}. Diagnostico: {facts['categoria']} (certeza {facts['certeza']}). "
                "Nada foi comprado nem renovado; peca aprovacao ou informacoes.")
        handoff, _ = self.handoffs.delegate(
            ctx, case_id, financeiro_agent_id, reason="Possivel renovacao de dominio", objective=f"Avaliar renovacao de {host}",
            expected_result="Proposta de renovacao com valor e moeda para aprovacao, ou lista de informacoes necessarias",
            summary=text, evidence_ids=[diagnosis_evidence_id], return_to=case["owner_agent_id"])
        return handoff

    # -- 2. Financeiro: ask for approval or for information -------------------------------------------------------------
    def propose(self, ctx, case_id, domain, *, counterparty, amount_cents, currency, document=None, period_months=12):
        """Financeiro proposes one exact renewal for approval, unless the finance records block it."""
        self._ctx(ctx)
        host = self._host(domain)
        if self.finance is not None:
            analysis = self.finance.analyze(ctx.tenant_id, counterparty, document=document)
            blockers = [f for f in analysis["achados"] if f["bloqueia"]]
            if blockers:
                questions = analysis["esclarecimentos"] or ["Os registros financeiros tem pendencias; confirme os dados antes de propor."]
                return {"acao": "pedir_informacoes", "perguntas": questions, "bloqueios": sorted({f["codigo"] for f in blockers}),
                        "mensagem": "Preciso de informacoes antes de pedir aprovacao. Nenhuma renovacao foi feita nem sera feita sem aprovacao.",
                        "aprovacao": None}
        params = {"dominio": host, "periodo_meses": int(period_months), "execucao": "manual", "contraparte": clean_text(counterparty)[:120]}
        approval, created = self.approvals.request(
            ctx, case_id, ACTION, resource=f"dominio:{host}", amount_cents=amount_cents, currency=currency.lower() if currency else None,
            params=params, justification=f"Proposta de renovacao do dominio {host} por {period_months} meses; execucao manual por uma pessoa.")
        return {"acao": "aprovacao_solicitada", "aprovacao": approval["id"], "nova_solicitacao": created,
                "mensagem": (f"Solicitei aprovacao para renovar {host}. A renovacao AINDA NAO foi feita e depende da decisao humana."),
                "perguntas": [], "bloqueios": []}

    # -- 3. A person renews; the operator records it ----------------------------------------------------------------
    def record_manual_renewal(self, ctx, case_id, approval_id, *, reference, amount_cents, currency, paid_at, registrar=None):
        """Record the human confirmation (reference, amount, date) against one approved renewal. Consumes the approval."""
        self._operator(ctx)
        reference = clean_text(reference)[:120]
        try:
            moment = _parse(paid_at)
        except ValueError:
            raise FlowRejected("Data invalida (use AAAA-MM-DDTHH:MM:SSZ).") from None
        with self.repository.transaction(ctx.tenant_id) as session:
            approval = session.get("approval_request", approval_id) if isinstance(approval_id, str) else None
        if approval is None or approval["case_id"] != case_id or approval["action"] != ACTION:
            raise NotFound()
        if approval["state"] != "aprovada":
            raise FlowRejected("Somente uma renovacao aprovada pode ter a execucao manual registrada.")
        if amount_cents != approval["amount_cents"] or (currency or "").lower() != (approval["currency"] or ""):
            raise FlowRejected("Valor ou moeda diferem do que foi aprovado. Peca uma nova aprovacao com os dados reais.")
        if moment > self.clock() + timedelta(minutes=5):
            raise FlowRejected("A data da renovacao esta no futuro.")
        normalized = approval["normalized_action"]
        try:
            self.approvals.consume(ctx, approval_id, {"case_id": case_id, "action": ACTION, "resource": normalized["resource"],
                                                      "recipients": normalized["recipients"], "amount_cents": normalized["amount_cents"],
                                                      "currency": normalized["currency"], "params": normalized["params"]})
        except Exception as error:
            raise FlowRejected("A aprovacao nao pode ser usada agora (expirada, ja usada ou a demanda mudou).") from error
        detail = f"Renovacao informada por pessoa: referencia {reference}, {amount_cents} centavos {currency.upper()}, em {_stamp(moment)}."
        self.approvals.record_effect(ctx, approval_id, "confirmado", detail)
        summary = (f"referencia={reference};valor_centavos={amount_cents};moeda={currency.upper()};pago_em={_stamp(moment)};"
                   f"registrador={clean_text(registrar)[:80] if registrar else '-'};aprovacao={approval_id};dominio={normalized['params']['dominio']};"
                   "origem=confirmacao_humana_nao_verificada_no_registrador")
        evidence = self.cases.add_evidence(ctx, case_id, source=TAGS["recibo"], summary=summary, evidence_type="recibo",
                                           reliability="declarada", collected_at=_stamp(moment))
        return {"evidencia": evidence["id"], "mensagem": "Confirmacao humana registrada. Verifique o dominio agora para confirmar o efeito."}

    # -- 4. Verify again --------------------------------------------------------------------------------------------
    def reverify(self, ctx, case_id, domain):
        """Check DNS/TLS/HTTP and the registry again and say whether we are propagating, healthy or still failing."""
        self._ctx(ctx)
        host = self._host(domain)
        receipt = self._latest(ctx, case_id, "recibo")
        diagnostics = self.diagnostics(ctx.tenant_id)
        try:
            result = diagnostics.diagnose(host)
            registry = diagnostics.check_registration(host)
        except DiagnosticRefused as refused:
            raise FlowRejected(str(refused)) from None
        category, now = result["categoria"], self.clock()
        if receipt is None:
            state, detail = "sem_confirmacao_de_renovacao", "Nao ha confirmacao humana de renovacao registrada; a verificacao e apenas informativa."
        elif category == "site_ok":
            state, detail = "resolvido_tecnicamente", "DNS, TLS e HTTP respondem normalmente apos a renovacao informada."
        else:
            paid = _parse(_tags(receipt["summary"])["pago_em"])
            if category in PROPAGATING and now - paid < timedelta(hours=PROPAGATION_HOURS):
                state, detail = "em_propagacao", (f"Renovacao informada ha menos de {PROPAGATION_HOURS} h e o dominio ainda falha "
                                                  f"({category}); pode ser propagacao. Verifique de novo depois de {_stamp(paid + timedelta(hours=PROPAGATION_HOURS))}.")
            else:
                state, detail = "falha_persistente", f"O problema continua ({category}) e nao se explica por propagacao."
        summary = (f"estado={state};categoria={category};certeza={result['certeza']};dominio={host};em={_stamp(now)};"
                   f"expira_em={registry.get('expira_em') or '-'};detalhe={clean_text(detail)[:300]}")
        evidence = self._evidence(ctx, case_id, "verificacao", summary, {"resultado": result, "registro": registry})
        return {"estado": state, "categoria": category, "detalhe": detail, "expira_em": registry.get("expira_em"),
                "proximo_passo": result["proximo_passo"], "evidencia": evidence["id"]}

    # -- 5. Return / take over / close ------------------------------------------------------------------------------
    def return_to_support(self, ctx, task_id, summary):
        """Financeiro returns its result to Support for technical validation (the handoff's return target)."""
        text = clean_text(summary)
        if re.search(r"(?i)\b(renovad[oa]|comprad[oa]|pag[oa]|quitad[oa])\b", text) and not re.search(r"(?i)\b(nao|ainda nao|sem)\b", text):
            raise FlowRejected("Nao afirme renovacao ou pagamento sem confirmacao humana registrada no sistema.")
        return self.handoffs.complete_task(ctx, task_id, text)

    def hand_over_billing(self, ctx, case_id, financeiro_agent_id, summary):
        """Support proposes giving the case to Financeiro when only billing is left; Financeiro still has to accept."""
        self._ctx(ctx)
        handoff, _ = self.handoffs.propose_transfer(ctx, case_id, financeiro_agent_id, reason="Resta apenas a cobranca",
                                                    summary=clean_text(summary))
        return handoff

    def status(self, ctx, case_id):
        """Where the flow stands, what blocks closing, and what to do next. Read-only."""
        self._ctx(ctx)
        diagnosis, receipt, verification = (self._latest(ctx, case_id, t) for t in ("diagnostico", "recibo", "verificacao"))
        with self.repository.transaction(ctx.tenant_id) as session:
            case = session.get("case", case_id)
            if case is None:
                raise NotFound()
            approvals = [a for a in session.list("approval_request", case_id=case_id, limit=1000) if a["action"] == ACTION]
            handoffs = session.list("handoff", case_id=case_id, limit=1000)
        verified = _tags(verification["summary"]) if verification else {}
        fresh = bool(verification and receipt and _parse(verification["collected_at"]) >= _parse(receipt["collected_at"]))
        last = approvals[-1] if approvals else None
        blockers = []
        if diagnosis is None:
            stage, step = "sem_diagnostico", "Rodar o diagnostico do dominio."
            blockers.append("sem diagnostico tecnico")
        elif last is None:
            stage, step = "diagnosticado", "Support delega a avaliacao ao Financeiro."
            blockers.append("sem aprovacao de renovacao")
        elif last["state"] == "pendente":
            stage, step = "aprovacao_pendente", "Aguardar a decisao humana."
            blockers.append("aprovacao pendente")
        elif last["state"] in ("negada", "expirada", "cancelada"):
            stage, step = "aprovacao_" + last["state"], "Sem renovacao aprovada: reavaliar com o cliente ou pedir nova aprovacao."
            blockers.append(f"aprovacao {last['state']}")
        elif receipt is None:
            stage, step = "aprovada_aguardando_execucao_manual", "Uma pessoa deve renovar fora do sistema e registrar a confirmacao."
            blockers.append("renovacao ainda nao confirmada por uma pessoa")
        elif not fresh:
            stage, step = "renovacao_confirmada_aguardando_verificacao", "Verificar o dominio e o site novamente."
            blockers.append("sem verificacao posterior a confirmacao")
        else:
            stage = verified.get("estado", "verificado")
            step = {"resolvido_tecnicamente": "Support valida e o operador encerra a demanda.",
                    "em_propagacao": "Aguardar a propagacao e verificar de novo.",
                    "falha_persistente": "Investigar o problema persistente (nameservers, hospedagem, registrador)."}.get(stage, "Revisar.")
            if stage != "resolvido_tecnicamente":
                blockers.append(f"verificacao: {stage}")
        if case["state"] in ("resolvida", "cancelada", "falhou"):
            blockers = []
        return {"etapa": stage, "proximo_passo": step, "bloqueios_para_encerrar": blockers, "pode_encerrar": not blockers and case["state"] not in ("resolvida", "cancelada", "falhou"),
                "execucao": "manual", "aviso": self.registrar_notice(ctx.tenant_id), "aprovacoes": [a["state"] for a in approvals],
                "encaminhamentos": [h["state"] for h in handoffs], "demanda": case["state"]}

    def close_if_verified(self, ctx, case_id, summary):
        """Resolve the case only when a verification newer than the human confirmation shows the site healthy."""
        self._operator(ctx)
        status = self.status(ctx, case_id)
        if not status["pode_encerrar"]:
            raise FlowRejected("Ainda nao e possivel encerrar: " + "; ".join(status["bloqueios_para_encerrar"] or [f"demanda {status['demanda']}"]) + ".")
        case = self.cases.get_case(ctx, case_id)
        try:
            return self.cases.transition(ctx, case_id, case["revision"], "resolvida", reason=clean_text(summary))
        except CaseRejected as error:
            raise FlowRejected(str(error)) from None

    def _latest(self, ctx, case_id, tag):
        with self.repository.transaction(ctx.tenant_id) as session:
            if session.get("case", case_id) is None:
                raise NotFound()
            found = [e for e in session.list("evidence", case_id=case_id, limit=1000) if e["source"] == TAGS[tag]]
        return max(found, key=lambda e: (e["collected_at"], e["created_at"], e["id"]), default=None)
