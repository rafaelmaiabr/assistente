"""Repository boundary for the team domain, with a SQLite implementation.

Every operation runs inside ``Repository.transaction(tenant_id)``: state writes, events and the
outbox row commit together or not at all, and nothing can read or write another tenant's rows.
Another backend (PostgreSQL, T34) implements the same two abstract classes.
"""
import json
import sqlite3
import unicodedata
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, migrate
from .schema import SCHEMAS, validate

TABLES = {
    "tenant": "tenants", "department": "departments", "agent_definition": "agent_definitions",
    "agent_run": "agent_runs", "skill_definition": "skill_definitions", "skill_grant": "skill_grants",
    "connection": "connections", "case": "cases", "task": "tasks", "approval_request": "approval_requests",
    "handoff": "handoffs", "evidence": "evidences", "contact": "contacts",
    "contact_address": "contact_addresses", "contact_link": "contact_links", "access_grant": "access_grants",
    "credential": "credentials", "mcp_catalog_entry": "mcp_catalog",
    "agent_profile_version": "agent_profile_versions", "model_policy": "model_policies", "case_entry": "case_entries", "capability": "capabilities", "triage_decision": "triage_decisions",
    "outbound_message": "outbound_messages", "knowledge_document": "knowledge_documents", "monitored_asset": "monitored_assets", "financial_record": "financial_records", "email_message": "email_messages", "mailbox_cursor": "mailbox_cursors", "schedule": "schedules", "notification": "notifications", "oauth_app": "oauth_apps", "oauth_grant": "oauth_grants",
    "memory_item": "memory_items",
}
# Lookup columns that are not plain references; ``address_key`` is derived (see column_value).
EXTRA_INDEXED = {"contact_address": ["address_key"], "access_grant": ["principal_id", "resource_kind"],
                 "agent_profile_version": ["agent_version", "status", "content_hash"], "agent_run": ["agent_version"],
                 "model_policy": ["name"], "capability": ["name"],
                 "outbound_message": ["dedup_key", "provider_id"], "knowledge_document": ["status", "doc_key"],
                 "memory_item": ["scope"], "monitored_asset": ["status", "hostname"],
                 "financial_record": ["record_kind", "counterparty_key", "document_number", "batch_id", "status"],
                 "email_message": ["folder", "uidvalidity", "uid", "message_id", "classification"], "mailbox_cursor": ["folder", "status"],
                 "schedule": ["state", "next_run_at", "job_kind"], "notification": ["state", "dedupe_key", "severity", "topic"],
                 "oauth_app": ["provider", "status"], "oauth_grant": ["provider", "state"]}
DELETABLE = ("contact_address", "contact_link", "access_grant", "credential", "memory_item")
GLOBAL_KINDS = ("tenant", "skill_definition", "mcp_catalog_entry")
MAX_LIST = 1000


class RepositoryError(Exception):
    pass


class RevisionConflict(RepositoryError):
    """The stored revision is not the one the writer expected (retry from a fresh read)."""


class TenantMismatch(RepositoryError):
    """A document or query names a different tenant than the transaction's."""


class IntegrityViolation(RepositoryError):
    """A reference does not exist inside the tenant, or a unique constraint failed."""


def indexed_columns(kind: str) -> list:
    """Columns mirrored from the document for lookups: scalar references plus ``state``."""
    spec = SCHEMAS[kind]
    refs = [name for name, field in spec.items()
            if isinstance(field, str) and field.startswith("ref:") and name != "tenant_id"]
    return refs + (["state"] if "state" in spec else []) + EXTRA_INDEXED.get(kind, [])


def address_key(channel: str, address: str) -> str:
    """Normalized identity of an address inside a tenant (case-insensitive)."""
    return f"{channel}:{address.strip().lower()}"


def party_key(name: str) -> str:
    """Normalized identity of a counterparty (accent- and case-insensitive, single spaces)."""
    plain = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    return " ".join(plain.split())


def column_value(doc, name):
    if name == "address_key":
        return address_key(doc["channel"], doc["address"])
    if name == "counterparty_key":
        return party_key(doc["counterparty"])
    return doc.get(name)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(doc):
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TenantSession(ABC):
    """Operations bound to one tenant; obtained from ``Repository.transaction``."""

    @abstractmethod
    def get(self, kind, doc_id): ...

    @abstractmethod
    def list(self, kind, limit=100, **filters): ...

    @abstractmethod
    def save(self, doc, event=None): ...

    @abstractmethod
    def delete(self, kind, doc_id) -> bool: ...

    @abstractmethod
    def record_event(self, event, topic="domain"): ...

    @abstractmethod
    def register_command(self, command) -> bool: ...

    @abstractmethod
    def events(self, case_id=None, after_seq=0, limit=100): ...

    @abstractmethod
    def last_event_seq(self, case_id=None) -> int: ...

    @abstractmethod
    def record_tool_decision(self, decision): ...

    @abstractmethod
    def tool_decisions(self, run_id, limit=100): ...

    @abstractmethod
    def pending_outbox(self, limit=100): ...

    @abstractmethod
    def mark_dispatched(self, outbox_ids): ...

    @abstractmethod
    def record_outbox_failure(self, outbox_ids): ...


class Repository(ABC):
    @abstractmethod
    def create_tenant(self, doc): ...

    @abstractmethod
    def save_global(self, doc): ...

    @abstractmethod
    def get_global(self, kind, doc_id): ...

    @abstractmethod
    def list_global(self, kind, limit=100): ...

    @abstractmethod
    def transaction(self, tenant_id): ...


class SqliteSession(TenantSession):
    def __init__(self, conn, tenant_id):
        self.conn, self.tenant_id = conn, tenant_id

    def _table(self, kind):
        if kind in GLOBAL_KINDS or kind not in TABLES:
            raise RepositoryError(f"tipo nao pertence a uma empresa: {kind}")
        return TABLES[kind]

    def get(self, kind, doc_id):
        row = self.conn.execute(
            f"SELECT doc FROM {self._table(kind)} WHERE tenant_id = ? AND id = ?", (self.tenant_id, doc_id)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind, limit=100, **filters):
        allowed = indexed_columns(kind)
        unknown = set(filters) - set(allowed)
        if unknown:
            raise RepositoryError(f"filtro nao indexado para {kind}: {', '.join(sorted(unknown))}")
        where = "".join(f" AND {name} = ?" for name in filters)
        rows = self.conn.execute(
            f"SELECT doc FROM {self._table(kind)} WHERE tenant_id = ?{where} ORDER BY created_at, id LIMIT ?",
            (self.tenant_id, *filters.values(), min(max(int(limit), 1), MAX_LIST))).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, doc, event=None):
        """Insert (revision 1) or update (revision == stored + 1); optionally record an event atomically."""
        kind = doc.get("kind") if isinstance(doc, dict) else None
        table = self._table(kind)
        validate(kind, doc)
        if doc["tenant_id"] != self.tenant_id:
            raise TenantMismatch("documento pertence a outra empresa")
        row = self.conn.execute(f"SELECT revision FROM {table} WHERE id = ?", (doc["id"],)).fetchone()
        if row is None:
            if doc["revision"] != 1:
                raise RevisionConflict("novo documento deve iniciar na revisao 1")
        elif doc["revision"] != row[0] + 1:
            raise RevisionConflict(f"revisao esperada {row[0] + 1}, recebida {doc['revision']}")
        columns = indexed_columns(kind)
        names = ["id", "tenant_id", "revision", "created_at", "updated_at", *columns, "doc"]
        values = [doc["id"], self.tenant_id, doc["revision"], doc["created_at"], doc["updated_at"],
                  *(column_value(doc, name) for name in columns), _dump(doc)]
        try:
            if row is None:
                self.conn.execute(
                    f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", values)
            else:
                assignments = ", ".join(f"{name} = ?" for name in names[2:])
                self.conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ? AND tenant_id = ?",
                                  [*values[2:], doc["id"], self.tenant_id])
        except sqlite3.IntegrityError as error:
            raise IntegrityViolation(str(error)) from error
        if event is not None:
            self.record_event(event)
        return doc

    def delete(self, kind, doc_id):
        """Remove a link-like record (never entities with history); True when a row was deleted."""
        if kind not in DELETABLE:
            raise RepositoryError(f"tipo nao pode ser removido: {kind}")
        try:
            cursor = self.conn.execute(
                f"DELETE FROM {self._table(kind)} WHERE tenant_id = ? AND id = ?", (self.tenant_id, doc_id))
        except sqlite3.IntegrityError as error:
            raise IntegrityViolation(str(error)) from error
        return cursor.rowcount == 1

    def record_event(self, event, topic="domain"):
        validate("event", event)
        if event["tenant_id"] != self.tenant_id:
            raise TenantMismatch("evento pertence a outra empresa")
        try:
            cursor = self.conn.execute(
                "INSERT INTO events (id, tenant_id, case_id, revision, occurred_at, doc) VALUES (?, ?, ?, ?, ?, ?)",
                (event["id"], self.tenant_id, event["case_id"], event["revision"], event["occurred_at"], _dump(event)))
        except sqlite3.IntegrityError as error:
            if "events.revision" in str(error) or "UNIQUE" in str(error):
                raise RevisionConflict(f"evento com revisao {event['revision']} ja existe na demanda") from error
            raise IntegrityViolation(str(error)) from error
        self.conn.execute("INSERT INTO outbox (tenant_id, event_seq, topic, created_at) VALUES (?, ?, ?, ?)",
                          (self.tenant_id, cursor.lastrowid, topic, _now()))
        return cursor.lastrowid

    def register_command(self, command):
        """Store a command once per idempotency key; False means it was already registered."""
        validate("command", command)
        if command["tenant_id"] != self.tenant_id:
            raise TenantMismatch("comando pertence a outra empresa")
        try:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO commands (id, tenant_id, case_id, idempotency_key, issued_at, doc) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (command["id"], self.tenant_id, command["case_id"], command["idempotency_key"],
                 command["issued_at"], _dump(command)))
        except sqlite3.IntegrityError as error:
            raise IntegrityViolation(str(error)) from error
        return cursor.rowcount == 1

    def events(self, case_id=None, after_seq=0, limit=100):
        clause, args = ("AND case_id = ? ", [case_id]) if case_id else ("", [])
        rows = self.conn.execute(
            f"SELECT seq, doc FROM events WHERE tenant_id = ? AND seq > ? {clause}ORDER BY seq LIMIT ?",
            (self.tenant_id, after_seq, *args, min(max(int(limit), 1), MAX_LIST))).fetchall()
        return [(seq, json.loads(doc)) for seq, doc in rows]

    def last_event_seq(self, case_id=None):
        """Highest event sequence number for the company (or one case): the cursor clients resume from."""
        clause, args = ("AND case_id = ?", [case_id]) if case_id else ("", [])
        row = self.conn.execute(f"SELECT COALESCE(MAX(seq), 0) FROM events WHERE tenant_id = ? {clause}",
                                (self.tenant_id, *args)).fetchone()
        return row[0]

    def record_tool_decision(self, decision):
        """Minimal gateway audit row: identifiers, tool name, verdict and policy version, never arguments or results."""
        try:
            self.conn.execute(
                "INSERT INTO tool_decisions (tenant_id, run_id, agent_id, agent_version, tool, operation, decision, "
                "reason, policy_version, result_bytes, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.tenant_id, decision["run_id"], decision["agent_id"], decision["agent_version"], decision["tool"],
                 decision.get("operation"), decision["decision"], decision["reason"], decision["policy_version"],
                 decision.get("result_bytes"), decision.get("duration_ms"), _now()))
        except sqlite3.IntegrityError as error:
            raise IntegrityViolation(str(error)) from error

    def tool_decisions(self, run_id, limit=100):
        rows = self.conn.execute(
            "SELECT run_id, agent_id, agent_version, tool, operation, decision, reason, policy_version, result_bytes, "
            "duration_ms FROM tool_decisions WHERE tenant_id = ? AND run_id = ? ORDER BY id LIMIT ?",
            (self.tenant_id, run_id, min(max(int(limit), 1), MAX_LIST))).fetchall()
        keys = ("run_id", "agent_id", "agent_version", "tool", "operation", "decision", "reason", "policy_version",
                "result_bytes", "duration_ms")
        return [dict(zip(keys, row)) for row in rows]

    def pending_outbox(self, limit=100):
        rows = self.conn.execute(
            "SELECT o.id, o.topic, o.attempts, e.doc FROM outbox o JOIN events e ON e.seq = o.event_seq "
            "WHERE o.tenant_id = ? AND o.status = 'pending' ORDER BY o.id LIMIT ?",
            (self.tenant_id, min(max(int(limit), 1), MAX_LIST))).fetchall()
        return [{"id": i, "topic": topic, "attempts": attempts, "event": json.loads(doc)}
                for i, topic, attempts, doc in rows]

    def record_outbox_failure(self, outbox_ids):
        self.conn.executemany("UPDATE outbox SET attempts = attempts + 1 WHERE id = ? AND tenant_id = ?",
                              [(i, self.tenant_id) for i in outbox_ids])

    def mark_dispatched(self, outbox_ids):
        self.conn.executemany(
            "UPDATE outbox SET status = 'dispatched', attempts = attempts + 1, dispatched_at = ? "
            "WHERE id = ? AND tenant_id = ?", [(_now(), i, self.tenant_id) for i in outbox_ids])


class SqliteRepository(Repository):
    def __init__(self, path):
        self.path = Path(path)

    def migrate(self):
        return migrate(self.path)

    @contextmanager
    def _transaction(self):
        conn = connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")  # one writer at a time; others wait on busy_timeout
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self, tenant_id):
        if not isinstance(tenant_id, str) or not tenant_id:
            raise TenantMismatch("contexto de empresa obrigatorio")
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone() is None:
                raise TenantMismatch("empresa desconhecida")
            yield SqliteSession(conn, tenant_id)

    def _save_global(self, doc, kind):
        validate(kind, doc)
        table = TABLES[kind]
        extra = {"skill_definition": ("skill_key", "skill_version"),
                 "mcp_catalog_entry": ("catalog_key", "entry_version")}.get(kind, ())
        names = ["id", "revision", "created_at", "updated_at", *extra, "doc"]
        values = [doc["id"], doc["revision"], doc["created_at"], doc["updated_at"], *(doc[n] for n in extra), _dump(doc)]
        with self._transaction() as conn:
            row = conn.execute(f"SELECT revision FROM {table} WHERE id = ?", (doc["id"],)).fetchone()
            if (row is None and doc["revision"] != 1) or (row is not None and doc["revision"] != row[0] + 1):
                raise RevisionConflict("revisao inesperada")
            try:
                if row is None:
                    conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", values)
                else:
                    conn.execute(f"UPDATE {table} SET {', '.join(f'{n} = ?' for n in names[1:])} WHERE id = ?",
                                 [*values[1:], doc["id"]])
            except sqlite3.IntegrityError as error:
                raise IntegrityViolation(str(error)) from error
        return doc

    def create_tenant(self, doc):
        return self._save_global(doc, "tenant")

    def save_global(self, doc):
        if doc.get("kind") not in ("skill_definition", "mcp_catalog_entry"):
            raise RepositoryError("tipo nao e um catalogo global")
        return self._save_global(doc, doc["kind"])

    def get_global(self, kind, doc_id):
        if kind not in GLOBAL_KINDS:
            raise RepositoryError(f"tipo nao e global: {kind}")
        conn = connect(self.path)
        try:
            row = conn.execute(f"SELECT doc FROM {TABLES[kind]} WHERE id = ?", (doc_id,)).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None

    def list_global(self, kind, limit=100):
        if kind not in GLOBAL_KINDS:
            raise RepositoryError(f"tipo nao e global: {kind}")
        conn = connect(self.path)
        try:
            rows = conn.execute(f"SELECT doc FROM {TABLES[kind]} ORDER BY created_at, id LIMIT ?",
                                (min(max(int(limit), 1), MAX_LIST),)).fetchall()
        finally:
            conn.close()
        return [json.loads(row[0]) for row in rows]
