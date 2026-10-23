"""Companies, departments, contacts, links and access grants, always through an access context.

Every method takes a context built by ``AccessService``. Anything not visible to the context
(other company, no grant) raises ``NotFound`` exactly like a missing id, lists silently omit it,
and exports/events apply the same per-resource checks.
"""
from datetime import datetime, timezone

from .access import AccessContext, AccessError, NotFound, OperatorContext, PermissionDenied
from .ids import new_id
from .repository import DELETABLE, TABLES, IntegrityViolation, RevisionConflict, address_key
from .schema import SCHEMAS

SERVICE_KINDS = ("department", "contact", "contact_address", "contact_link", "access_grant")
# Fields an update may change; identity, tenant, references and history never change this way.
MUTABLE = {
    "department": ("name", "scope", "status"), "contact": ("display_name", "status"),
    "contact_link": ("relation",), "access_grant": ("actions", "expires_at"), "contact_address": (),
}
RESERVED = ("id", "kind", "tenant_id", "schema_version", "revision", "created_at", "updated_at")


class Conflict(AccessError):
    """The change collides with an existing record of the same tenant (for example a duplicate address)."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reference_fields(kind):
    return [(name, spec[4:].rstrip("?")) for name, spec in SCHEMAS[kind].items()
            if isinstance(spec, str) and spec.startswith("ref:") and name != "tenant_id"]


class TeamService:
    def __init__(self, repository, access):
        self.repository, self.access = repository, access

    # -- companies (operator level) -------------------------------------------------------------
    def _operator(self, operator):
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.access.operator_id:
            raise PermissionDenied()

    def create_company(self, operator, name):
        self._operator(operator)
        now = _now()
        doc = {"kind": "tenant", "schema_version": 1, "id": new_id("tenant"), "name": name, "status": "ativo",
               "revision": 1, "created_at": now, "updated_at": now}
        return self.repository.create_tenant(doc)

    def list_companies(self, operator):
        self._operator(operator)
        return self.repository.list_global("tenant")

    def update_company(self, operator, tenant_id, expected_revision, /, **changes):
        self._operator(operator)
        current = self.repository.get_global("tenant", tenant_id)
        if current is None:
            raise NotFound()
        if set(changes) - {"name", "status"}:
            raise ValueError("somente name e status podem mudar")
        if current["revision"] != expected_revision:
            raise RevisionConflict("revisao desatualizada")
        return self.repository.create_tenant({**current, **changes, "revision": expected_revision + 1,
                                              "updated_at": _now()})

    # -- tenant resources -------------------------------------------------------------------------
    def _checked(self, ctx, kind):
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()
        if kind not in SERVICE_KINDS:
            raise ValueError(f"tipo nao gerenciado aqui: {kind}")

    def _visible(self, ctx, session, action, kind, doc_id):
        if not self.access.allowed(ctx, session, action, kind, doc_id):
            raise NotFound()

    def get(self, ctx, kind, doc_id):
        self._checked(ctx, kind)
        with self.repository.transaction(ctx.tenant_id) as session:
            doc = session.get(kind, doc_id) if isinstance(doc_id, str) else None
            if doc is None or not self.access.allowed(ctx, session, "read", kind, doc_id):
                raise NotFound()
            return doc

    def list(self, ctx, kind, limit=100, **filters):
        self._checked(ctx, kind)
        with self.repository.transaction(ctx.tenant_id) as session:
            return [doc for doc in session.list(kind, limit=limit, **filters)
                    if self.access.allowed(ctx, session, "read", kind, doc["id"])]

    def create(self, ctx, kind, /, **fields):
        self._checked(ctx, kind)
        if set(fields) & set(RESERVED):
            raise ValueError("campos reservados sao definidos pelo backend: " + ", ".join(sorted(set(fields) & set(RESERVED))))
        if kind == "access_grant" and ctx.role != "operator":
            raise PermissionDenied()
        now = _now()
        doc = {"kind": kind, "schema_version": 1, "id": new_id(kind), "tenant_id": ctx.tenant_id,
               "revision": 1, "created_at": now, "updated_at": now, **fields}
        with self.repository.transaction(ctx.tenant_id) as session:
            if not self.access.allowed(ctx, session, "write", kind):
                raise PermissionDenied()
            self._check_references(ctx, session, kind, doc)
            try:
                return session.save(doc)
            except IntegrityViolation as error:
                raise Conflict("registro duplicado ou referencia invalida") from error

    def _check_references(self, ctx, session, kind, doc):
        for name, target_kind in _reference_fields(kind):
            target = doc.get(name)
            if target is None:
                continue
            if not isinstance(target, str) or session.get(target_kind, target) is None \
                    or not self.access.allowed(ctx, session, "relate", target_kind, target):
                raise NotFound()

    def update(self, ctx, kind, doc_id, expected_revision, /, **changes):
        self._checked(ctx, kind)
        if set(changes) - set(MUTABLE[kind]):
            raise ValueError("campos nao alteraveis: " + ", ".join(sorted(set(changes) - set(MUTABLE[kind]))))
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get(kind, doc_id) if isinstance(doc_id, str) else None
            if current is None or not self.access.allowed(ctx, session, "write", kind, doc_id):
                raise NotFound()
            if current["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            return session.save({**current, **changes, "revision": expected_revision + 1, "updated_at": _now()})

    def deactivate(self, ctx, kind, doc_id, expected_revision):
        if kind not in ("department", "contact"):
            raise ValueError("somente departamentos e contatos podem ser desativados")
        return self.update(ctx, kind, doc_id, expected_revision, status="inativo")

    def delete(self, ctx, kind, doc_id):
        self._checked(ctx, kind)
        if kind not in DELETABLE:
            raise ValueError(f"{kind} nao pode ser removido; desative-o")
        with self.repository.transaction(ctx.tenant_id) as session:
            if not isinstance(doc_id, str) or session.get(kind, doc_id) is None \
                    or not self.access.allowed(ctx, session, "write", kind, doc_id):
                raise NotFound()
            session.delete(kind, doc_id)

    # -- contact identity --------------------------------------------------------------------------
    def observe_address(self, ctx, channel, address):
        """Record a sender address as *observed*; it identifies nobody until an operator links it."""
        self._checked(ctx, "contact_address")
        with self.repository.transaction(ctx.tenant_id) as session:
            if not self.access.allowed(ctx, session, "write", "contact_address"):
                raise PermissionDenied()
            found = session.list("contact_address", address_key=address_key(channel, address), limit=1)
        if found:
            return found[0]
        return self.create(ctx, "contact_address", contact_id=None, channel=channel, address=address,
                           link_state="observado")

    def link_address(self, ctx, address_id, contact_id, expected_revision):
        return self._relink(ctx, address_id, contact_id, "vinculado", expected_revision)

    def unlink_address(self, ctx, address_id, expected_revision):
        return self._relink(ctx, address_id, None, "observado", expected_revision)

    def _relink(self, ctx, address_id, contact_id, state, expected_revision):
        self._checked(ctx, "contact_address")
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("contact_address", address_id) if isinstance(address_id, str) else None
            if current is None or not self.access.allowed(ctx, session, "relate", "contact_address", address_id):
                raise NotFound()
            if current["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            changed = {**current, "contact_id": contact_id, "link_state": state,
                       "revision": expected_revision + 1, "updated_at": _now()}
            self._check_references(ctx, session, "contact_address", changed)
            return session.save(changed)

    def find_contact_by_address(self, ctx, channel, address):
        """The contact behind an address, only when an operator linked it; observed senders resolve to None."""
        self._checked(ctx, "contact_address")
        with self.repository.transaction(ctx.tenant_id) as session:
            for found in session.list("contact_address", address_key=address_key(channel, address), limit=1):
                if found["link_state"] != "vinculado" \
                        or not self.access.allowed(ctx, session, "read", "contact_address", found["id"]):
                    continue
                contact = session.get("contact", found["contact_id"])
                if contact and self.access.allowed(ctx, session, "read", "contact", contact["id"]):
                    return contact
        return None

    # -- export and events -------------------------------------------------------------------------
    def export(self, ctx):
        """All documents of the tenant that the context may export; same per-resource checks as reads."""
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()
        documents = []
        with self.repository.transaction(ctx.tenant_id) as session:
            for kind in TABLES:
                if kind in ("tenant", "skill_definition"):
                    continue
                for doc in session.list(kind, limit=1000):
                    if kind == "access_grant" and ctx.role != "operator":
                        continue
                    if self.access.allowed(ctx, session, "export", kind, doc["id"]):
                        documents.append(doc)
            if ctx.role == "operator":
                tenant = self.repository.get_global("tenant", ctx.tenant_id)
                documents.insert(0, tenant)
        return {"tenant_id": ctx.tenant_id, "documents": documents}

    def events(self, ctx, case_id=None, after_seq=0, limit=100):
        """Events of one readable case, or of every case when the context may read all events."""
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()
        with self.repository.transaction(ctx.tenant_id) as session:
            if case_id is not None:
                if not isinstance(case_id, str) or session.get("case", case_id) is None \
                        or not self.access.allowed(ctx, session, "read", "case", case_id) \
                        or not self.access.allowed(ctx, session, "read", "event"):
                    raise NotFound()
            elif not self.access.allowed(ctx, session, "read", "event"):
                raise PermissionDenied()
            return session.events(case_id, after_seq, limit)
