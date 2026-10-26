"""Knowledge base and memory with scope, versions, retention and local full-text search (SQLite FTS5 or PostgreSQL ``tsvector``).

Three things stay separate: the **case history** (read from the case), **confirmed memory** (dated
preferences and facts) and **published knowledge** (versioned documents with a source). Retrieval
authorizes before it searches, caps what it returns, and hands everything back as *untrusted data*:
nothing retrieved can change a policy, a permission or the system prompt. Memory inferred by an agent
is only a proposal until the operator confirms it. Revoking, expiring or deleting removes the text
from the search index at once.

The index is SQLite FTS5 on the SQLite repository and a ``tsvector`` column (``simple`` configuration, GIN index) on PostgreSQL.
Both index the same words (accents removed, letters and digits only), so a query finds the same documents; only the order of the
results can differ, because the two engines rank differently. The excerpt is cut by the database on SQLite and in Python on
PostgreSQL (``excerpt``), so the text shown is the same kind of window around the first match.
"""
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from .access import AccessContext, NotFound, PermissionDenied
from .cases import clean_text
from .ids import new_id
from .prompting import ExternalItem
from .repository import RevisionConflict
from .schema import content_hash
from .triage import looks_like_injection

MAX_TITLE = 200
MAX_BODY = 20_000
MAX_ITEMS = 6
MAX_CHARS = 4_000
EXCERPT = 600
RETENTION_REVOKED_DAYS = 90
HISTORY_ENTRIES = 8


class KnowledgeRejected(ValueError):
    """The change is not allowed; the message is safe to show."""


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def query_words(text):
    """The search words of a query: lowercase ASCII letters and digits (accents removed), at most 12, no operators."""
    plain = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return list(dict.fromkeys(re.findall(r"[a-z0-9]{2,}", plain)))[:12]


def fts_query(text):
    """A safe FTS5 query: quoted word tokens joined with OR (user text can never inject operators)."""
    return " OR ".join(f'"{w}"' for w in query_words(text))


def _fold_chars(text):
    """Lowercase ASCII-folded text with exactly one character per original character (so positions line up)."""
    return "".join((unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode() or c)[:1].lower() for c in text)


def index_text(title, body):
    """What the PostgreSQL index stores: the words of the title and body, folded the way the SQLite tokenizer does."""
    return " ".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", f"{title} {body}").encode("ascii", "ignore").decode().lower()))


def excerpt(body, words, size=40):
    """A window of about ``size`` words around the first occurrence of any search word (the SQLite ``snippet`` counterpart)."""
    pieces = list(re.finditer(r"\S+", body))
    if not pieces:
        return ""
    folded = _fold_chars(body)
    hit = re.search(r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, words)) + r")(?![a-z0-9])", folded) if words else None
    at = next((i for i, m in enumerate(pieces) if hit and m.end() > hit.start()), 0)
    start = max(at - size // 4, 0)
    end = min(start + size, len(pieces))
    text = body[pieces[start].start():pieces[end - 1].end()]
    return ("…" if start else "") + text + ("…" if end < len(pieces) else "")


class KnowledgeService:
    def __init__(self, repository, access, clock=lambda: datetime.now(timezone.utc)):
        self.repository, self.access, self.clock = repository, access, clock

    # -- helpers -----------------------------------------------------------------------------------------------------
    def _ctx(self, ctx):
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()

    def _operator(self, ctx):
        self._ctx(ctx)
        if ctx.role != "operator" or ctx.principal_id != self.access.operator_id:
            raise PermissionDenied("apenas o operador confiavel gerencia conhecimento e memoria")

    def _doc(self, session, doc_id):
        doc = session.get("knowledge_document", doc_id) if isinstance(doc_id, str) else None
        if doc is None:
            raise NotFound()
        return doc

    def _postgres(self):
        return getattr(self.repository, "dialect", "sqlite") == "postgres"

    def _index(self, session, doc):
        if self._postgres():
            session.conn.execute("INSERT INTO knowledge_index (title, body, tenant_id, doc_id, tsv) VALUES (?, ?, ?, ?, to_tsvector('simple', ?))",
                                 (doc["title"], doc["body"], doc["tenant_id"], doc["id"], index_text(doc["title"], doc["body"])))
            return
        session.conn.execute("INSERT INTO knowledge_index (title, body, tenant_id, doc_id) VALUES (?, ?, ?, ?)",
                             (doc["title"], doc["body"], doc["tenant_id"], doc["id"]))

    def _unindex(self, session, doc_id):
        session.conn.execute("DELETE FROM knowledge_index WHERE doc_id = ?", (doc_id,))

    def _save(self, session, doc, **changes):
        return session.save({**doc, **changes, "revision": doc["revision"] + 1, "updated_at": _stamp(self.clock())})

    # -- documents ---------------------------------------------------------------------------------------------------
    def publish(self, ctx, *, doc_key, title, body, source, department_id=None, topic=None, valid_until=None,
                accept_risk=False):
        """Publish a document, or the next version of ``doc_key`` (a correction). The old version leaves the index."""
        self._operator(ctx)
        text, heading = clean_text(body), clean_text(title)
        if len(text) > MAX_BODY or len(heading) > MAX_TITLE:
            raise KnowledgeRejected("Documento grande demais para a base de conhecimento.")
        suspicious = looks_like_injection(text) or looks_like_injection(heading)
        if suspicious and not accept_risk:
            raise KnowledgeRejected("O texto parece instruir o assistente (ex.: ignorar regras). Revise ou aceite o risco "
                                    "explicitamente; ele continuara sendo tratado apenas como dado.")
        now = _stamp(self.clock())
        with self.repository.transaction(ctx.tenant_id) as session:
            if department_id is not None and session.get("department", department_id) is None:
                raise NotFound()
            versions = session.list("knowledge_document", doc_key=doc_key, limit=1000)
            version = max((d["version"] for d in versions), default=0) + 1
            doc = {"kind": "knowledge_document", "schema_version": 1, "id": new_id("knowledge_document"),
                   "tenant_id": ctx.tenant_id, "revision": 1, "created_at": now, "updated_at": now,
                   "department_id": department_id, "doc_key": doc_key, "version": version, "title": heading, "body": text,
                   "source": clean_text(source)[:300], "topic": topic, "status": "publicado", "valid_until": valid_until,
                   "published_by": ctx.principal_id, "content_hash": content_hash({"title": heading, "body": text}),
                   "suspicious": suspicious}
            for old in versions:
                if old["status"] == "publicado":
                    self._save(session, old, status="substituido")
                    self._unindex(session, old["id"])
            session.save(doc)
            self._index(session, doc)
            return doc

    def revoke(self, ctx, doc_id, reason):
        """Withdraw a document: it leaves the index immediately and is purged after the retention period."""
        self._operator(ctx)
        clean_text(reason)
        with self.repository.transaction(ctx.tenant_id) as session:
            doc = self._doc(session, doc_id)
            if doc["status"] != "publicado":
                raise KnowledgeRejected("Somente documentos publicados podem ser revogados.")
            self._unindex(session, doc_id)
            return self._save(session, doc, status="revogado")

    def delete(self, ctx, doc_id):
        """Erase the content now (and from the index), keeping only a tombstone with no text."""
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            doc = self._doc(session, doc_id)
            self._unindex(session, doc_id)
            return self._save(session, doc, status="excluido", title="[removido]", body="[removido]",
                              source="[removido]", content_hash=content_hash({"removido": doc_id}), suspicious=False)

    def get_document(self, ctx, doc_id):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            return self._doc(session, doc_id)

    def list_documents(self, ctx, doc_key=None, status=None):
        self._operator(ctx)
        filters = {k: v for k, v in (("doc_key", doc_key), ("status", status)) if v}
        with self.repository.transaction(ctx.tenant_id) as session:
            return sorted(session.list("knowledge_document", limit=1000, **filters), key=lambda d: (d["doc_key"], d["version"]))

    # -- memory ------------------------------------------------------------------------------------------------------
    def _memory(self, session, memory_id):
        item = session.get("memory_item", memory_id) if isinstance(memory_id, str) else None
        if item is None:
            raise NotFound()
        return item

    def _new_memory(self, ctx, state, *, scope, text, source, memory_kind="preferencia", department_id=None, subject=None,
                    expires_at=None):
        now = _stamp(self.clock())
        doc = {"kind": "memory_item", "schema_version": 1, "id": new_id("memory_item"), "tenant_id": ctx.tenant_id,
               "revision": 1, "created_at": now, "updated_at": now, "scope": scope, "department_id": department_id,
               "subject": subject, "memory_kind": memory_kind, "text": clean_text(text)[:2000], "state": state,
               "source": clean_text(source)[:300], "proposed_by": ctx.principal_id, "decided_by": None, "decided_at": None,
               "observed_at": now, "expires_at": expires_at}
        if state == "confirmada":
            doc["decided_by"], doc["decided_at"] = ctx.principal_id, now
        with self.repository.transaction(ctx.tenant_id) as session:
            return session.save(doc)

    def propose_memory(self, ctx, **fields):
        """Anyone in the team may *suggest* a memory; it is not used until the operator confirms it."""
        self._ctx(ctx)
        return self._new_memory(ctx, "proposta", **fields)

    def remember(self, ctx, **fields):
        """The operator records a confirmed memory directly."""
        self._operator(ctx)
        return self._new_memory(ctx, "confirmada", **fields)

    def decide_memory(self, ctx, memory_id, expected_revision, confirm, note=None):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            item = self._memory(session, memory_id)
            if item["state"] != "proposta":
                raise KnowledgeRejected("Esta memoria ja foi decidida.")
            if item["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            return session.save({**item, "state": "confirmada" if confirm else "rejeitada", "decided_by": ctx.principal_id,
                                 "decided_at": _stamp(self.clock()), "revision": item["revision"] + 1,
                                 "updated_at": _stamp(self.clock())})

    def forget(self, ctx, memory_id):
        """Delete a memory for good."""
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            self._memory(session, memory_id)
            session.delete("memory_item", memory_id)

    def list_memory(self, ctx, state=None):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            return session.list("memory_item", limit=1000, **({"state": state} if state else {}))

    def reindex(self, tenant_id, now=None):
        """Rebuild this company's search index from the documents (the index is derived data: it is not copied between databases)."""
        moment = now or self.clock()
        with self.repository.transaction(tenant_id) as session:
            session.conn.execute("DELETE FROM knowledge_index WHERE tenant_id = ?", (tenant_id,))
            count = 0
            for doc in session.list("knowledge_document", limit=1000):
                if doc["status"] == "publicado" and not (doc["valid_until"] and _parse(doc["valid_until"]) <= moment):
                    self._index(session, doc)
                    count += 1
        return count

    # -- retention ---------------------------------------------------------------------------------------------------
    def purge(self, tenant_id, now=None):
        """Backend sweep: expire dated documents and memories, and erase revoked documents past the retention period."""
        moment = now or self.clock()
        expired, erased = [], []
        with self.repository.transaction(tenant_id) as session:
            for doc in session.list("knowledge_document", limit=1000):
                if doc["status"] == "publicado" and doc["valid_until"] and _parse(doc["valid_until"]) <= moment:
                    self._unindex(session, doc["id"])
                    expired.append(self._save(session, doc, status="expirado")["id"])
                elif doc["status"] in ("revogado", "expirado", "substituido") and \
                        _parse(doc["updated_at"]) + timedelta(days=RETENTION_REVOKED_DAYS) <= moment:
                    self._unindex(session, doc["id"])
                    self._save(session, doc, status="excluido", title="[removido]", body="[removido]", source="[removido]",
                               content_hash=content_hash({"removido": doc["id"]}), suspicious=False)
                    erased.append(doc["id"])
            for item in session.list("memory_item", state="confirmada", limit=1000):
                if item["expires_at"] and _parse(item["expires_at"]) <= moment:
                    session.save({**item, "state": "expirada", "revision": item["revision"] + 1, "updated_at": _stamp(moment)})
                    expired.append(item["id"])
        return {"expired": expired, "erased": erased}

    # -- retrieval ---------------------------------------------------------------------------------------------------
    def retrieve(self, ctx, query, *, department_id=None, case_id=None, customer_ref=None, max_items=MAX_ITEMS,
                 max_chars=MAX_CHARS):
        """Authorized, bounded context for one run: knowledge, confirmed preferences and case history, kept apart.

        An agent's department comes from its own definition (never from an argument); the operator may state one.
        Everything returned is ``untrusted`` data with its source, version and date.
        """
        self._ctx(ctx)
        now = self.clock()
        budget = {"left": max(int(max_chars), 0), "truncated": False}
        with self.repository.transaction(ctx.tenant_id) as session:
            if ctx.role == "agent":
                agent = session.get("agent_definition", ctx.principal_id)
                department_id = agent["department_id"] if agent else None
            knowledge = self._search(session, ctx, query, department_id, now, max_items, budget)
            preferences = self._preferences(session, department_id, customer_ref, now, budget)
            history = self._history(session, ctx, case_id, budget)
        conflicts = {}
        for item in knowledge:
            if item["topic"]:
                conflicts.setdefault(item["topic"], []).append(item)
        for group in conflicts.values():
            if len({i["content_hash"] for i in group}) > 1 and len({i["source"] for i in group}) > 1:
                for item in group:
                    item["conflict"] = True
        for item in knowledge:
            item.pop("content_hash", None)
            item.setdefault("conflict", False)
        return {"knowledge": knowledge, "preferences": preferences, "history": history,
                "truncated": budget["truncated"], "department_id": department_id}

    def _take(self, text, budget, limit=EXCERPT):
        piece = text[:limit]
        if len(piece) > budget["left"]:
            piece, budget["truncated"] = piece[:budget["left"]], True
        if len(text) > len(piece):
            budget["truncated"] = True
        budget["left"] -= len(piece)
        return piece

    def _search(self, session, ctx, query, department_id, now, max_items, budget):
        match = fts_query(query)
        if not match or budget["left"] <= 0:
            return []
        limit = max(int(max_items), 1) * 3
        if self._postgres():
            words = query_words(query)
            tsquery = " | ".join(words)
            rows = [(doc_id, excerpt(body, words)) for doc_id, body in session.conn.execute(
                "SELECT doc_id, body FROM knowledge_index WHERE tenant_id = ? AND tsv @@ to_tsquery('simple', ?) "
                "ORDER BY ts_rank(tsv, to_tsquery('simple', ?)) DESC, doc_id LIMIT ?",
                (ctx.tenant_id, tsquery, tsquery, limit)).fetchall()]
        else:
            rows = session.conn.execute(
                "SELECT doc_id, snippet(knowledge_index, 1, '', '', '…', 40) FROM knowledge_index "
                "WHERE knowledge_index MATCH ? AND tenant_id = ? ORDER BY rank LIMIT ?",
                (match, ctx.tenant_id, limit)).fetchall()
        found = []
        for doc_id, snippet in rows:
            doc = session.get("knowledge_document", doc_id)
            if doc is None or doc["status"] != "publicado" or doc["tenant_id"] != ctx.tenant_id:
                continue  # the database is the authority: a stale index row never leaks content
            if doc["valid_until"] and _parse(doc["valid_until"]) <= now:
                continue
            if doc["department_id"] not in (None, department_id):
                continue  # another department's document is not authorized for this caller
            if len(found) >= max_items or budget["left"] <= 0:
                budget["truncated"] = budget["truncated"] or len(found) >= max_items
                break
            found.append({"id": doc["id"], "title": doc["title"], "excerpt": self._take(snippet or doc["body"], budget),
                          "source": doc["source"], "version": doc["version"], "doc_key": doc["doc_key"],
                          "topic": doc["topic"], "department_id": doc["department_id"], "published_at": doc["created_at"],
                          "valid_until": doc["valid_until"], "suspicious": bool(doc.get("suspicious")),
                          "content_hash": doc["content_hash"], "untrusted": True})
        return found

    def _preferences(self, session, department_id, customer_ref, now, budget):
        selected = []
        for item in session.list("memory_item", state="confirmada", limit=1000):
            if item["expires_at"] and _parse(item["expires_at"]) <= now:
                continue
            scope = item["scope"]
            if scope == "departamento" and item["department_id"] != department_id:
                continue
            if scope == "cliente" and item["subject"] != customer_ref:
                continue
            if budget["left"] <= 0:
                budget["truncated"] = True
                break
            selected.append({"id": item["id"], "scope": scope, "text": self._take(item["text"], budget),
                             "kind": item["memory_kind"], "source": item["source"], "observed_at": item["observed_at"],
                             "expires_at": item["expires_at"], "untrusted": True})
        return selected

    def _history(self, session, ctx, case_id, budget):
        if not case_id:
            return []
        case = session.get("case", case_id)
        if case is None or not self.access.allowed(ctx, session, "read", "case", case_id):
            return []
        entries = sorted(session.list("case_entry", case_id=case_id, limit=1000), key=lambda e: (e["created_at"], e["id"]))
        out = []
        for entry in entries[-HISTORY_ENTRIES:]:
            if budget["left"] <= 0:
                budget["truncated"] = True
                break
            out.append({"id": entry["id"], "type": entry["entry_type"], "direction": entry.get("direction"),
                        "text": self._take(entry["body"], budget, 400), "at": entry["created_at"], "untrusted": True})
        return out

    @staticmethod
    def external_items(bundle):
        """The retrieved context as ``ExternalItem`` data for ``compile_context`` (escaped and kept out of the system prompt)."""
        items = []
        for k in bundle["knowledge"]:
            flags = (" [conflita com outra fonte]" if k["conflict"] else "") + (" [texto suspeito]" if k["suspicious"] else "")
            items.append(ExternalItem(f"conhecimento: {k['title']} v{k['version']} ({k['source']})", "conhecimento",
                                      k["excerpt"] + flags))
        for p in bundle["preferences"]:
            items.append(ExternalItem(f"memoria confirmada ({p['scope']}, {p['observed_at']})", "memoria", p["text"]))
        for h in bundle["history"]:
            items.append(ExternalItem(f"historico da demanda ({h['type']}, {h['at']})", "historico", h["text"]))
        return items
