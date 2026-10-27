"""Real MCP client transport for the gateway (stdio, streamable HTTP and SSE), on the installed ``mcp`` package.

The gateway keeps every rule (grants, approved classification, limits, revalidation, logging); this class only
moves one call to a server. What it adds on top of the protocol:

* ``stdio`` runs the argv of an operator-registered launcher, no shell, and a clean environment (the library's
  safe defaults plus the injected values below); the child's stderr is discarded. Leaving the call (done, timed
  out or cancelled) ends the process.
* ``http``/``sse`` require TLS with a verified certificate (plain HTTP only for loopback, for tests), never send
  userinfo in the URL and never follow a redirect to another origin (the library enforces the latter).
* Identity and secret are injected by the backend: ``Authorization: Bearer <secret>`` plus ``X-Assistente-*``
  headers over HTTP, ``ASSISTENTE_SECRET`` plus ``ASSISTENTE_*`` variables over stdio. The model never sets them.
* ``describe`` reports ``read_only`` only when the server annotates the tool (``readOnlyHint``); no annotation is
  no claim. The gateway still denies a tool approved as ``read`` that the server says is not read-only.
* Failures (connection, protocol, server crash, server-reported error) become ``ToolUnavailable`` or a fixed
  message; text the server sent is never copied into errors. Results are capped before they reach the gateway.
"""
import json
import os
import ssl
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from .gateway import GatewayError, McpTransport, ResultTooLarge, ToolUnavailable

KINDS = ("stdio", "http", "sse")
MAX_RESULT_BYTES = 1_048_576
CONNECT_TIMEOUT = 15.0
_MAX_PAGES = 10
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
SERVER_ERROR = {"erro": "erro_do_servidor", "mensagem": "O servidor MCP informou um erro."}


def _identity_pairs(identity):
    return {"TENANT_ID": identity.get("tenant_id"), "AGENT_ID": identity.get("agent_id"), "RUN_ID": identity.get("run_id"),
            "CONNECTION_ID": identity.get("connection_id"), "OPERATION": identity.get("operation")}


def check_url(url, kind):
    """TLS (or loopback) and no embedded credentials; returns the URL or raises ``ToolUnavailable``."""
    if not isinstance(url, str):
        raise ToolUnavailable()
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise ToolUnavailable() from None
    if parts.scheme not in ("https", "http") or not host or parts.username or parts.password:
        raise ToolUnavailable()
    if parts.scheme == "http" and host not in _LOOPBACK:
        raise ToolUnavailable()
    return url


def _gateway_error(error):
    """The library reports failures as (nested) exception groups; keep our own typed errors, hide everything else."""
    leaves = [error]
    while leaves:
        leaf = leaves.pop(0)
        if isinstance(leaf, GatewayError):
            return leaf
        leaves.extend(getattr(leaf, "exceptions", ()))
    return ToolUnavailable()


class McpClientTransport(McpTransport):
    def __init__(self, kind, *, ca_file=None, max_result_bytes=MAX_RESULT_BYTES, connect_timeout=CONNECT_TIMEOUT):
        if kind not in KINDS:
            raise ValueError("kind must be stdio, http or sse")
        self.kind, self.max_result_bytes, self.connect_timeout = kind, max_result_bytes, connect_timeout
        self.verify = ssl.create_default_context(cafile=ca_file) if ca_file else True

    # -- connection ----------------------------------------------------------------------------------------------
    @asynccontextmanager
    async def _session(self, server, identity, secret):
        try:
            async with AsyncExitStack() as stack:
                if self.kind == "stdio":
                    errlog = stack.enter_context(open(os.devnull, "w"))
                    read, write = await stack.enter_async_context(stdio_client(self._params(server, identity, secret), errlog=errlog))
                else:
                    headers = self._headers(identity, secret)
                    url = check_url(server.get("url"), self.kind)
                    if self.kind == "http":
                        client = await stack.enter_async_context(httpx2.AsyncClient(
                            headers=headers, verify=self.verify, timeout=httpx2.Timeout(30.0, read=300.0)))
                        read, write = await stack.enter_async_context(streamable_http_client(url, http_client=client))
                    else:
                        read, write = await stack.enter_async_context(sse_client(
                            url, headers=headers, timeout=self.connect_timeout,
                            httpx_client_factory=lambda headers=None, timeout=None, auth=None: httpx2.AsyncClient(
                                headers=headers, timeout=timeout, auth=auth, verify=self.verify)))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                yield session
        except Exception as error:
            raise _gateway_error(error) from None  # never forward text from the server or the library

    def _params(self, server, identity, secret):
        argv = server.get("argv")
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) and os.path.isabs(argv[0])):
            raise ToolUnavailable()
        env = {f"ASSISTENTE_{key}": value for key, value in _identity_pairs(identity or {}).items() if value}
        if secret:
            env["ASSISTENTE_SECRET"] = secret
        return StdioServerParameters(command=argv[0], args=argv[1:], env=env)

    @staticmethod
    def _headers(identity, secret):
        headers = {f"X-Assistente-{key.replace('_', '-').title()}": value
                   for key, value in _identity_pairs(identity or {}).items() if value}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    # -- McpTransport --------------------------------------------------------------------------------------------
    async def describe(self, server, tool):
        async with self._session(server, None, None) as session:
            cursor = None
            for _ in range(_MAX_PAGES):
                try:
                    page = await session.list_tools(params=None if cursor is None else {"cursor": cursor})
                except Exception:
                    raise ToolUnavailable() from None
                for item in page.tools:
                    if item.name == tool:
                        hint = item.annotations.read_only_hint if item.annotations else None
                        return {} if hint is None else {"read_only": bool(hint)}
                cursor = page.next_cursor
                if not cursor:
                    break
        return None

    async def call_tool(self, server, tool, arguments, *, identity, secret):
        async with self._session(server, identity, secret) as session:
            try:
                result = await session.call_tool(tool, arguments)
            except Exception:
                raise ToolUnavailable() from None
        return self._data(result)

    def _data(self, result):
        if getattr(result, "is_error", False):
            return dict(SERVER_ERROR)
        if result.structured_content is not None:
            data = result.structured_content
        else:
            texts, ignored = [], 0
            for block in result.content or []:
                if getattr(block, "type", None) == "text":
                    texts.append(block.text)
                else:
                    ignored += 1
            data = _parse(texts[0]) if len(texts) == 1 and not ignored else {"texto": texts, "conteudo_ignorado": ignored}
        if len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")) > self.max_result_bytes:
            raise ResultTooLarge()
        return data


def _parse(text):
    try:
        value = json.loads(text)
    except ValueError:
        return {"texto": [text]}
    return value if isinstance(value, (dict, list)) else {"texto": [text]}
