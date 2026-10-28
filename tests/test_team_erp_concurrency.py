"""The ledger's write must be checked against the revision it read, not against whatever is committed when it writes (#124).

``tests/test_team_erp.py`` runs the same races with real threads on SQLite (which serializes writers) and, in CI, on PostgreSQL
(which does not). These tests make the interleaving explicit and deterministic: a repository with READ COMMITTED behaviour
(every statement sees what is committed *now*) lets another transaction commit exactly between a reader's ``get`` and ``put``.
"""
import copy
import unittest
from contextlib import contextmanager

from team.connectors.erp_ledger import Reconciler, StoreConflict
from team.connectors.erp_store import LedgerStore
from team.repository import IntegrityViolation, RevisionConflict

T, P = "ten_00000000000000000000000000000001", "erp-fake"
CHARGE = {"documento": "NF-1", "cliente": "Acme Ltda", "beneficiario": "Minha Empresa", "valor_centavos": 150_00,
          "moeda": "BRL", "status": "emitida", "vencimento": "2026-12-10"}


def event(event_id, status="paga", minute=1):
    return {"event_id": event_id, "documento": "NF-1", "status": status, "occurred_at": f"2026-11-01T10:{minute:02d}:00Z",
            "cliente": "Acme Ltda", "valor_centavos": 150_00, "moeda": "brl"}


class ReadCommittedSession:
    """What the ledger asks of a repository session, with the guarantees the real ones give (unique key, revision check).
    Writes stay in the session until the transaction commits and are dropped when it fails; the session sees its own writes."""

    def __init__(self, repo):
        self.repo, self.pending = repo, {}

    def view(self):
        return {**self.repo.rows, **self.pending}

    def list(self, kind, limit=1, **filters):
        found = [copy.deepcopy(d) for d in self.view().values() if all(d.get(k) == v for k, v in filters.items())][:limit]
        self.repo.reads += 1
        if self.repo.after_read is not None and self.repo.reads == self.repo.after_read[0]:
            _, hook = self.repo.after_read
            self.repo.after_read = None
            hook()  # another transaction commits now, after this statement already returned its (now stale) answer
        return found

    def save(self, doc, event=None):
        rows = self.view()
        stored = rows.get(doc["id"])
        if stored is None:
            if doc["revision"] != 1:
                raise RevisionConflict("novo documento deve iniciar na revisao 1")
            if any((d["namespace"], d["provider"], d["entry_key"]) == (doc["namespace"], doc["provider"], doc["entry_key"])
                   for d in rows.values()):
                raise IntegrityViolation("unique (tenant, namespace, provider, entry_key)")
        elif doc["revision"] != stored["revision"] + 1:
            raise RevisionConflict("revisao esperada")
        self.pending[doc["id"]] = copy.deepcopy(doc)
        return doc


class ReadCommittedRepository:
    def __init__(self):
        self.rows, self.reads, self.after_read = {}, 0, None

    @contextmanager
    def transaction(self, tenant_id):
        session = ReadCommittedSession(self)
        yield session  # an exception here skips the commit below: the whole transaction is dropped
        self.rows.update(session.pending)


class LedgerWriteTests(unittest.TestCase):
    def setUp(self):
        self.repo = ReadCommittedRepository()
        self.store, self.other = LedgerStore(self.repo), LedgerStore(self.repo)

    def test_an_event_inserted_by_another_transaction_after_our_read_is_a_conflict_not_an_update(self):
        with self.assertRaises(StoreConflict):
            with self.store.atomic(T):
                self.assertIsNone(self.store.get(T, "event", P, "e1"))
                self.other.put(T, "event", P, "e1", {"visto": True})  # commits in between
                self.store.put(T, "event", P, "e1", {"visto": True})  # used to "update" the winner's row and carry on
        self.assertEqual(len(self.repo.rows), 1)

    def test_a_charge_changed_by_another_transaction_after_our_read_is_not_overwritten(self):
        self.other.put(T, "charge", P, "NF-1", {"status": "emitida"})
        with self.assertRaises(StoreConflict):
            with self.store.atomic(T):
                self.assertEqual(self.store.get(T, "charge", P, "NF-1"), {"status": "emitida"})
                self.other.put(T, "charge", P, "NF-1", {"status": "paga"})  # commits in between
                self.store.put(T, "charge", P, "NF-1", {"status": "estornada"})  # used to replace "paga" with this
        self.assertEqual(self.other.get(T, "charge", P, "NF-1"), {"status": "paga"})

    def test_without_a_competitor_a_read_then_write_works_and_can_be_repeated(self):
        with self.store.atomic(T):
            self.assertIsNone(self.store.get(T, "charge", P, "NF-1"))
            self.store.put(T, "charge", P, "NF-1", {"status": "emitida"})
            self.assertEqual(self.store.get(T, "charge", P, "NF-1"), {"status": "emitida"})
            self.store.put(T, "charge", P, "NF-1", {"status": "paga"})  # our own earlier write is not a conflict
            self.store.put(T, "charge", P, "NF-1", {"status": "estornada"})
        self.assertEqual(self.other.get(T, "charge", P, "NF-1"), {"status": "estornada"})
        self.assertEqual([d["revision"] for d in self.repo.rows.values()], [3])

    def test_a_write_with_no_earlier_read_still_writes_over_what_is_there(self):
        self.store.put(T, "charge", P, "NF-1", {"status": "emitida"})
        self.store.put(T, "charge", P, "NF-1", {"status": "paga"})  # outside any read: nothing to check against, as before
        self.assertEqual(self.other.get(T, "charge", P, "NF-1"), {"status": "paga"})

    def test_the_first_read_is_what_a_write_is_checked_against_even_after_a_later_read(self):
        self.other.put(T, "charge", P, "NF-1", {"status": "emitida"})
        with self.assertRaises(StoreConflict):
            with self.store.atomic(T):
                self.store.get(T, "charge", P, "NF-1")
                self.other.put(T, "charge", P, "NF-1", {"status": "paga"})
                self.store.get(T, "charge", P, "NF-1")  # a re-read shows the winner's row, but our copy came from the first read
                self.store.put(T, "charge", P, "NF-1", {"status": "estornada"})
        self.assertEqual(self.other.get(T, "charge", P, "NF-1"), {"status": "paga"})


class ReconcilerRaceTests(unittest.TestCase):
    """The two races the PostgreSQL CI job caught, replayed step by step."""

    def setUp(self):
        self.repo = ReadCommittedRepository()
        self.loser, self.winner = Reconciler(LedgerStore(self.repo)), Reconciler(LedgerStore(self.repo))
        self.winner.record_issued(T, P, CHARGE)

    def race(self, loser_event, winner_event):
        """The loser reads event and charge; before it writes, the winner applies its event and commits."""
        outcome = {}
        self.repo.reads, self.repo.after_read = 0, (2, lambda: outcome.setdefault(
            "winner", self.winner.apply(T, P, winner_event, source="webhook")["resultado"]))
        outcome["loser"] = self.loser.apply(T, P, loser_event, source="webhook")["resultado"]
        return outcome

    def receipts(self):
        return len(self.winner.charge(T, P, "NF-1")["recibos"])

    def test_the_same_event_delivered_at_once_is_applied_once_and_the_other_is_a_duplicate(self):
        outcome = self.race(event("mesmo-id"), event("mesmo-id"))
        self.assertEqual(outcome, {"winner": "aplicado", "loser": "duplicado"})  # was: "aplicado" twice, or "ja_aplicado"
        self.assertEqual(self.receipts(), 1)

    def test_the_same_fact_with_different_ids_at_once_pays_once(self):
        outcome = self.race(event("id-b"), event("id-a"))
        self.assertEqual(outcome, {"winner": "aplicado", "loser": "ja_aplicado"})  # was: "aplicado" twice, the loser overwriting
        self.assertEqual(self.receipts(), 1)
        self.assertEqual(self.winner.charge(T, P, "NF-1")["status"], "paga")


if __name__ == "__main__":
    unittest.main()
