"""Deterministic gates: authorization, approval, single owner, duplicate sending and leakage between companies.

Every check builds its own case in the shared fictitious world and returns ``None`` when the system held, or a short
sentence saying what it allowed. A gate passes only with **zero** violations. This proves the listed scenarios behave;
it does not prove the system is invulnerable.
"""
import asyncio
import json
from pathlib import Path

from team.access import AccessContext, NotFound, PermissionDenied
from team.approvals import ApprovalInvalid, ApprovalRejected
from team.cases import CaseRejected
from team import overview
from team.outbound import Busy, OutboundError

CORPUS = Path(__file__).resolve().parent / "corpus"
CHECKS = {"autorizacao": [], "aprovacao": [], "dono_unico": []}


def corpus(name):
    return json.loads((CORPUS / f"{name}.json").read_text(encoding="utf-8"))


def check(suite):
    def register(function):
        CHECKS[suite].append(function)
        return function
    return register


def refused(call, *errors):
    """``None`` when ``call`` raised one of ``errors``; otherwise a description of what happened instead."""
    try:
        call()
    except errors:
        return None
    except Exception as error:  # a different failure is not the refusal we expect
        return f"falhou de outro jeito: {type(error).__name__}"
    return "foi permitido"


def must_refuse(what, call, *errors):
    outcome = refused(call, *errors)
    return None if outcome is None else f"{what}: {outcome}"


# -- authorization ----------------------------------------------------------------------------------------------------------
@check("autorizacao")
def agente_sem_concessao_nao_le_demanda(w):
    case = w.working_case()
    return must_refuse("agente sem concessao leu a demanda", lambda: w.cases.get_case(w.agent_ctx(w.sem_acesso), case["id"]), NotFound)


@check("autorizacao")
def agente_nao_cria_concessao(w):
    ctx = w.agent_ctx()
    return must_refuse("agente criou uma concessao de acesso", lambda: w.team.create(
        ctx, "access_grant", principal_id=w.sem_acesso["id"], resource_kind="case", resource_id=None, actions=["read"],
        granted_by=ctx.principal_id, expires_at=None), PermissionDenied)


@check("autorizacao")
def agente_nao_aprova(w):
    approval = w.request_approval(w.working_case())
    return must_refuse("agente aprovou", lambda: w.approvals.approve(w.agent_ctx(), approval["id"], approval["payload_hash"]), PermissionDenied)


@check("autorizacao")
def leitor_nao_escreve(w):
    reader = w.member("leitor@exemplo.invalid", "leitor")
    return must_refuse("leitor abriu demanda", lambda: w.cases.open_case(reader, department_id=w.dep["id"], objective="Nao deveria"), PermissionDenied)


@check("autorizacao")
def atendente_nao_aprova(w):
    clerk = w.member("atendente@exemplo.invalid", "atendente")
    approval = w.request_approval(w.working_case())
    return must_refuse("atendente aprovou", lambda: w.approvals.approve(clerk, approval["id"], approval["payload_hash"]), PermissionDenied)


@check("autorizacao")
def atendente_nao_configura(w):
    clerk = w.member("atendente2@exemplo.invalid", "atendente")
    return must_refuse("atendente criou departamento", lambda: w.team.create(clerk, "department", name="Novo", scope="x", status="ativo"), PermissionDenied)


@check("autorizacao")
def membro_limitado_por_departamento(w):
    case = w.working_case()
    limited = w.member("financas@exemplo.invalid", "atendente", [w.dep2["id"]])
    return must_refuse("membro limitado leu demanda de outro departamento", lambda: w.cases.get_case(limited, case["id"]), NotFound)


@check("autorizacao")
def contexto_so_pelo_backend(w):
    return must_refuse("contexto de acesso foi forjado", lambda: AccessContext(w.a["id"], "usr_" + "1" * 32, "operator", object()), PermissionDenied)


@check("autorizacao")
def empresa_do_cliente_e_ignorada(w):
    return must_refuse("o corpo escolheu a empresa", lambda: w.team.create(w.ca, "department", name="X", scope="x", status="ativo", tenant_id=w.b["id"]), ValueError)


@check("autorizacao")
def sessao_revogada_deixa_de_valer(w):
    w.identity.invite(w.ca, w.a["id"], "sessao@exemplo.invalid", "admin")
    token, _ = w.identity.sign_in("sub-sessao", "sessao@exemplo.invalid")
    identity = w.identity.resolve(token)
    w.identity.revoke_sessions(w.op, identity.user_id)
    return None if w.identity.resolve(token) is None else "sessao revogada continuou valendo"


@check("autorizacao")
def papel_alterado_vale_na_hora(w):
    w.identity.invite(w.ca, w.a["id"], "papel@exemplo.invalid", "atendente")
    token, _ = w.identity.sign_in("sub-papel", "papel@exemplo.invalid")
    identity = w.identity.resolve(token)
    w.identity.set_role(w.ca, w.a["id"], identity.user_id, "leitor")
    ctx = w.identity.context(identity, w.a["id"])
    return must_refuse("papel antigo continuou valendo", lambda: w.cases.open_case(ctx, department_id=w.dep["id"], objective="x"), PermissionDenied)


@check("autorizacao")
def worker_nao_aprova_nem_administra(w):
    worker = w.access.service_context(w.a["id"], "worker")
    approval = w.request_approval(w.working_case())
    return (must_refuse("worker aprovou", lambda: w.approvals.approve(worker, approval["id"], approval["payload_hash"]), PermissionDenied)
            or must_refuse("worker gerenciou membros", lambda: w.identity.invite(worker, w.a["id"], "x@exemplo.invalid", "leitor"), PermissionDenied))


# -- approval --------------------------------------------------------------------------------------------------------------
def _approved(w, **options):
    case = w.working_case()
    approval = w.request_approval(case)
    return case, w.approvals.approve(w.ca, approval["id"], approval["payload_hash"])


@check("aprovacao")
def payload_alterado_e_recusado(w):
    case, approval = _approved(w)
    exact = w.payload(approval)
    for change in ({"recipients": ["outro@exemplo.invalid"]}, {"params": {"assunto": "Outro"}}, {"amount_cents": 100, "currency": "brl"},
                   {"resource": "con_y"}, {"action": "enviar_mensagem"}, {"case_id": "cas_" + "0" * 32}):
        problem = refused(lambda change=change: w.approvals.consume(w.ca, approval["id"], {**exact, **change}), ApprovalInvalid)
        if problem:
            return f"payload alterado ({', '.join(change)}): {problem}"
    return None


@check("aprovacao")
def aprovacao_vale_uma_vez(w):
    case, approval = _approved(w)
    w.approvals.consume(w.ca, approval["id"], w.payload(approval))
    return must_refuse("a mesma aprovacao foi usada duas vezes", lambda: w.approvals.consume(w.ca, approval["id"], w.payload(approval)), ApprovalInvalid)


@check("aprovacao")
def aprovacao_expirada_nao_vale(w):
    case = w.working_case()
    approval, _ = w.approvals.request(w.ca, case["id"], action="enviar_email", resource="con_x", recipients=["c@exemplo.invalid"],
                                      params={"assunto": "Oi"}, ttl_seconds=60)
    approval = w.approvals.approve(w.ca, approval["id"], approval["payload_hash"])
    w.clock.advance(minutes=10)
    return must_refuse("aprovacao expirada foi usada", lambda: w.approvals.consume(w.ca, approval["id"], w.payload(approval)), ApprovalInvalid)


@check("aprovacao")
def aprovacao_revogada_nao_vale(w):
    case, approval = _approved(w)
    w.approvals.revoke(w.ca, approval["id"], "mudou de ideia")
    return must_refuse("aprovacao revogada foi usada", lambda: w.approvals.consume(w.ca, approval["id"], w.payload(approval)), ApprovalInvalid)


@check("aprovacao")
def aprovacao_negada_ou_pendente_nao_vale(w):
    case = w.working_case()
    denied = w.request_approval(case, n=1)
    w.approvals.deny(w.ca, denied["id"], "nao")
    pending = w.request_approval(case, n=2)
    return (must_refuse("aprovacao negada foi usada", lambda: w.approvals.consume(w.ca, denied["id"], w.payload(denied)), ApprovalInvalid)
            or must_refuse("aprovacao pendente foi usada", lambda: w.approvals.consume(w.ca, pending["id"], w.payload(pending)), ApprovalInvalid))


@check("aprovacao")
def aprovacao_de_uma_demanda_nao_serve_a_outra(w):
    case, approval = _approved(w)
    other = w.working_case()
    return must_refuse("aprovacao serviu a outra demanda", lambda: w.approvals.consume(w.ca, approval["id"], {**w.payload(approval), "case_id": other["id"]}), ApprovalInvalid)


@check("aprovacao")
def hash_mostrado_diferente_recusa_a_aprovacao(w):
    approval = w.request_approval(w.working_case())
    return must_refuse("aprovou com um hash diferente do mostrado", lambda: w.approvals.approve(w.ca, approval["id"], "0" * 64), ApprovalInvalid, ApprovalRejected)


@check("aprovacao")
def outra_empresa_nao_consome(w):
    case, approval = _approved(w)
    return must_refuse("outra empresa consumiu a aprovacao", lambda: w.approvals.consume(w.cb, approval["id"], w.payload(approval)), ApprovalInvalid, NotFound)


# -- single owner ----------------------------------------------------------------------------------------------------------
@check("dono_unico")
def nao_dono_nao_muda_o_estado(w):
    case = w.working_case()
    return must_refuse("quem nao e dono mudou o estado", lambda: w.cases.transition(w.agent_ctx(w.financeiro), case["id"], case["revision"], "pausada",
                                                                                      reason="x"), PermissionDenied)


@check("dono_unico")
def nao_dono_nao_fala_com_o_cliente(w):
    case = w.working_case()
    return must_refuse("quem nao e dono falou com o cliente", lambda: w.cases.add_message(w.agent_ctx(w.financeiro), case["id"], "saida", "email", "oi"),
                       PermissionDenied)


@check("dono_unico")
def so_o_dono_cria_rascunho(w):
    case = w.working_case()
    return must_refuse("quem nao e dono criou rascunho", lambda: w.draft_as(w.financeiro, case), NotFound, PermissionDenied, OutboundError)


@check("dono_unico")
def transferencia_proposta_nao_move_o_dono(w):
    case = w.working_case()
    w.handoffs.propose_transfer(w.agent_ctx(), case["id"], w.financeiro["id"], reason="Passar", summary="Resumo")
    owner = w.cases.get_case(w.ca, case["id"])["owner_agent_id"]
    return None if owner == w.suporte["id"] else "o dono mudou antes do aceite"


@check("dono_unico")
def depois_do_aceite_o_antigo_dono_perde_o_direito(w):
    case = w.working_case()
    old_ctx, new_ctx = w.agent_ctx(), w.agent_ctx(w.financeiro)
    proposal, _ = w.handoffs.propose_transfer(old_ctx, case["id"], w.financeiro["id"], reason="Passar", summary="Resumo")
    w.handoffs.accept(new_ctx, proposal["id"])
    current = w.cases.get_case(w.ca, case["id"])
    return (must_refuse("o antigo dono ainda age", lambda: w.cases.transition(old_ctx, case["id"], current["revision"], "pausada", reason="x"), PermissionDenied)
            or (None if w.cases.get_case(w.ca, case["id"])["owner_agent_id"] == w.financeiro["id"] else "o novo dono nao assumiu"))


@check("dono_unico")
def atribuir_dono_duas_vezes_e_recusado(w):
    case = w.working_case()
    return must_refuse("a demanda ganhou um segundo dono", lambda: w.cases.assign_owner(w.ca, case["id"], case["revision"], w.financeiro["id"]), CaseRejected)


@check("dono_unico")
def operador_assume_e_o_agente_para(w):
    case = w.working_case()
    w.control.take_over(w.ca, case["id"], case["revision"], "assumo")
    current = w.cases.get_case(w.ca, case["id"])
    return must_refuse("o agente agiu depois que o operador assumiu", lambda: w.cases.transition(w.agent_ctx(), case["id"], current["revision"], "em_execucao"),
                       PermissionDenied)


# -- duplicate sending (scenarios come from the corpus) -----------------------------------------------------------------------
def duplicate_effects(w, scenario, repeats):
    """Run the scenario and return how many times its external effect really happened."""
    case = w.working_case()
    before = len(w.provider.sent)
    if scenario == "rascunho_repetido":
        created = [w.draft(case, dedup_key="chave-fixa")[1] for _ in range(repeats)]
        return sum(1 for item in created if item)
    if scenario == "envio_repetido":
        message = w.approved_message(case)
        for _ in range(repeats):
            w.dispatch(message)
        return len(w.provider.sent) - before
    if scenario == "envio_concorrente":
        message = w.approved_message(case)

        async def burst():
            return await asyncio.gather(*(w.outbound.dispatch(w.a["id"], message["id"]) for _ in range(repeats)), return_exceptions=True)
        results = asyncio.run(burst())
        unexpected = [r for r in results if isinstance(r, Exception) and not isinstance(r, (Busy, OutboundError))]
        if unexpected:
            raise unexpected[0]
        return len(w.provider.sent) - before
    if scenario == "aprovacao_consumida_varias_vezes":
        _, approval = _approved(w)
        wins = 0
        for _ in range(repeats):
            try:
                w.approvals.consume(w.ca, approval["id"], w.payload(approval))
                wins += 1
            except ApprovalInvalid:
                pass
        return wins
    if scenario == "encaminhamento_repetido":
        ids = {w.handoffs.propose_transfer(w.agent_ctx(), case["id"], w.financeiro["id"], reason="Passar", summary="Resumo",
                                           idempotency_key="chave-de-encaminhamento")[0]["id"] for _ in range(repeats)}
        return len(ids)
    if scenario == "importacao_repetida":
        text = ("tipo,contraparte,beneficiario,pagador,documento,referencia,valor,moeda,emissao,vencimento,origem,data_da_fonte,observacao\n"
                "fatura_fornecedor,Hospedagem Alfa,Hospedagem Alfa,Empresa A,FAT-1,,100.00,BRL,2026-05-01,2026-06-10,erp,2026-05-01,\n")
        for _ in range(repeats):
            w.finance.import_csv(w.ca, text, "repeticao")
        return len(w.finance.list_records(w.a["id"]))
    if scenario == "resposta_perdida_nao_reenvia":
        w.provider.mode = "lose_response"
        try:
            message = w.approved_message(case)
            for _ in range(repeats):
                w.dispatch(message)
            return len(w.provider.sent) - before
        finally:
            w.provider.mode = "ok"
    raise ValueError(f"cenario desconhecido: {scenario}")


# -- leakage between companies ------------------------------------------------------------------------------------------------
def leakage_outcome(w, via):
    """Run one attempt of company B against what belongs to company A. ``None`` = held; otherwise what leaked."""
    case = w.working_case()
    approval = w.request_approval(case)
    message = w.approved_message(case)
    proposal, _ = w.handoffs.propose_transfer(w.agent_ctx(), case["id"], w.financeiro["id"], reason="Passar", summary="Resumo")
    document = w.knowledge.publish(w.ca, doc_key="manual", title="Manual interno", body="Segredo da empresa A", source="teste")
    w.finance.import_csv(w.ca, "tipo,contraparte,beneficiario,pagador,documento,referencia,valor,moeda,emissao,vencimento,origem,data_da_fonte,observacao\n"
                               "fatura_fornecedor,Hospedagem Alfa,Hospedagem Alfa,Empresa A,FAT-1,,100.00,BRL,2026-05-01,2026-06-10,erp,2026-05-01,\n", "teste")
    services = type("S", (), {"repo": w.repo, "access": w.access})()
    hidden = (NotFound, PermissionDenied)
    calls = {
        "caso_por_id": lambda: w.cases.get_case(w.cb, case["id"]),
        "caso_detalhe": lambda: overview.snapshot(services, w.cb, case["id"]),
        "eventos_com_cursor": lambda: overview.events_after(services, w.cb, 0, case_id=case["id"]),
        "aprovacao_por_id": lambda: w.approvals.get(w.cb, approval["id"]),
        "mensagem_por_id": lambda: w.outbound.get_message(w.cb, message["id"]),
        "agente_por_id": lambda: w.agents.get_agent(w.cb, w.suporte["id"]),
        "documento_de_conhecimento": lambda: w.knowledge.get_document(w.cb, document["id"]),
        "abrir_demanda_em_departamento_alheio": lambda: w.cases.open_case(w.cb, department_id=w.dep["id"], objective="Cruzado"),
        "atribuir_agente_alheio": lambda: w.cases.assign_owner(
            w.cb, w.cases.open_case(w.cb, department_id=w.dep_b["id"], objective="Sem dono")["id"], 1, w.suporte["id"]),
        "empresa_no_corpo": lambda: w.team.create(w.cb, "department", name="X", scope="x", status="ativo", tenant_id=w.a["id"]),
        "contexto_de_agente_de_outra_empresa": lambda: w.access.agent_context(w.op, w.b["id"], w.agent_ctx_run_id()),
        "membro_de_uma_empresa_em_outra": lambda: w.identity.context(w.outsider_identity(), w.b["id"]),
    }
    lists = {
        "lista_de_demandas": lambda: [c["id"] for c in w.cases.list_cases(w.cb)],
        "lista_de_mensagens": lambda: [m["id"] for m in w.outbound.list_messages(w.cb)],
        "registros_financeiros": lambda: w.finance.list_records(w.b["id"]),
        "encaminhamento_por_id": lambda: [h["id"] for h in w.handoffs.list_handoffs(w.cb, case_id=case["id"])] + (
            [proposal["id"]] if refused(lambda: w.handoffs.accept(w.agent_ctx(tenant="b"), proposal["id"]), NotFound, PermissionDenied) else []),
    }
    if via in lists:
        found = lists[via]()
        return None if not found else f"{via}: a empresa B enxergou {len(found)} item(ns) da A"
    if via not in calls:
        raise ValueError(f"tentativa desconhecida: {via}")
    return must_refuse(f"{via}: a empresa B alcancou o que e da A", calls[via], *hidden, ValueError)
