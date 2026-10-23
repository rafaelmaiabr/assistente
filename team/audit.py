"""Sanitized audit trail and operational metrics (read-only, operator-only).

The trail answers "who did what, under which policy, with which result" for a case or a run. It carries
identifiers, states, decisions, durations and evidence references, and **never** message bodies,
summaries, tool arguments/results, tokens, credentials or model reasoning. Free text that does appear
(a reason, an error code) is masked for credentials and personal data first. A raw event payload can be
added only after the operator switches it on, and it is still masked.

Metrics describe the queue, retries, hand-offs, failures, human waiting time and token counts. Tokens
come from the runtime (provider usage, or a size estimate when the provider reports none), and no price
is assumed: cost is reported as unavailable.
"""
import json
import os
import re
from datetime import datetime, timezone

from .access import AccessContext, NotFound, OperatorContext, PermissionDenied
from .schema import redact_secrets

MAX_ITEMS = 500
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_URL_SECRET = re.compile(r"([?&](?:token|key|secret|password|senha|sig|signature|code)=)[^&\s]+", re.I)
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")


def sanitize_text(value, limit=200):
    """Mask credentials and personal data in free text and cut it to ``limit`` characters."""
    text = redact_secrets(str(value))
    text = _BEARER.sub(r"\1 ***", text)
    text = _URL_SECRET.sub(r"\1***", text)
    text = _EMAIL.sub("[email]", text)
    text = _CNPJ.sub("[cnpj]", text)
    text = _CPF.sub("[cpf]", text)
    text = _CARD.sub("[numero]", text)
    text = _PHONE.sub("[telefone]", text)
    text = _LONG_TOKEN.sub("***", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _scalar(value, limit=120):
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return sanitize_text(value, limit)
    return None  # nested structures never enter the trail


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class AuditService:
    def __init__(self, repository, access, settings_path, clock=lambda: datetime.now(timezone.utc)):
        self.repository, self.access, self.settings_path, self.clock = repository, access, settings_path, clock

    # -- settings ----------------------------------------------------------------------------------------------
    def raw_payload_enabled(self):
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8")).get("raw_payload") is True
        except (OSError, ValueError, AttributeError):
            return False

    def set_raw_payload(self, operator, enabled):
        """Opt in to (masked) raw event payloads in the trail. Off by default."""
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.access.operator_id:
            raise PermissionDenied()
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"raw_payload": bool(enabled)}), encoding="utf-8")
        os.replace(temp, self.settings_path)

    def _operator(self, ctx):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied()

    # -- trail -------------------------------------------------------------------------------------------------
    def trail(self, ctx, *, case_id=None, run_id=None, limit=200, include_raw=False):
        """Chronological sanitized records for one case or one run."""
        self._operator(ctx)
        if bool(case_id) == bool(run_id):
            raise ValueError("Informe a demanda ou a execucao (apenas uma).")
        limit = max(1, min(int(limit), MAX_ITEMS))
        raw = include_raw and self.raw_payload_enabled()
        items = []
        with self.repository.transaction(ctx.tenant_id) as session:
            conn, tenant = session.conn, ctx.tenant_id
            if run_id:
                run = session.get("agent_run", run_id) if isinstance(run_id, str) else None
                if run is None:
                    raise NotFound()
                runs = [run]
            else:
                if not isinstance(case_id, str) or session.get("case", case_id) is None:
                    raise NotFound()
                runs = session.list("agent_run", limit=1000, case_id=case_id)
            for run in runs:
                items.append(self._item(run["created_at"], "execucao", run["id"], run["agent_id"], {
                    "estado": run["state"], "agente_versao": run["agent_version"], "iniciada_em": run.get("started_at"),
                    "finalizada_em": run.get("finished_at"), "skills": [sanitize_text(s, 80) for s in run.get("skill_versions", [])[:10]]}))
                for row in conn.execute(
                        "SELECT tool, operation, decision, reason, policy_version, result_bytes, duration_ms, created_at "
                        "FROM tool_decisions WHERE tenant_id = ? AND run_id = ? ORDER BY id", (tenant, run["id"])):
                    items.append(self._item(row[7], "ferramenta", run["id"], run["agent_id"], {
                        "ferramenta": sanitize_text(row[0], 120), "operacao": row[1], "decisao": row[2], "motivo": sanitize_text(row[3], 120),
                        "politica": row[4], "bytes_resultado": row[5], "duracao_ms": row[6]}))
            for job in conn.execute(
                    "SELECT id, kind, state, attempts, max_attempts, last_error, result, created_ms, updated_ms, run_id FROM jobs "
                    "WHERE tenant_id = ? AND (case_id = ? OR run_id = ?) ORDER BY created_ms, id", (tenant, case_id, run_id)):
                result = json.loads(job[6]) if job[6] else {}
                items.append(self._item(_ms_stamp(job[7]), "job", job[0], None, {
                    "tipo": job[1], "estado": job[2], "tentativas": job[3], "maximo": job[4], "erro": sanitize_text(job[5] or "", 80) or None,
                    "tokens": result.get("tokens") if isinstance(result.get("tokens"), int) else None,
                    "passos": result.get("steps") if isinstance(result.get("steps"), int) else None, "atualizado_em": _ms_stamp(job[8])}))
            if case_id:
                items += self._case_items(session, case_id, raw)
        items.sort(key=lambda item: (item["em"], item["tipo"], item["id"]))
        truncated = len(items) > limit
        return {"itens": items[-limit:], "truncado": truncated, "payload_bruto": raw, "contem": [
            "identificadores", "estados", "decisoes", "duracoes", "referencias de evidencia"],
            "nao_contem": ["corpo de mensagens", "argumentos e resultados de ferramentas", "tokens de acesso", "raciocinio do modelo"]}

    @staticmethod
    def _item(at, kind, item_id, actor, data):
        return {"em": at, "tipo": kind, "id": item_id, "ator": actor, "dados": {k: v for k, v in data.items() if v is not None}}

    def _case_items(self, session, case_id, raw):
        out = []
        for _, event in session.events(case_id=case_id, limit=1000):
            payload = event.get("payload") or {}
            data = {k: _scalar(v) for k, v in payload.items() if _scalar(v) is not None}
            if raw:
                data["payload_bruto"] = _mask(payload)
            out.append(self._item(event["occurred_at"], "evento", event["id"], event["actor_id"], {"evento": event["type"], **data}))
        for approval in session.list("approval_request", limit=1000, case_id=case_id):
            out.append(self._item(approval["updated_at"], "aprovacao", approval["id"], approval.get("decided_by") or approval["requested_by"], {
                "acao": sanitize_text(approval["action"], 80), "estado": approval["state"], "hash_do_payload": approval["payload_hash"],
                "valor_centavos": approval.get("amount_cents"), "moeda": approval.get("currency"), "politica": approval.get("policy_ref"),
                "resultado_do_efeito": approval.get("effect_outcome"), "decidida_em": approval.get("decided_at")}))
        for handoff in session.list("handoff", limit=1000, case_id=case_id):
            out.append(self._item(handoff["updated_at"], "encaminhamento", handoff["id"], handoff["from_agent_id"], {
                "tipo": handoff["handoff_kind"], "para": handoff["to_agent_id"], "estado": handoff["state"]}))
        for message in session.list("outbound_message", limit=1000, case_id=case_id):
            out.append(self._item(message["updated_at"], "mensagem_saida", message["id"], message["created_by"], {
                "canal": message["channel"], "estado": message["state"], "aprovacao": message.get("approval_id"),
                "tentativas": message["attempts"], "erro": sanitize_text(message.get("last_error") or "", 80) or None,
                "id_no_provedor": sanitize_text(message.get("provider_id") or "", 60) or None}))
        for evidence in session.list("evidence", limit=1000, case_id=case_id):
            out.append(self._item(evidence["collected_at"], "evidencia", evidence["id"], evidence.get("recorded_by"), {
                "fonte": sanitize_text(evidence["source"], 120), "tipo": evidence.get("evidence_type"),
                "confiabilidade": evidence.get("reliability"), "hash": evidence.get("content_hash")}))
        return out

    # -- metrics -----------------------------------------------------------------------------------------------
    def metrics(self, ctx):
        """Counts and waits for one company; no content."""
        self._operator(ctx)
        now = self.clock()
        now_ms = int(now.timestamp() * 1000)
        with self.repository.transaction(ctx.tenant_id) as session:
            conn, tenant = session.conn, ctx.tenant_id

            def counts(sql, *args):
                return {row[0]: row[1] for row in conn.execute(sql, (tenant, *args))}

            jobs = counts("SELECT state, COUNT(*) FROM jobs WHERE tenant_id = ? GROUP BY state")
            retries = conn.execute("SELECT COALESCE(SUM(MAX(attempts - 1, 0)), 0) FROM jobs WHERE tenant_id = ?", (tenant,)).fetchone()[0]
            oldest = conn.execute("SELECT MIN(created_ms) FROM jobs WHERE tenant_id = ? AND state = 'pendente'", (tenant,)).fetchone()[0]
            tokens = steps = 0
            for (result,) in conn.execute("SELECT result FROM jobs WHERE tenant_id = ? AND result IS NOT NULL", (tenant,)):
                data = json.loads(result)
                tokens += data.get("tokens") if isinstance(data.get("tokens"), int) else 0
                steps += data.get("steps") if isinstance(data.get("steps"), int) else 0
            decisions = counts("SELECT decision, COUNT(*) FROM tool_decisions WHERE tenant_id = ? GROUP BY decision")
            slow = conn.execute("SELECT COALESCE(AVG(duration_ms), 0), COALESCE(MAX(duration_ms), 0) FROM tool_decisions "
                                "WHERE tenant_id = ? AND duration_ms IS NOT NULL", (tenant,)).fetchone()
            handoffs = counts("SELECT json_extract(doc, '$.state'), COUNT(*) FROM handoffs WHERE tenant_id = ? GROUP BY 1")
            attention = conn.execute("SELECT COUNT(*) FROM handoffs WHERE tenant_id = ? AND json_extract(doc, '$.attention') = 1", (tenant,)).fetchone()[0]
            runs = counts("SELECT json_extract(doc, '$.state'), COUNT(*) FROM agent_runs WHERE tenant_id = ? GROUP BY 1")
            messages = counts("SELECT state, COUNT(*) FROM outbound_messages WHERE tenant_id = ? GROUP BY state")
            cases = counts("SELECT json_extract(doc, '$.state'), COUNT(*) FROM cases WHERE tenant_id = ? GROUP BY 1")
            pending = [a for a in session.list("approval_request", limit=1000, state="pendente")]
            waits = [(_parse(a["decided_at"]) - _parse(a["created_at"])).total_seconds()
                     for a in session.list("approval_request", limit=1000) if a.get("decided_at") and _parse(a["decided_at"])]
            outbox = conn.execute("SELECT COUNT(*) FROM outbox WHERE tenant_id = ? AND status = 'pending'", (tenant,)).fetchone()[0]
        oldest_pending = min((_parse(a["created_at"]) for a in pending), default=None)
        return {
            "gerado_em": _stamp(now),
            "fila": {"por_estado": jobs, "tentativas_extras": retries, "esgotados": jobs.get("esgotado", 0),
                     "para_reconciliar": jobs.get("reconciliar", 0), "mais_antigo_pendente_segundos":
                     None if oldest is None else max(0, (now_ms - oldest) // 1000), "eventos_na_caixa_de_saida": outbox},
            "encaminhamentos": {"por_estado": handoffs, "exigem_atencao": attention},
            "execucoes": {"por_estado": runs, "decisoes_de_ferramenta": decisions, "ferramenta_duracao_media_ms": round(slow[0]),
                          "ferramenta_duracao_max_ms": slow[1]},
            "falhas": {"execucoes": runs.get("falhou", 0), "ferramentas_negadas": decisions.get("denied", 0),
                       "ferramentas_com_erro": decisions.get("error", 0), "mensagens_falhas": messages.get("falhou", 0),
                       "mensagens_desconhecidas": messages.get("desconhecido", 0), "jobs_falhos": jobs.get("falhou", 0)},
            "espera_humana": {"aprovacoes_pendentes": len(pending), "mais_antiga_pendente_segundos":
                              None if oldest_pending is None else max(0, int((now - oldest_pending).total_seconds())),
                              "tempo_medio_de_decisao_segundos": round(sum(waits) / len(waits)) if waits else None,
                              "demandas_por_estado": cases},
            "tokens": {"total": tokens, "passos": steps, "origem": "uso informado pelo provedor ou estimativa pelo tamanho quando ausente",
                       "custo": None, "custo_observacao": "Preco e uso faturavel nao estao disponiveis; nenhum custo foi estimado."},
        }


def _ms_stamp(value):
    return _stamp(datetime.fromtimestamp(value / 1000, timezone.utc))


def _mask(value, depth=0):
    """Recursively mask strings in a payload (credentials and personal data); depth and size are capped."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {str(k)[:60]: _mask(v, depth + 1) for k, v in list(redact_secrets(value).items())[:40]}
    if isinstance(value, list):
        return [_mask(v, depth + 1) for v in value[:40]]
    if isinstance(value, str):
        return sanitize_text(value, 500)
    return value if isinstance(value, (bool, int, float)) or value is None else None
