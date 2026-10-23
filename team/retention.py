"""Scoped export, controlled erasure and retention of cases.

* **Export** is always for one case, needs an explicit acknowledgement that it contains personal data, never
  includes credentials, and is registered so that erasing the case removes it too.
* **Erasure** deletes a case and everything that hangs from it (entries, evidences, approvals, hand-offs,
  outbound messages, jobs, runs, tool decisions, events and the outbox rows for them) in one transaction,
  then removes its exports, and leaves only a content-free record (ids, counts, when, by whom).
* **Retention** applies the operator's policy: closed cases older than N days are erased, old exports are
  removed. Backups follow their own age limit in ``team.backup``.

What this does not reach: copies already inside older backups (they expire by age), memory or knowledge an
operator created from the case, contacts, and anything outside this computer.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from .access import AccessContext, NotFound, PermissionDenied
from .schema import redact_secrets

TERMINAL = ("resolvida", "cancelada", "falhou")
EXPORT_NOTICE = ("Esta exportacao contem dados pessoais e o conteudo da demanda. Guarde em local protegido, nao a publique "
                 "e apague-a quando nao for mais necessaria. Credenciais nao sao incluidas.")
DEFAULT_POLICY = {"closed_case_days": None, "export_days": 30, "backup_days": 30}


class RetentionRejected(ValueError):
    """The operation is not allowed or could not be completed; the message is safe to show."""


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


class RetentionService:
    def __init__(self, repository, access, base_dir, clock=lambda: datetime.now(timezone.utc)):
        self.repository, self.access, self.base, self.clock = repository, access, base_dir, clock
        self.exports = base_dir / "exports"
        self.policy_path = base_dir / "retention.json"

    def _operator(self, ctx):
        if not isinstance(ctx, AccessContext) or ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied()

    # -- policy ------------------------------------------------------------------------------------------------
    def policy(self):
        try:
            stored = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        return {**DEFAULT_POLICY, **{k: v for k, v in stored.items() if k in DEFAULT_POLICY}}

    def set_policy(self, ctx, **changes):
        self._operator(ctx)
        unknown = set(changes) - set(DEFAULT_POLICY)
        if unknown:
            raise RetentionRejected("Politica desconhecida: " + ", ".join(sorted(unknown)))
        for name, value in changes.items():
            if value is not None and not (isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 3650):
                raise RetentionRejected(f"{name}: informe de 1 a 3650 dias, ou vazio para nao apagar automaticamente.")
        policy = {**self.policy(), **changes}
        _write_private(self.policy_path, json.dumps(policy, indent=2))
        return policy

    # -- export ------------------------------------------------------------------------------------------------
    def _index(self):
        try:
            data = json.loads((self.exports / "index.json").read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_index(self, entries):
        _write_private(self.exports / "index.json", json.dumps(entries, indent=2))

    def export_case(self, ctx, case_id, *, acknowledge_sensitive=False):
        self._operator(ctx)
        if acknowledge_sensitive is not True:
            raise RetentionRejected("Confirme que entende que a exportacao contem dados pessoais (acknowledge_sensitive=True).")
        with self.repository.transaction(ctx.tenant_id) as session:
            case = session.get("case", case_id) if isinstance(case_id, str) else None
            if case is None:
                raise NotFound()
            sections = {
                "demanda": [case],
                "entradas": session.list("case_entry", limit=1000, case_id=case_id),
                "evidencias": session.list("evidence", limit=1000, case_id=case_id),
                "aprovacoes": session.list("approval_request", limit=1000, case_id=case_id),
                "encaminhamentos": session.list("handoff", limit=1000, case_id=case_id),
                "mensagens_de_saida": session.list("outbound_message", limit=1000, case_id=case_id),
                "triagem": session.list("triage_decision", limit=1000, case_id=case_id),
            }
        now = self.clock()
        body = {"exportado_em": _stamp(now), "escopo": {"empresa": ctx.tenant_id, "demanda": case_id}, "aviso": EXPORT_NOTICE,
                "secoes": redact_secrets(sections)}
        path = self.exports / ctx.tenant_id / f"{case_id}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
        _write_private(path, json.dumps(body, ensure_ascii=False, indent=2))
        self._save_index(self._index() + [{"path": str(path.relative_to(self.exports)), "tenant_id": ctx.tenant_id,
                                          "case_id": case_id, "created_at": _stamp(now)}])
        return {"path": str(path), "aviso": EXPORT_NOTICE, "itens": {name: len(rows) for name, rows in sections.items()}}

    def _drop_exports(self, keep=lambda entry: False):
        removed, kept = 0, []
        for entry in self._index():
            if keep(entry):
                kept.append(entry)
                continue
            try:
                (self.exports / entry["path"]).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                kept.append(entry)  # still on disk: keep tracking it
                continue
            removed += 1
        self._save_index(kept)
        return removed

    # -- erasure -----------------------------------------------------------------------------------------------
    def erase_case(self, ctx, case_id, confirm):
        """Delete a case and everything hanging from it. ``confirm`` must repeat the case id."""
        self._operator(ctx)
        if confirm != case_id:
            raise RetentionRejected("Para excluir, repita o identificador da demanda em 'confirm'.")
        counts = {}
        try:
            with self.repository.transaction(ctx.tenant_id) as session:
                if session.get("case", case_id) is None:
                    raise NotFound()
                conn, tenant = session.conn, ctx.tenant_id
                conn.execute("PRAGMA defer_foreign_keys = ON")
                conn.execute("INSERT INTO erasure_guard (token) VALUES (1)")  # lets the append-only tables be deleted, this transaction only
                runs = "(SELECT id FROM agent_runs WHERE tenant_id = ? AND case_id = ?)"
                steps = [
                    ("decisoes_de_ferramenta", f"DELETE FROM tool_decisions WHERE tenant_id = ? AND run_id IN {runs}", (tenant, tenant, case_id)),
                    ("caixa_de_saida", "DELETE FROM outbox WHERE tenant_id = ? AND event_seq IN "
                                       "(SELECT seq FROM events WHERE tenant_id = ? AND case_id = ?)", (tenant, tenant, case_id)),
                    ("eventos", "DELETE FROM events WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("entradas", "DELETE FROM case_entries WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("evidencias", "DELETE FROM evidences WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("triagem", "DELETE FROM triage_decisions WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("mensagens_de_saida", "DELETE FROM outbound_messages WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("aprovacoes", "DELETE FROM approval_requests WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("encaminhamentos", "DELETE FROM handoffs WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("jobs", f"DELETE FROM jobs WHERE tenant_id = ? AND (case_id = ? OR run_id IN {runs})", (tenant, case_id, tenant, case_id)),
                    ("comandos", "DELETE FROM commands WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("subtarefas", "DELETE FROM tasks WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("execucoes", "DELETE FROM agent_runs WHERE tenant_id = ? AND case_id = ?", (tenant, case_id)),
                    ("demanda", "DELETE FROM cases WHERE tenant_id = ? AND id = ?", (tenant, case_id)),
                ]
                for name, sql, args in steps:
                    counts[name] = conn.execute(sql, args).rowcount
                conn.execute("DELETE FROM erasure_guard")
                conn.execute("INSERT INTO erasure_log (tenant_id, case_id, erased_at, erased_by, counts) VALUES (?, ?, ?, ?, ?)",
                             (tenant, case_id, _stamp(self.clock()), ctx.principal_id, json.dumps(counts)))
        except sqlite3.IntegrityError:
            raise RetentionRejected("A demanda ainda e referenciada por outros registros; nada foi apagado.") from None
        exports = self._drop_exports(lambda e: not (e["tenant_id"] == ctx.tenant_id and e["case_id"] == case_id))
        return {"apagado": counts, "exportacoes_removidas": exports,
                "nao_alcanca": "copias em backups antigos (expiram por idade), memoria/conhecimento criados a partir da demanda e contatos"}

    def erasure_log(self, ctx):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            rows = session.conn.execute(
                "SELECT case_id, erased_at, erased_by, counts FROM erasure_log WHERE tenant_id = ? ORDER BY id", (ctx.tenant_id,)).fetchall()
        return [{"case_id": r[0], "apagada_em": r[1], "apagada_por": r[2], "contagens": json.loads(r[3])} for r in rows]

    # -- policy application ------------------------------------------------------------------------------------
    def apply_policy(self, ctx):
        """Erase closed cases and exports older than the policy allows (this company only)."""
        self._operator(ctx)
        policy, now = self.policy(), self.clock()
        erased = []
        if policy["closed_case_days"]:
            limit = now - timedelta(days=policy["closed_case_days"])
            with self.repository.transaction(ctx.tenant_id) as session:
                old = [c["id"] for c in session.list("case", limit=1000)
                       if c["state"] in TERMINAL and _parse(c.get("closed_at") or c["updated_at"]) <= limit]
            for case_id in old:
                self.erase_case(ctx, case_id, case_id)
                erased.append(case_id)
        removed = 0
        if policy["export_days"]:
            limit = now - timedelta(days=policy["export_days"])
            removed = self._drop_exports(lambda e: not (e["tenant_id"] == ctx.tenant_id and _parse(e["created_at"]) <= limit))
        return {"demandas_apagadas": erased, "exportacoes_removidas": removed, "politica": policy}
