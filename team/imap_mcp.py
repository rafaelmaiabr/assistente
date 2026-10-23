"""Read-only IMAP as an approved local MCP server: account registry, in-process transport, connection test.

The model only sees the four tools below (all ``read``). Host, port, user and the folder allowlist are
decided by the operator per approved connection and kept in a backend file; the password comes from the
vault and is handed to the transport by the gateway, never to the model. Failures come back as short,
actionable messages that cannot contain the credential.
"""
import asyncio
import json
import os

from .access import NotFound, OperatorContext, PermissionDenied
from .connections import ConnectionRejected
from .gateway import McpTransport
from .imap_client import ImapConfig, ImapError, ReadOnlyImap

PROVIDER = "imap"
CATALOG_KEY = "imap-somente-leitura"
TOOLS = ("listar-pastas", "listar-mensagens", "buscar-mensagens", "ler-mensagem")
_PAGING = {"limite": {"type": "integer", "minimum": 1, "maximum": 50}, "antes_de_uid": {"type": "integer", "minimum": 1}}
_DATE = {"type": "string", "max_length": 10}
TOOL_SPECS = {
    "listar-pastas": {"operation": "read", "arguments": {}, "timeout_seconds": 30, "max_result_bytes": 65536},
    "listar-mensagens": {"operation": "read", "timeout_seconds": 30, "max_result_bytes": 262144, "max_items": 50,
                         "arguments": {"pasta": {"type": "string", "required": True, "max_length": 200}, **_PAGING,
                                       "desde": _DATE}},
    "buscar-mensagens": {"operation": "read", "timeout_seconds": 30, "max_result_bytes": 262144, "max_items": 50,
                         "arguments": {"pasta": {"type": "string", "required": True, "max_length": 200},
                                       "texto": {"type": "string", "max_length": 200},
                                       "remetente": {"type": "string", "max_length": 200},
                                       "assunto": {"type": "string", "max_length": 200},
                                       "desde": _DATE, "ate": _DATE, **_PAGING}},
    "ler-mensagem": {"operation": "read", "timeout_seconds": 45, "max_result_bytes": 262144,
                     "arguments": {"pasta": {"type": "string", "required": True, "max_length": 200},
                                   "uid": {"type": "integer", "required": True, "minimum": 1},
                                   "uidvalidity": {"type": "integer", "required": True, "minimum": 1}}},
}


def approve_catalog(connections, operator, entry_version="1.0.0"):
    """Operator step: approve the built-in read-only IMAP server in the global catalog."""
    return connections.approve_mcp_server(operator, catalog_key=CATALOG_KEY, entry_version=entry_version, transport="local",
                                          tools=list(TOOLS), tool_specs=TOOL_SPECS)


class ImapAccountRegistry:
    """``connection_id -> account`` decided by the operator (host, port, user, folders); never model input."""

    def __init__(self, path, operator_id):
        self.path, self.operator_id = path, operator_id

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))["accounts"]
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, KeyError):
            return {}

    def register(self, operator, tenant_id, connection_id, config: ImapConfig):
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.operator_id:
            raise PermissionDenied()
        try:
            config.validate()
        except ImapError as error:
            raise ConnectionRejected(error.message) from None
        accounts = self._load()
        accounts[connection_id] = {"tenant_id": tenant_id, "host": config.host, "port": config.port, "username": config.username,
                                   "folders": list(config.folders), "tls": config.tls, "timeout": config.timeout,
                                   "ca_file": config.ca_file}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"accounts": accounts}, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def get(self, tenant_id, connection_id):
        raw = self._load().get(connection_id)
        if not raw or raw.get("tenant_id") != tenant_id:
            return None
        return ImapConfig(host=raw["host"], port=raw["port"], username=raw["username"], folders=tuple(raw["folders"]),
                          tls=raw["tls"], timeout=raw["timeout"], ca_file=raw.get("ca_file"))


class ImapTransport(McpTransport):
    """In-process transport for the catalog entry ``transport="local"``; the blocking client runs in a thread."""

    def __init__(self, registry, ssl_context=None):
        self.registry, self.ssl_context = registry, ssl_context

    async def describe(self, server, tool):
        return {"read_only": True} if server.get("key") == CATALOG_KEY and tool in TOOLS else None

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        if secret is None:
            return {"erro": "sem_credencial", "mensagem": "Esta conexao ainda nao tem senha cadastrada no cofre."}
        config = self.registry.get(identity["tenant_id"], identity["connection_id"])
        if config is None:
            return {"erro": "conta_nao_configurada", "mensagem": "O operador ainda nao configurou servidor e pastas desta conexao."}
        return await asyncio.to_thread(self._run, config, secret, tool, arguments)

    def _run(self, config, secret, tool, args):
        try:
            with ReadOnlyImap(config, secret, ssl_context=self.ssl_context) as imap:
                if tool == "listar-pastas":
                    return {"pastas": imap.list_folders()}
                if tool == "listar-mensagens":
                    return imap.list_messages(args["pasta"], args.get("limite", 20), args.get("antes_de_uid"), args.get("desde"))
                if tool == "buscar-mensagens":
                    return imap.search(args["pasta"], text=args.get("texto"), sender=args.get("remetente"), subject=args.get("assunto"),
                                       since=args.get("desde"), before=args.get("ate"), limit=args.get("limite", 20),
                                       before_uid=args.get("antes_de_uid"))
                if tool == "ler-mensagem":
                    return imap.read_message(args["pasta"], args["uid"], args["uidvalidity"])
        except ImapError as error:
            return {"erro": error.code, "mensagem": error.message}
        except ValueError:
            return {"erro": "argumento_invalido", "mensagem": "Data ou valor invalido (use AAAA-MM-DD)."}
        return {"erro": "ferramenta_desconhecida", "mensagem": "Ferramenta nao disponivel."}


class ImapAccounts:
    """Operator-facing setup: bind an approved ``imap`` connection to a server and test it."""

    def __init__(self, connections, registry, ssl_context=None):
        self.connections, self.registry, self.ssl_context = connections, registry, ssl_context
        connections.register_connector(PROVIDER)

    def _connection(self, ctx, connection_id):
        connection = self.connections.get_connection(ctx, connection_id)  # operator-only, same-company lookup
        if connection["provider"] != PROVIDER:
            raise NotFound()
        return connection

    def configure(self, operator, ctx, connection_id, *, host, username, folders, port=993, tls=True, timeout=15.0, ca_file=None):
        self._connection(ctx, connection_id)
        config = ImapConfig(host=host, port=port, username=username, folders=tuple(folders), tls=tls, timeout=timeout, ca_file=ca_file)
        self.registry.register(operator, ctx.tenant_id, connection_id, config)

    def test(self, ctx, connection_id):
        """Connect with the vaulted password and list the authorized folders. Returns ``{ok, ...}``; never the secret."""
        self._connection(ctx, connection_id)
        config = self.registry.get(ctx.tenant_id, connection_id)
        if config is None:
            return {"ok": False, "erro": "conta_nao_configurada", "mensagem": "Informe servidor, usuario e pastas autorizadas."}

        def probe(secret):
            try:
                with ReadOnlyImap(config, secret, ssl_context=self.ssl_context) as imap:
                    return {"ok": True, "pastas": imap.list_folders()}
            except ImapError as error:
                return {"ok": False, "erro": error.code, "mensagem": error.message}

        return self.connections.use_credential(ctx, connection_id, PROVIDER, probe)
