"""Who is asking: people who sign in with Google, their role in each company, and revocable sessions.

This is only used by the remote listener. The local owner (the person at the PC, on loopback) is a different mode: it is never
a "member", never counted as a global administrator of a remote environment, and cannot be reached through a session.

* A person can sign in only if an administrator (or the local owner) registered their email and gave them a role in at least
  one company. Registering is not signing in: the Google account ID (``sub``) is bound at the first successful sign-in and must
  match afterwards, so a recycled email address does not inherit the access.
* Roles are per company: ``leitor`` (read) < ``atendente`` (work cases) < ``aprovador`` (decide approvals) < ``admin``
  (configure the company, manage members). The company comes from the URL and is checked against the membership on every
  request; nothing the browser sends can name another one.
* A member below ``admin`` may be limited to some departments of the company; an administrator never is. Records of other
  departments answer "not found" (see ``AccessService.in_scope``).
* Only the SHA-256 of a session token is stored. Every request looks the session, the person and the membership up again, so
  revoking a session, deactivating a person or changing a role takes effect on the very next request.
* The trail (``auth_events``) records sign-ins, refusals, sign-outs and membership changes with ids and reasons, never tokens,
  codes or names.
"""
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .db import connect
from .access import HUMAN_ROLES, AccessContext, NotFound, OperatorContext, PermissionDenied
from .ids import new_id

IDLE = timedelta(minutes=480)
ABSOLUTE = timedelta(hours=168)
TOUCH_EVERY = timedelta(seconds=60)
_EMAIL = re.compile(r"^[^@\s,;<>()\[\]\\\"]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
_TOKEN = object()


class IdentityRejected(ValueError):
    """The request is refused; the message is safe to show."""


class LoginDenied(Exception):
    """Signing in is not allowed. One generic outcome for the person; the real reason goes only to the trail."""

    def __init__(self, reason):
        super().__init__("conta nao autorizada")
        self.reason = reason


@dataclass(frozen=True)
class Identity:
    """A verified session for the current request, produced only by ``IdentityService.resolve``."""
    user_id: str
    email: str
    name: str | None
    session_id: str
    memberships: dict  # tenant id -> role, active memberships only
    _token: object = field(repr=False, compare=False)
    scopes: dict = field(default_factory=dict, compare=False)  # tenant id -> tuple of department ids (absent = whole company)

    def __post_init__(self):
        if self._token is not _TOKEN:
            raise PermissionDenied("identidade so pode ser criada pelo backend")


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def hash_token(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def normalize_email(value):
    email = str(value or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise IdentityRejected("Informe um email valido.")
    return email


class IdentityService:
    def __init__(self, repository, access, clock=lambda: datetime.now(timezone.utc), idle=IDLE, absolute=ABSOLUTE):
        self.repository, self.access, self.clock, self.idle, self.absolute = repository, access, clock, idle, absolute

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _now(self):
        return self.clock()

    def _event(self, conn, kind, *, user_id=None, tenant_id=None, actor_id=None, detail=None):
        conn.execute("INSERT INTO auth_events (at, kind, user_id, tenant_id, actor_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
                     (_stamp(self._now()), kind, user_id, tenant_id, actor_id, (detail or "")[:200] or None))

    def _manager(self, ctx, tenant_id):
        """The local owner or a company administrator, for this company only. Returns the id to record as the actor."""
        if not isinstance(ctx, AccessContext) or ctx.tenant_id != tenant_id or not self.access.is_operator(ctx, "admin"):
            raise PermissionDenied("apenas o dono local ou um administrador da empresa gerencia membros")
        return ctx.principal_id

    def _local_owner(self, operator):
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.access.operator_id:
            raise PermissionDenied("apenas o dono local pode fazer isto")
        return operator.principal_id

    # -- members (managed by the owner or a company administrator) -----------------------------------------------------
    def _departments(self, tenant_id, role, departments):
        """Validated JSON for the scope column: ``None`` (whole company) or a sorted list of this company's departments."""
        if not departments:
            return None
        if role == "admin":
            raise IdentityRejected("Um administrador sempre alcanca a empresa inteira; escolha outro papel para limitar por departamento.")
        if not isinstance(departments, (list, tuple)) or len(departments) > 50 or any(not isinstance(d, str) for d in departments):
            raise IdentityRejected("Informe uma lista de departamentos.")
        wanted = sorted(set(departments))
        with self.repository.transaction(tenant_id) as session:
            if any(session.get("department", d) is None for d in wanted):
                raise IdentityRejected("Algum departamento nao existe nesta empresa.")
        return json.dumps(wanted)

    def invite(self, ctx, tenant_id, email, role, display_name=None, departments=None):
        """Register an email with a role in this company. Creates the person if new; they sign in with Google afterwards."""
        actor = self._manager(ctx, tenant_id)
        email = normalize_email(email)
        if role not in HUMAN_ROLES:
            raise IdentityRejected("Papel desconhecido.")
        scope = self._departments(tenant_id, role, departments)
        name = str(display_name).strip()[:80] if display_name else None
        now = _stamp(self._now())
        with self.repository._transaction() as conn:
            user = conn.execute("SELECT id, status FROM auth_users WHERE email = ?", (email,)).fetchone()
            if user is None:
                user_id = new_id("user")
                conn.execute("INSERT INTO auth_users (id, email, display_name, status, created_at, updated_at) "
                             "VALUES (?, ?, ?, 'ativo', ?, ?)", (user_id, email, name, now, now))
            else:
                user_id = user[0]
                if user[1] != "ativo":
                    raise IdentityRejected("Esta pessoa foi desativada pelo dono local e nao pode ser convidada.")
            existing = conn.execute("SELECT status FROM auth_memberships WHERE user_id = ? AND tenant_id = ?",
                                    (user_id, tenant_id)).fetchone()
            if existing is None:
                conn.execute("INSERT INTO auth_memberships (user_id, tenant_id, role, status, granted_by, created_at, updated_at, departments) "
                             "VALUES (?, ?, ?, 'ativo', ?, ?, ?, ?)", (user_id, tenant_id, role, actor, now, now, scope))
            else:
                conn.execute("UPDATE auth_memberships SET role = ?, status = 'ativo', granted_by = ?, updated_at = ?, departments = ? "
                             "WHERE user_id = ? AND tenant_id = ?", (role, actor, now, scope, user_id, tenant_id))
            self._event(conn, "membro_convidado", user_id=user_id, tenant_id=tenant_id, actor_id=actor, detail=role)
        return self._member(tenant_id, user_id)

    def set_role(self, ctx, tenant_id, user_id, role):
        actor = self._manager(ctx, tenant_id)
        if role not in HUMAN_ROLES:
            raise IdentityRejected("Papel desconhecido.")
        with self.repository._transaction() as conn:
            row = self._membership_row(conn, tenant_id, user_id)
            self._keep_an_admin(conn, ctx, tenant_id, user_id, row, new_role=role)
            # becoming an administrator drops any department limit: the two never coexist
            conn.execute("UPDATE auth_memberships SET role = ?, updated_at = ?, departments = CASE WHEN ? = 'admin' THEN NULL ELSE departments END "
                         "WHERE user_id = ? AND tenant_id = ?", (role, _stamp(self._now()), role, user_id, tenant_id))
            self._event(conn, "papel_alterado", user_id=user_id, tenant_id=tenant_id, actor_id=actor, detail=f"{row[0]}->{role}")
        return self._member(tenant_id, user_id)

    def set_departments(self, ctx, tenant_id, user_id, departments):
        """Limit a member to some departments (an empty list means the whole company). Never for an administrator."""
        actor = self._manager(ctx, tenant_id)
        with self.repository._transaction() as conn:
            row = self._membership_row(conn, tenant_id, user_id)
        scope = self._departments(tenant_id, row[0], departments)
        with self.repository._transaction() as conn:
            self._membership_row(conn, tenant_id, user_id)
            conn.execute("UPDATE auth_memberships SET departments = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ?",
                         (scope, _stamp(self._now()), user_id, tenant_id))
            self._event(conn, "escopo_alterado", user_id=user_id, tenant_id=tenant_id, actor_id=actor, detail=f"{len(json.loads(scope)) if scope else 0} departamentos")
        return self._member(tenant_id, user_id)

    def set_member_status(self, ctx, tenant_id, user_id, status):
        actor = self._manager(ctx, tenant_id)
        if status not in ("ativo", "inativo"):
            raise IdentityRejected("Situacao desconhecida.")
        with self.repository._transaction() as conn:
            row = self._membership_row(conn, tenant_id, user_id)
            if status == "inativo":
                self._keep_an_admin(conn, ctx, tenant_id, user_id, row, new_role=None)
            conn.execute("UPDATE auth_memberships SET status = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ?",
                         (status, _stamp(self._now()), user_id, tenant_id))
            self._event(conn, "membro_ativado" if status == "ativo" else "membro_desativado", user_id=user_id,
                        tenant_id=tenant_id, actor_id=actor)
        return self._member(tenant_id, user_id)

    @staticmethod
    def _membership_row(conn, tenant_id, user_id):
        row = conn.execute("SELECT role, status FROM auth_memberships WHERE user_id = ? AND tenant_id = ?",
                           (str(user_id), tenant_id)).fetchone()
        if row is None:
            raise NotFound()
        return row

    @staticmethod
    def _keep_an_admin(conn, ctx, tenant_id, user_id, row, new_role):
        """A company administrator cannot lock the company out: not by demoting or deactivating themselves, and never the last one."""
        if ctx.member_role is not None and ctx.principal_id == user_id:
            raise IdentityRejected("Voce nao pode reduzir o proprio acesso; peca a outro administrador.")
        if row[0] == "admin" and row[1] == "ativo" and new_role != "admin":
            others = conn.execute("SELECT COUNT(*) FROM auth_memberships WHERE tenant_id = ? AND role = 'admin' AND status = 'ativo' "
                                  "AND user_id != ?", (tenant_id, user_id)).fetchone()[0]
            if others == 0:
                raise IdentityRejected("A empresa precisa de ao menos um administrador ativo.")

    def _member(self, tenant_id, user_id):
        return next(m for m in self.members(None, tenant_id, _trusted=True) if m["user_id"] == user_id)

    def members(self, ctx, tenant_id, _trusted=False):
        if not _trusted:
            self._manager(ctx, tenant_id)
        with self.repository._transaction() as conn:
            rows = conn.execute(
                "SELECT u.id, u.email, u.display_name, u.status, u.google_sub IS NOT NULL, u.last_login_at, m.role, m.status, m.departments "
                "FROM auth_memberships m JOIN auth_users u ON u.id = m.user_id WHERE m.tenant_id = ? ORDER BY u.email",
                (tenant_id,)).fetchall()
        return [{"user_id": r[0], "email": r[1], "name": r[2], "user_status": r[3], "signed_in_before": bool(r[4]),
                 "last_login_at": r[5], "role": r[6], "status": r[7], "departments": json.loads(r[8]) if r[8] else []} for r in rows]

    # -- the local owner's controls over people and sessions --------------------------------------------------------------
    def set_user_status(self, operator, user_id, status):
        """Disable (or enable) a person everywhere. Disabling ends every session at once."""
        actor = self._local_owner(operator)
        if status not in ("ativo", "inativo"):
            raise IdentityRejected("Situacao desconhecida.")
        now = _stamp(self._now())
        with self.repository._transaction() as conn:
            if conn.execute("SELECT 1 FROM auth_users WHERE id = ?", (str(user_id),)).fetchone() is None:
                raise NotFound()
            conn.execute("UPDATE auth_users SET status = ?, updated_at = ? WHERE id = ?", (status, now, user_id))
            if status == "inativo":
                conn.execute("UPDATE auth_sessions SET revoked_at = ?, revoked_reason = 'usuario_desativado' "
                             "WHERE user_id = ? AND revoked_at IS NULL", (now, user_id))
            self._event(conn, "usuario_desativado" if status == "inativo" else "usuario_ativado", user_id=user_id, actor_id=actor)

    def revoke_sessions(self, operator, user_id, reason="revogada_pelo_dono"):
        actor = self._local_owner(operator)
        now = _stamp(self._now())
        with self.repository._transaction() as conn:
            count = conn.execute("UPDATE auth_sessions SET revoked_at = ?, revoked_reason = ? WHERE user_id = ? AND revoked_at IS NULL",
                                 (now, reason[:60], str(user_id))).rowcount
            self._event(conn, "sessoes_revogadas", user_id=str(user_id), actor_id=actor, detail=str(count))
        return count

    def sessions(self, operator, user_id):
        self._local_owner(operator)
        with self.repository._transaction() as conn:
            rows = conn.execute("SELECT id, created_at, last_seen_at, expires_at, revoked_at FROM auth_sessions WHERE user_id = ? "
                                "ORDER BY created_at DESC LIMIT 50", (str(user_id),)).fetchall()
        return [{"id": r[0], "created_at": r[1], "last_seen_at": r[2], "expires_at": r[3], "revoked_at": r[4]} for r in rows]

    # -- signing in and out -------------------------------------------------------------------------------------------------
    def sign_in(self, google_sub, email, name=None):
        """Create a session for a verified Google identity, or raise ``LoginDenied``. Returns ``(token, session_id)``."""
        if not isinstance(google_sub, str) or not google_sub or len(google_sub) > 64:
            raise LoginDenied("identidade_invalida")
        try:
            email = normalize_email(email)
        except IdentityRejected:
            raise LoginDenied("email_invalido") from None
        now = self._now()
        stamp = _stamp(now)
        with self.repository._transaction() as conn:
            row = conn.execute("SELECT id, email, google_sub, status FROM auth_users WHERE google_sub = ?", (google_sub,)).fetchone()
            if row is None:
                row = conn.execute("SELECT id, email, google_sub, status FROM auth_users WHERE email = ?", (email,)).fetchone()
            reason, token, session_id = None, None, None
            if row is None:
                reason = "nao_cadastrado"
            elif row[3] != "ativo":
                reason = "usuario_inativo"
            elif row[2] is not None and row[2] != google_sub:
                reason = "conta_google_diferente"
            elif conn.execute("SELECT 1 FROM auth_memberships WHERE user_id = ? AND status = 'ativo'", (row[0],)).fetchone() is None:
                reason = "sem_empresa_ativa"
            if reason:  # the refusal is recorded (this transaction commits); the error is raised after it
                self._event(conn, "login_negado", user_id=row[0] if row else None, detail=reason)
            else:
                user_id = row[0]
                conn.execute("UPDATE auth_users SET google_sub = ?, last_login_at = ?, updated_at = ?, "
                             "display_name = COALESCE(display_name, ?) WHERE id = ?",
                             (google_sub, stamp, stamp, (str(name).strip()[:80] if name else None), user_id))
                token = secrets.token_urlsafe(32)
                session_id = "ses_" + secrets.token_hex(16)
                conn.execute("INSERT INTO auth_sessions (id, user_id, token_hash, created_at, last_seen_at, idle_expires_at, expires_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (session_id, user_id, hash_token(token), stamp, stamp, _stamp(now + self.idle), _stamp(now + self.absolute)))
                self._event(conn, "login", user_id=user_id)
        if reason:
            raise LoginDenied(reason)
        return token, session_id

    def resolve(self, token):
        """The verified identity behind a session cookie, or ``None``. Looks everything up again on every call."""
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            return None
        now = self._now()
        stamp = _stamp(now)
        conn = connect(self.repository.path)  # a plain read: no write lock for every request
        try:
            row = conn.execute(
                "SELECT s.id, s.last_seen_at, s.idle_expires_at, s.expires_at, s.revoked_at, u.id, u.email, u.display_name, u.status "
                "FROM auth_sessions s JOIN auth_users u ON u.id = s.user_id WHERE s.token_hash = ?", (hash_token(token),)).fetchone()
            if row is None or row[4] is not None or row[8] != "ativo" or row[2] <= stamp or row[3] <= stamp:
                return None
            found = conn.execute("SELECT m.tenant_id, m.role, m.departments FROM auth_memberships m "
                                 "WHERE m.user_id = ? AND m.status = 'ativo'", (row[5],)).fetchall()
            memberships = {tenant: role for tenant, role, _ in found}
            scopes = {tenant: tuple(json.loads(departments)) for tenant, _, departments in found if departments}
        finally:
            conn.close()
        if _parse(row[1]) + TOUCH_EVERY <= now:  # sliding idle timeout, written at most once a minute
            with self.repository._transaction() as write:
                write.execute("UPDATE auth_sessions SET last_seen_at = ?, idle_expires_at = ? WHERE id = ? AND revoked_at IS NULL",
                              (stamp, _stamp(min(now + self.idle, _parse(row[3]))), row[0]))
        return Identity(row[5], row[6], row[7], row[0], memberships, _TOKEN, scopes)

    def sign_out(self, token):
        now = _stamp(self._now())
        with self.repository._transaction() as conn:
            row = conn.execute("SELECT id, user_id FROM auth_sessions WHERE token_hash = ? AND revoked_at IS NULL",
                               (hash_token(token),)).fetchone()
            if row is not None:
                conn.execute("UPDATE auth_sessions SET revoked_at = ?, revoked_reason = 'saida' WHERE id = ?", (now, row[0]))
                self._event(conn, "logout", user_id=row[1])

    def context(self, identity, tenant_id):
        """Access context of this person for one company, from the role stored right now (not from the cached identity)."""
        if not isinstance(identity, Identity):
            raise PermissionDenied()
        conn = connect(self.repository.path)
        try:
            row = conn.execute("SELECT m.role, m.departments FROM auth_memberships m JOIN auth_users u ON u.id = m.user_id "
                               "WHERE m.user_id = ? AND m.tenant_id = ? AND m.status = 'ativo' AND u.status = 'ativo'",
                               (identity.user_id, str(tenant_id))).fetchone()
            live = conn.execute("SELECT 1 FROM auth_sessions WHERE id = ? AND revoked_at IS NULL", (identity.session_id,)).fetchone()
        finally:
            conn.close()
        if row is None or live is None:
            raise NotFound()  # same answer as a company that does not exist: nothing about other companies is revealed
        departments = json.loads(row[1]) if row[1] else None
        return self.access.member_context(
            Identity(identity.user_id, identity.email, identity.name, identity.session_id, {**identity.memberships, str(tenant_id): row[0]}, _TOKEN),
            str(tenant_id), row[0], departments)

    def trail(self, operator, limit=200):
        """Recent authentication events (ids and reasons only). Local owner only."""
        self._local_owner(operator)
        with self.repository._transaction() as conn:
            rows = conn.execute("SELECT seq, at, kind, user_id, tenant_id, actor_id, detail FROM auth_events ORDER BY seq DESC LIMIT ?",
                                (min(max(int(limit), 1), 1000),)).fetchall()
        return [dict(zip(("seq", "at", "kind", "user_id", "tenant_id", "actor_id", "detail"), r)) for r in rows]
