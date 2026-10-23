"""Financial analysis from records imported on purpose, producing proposals and clarification requests.

Nothing here moves money. The analysis reads records the operator imported (with source and date), keeps
payments the company made, customer charges, supplier quotes, supplier invoices and renewal notices apart,
never invents an amount or currency, and turns suspicious situations into *blocking findings* that become a
clarification request instead of a payment proposal. A proposal is only a structured suggestion: any
external action still needs a human approval (T13) and a specific connector. Text that arrives by email does
not prove identity, beneficiary or authorization.
"""
import csv
import io
import re
from datetime import datetime, timezone

from .access import AccessContext, NotFound, PermissionDenied
from .cases import clean_text
from .enums import RECORD_KINDS, RECORD_ORIGINS
from .ids import new_id
from .repository import party_key
from .schema import content_hash
from .triage import looks_like_injection

MAX_ROWS = 5000
MAX_CSV_BYTES = 1_000_000
MAX_LIST = 50
_AMOUNT = re.compile(r"^\d{1,12}(\.\d{1,2})?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAYABLE, RECEIVABLE = "fatura_fornecedor", "cobranca_cliente"
DOC_TOPICS = "fatura pagamento pagamentos beneficiario fornecedor fornecedores cobranca renovacao orcamento"
GUARANTEES = ("Nenhuma movimentacao bancaria, pagamento ou envio e feito por esta analise.",
              "Pagamentos e cobrancas exigem aprovacao humana e um conector especifico; texto livre nunca executa nada.",
              "Beneficiario, identidade e autorizacao nao sao validados apenas por um email recebido.")
QUESTIONS = {
    "dados_incompletos": "Qual e o valor e a moeda corretos deste registro? A fonte nao informa e nada foi presumido.",
    "cobranca_de_outra_empresa": "Este documento esta em nome de outra empresa. Ele pertence a esta empresa ou foi enviado por engano?",
    "fatura_duplicada": "Ha mais de um registro para o mesmo documento ou valor. Qual deles e o valido e o outro deve ser descartado?",
    "possivel_pagamento_anterior": "Existe pagamento realizado correspondente. Esta cobranca ja foi quitada?",
    "moeda_divergente": "A moeda difere entre os registros relacionados. Qual moeda foi acordada?",
    "beneficiario_alterado": "O beneficiario mudou em relacao ao historico. Confirme por um canal independente do email (telefone conhecido ou ERP) antes de qualquer pagamento.",
}


class FinanceRejected(ValueError):
    """The import or request is not valid; the message is safe to show."""


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _date(value):
    if not _DATE.match(value):
        raise ValueError("data invalida (use AAAA-MM-DD)")
    datetime.strptime(value, "%Y-%m-%d")
    return value + "T00:00:00Z"


def _text(value, limit):
    """Cleaned text, or None when blank (clean_text itself refuses empty input)."""
    value = (value or "").strip()
    return clean_text(value)[:limit] if value else None


def _row_to_record(row, line, batch_id, source, operator_id, tenant_id, now):
    """Validate one CSV row into a ``financial_record`` document; raises ValueError with a reason."""
    kind = (row.get("tipo") or "").strip()
    if kind not in RECORD_KINDS:
        raise ValueError("tipo desconhecido (" + ", ".join(RECORD_KINDS) + ")")
    counterparty = _text(row.get("contraparte"), 200)
    if not counterparty:
        raise ValueError("contraparte obrigatoria")
    amount_text, currency = (row.get("valor") or "").strip(), (row.get("moeda") or "").strip().upper() or None
    amount = None
    if amount_text:
        if not _AMOUNT.match(amount_text):
            raise ValueError("valor ambiguo: use ponto decimal e nenhum separador de milhar (ex.: 1234.56)")
        whole, _, cents = amount_text.partition(".")
        amount = int(whole) * 100 + int((cents + "0")[:2]) if cents else int(whole) * 100
        if currency is None:
            raise ValueError("moeda obrigatoria quando ha valor (ex.: BRL); nada e presumido")
    elif currency is not None:
        raise ValueError("moeda informada sem valor")
    if currency is not None and not re.match(r"^[A-Z]{3}$", currency):
        raise ValueError("moeda invalida (codigo ISO de 3 letras)")
    origin = (row.get("origem") or "importacao").strip()
    if origin not in RECORD_ORIGINS:
        raise ValueError("origem desconhecida (" + ", ".join(RECORD_ORIGINS) + ")")
    source_date = _date((row.get("data_da_fonte") or "").strip())
    note, warning = _text(row.get("observacao"), 300), None
    if note and looks_like_injection(note):
        note, warning = None, "observacao descartada: parece instruir o assistente"
    doc = {"kind": "financial_record", "schema_version": 1, "id": new_id("financial_record"), "tenant_id": tenant_id, "revision": 1,
           "created_at": now, "updated_at": now, "record_kind": kind, "counterparty": counterparty,
           "beneficiary": _text(row.get("beneficiario"), 200), "payer": _text(row.get("pagador"), 200),
           "document_number": _text(row.get("documento"), 80), "reference": _text(row.get("referencia"), 80),
           "amount_cents": amount, "currency": currency, "issued_at": _date(row["emissao"].strip()) if (row.get("emissao") or "").strip() else None,
           "due_at": _date(row["vencimento"].strip()) if (row.get("vencimento") or "").strip() else None, "origin": origin,
           "source": _text(source, 300) or "importacao", "source_date": source_date, "batch_id": batch_id, "imported_by": operator_id,
           "status": "ativo", "note": note}
    return doc, warning


def _content_key(doc):
    return content_hash({k: doc[k] for k in ("record_kind", "counterparty", "beneficiary", "payer", "document_number", "reference",
                                             "amount_cents", "currency", "issued_at", "due_at")})


def _public(doc):
    """What leaves the service: facts and provenance, never the free-text note."""
    return {"id": doc["id"], "tipo": doc["record_kind"], "contraparte": doc["counterparty"], "beneficiario": doc["beneficiary"],
            "pagador": doc["payer"], "documento": doc["document_number"], "referencia": doc["reference"], "valor_centavos": doc["amount_cents"],
            "moeda": doc["currency"], "emissao": doc["issued_at"], "vencimento": doc["due_at"], "origem": doc["origin"],
            "fonte": doc["source"], "data_da_fonte": doc["source_date"]}


class FinanceService:
    def __init__(self, repository, access, knowledge=None, clock=lambda: datetime.now(timezone.utc)):
        self.repository, self.access, self.knowledge, self.clock = repository, access, knowledge, clock

    def _operator(self, ctx):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied()

    # -- import (operator only, explicit) ----------------------------------------------------------------------------
    def import_csv(self, ctx, text, source):
        """Import records from CSV text. Columns: tipo, contraparte, beneficiario, pagador, documento, referencia, valor,
        moeda, emissao, vencimento, origem, data_da_fonte (required), observacao. Invalid rows are reported, not guessed."""
        self._operator(ctx)
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CSV_BYTES:
            raise FinanceRejected("Arquivo grande demais (limite de 1 MB).")
        head = text.splitlines()[0] if text.strip() else ""
        reader = csv.DictReader(io.StringIO(text), delimiter=";" if ";" in head and "," not in head else ",")
        if not reader.fieldnames or "tipo" not in reader.fieldnames or "contraparte" not in reader.fieldnames \
                or "data_da_fonte" not in reader.fieldnames:
            raise FinanceRejected("Cabecalho invalido: informe ao menos tipo, contraparte e data_da_fonte.")
        batch_id, now = new_id("correlation").replace("cor_", "lote-"), _stamp(self.clock())
        imported, rejected, skipped, warnings = [], [], 0, []
        with self.repository.transaction(ctx.tenant_id) as session:
            existing = {_content_key(r) for r in session.list("financial_record", limit=1000)}
            for line, row in enumerate(reader, start=2):
                if line - 1 > MAX_ROWS:
                    rejected.append({"linha": line, "motivo": f"limite de {MAX_ROWS} linhas por importacao"})
                    break
                try:
                    doc, warning = _row_to_record(row, line, batch_id, source, ctx.principal_id, ctx.tenant_id, now)
                except (ValueError, KeyError) as error:
                    rejected.append({"linha": line, "motivo": str(error)})
                    continue
                key = _content_key(doc)
                if key in existing:
                    skipped += 1
                    continue
                existing.add(key)
                session.save(doc)
                imported.append(doc["id"])
                if warning:
                    warnings.append({"linha": line, "aviso": warning})
        return {"lote": batch_id, "importados": len(imported), "ignorados_por_conteudo_identico": skipped, "rejeitados": rejected,
                "avisos": warnings}

    def archive(self, ctx, record_id):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            record = session.get("financial_record", record_id) if isinstance(record_id, str) else None
            if record is None:
                raise NotFound()
            return _public(session.save({**record, "status": "inativo", "revision": record["revision"] + 1, "updated_at": _stamp(self.clock())}))

    # -- reading (backend: the MCP gateway already authorized the agent) --------------------------------------------
    def list_records(self, tenant_id, *, counterparty=None, kind=None, document=None, limit=MAX_LIST):
        filters = {"status": "ativo"}
        if counterparty:
            filters["counterparty_key"] = party_key(counterparty)
        if kind:
            filters["record_kind"] = kind
        if document:
            filters["document_number"] = document
        with self.repository.transaction(tenant_id) as session:
            rows = session.list("financial_record", limit=1000, **filters)
        return [_public(r) for r in rows[: max(1, min(int(limit), MAX_LIST))]]

    def company_names(self, tenant_id):
        return [t["name"] for t in self.repository.list_global("tenant", limit=1000) if t["id"] == tenant_id]

    def analyze(self, tenant_id, counterparty, *, document=None, company_names=None, agent_ctx=None):
        """Structured analysis for one counterparty (optionally one document). Never executes anything."""
        key = party_key(counterparty or "")
        if not key:
            raise FinanceRejected("Informe a contraparte.")
        with self.repository.transaction(tenant_id) as session:
            records = session.list("financial_record", limit=1000, status="ativo", counterparty_key=key)
        names = list(company_names) if company_names is not None else self.company_names(tenant_id)
        result = analyse(records, names, self.clock(), document=document)
        result["contraparte"] = clean_text(counterparty)[:200]
        result["documentos"] = self._documents(agent_ctx, counterparty)
        return result

    def _documents(self, ctx, query):
        if self.knowledge is None or ctx is None:
            return []
        try:
            found = self.knowledge.retrieve(ctx, f"{query} {DOC_TOPICS}")["knowledge"]
        except Exception:
            return []
        return [{"titulo": d["title"], "fonte": d["source"], "versao": d["version"], "publicado_em": d["published_at"],
                 "trecho": d["excerpt"], "conflito": d.get("conflict", False), "confiavel": False} for d in found]


# -- pure analysis ------------------------------------------------------------------------------------------------------
def _matches_company(payer, names):
    payer_key = party_key(payer)
    return any(party_key(name) and (party_key(name) in payer_key or payer_key in party_key(name)) for name in names)


def analyse(records, company_names, now, *, document=None):
    findings, seen = [], set()

    def add(code, severity, blocks, message, ids):
        marker = (code, tuple(sorted(ids)))
        if marker not in seen:
            seen.add(marker)
            findings.append({"codigo": code, "gravidade": severity, "bloqueia": blocks, "mensagem": message, "registros": sorted(ids)})

    ordered = sorted(records, key=lambda r: (r["source_date"], r["id"]))
    targets = [r for r in ordered if document is None or r["document_number"] == document or r.get("reference") == document]
    kinds = {k: [r for r in ordered if r["record_kind"] == k] for k in RECORD_KINDS}
    flagged = set()  # records that must not be proposed for action

    def cents(r):
        return None if r["amount_cents"] is None else (r["amount_cents"], r["currency"])

    for r in targets:
        if r["record_kind"] in (PAYABLE, RECEIVABLE, "pagamento_realizado") and cents(r) is None:
            add("dados_incompletos", "alta", True, "Valor e moeda nao constam da fonte; nada foi presumido.", [r["id"]])
            flagged.add(r["id"])
        if r["record_kind"] == PAYABLE and not r["beneficiary"]:
            add("dados_incompletos", "alta", True, "A fatura nao informa o beneficiario.", [r["id"]])
            flagged.add(r["id"])
        if r["record_kind"] != RECEIVABLE and r["payer"] and company_names and not _matches_company(r["payer"], company_names):
            add("cobranca_de_outra_empresa", "alta", True, "O pagador do documento nao corresponde a esta empresa.", [r["id"]])
            flagged.add(r["id"])
    invoices = [r for r in kinds[PAYABLE] + kinds[RECEIVABLE] if r in targets]
    for r in invoices:
        twins = [o for o in kinds[r["record_kind"]] if o["id"] != r["id"] and (
            (r["document_number"] and o["document_number"] == r["document_number"]) or
            (cents(r) and cents(o) == cents(r) and r["due_at"] and o["due_at"] == r["due_at"]))]
        if twins:
            ids = [r["id"]] + [o["id"] for o in twins]
            differences = sorted({field for o in twins for field in ("amount_cents", "currency", "beneficiary") if o[field] != r[field]})
            add("fatura_duplicada", "alta", True,
                "Mais de um registro para o mesmo documento ou valor/vencimento" + (f" (diferem em: {', '.join(differences)})." if differences else "."), ids)
            flagged.update(ids)
        paid = [p for p in kinds["pagamento_realizado"] if (r["document_number"] and (p["reference"] == r["document_number"] or p["document_number"] == r["document_number"]))
                or (cents(r) and cents(p) == cents(r) and party_key(p["counterparty"]) == party_key(r["counterparty"]) and p["reference"] in (None, r["document_number"]))]
        if paid and r["record_kind"] == PAYABLE:
            add("possivel_pagamento_anterior", "alta", True, "Ha pagamento realizado correspondente; evitar pagamento em duplicidade.", [r["id"]] + [p["id"] for p in paid])
            flagged.add(r["id"])
        related = [o for o in ordered if o["id"] != r["id"] and o["record_kind"] != r["record_kind"] and (
            (r["reference"] and o["document_number"] == r["reference"]) or (r["document_number"] and o["reference"] == r["document_number"]))]
        for o in related:
            if cents(o) and cents(r) and o["currency"] != r["currency"]:
                add("moeda_divergente", "alta", True, f"Moeda {r['currency']} difere de {o['currency']} no registro relacionado ({_label(o['record_kind'])}).", [r["id"], o["id"]])
                flagged.add(r["id"])
            elif cents(o) and cents(r) and o["amount_cents"] != r["amount_cents"] and o["record_kind"] == "orcamento_fornecedor":
                add("valor_divergente_do_orcamento", "media", False, "O valor da fatura difere do orcamento relacionado.", [r["id"], o["id"]])
        if r["record_kind"] == PAYABLE and r["beneficiary"]:
            history = {party_key(o["beneficiary"]) for o in ordered if o["beneficiary"] and o["id"] != r["id"] and o["source_date"] <= r["source_date"]
                       and o["record_kind"] in (PAYABLE, "pagamento_realizado") and party_key(o["counterparty"]) == party_key(r["counterparty"])}
            if history and party_key(r["beneficiary"]) not in history:
                add("beneficiario_alterado", "critica", True, "O beneficiario difere do historico desta contraparte.", [r["id"]])
                flagged.add(r["id"])
            elif not history:
                add("beneficiario_sem_historico", "media", False, "Nao ha historico para confirmar o beneficiario.", [r["id"]])
        if r["origin"] == "email":
            add("origem_email_nao_verifica_identidade", "media", False, "Dado recebido por email: identidade, beneficiario e autorizacao precisam de verificacao fora do email.", [r["id"]])
    for r in targets:
        if r["record_kind"] == "orcamento_fornecedor":
            add("orcamento_nao_e_divida", "info", False, "Orcamento do fornecedor nao e obrigacao de pagamento.", [r["id"]])
        if r["record_kind"] == "renovacao":
            add("renovacao_nao_e_obrigacao_confirmada", "info", False, "Aviso de renovacao nao comprova contrato nem obrigacao; exige avaliacao e aprovacao.", [r["id"]])
    blocking = sorted({f["codigo"] for f in findings if f["bloqueia"]})
    proposals = _proposals(targets, flagged, findings, now)
    totals = {}
    for r in targets:
        if r["id"] in flagged or cents(r) is None:
            continue
        side = "a_pagar" if r["record_kind"] == PAYABLE else "a_receber" if r["record_kind"] == RECEIVABLE else None
        if side:
            totals.setdefault(r["currency"], {"a_pagar": 0, "a_receber": 0})[side] += r["amount_cents"]
    return {"analisado_em": _stamp(now), "registros": [_public(r) for r in targets], "achados": findings,
            "totais_confiaveis_por_moeda": totals, "totais_observacao": "Moedas nunca sao somadas entre si; registros sinalizados ficam fora.",
            "propostas": proposals, "esclarecimentos": [QUESTIONS[c] for c in blocking if c in QUESTIONS],
            "executado": False, "garantias": list(GUARANTEES), "dados_nao_confiaveis": True}


def _label(kind):
    return kind.replace("_", " ")


def _proposals(targets, flagged, findings, now):
    out = []
    by_id = {}
    for f in findings:
        for rid in f["registros"]:
            by_id.setdefault(rid, []).append(f)
    for r in targets:
        notes = by_id.get(r["id"], [])
        invoice = r["record_kind"] in (PAYABLE, RECEIVABLE)  # findings about an invoice do not block its quote or history
        blocked = sorted({f["codigo"] for f in notes if f["bloqueia"] and (invoice or f["codigo"] == "dados_incompletos")})
        if r["id"] in flagged or blocked:
            out.append({"registro": r["id"], "acao": "pedir_esclarecimento", "bloqueada_por": blocked or ["registro_sinalizado"], "executa": False})
            continue
        verify = any(f["codigo"] in ("origem_email_nao_verifica_identidade", "beneficiario_sem_historico") for f in notes)
        base = {"registro": r["id"], "documento": r["document_number"], "executa": False, "exige_aprovacao": True}
        if r["record_kind"] == PAYABLE:
            out.append({**base, "acao": "solicitar_aprovacao_pagamento", "beneficiario": r["beneficiary"], "valor_centavos": r["amount_cents"],
                        "moeda": r["currency"], "vencimento": r["due_at"], "verificacao_fora_do_email": verify,
                        "conector": "conector bancario ou ERP especifico (nao configurado); sem pagamento por texto livre"})
        elif r["record_kind"] == RECEIVABLE:
            overdue = bool(r["due_at"] and _parse(r["due_at"]) < now)
            out.append({**base, "acao": "propor_cobranca_ao_cliente" if overdue else "acompanhar_vencimento", "valor_centavos": r["amount_cents"],
                        "moeda": r["currency"], "vencimento": r["due_at"], "canal": "mensagem de saida com aprovacao (T14)"})
        elif r["record_kind"] == "renovacao":
            out.append({**base, "acao": "avaliar_renovacao", "observacao": "Verifique contrato e necessidade antes de aprovar."})
        elif r["record_kind"] == "orcamento_fornecedor":
            out.append({"registro": r["id"], "acao": "comparar_orcamentos", "executa": False, "exige_aprovacao": False})
        else:
            out.append({"registro": r["id"], "acao": "nenhuma_acao", "executa": False, "motivo": "pagamento ja realizado (registro historico)"})
    return out
