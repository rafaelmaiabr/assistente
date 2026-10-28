"""Durable store for the ERP charge ledger (#110), on the team repository (SQLite or PostgreSQL).

Same interface as ``MemoryStore``. Rows are ``erp_ledger_entry`` documents, unique per (company, namespace, provider, key),
so the database itself refuses to store the same event or the same issue key twice. ``atomic`` opens one repository
transaction for the company and every ``get``/``put`` inside it (on the same thread) shares it: a whole reconciliation step
commits or rolls back together, and one company's session cannot reach another company's rows.

A ``put`` writes against the revision the first ``get`` of that key saw in the same transaction (absent: an insert, which the
unique key guards; present: ``UPDATE ... WHERE revision = <seen>``). Re-reading the row at write time would let a writer that
committed in between be overwritten under PostgreSQL's READ COMMITTED (SQLite serializes writers, so it never showed there).
Losing that race is a ``StoreConflict``: the caller retries and sees the winner's result.
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
            self._local.scope = (tenant_id, session, {})  # {(namespace, provider, key): revision first seen, None when absent}
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
            yield current[1], current[2]
        else:
            with self.atomic(tenant_id):
                yield self._local.scope[1], self._local.scope[2]

    @staticmethod
    def _find(session, namespace, provider, key):
        found = session.list("erp_ledger_entry", limit=1, namespace=namespace, provider=provider, entry_key=key)
        return found[0] if found else None

    def get(self, tenant_id, namespace, provider, key):
        with self._session(tenant_id) as (session, seen):
            entry = self._find(session, namespace, provider, key)
            seen.setdefault((namespace, provider, key), None if entry is None else entry["revision"])  # the first read is what a write is checked against
        return None if entry is None else entry["payload"]

    def put(self, tenant_id, namespace, provider, key, value):
        with self._session(tenant_id) as (session, seen):
            slot, entry, now = (namespace, provider, key), self._find(session, namespace, provider, key), _now()
            # without an earlier read in this transaction there is nothing to check against: write over what is there now
            expected = seen[slot] if slot in seen else (None if entry is None else entry["revision"])
            if expected is None and entry is not None:
                raise RevisionConflict("o registro foi criado por outro processo; releia e tente de novo")
            if expected is not None and entry is None:
                raise RevisionConflict("o registro deixou de existir; releia e tente de novo")
            if entry is None:
                doc = {"kind": "erp_ledger_entry", "schema_version": 1, "id": new_id("erp_ledger_entry"), "tenant_id": tenant_id,
                       "revision": 1, "created_at": now, "updated_at": now, "namespace": namespace, "provider": provider,
                       "entry_key": key, "payload": value}
            else:
                doc = {**entry, "revision": expected + 1, "updated_at": now, "payload": value}  # not entry["revision"] + 1: that is what was lost
            session.save(doc)
            seen[slot] = doc["revision"]
