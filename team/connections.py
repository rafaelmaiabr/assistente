"""Connections, credentials and the approved MCP server catalog.

Documents hold only ``credential_ref`` (an id), provider, status and scopes. Secret values live in
the vault and are handed to the authorized connector code (``use_credential``) and nowhere else:
every response is a sanitized state view, and every failure message is fixed text.
"""
import asyncio
import json
import os
import re
from datetime import datetime, timezone

from .access import AccessContext, AccessError, NotFound, OperatorContext, PermissionDenied
from .enums import OPERATIONS
from .ids import new_id
from .repository import IntegrityViolation, RevisionConflict
from .vault import SecretVault, VaultError


class ConnectionRejected(ValueError):
    """Invalid request; the message is safe to show and never contains secrets."""


class CredentialError(AccessError):
    """Vault or credential-state failure with a fixed, sanitized message."""


class ConnectorFailed(AccessError):
    """A connector raised or returned something unsafe; details are intentionally dropped."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_GENERIC_EXEC = re.compile(r"(^|[._-])(shell|exec|execute|eval|bash|powershell|pwsh|cmd|sh|run-?command|system)($|[._-])")
_RESERVED_ARGUMENTS = {"tenant_id", "company", "empresa", "connection_id", "credential", "credential_ref", "account",
                       "conta", "agent_id", "run_id", "case_id", "principal", "role", "password", "token", "secret",
                       "api_key", "authorization"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ARGUMENT_TYPES = ("string", "integer", "number", "boolean")
TOOL_LIMITS = {"timeout_seconds": (1, 120), "max_result_bytes": (1, 1_048_576), "max_items": (1, 1000)}
APPROVAL_BINDINGS = ("recipient_argument", "amount_argument", "currency_argument")


def _check_tool_specs(tools, specs):
    """Trusted per-tool classification and argument schema; validated once, when the operator approves."""
    if specs is None:
        return {}
    if not isinstance(specs, dict) or set(specs) - set(tools):
        raise ConnectionRejected("tool_specs deve descrever apenas ferramentas listadas em tools.")
    clean = {}
    for name, spec in specs.items():
        if not isinstance(spec, dict) or set(spec) - {"operation", "arguments", *TOOL_LIMITS, *APPROVAL_BINDINGS}                 or spec.get("operation") not in OPERATIONS:
            raise ConnectionRejected(f"Especificacao invalida para {str(name)[:40]}: informe operation (read/write/send/spend).")
        arguments = spec.get("arguments", {})
        if not isinstance(arguments, dict) or len(arguments) > 20:
            raise ConnectionRejected("arguments deve ser um objeto com ate 20 parametros.")
        for arg, rule in arguments.items():
            if not (isinstance(arg, str) and _SLUG.match(arg)) or arg in _RESERVED_ARGUMENTS:
                raise ConnectionRejected(f"Nome de parametro invalido ou reservado: {str(arg)[:40]}.")
            if not isinstance(rule, dict) or set(rule) - {"type", "required", "max_length", "minimum", "maximum", "enum"}                     or rule.get("type") not in ARGUMENT_TYPES:
                raise ConnectionRejected(f"Regra invalida para o parametro {arg}.")
        for key in APPROVAL_BINDINGS:  # which argument carries the recipient / amount shown to the approver
            if key in spec and spec[key] not in arguments:
                raise ConnectionRejected(f"{key} deve nomear um argumento da ferramenta.")
        for key, (low, high) in TOOL_LIMITS.items():
            if key in spec and not (isinstance(spec[key], int) and not isinstance(spec[key], bool) and low <= spec[key] <= high):
                raise ConnectionRejected(f"{key} deve estar entre {low} e {high}.")
        clean[name] = spec
    return clean


class LauncherRegistry:
    """Operator-approved stdio launchers: ``slug -> argv``. Catalog entries only name a slug,
    so neither an LLM nor a message can introduce a command line."""

    def __init__(self, path, operator_id):
        self.path, self.operator_id = path, operator_id

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))["launchers"]
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, KeyError):
            return {}

    def register(self, operator, slug, argv):
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.operator_id:
            raise PermissionDenied()
        if not isinstance(argv, list) or not 1 <= len(argv) <= 16 or not all(
                isinstance(item, str) and 0 < len(item) <= 500 and "\0" not in item for item in argv):
            raise ConnectionRejected("Comando invalido: informe uma lista de argumentos.")
        if not os.path.isabs(argv[0]):
            raise ConnectionRejected("O executavel deve ser um caminho absoluto.")
        launchers = self._load()
        launchers[slug] = argv
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"launchers": launchers}, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def argv(self, slug):
        value = self._load().get(slug)
        return list(value) if value else None


class ConnectionService:
    def __init__(self, repository, access, vault: SecretVault, launchers: LauncherRegistry,
                 legacy_elevenlabs=None):
        self.repository, self.access, self.vault, self.launchers = repository, access, vault, launchers
        self.legacy_elevenlabs = legacy_elevenlabs
        self._connectors = set()
        self.token_resolvers = {}  # credential store -> callable(tenant_id, connection_id) -> short-lived secret (OAuth access token)

    # -- trusted wiring (backend code, never LLM input) --------------------------------------------
    def register_connector(self, provider):
        self._connectors.add(provider)

    def _operator_ctx(self, ctx):
        if not self.access.is_operator(ctx, "admin"):
            raise PermissionDenied()

    # -- views -------------------------------------------------------------------------------------
    def _view(self, session, connection):
        credential = session.get("credential", connection["credential_ref"]) if connection.get("credential_ref") else None
        return {
            "id": connection["id"], "label": connection["label"], "provider": connection["provider"],
            "connection_kind": connection["connection_kind"], "status": connection["status"],
            "scopes": connection.get("scopes") or [], "transport": connection.get("transport"),
            "endpoint": connection.get("endpoint"), "catalog_entry_id": connection.get("catalog_entry_id"),
            "revision": connection["revision"],
            "credential": None if credential is None else {
                "state": credential["state"], "store": credential["store"], "updated_at": credential["updated_at"]},
        }

    def get_connection(self, ctx, connection_id):
        with self.repository.transaction(ctx.tenant_id) as session:
            connection = session.get("connection", connection_id) if isinstance(connection_id, str) else None
            if connection is None or not self.access.allowed(ctx, session, "read", "connection", connection_id):
                raise NotFound()
            return self._view(session, connection)

    def list_connections(self, ctx):
        with self.repository.transaction(ctx.tenant_id) as session:
            return [self._view(session, item) for item in session.list("connection", limit=1000)
                    if self.access.allowed(ctx, session, "read", "connection", item["id"])]

    # -- connection CRUD ---------------------------------------------------------------------------
    def create_connection(self, ctx, *, label, provider, connection_kind, catalog_entry_id=None,
                          endpoint=None, transport=None, scopes=()):
        self._operator_ctx(ctx)
        transport_value, endpoint_value = transport, endpoint
        if connection_kind == "mcp":
            entry = self.repository.get_global("mcp_catalog_entry", catalog_entry_id) \
                if isinstance(catalog_entry_id, str) else None
            if entry is None or entry["status"] != "ativo":
                raise ConnectionRejected("Servidor MCP nao aprovado ou inativo no catalogo.")
            if endpoint is not None or transport is not None:
                raise ConnectionRejected("Conexoes MCP usam o transporte e a origem do catalogo aprovado.")
            transport_value, endpoint_value = entry["transport"], entry["url"]
        elif catalog_entry_id is not None:
            raise ConnectionRejected("Somente conexoes MCP usam o catalogo.")
        now = _now()
        doc = {"kind": "connection", "schema_version": 1, "id": new_id("connection"), "tenant_id": ctx.tenant_id,
               "revision": 1, "created_at": now, "updated_at": now, "label": label, "provider": provider,
               "connection_kind": connection_kind, "transport": transport_value, "endpoint": endpoint_value,
               "credential_ref": None, "catalog_entry_id": catalog_entry_id, "scopes": list(scopes),
               "status": "ativo"}
        with self.repository.transaction(ctx.tenant_id) as session:
            session.save(doc)
            return self._view(session, doc)

    def update_connection(self, ctx, connection_id, expected_revision, /, **changes):
        self._operator_ctx(ctx)
        if set(changes) - {"label", "status", "scopes"}:
            raise ConnectionRejected("Somente nome, status e escopos podem ser alterados.")
        with self.repository.transaction(ctx.tenant_id) as session:
            current = session.get("connection", connection_id) if isinstance(connection_id, str) else None
            if current is None:
                raise NotFound()
            if current["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            changed = session.save({**current, **changes, "revision": expected_revision + 1, "updated_at": _now()})
            return self._view(session, changed)

    # -- credential lifecycle ----------------------------------------------------------------------
    def _locate(self, session, connection_id):
        connection = session.get("connection", connection_id) if isinstance(connection_id, str) else None
        if connection is None:
            raise NotFound()
        credential = session.get("credential", connection["credential_ref"]) if connection["credential_ref"] else None
        return connection, credential

    def set_credential(self, ctx, connection_id, secret):
        """Save a new secret or replace the current one (also re-activates a revoked credential)."""
        self._operator_ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            connection, credential = self._locate(session, connection_id)
        if credential is not None and credential["store"] != "cofre-local":
            raise ConnectionRejected("Esta credencial e gerenciada pelas configuracoes de voz.")
        credential_id = credential["id"] if credential else new_id("credential")
        try:
            self.vault.put(ctx.tenant_id, credential_id, secret)
        except VaultError as error:
            raise CredentialError(str(error)) from None
        now = _now()
        try:
            with self.repository.transaction(ctx.tenant_id) as session:
                connection, credential = self._locate(session, connection_id)
                if credential is None:
                    session.save({"kind": "credential", "schema_version": 1, "id": credential_id,
                                  "tenant_id": ctx.tenant_id, "revision": 1, "created_at": now, "updated_at": now,
                                  "label": connection["label"][:300], "store": "cofre-local", "state": "ativa"})
                    session.save({**connection, "credential_ref": credential_id,
                                  "revision": connection["revision"] + 1, "updated_at": now})
                else:
                    session.save({**credential, "state": "ativa", "revision": credential["revision"] + 1,
                                  "updated_at": now})
                return self._view(session, session.get("connection", connection_id))
        except Exception:
            if credential is None:  # nothing references the new secret: do not leave it behind
                try:
                    self.vault.delete(ctx.tenant_id, credential_id)
                except VaultError:
                    pass
            raise CredentialError("Nao foi possivel registrar a credencial. Tente novamente.") from None

    def revoke_credential(self, ctx, connection_id):
        """Make the credential unusable and wipe the stored secret; the record stays as 'revogada'."""
        self._operator_ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            connection, credential = self._locate(session, connection_id)
            if credential is None:
                raise ConnectionRejected("A conexao nao tem credencial.")
            session.save({**credential, "state": "revogada", "revision": credential["revision"] + 1,
                          "updated_at": _now()})
            view = self._view(session, connection)
        if credential["store"] in ("cofre-local", "oauth"):
            try:
                self.vault.delete(ctx.tenant_id, credential["id"])
            except VaultError:
                raise CredentialError("Credencial revogada, mas o segredo nao foi apagado do cofre. "
                                      "Use remover credencial para tentar de novo.") from None
        return view

    def remove_credential(self, ctx, connection_id):
        """Delete the secret and the credential record, and detach it from the connection."""
        self._operator_ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            connection, credential = self._locate(session, connection_id)
        if credential is None:
            raise ConnectionRejected("A conexao nao tem credencial.")
        if credential["store"] in ("cofre-local", "oauth"):
            try:
                self.vault.delete(ctx.tenant_id, credential["id"])
            except VaultError as error:
                raise CredentialError(str(error)) from None
        with self.repository.transaction(ctx.tenant_id) as session:
            connection, _ = self._locate(session, connection_id)
            session.save({**connection, "credential_ref": None, "revision": connection["revision"] + 1,
                          "updated_at": _now()})
            session.delete("credential", credential["id"])
            return self._view(session, session.get("connection", connection_id))

    def credential_status(self, ctx, connection_id):
        """State only: never the value. ``available`` is true when an active secret can be resolved."""
        self._operator_ctx(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            _, credential = self._locate(session, connection_id)
        if credential is None:
            return {"state": "ausente", "available": False}
        try:
            present = self._read_secret(ctx.tenant_id, credential) is not None
        except CredentialError:
            present = False
        return {"state": credential["state"], "store": credential["store"],
                "available": credential["state"] == "ativa" and present, "updated_at": credential["updated_at"]}

    def _read_secret(self, tenant_id, credential):
        try:
            if credential["store"] == "elevenlabs-legado":
                return self.legacy_elevenlabs.get() if self.legacy_elevenlabs else None
            return self.vault.get(tenant_id, credential["id"])
        except VaultError:
            raise CredentialError("Cofre de credenciais indisponivel.") from None

    def _authorized_secret(self, ctx, connection_id, connector, optional=False):
        """Checks shared by sync and async use; returns the secret (None only when ``optional`` and absent)."""
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()
        with self.repository.transaction(ctx.tenant_id) as session:
            connection, credential = (None, None)
            found = session.get("connection", connection_id) if isinstance(connection_id, str) else None
            if found is None or not self.access.allowed(ctx, session, "read", "connection", connection_id):
                raise NotFound()
            connection, credential = self._locate(session, connection_id)
        if connector not in self._connectors or connector != connection["provider"]:
            raise PermissionDenied("conector nao autorizado para esta conexao")
        if connection["status"] != "ativo":
            raise CredentialError("Credencial indisponivel ou revogada.")
        if credential is None and optional:
            return None  # a connection that needs no credential
        if credential is None or credential["state"] != "ativa":
            raise CredentialError("Credencial indisponivel ou revogada.")
        resolver = self.token_resolvers.get(credential["store"])
        if resolver is not None:  # the stored secret is a refresh token; connectors get a fresh access token instead
            return resolver(ctx.tenant_id, connection_id)
        secret = self._read_secret(ctx.tenant_id, credential)
        if secret is None:
            raise CredentialError("Credencial indisponivel ou revogada.")
        return secret

    @staticmethod
    def _checked_result(secret, result):
        if secret is not None and secret in repr(result):
            raise ConnectorFailed("Resposta do conector bloqueada: continha a credencial.")
        return result

    def use_credential(self, ctx, connection_id, connector, fn):
        """Run ``fn(secret)`` inside an authorized connector. The secret is not returned to the caller."""
        secret = self._authorized_secret(ctx, connection_id, connector)
        try:
            result = fn(secret)
        except Exception:
            raise ConnectorFailed("Falha no conector. Detalhes omitidos para proteger a credencial.") from None
        return self._checked_result(secret, result)

    async def use_credential_async(self, ctx, connection_id, connector, fn, optional=False):
        """Async twin used by the tool gateway; ``optional`` lets credential-less connections proceed.
        Cancellation and timeouts pass through untouched so callers can handle them."""
        secret = await asyncio.to_thread(self._authorized_secret, ctx, connection_id, connector, optional)  # may refresh a token
        try:
            result = await fn(secret)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(error, (TimeoutError, AccessError)):
                raise
            raise ConnectorFailed("Falha no conector. Detalhes omitidos para proteger a credencial.") from None
        return self._checked_result(secret, result)

    # -- ElevenLabs reuse ----------------------------------------------------------------------------
    def adopt_elevenlabs(self, ctx):
        """Expose the existing voice key as a connection, without copying or moving it."""
        self._operator_ctx(ctx)
        if self.legacy_elevenlabs is None:
            raise ConnectionRejected("Nenhuma chave ElevenLabs configurada.")
        try:
            configured = self.legacy_elevenlabs.get() is not None
        except VaultError as error:
            raise CredentialError(str(error)) from None
        if not configured:
            raise ConnectionRejected("Nenhuma chave ElevenLabs configurada.")
        with self.repository.transaction(ctx.tenant_id) as session:
            for existing in session.list("connection", limit=1000):
                if existing["provider"] == "elevenlabs":
                    return self._view(session, existing)
            now, credential_id = _now(), new_id("credential")
            session.save({"kind": "credential", "schema_version": 1, "id": credential_id, "tenant_id": ctx.tenant_id,
                          "revision": 1, "created_at": now, "updated_at": now, "label": "ElevenLabs (voz)",
                          "store": "elevenlabs-legado", "state": "ativa"})
            connection = session.save({
                "kind": "connection", "schema_version": 1, "id": new_id("connection"), "tenant_id": ctx.tenant_id,
                "revision": 1, "created_at": now, "updated_at": now, "label": "ElevenLabs (voz)",
                "provider": "elevenlabs", "connection_kind": "outro", "transport": None, "endpoint": None,
                "credential_ref": credential_id, "catalog_entry_id": None, "scopes": ["tts"], "status": "ativo"})
            return self._view(session, connection)

    # -- approved MCP catalog (operator only, global) ------------------------------------------------
    def _operator_only(self, operator):
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.access.operator_id:
            raise PermissionDenied()

    def approve_mcp_server(self, operator, *, catalog_key, entry_version, transport, url=None, launcher=None,
                           tools=(), status="ativo", tool_specs=None):
        self._operator_only(operator)
        for name in tools:
            if isinstance(name, str) and _GENERIC_EXEC.search(name):
                raise ConnectionRejected("Ferramentas genericas de shell ou execucao nao sao aprovadas.")
        specs = _check_tool_specs(tools, tool_specs)
        if transport == "stdio" and not (isinstance(launcher, str) and self.launchers.argv(launcher)):
            raise ConnectionRejected("stdio usa apenas um launcher previamente registrado pelo operador.")
        now = _now()
        doc = {"kind": "mcp_catalog_entry", "schema_version": 1, "id": new_id("mcp_catalog_entry"),
               "revision": 1, "created_at": now, "updated_at": now, "catalog_key": catalog_key,
               "entry_version": entry_version, "transport": transport, "url": url, "launcher": launcher,
               "tools": list(tools), "approved_by": operator.principal_id, "status": status, "tool_specs": specs}
        try:
            return self.repository.save_global(doc)
        except IntegrityViolation:
            raise ConnectionRejected("Ja existe esta versao no catalogo.") from None

    def set_mcp_status(self, operator, entry_id, expected_revision, status):
        self._operator_only(operator)
        current = self.repository.get_global("mcp_catalog_entry", entry_id)
        if current is None:
            raise NotFound()
        if current["revision"] != expected_revision:
            raise RevisionConflict("revisao desatualizada")
        return self.repository.save_global({**current, "status": status, "revision": expected_revision + 1,
                                            "updated_at": _now()})

    def list_mcp_catalog(self, ctx):
        if not (isinstance(ctx, OperatorContext) or (isinstance(ctx, AccessContext) and ctx.role == "operator")):
            raise PermissionDenied()
        return self.repository.list_global("mcp_catalog_entry", limit=1000)

    def stdio_command(self, entry_id):
        """argv for the runtime to start an approved stdio server; None unless active and registered."""
        entry = self.repository.get_global("mcp_catalog_entry", entry_id)
        if entry is None or entry["status"] != "ativo" or entry["transport"] != "stdio":
            return None
        return self.launchers.argv(entry["launcher"])

    def import_legacy_mcp(self, operator, config):
        """Register old ``mcp_servers`` URLs as *inactive* catalog entries awaiting review (default deny)."""
        self._operator_only(operator)
        created = []
        for number, server in enumerate(config.get("mcp_servers") or [], start=1):
            try:
                created.append(self.approve_mcp_server(
                    operator, catalog_key=f"legado-{number}", entry_version="1.0.0", transport="http",
                    url=server["url"], tools=[], status="inativo"))
            except ConnectionRejected:
                continue  # already imported
        return created
