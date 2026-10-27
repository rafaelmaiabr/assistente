"""Durable store for the ERP charge ledger (#110), on the team repository (SQLite or PostgreSQL).

Same interface as ``MemoryStore``. Rows are ``erp_ledger_entry`` documents, unique per (company, namespace, provider, key),
so the database itself refuses to store the same event or the same issue key twice. ``atomic`` opens one repository
transaction for the company and every ``get``/``put`` inside it (on the same thread) shares it: a whole reconciliation step
commits or rolls back together, and one company's session cannot reach another company's rows.
"""
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from ..ids import new_id
from ..repository import IntegrityViolation, RevisionConflict
from .erp_ledger import StoreConflict


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LedgerStore:
    def __init__(self, repository):
        self.repository = repository
        self._local = threading.local()

    @contextmanager
    def atomic(self, tenant_id):
        current = getattr(self._local, "scope", None)
        if current is not None:
            if current[0] != tenant_id:
                raise ValueError("uma transacao do livro pertence a uma unica empresa")
            yield
            return
        with self.repository.transaction(tenant_id) as session:
            self._local.scope = (tenant_id, session)
            try:
                yield
            except (IntegrityViolation, RevisionConflict) as error:
                raise StoreConflict() from error
            finally:
                self._local.scope = None

    @contextmanager
    def _session(self, tenant_id):
        current = getattr(self._local, "scope", None)
        if current is not None:
            if current[0] != tenant_id:
                raise ValueError("uma transacao do livro pertence a uma unica empresa")
            yield current[1]
        else:
            with self.atomic(tenant_id):
                yield self._local.scope[1]

    @staticmethod
    def _find(session, namespace, provider, key):
        found = session.list("erp_ledger_entry", limit=1, namespace=namespace, provider=provider, entry_key=key)
        return found[0] if found else None

    def get(self, tenant_id, namespace, provider, key):
        with self._session(tenant_id) as session:
            entry = self._find(session, namespace, provider, key)
        return None if entry is None else entry["payload"]

    def put(self, tenant_id, namespace, provider, key, value):
        with self._session(tenant_id) as session:
            entry, now = self._find(session, namespace, provider, key), _now()
            if entry is None:
                doc = {"kind": "erp_ledger_entry", "schema_version": 1, "id": new_id("erp_ledger_entry"), "tenant_id": tenant_id,
                       "revision": 1, "created_at": now, "updated_at": now, "namespace": namespace, "provider": provider,
                       "entry_key": key, "payload": value}
            else:
                doc = {**entry, "revision": entry["revision"] + 1, "updated_at": now, "payload": value}
            session.save(doc)
