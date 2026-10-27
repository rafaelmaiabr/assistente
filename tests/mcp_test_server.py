"""Tiny real MCP server for transport tests (stdio, streamable HTTP and SSE).

    python tests/mcp_test_server.py <stdio|streamable-http|sse> [port] [mode] [pidfile]

``mode`` ``mentiroso`` makes ``listar-cobrancas`` claim it is not read-only. ``pidfile`` receives the process id (to
prove the process is gone after a cancel); arguments, not environment, because stdio servers get a clean environment.
Tools never echo the secret: they return only a SHA-256 so tests can compare it with what the backend injected.
"""
import hashlib
import json
import os
import sys
import time

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

_args = sys.argv[1:] + ["-"] * 4
port = int(_args[1]) if _args[1].isdigit() else 8000
LISTA_SOMENTE_LEITURA = _args[2] != "mentiroso"
if _args[3] != "-":
    with open(_args[3], "w") as handle:
        handle.write(str(os.getpid()))
app = MCPServer("teste")


@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def eco(texto: str) -> dict:
    return {"eco": texto}


@app.tool(name="listar-cobrancas", annotations=ToolAnnotations(read_only_hint=LISTA_SOMENTE_LEITURA))
def listar_cobrancas(cliente: str, limite: int = 5, ordem: str = "data") -> dict:
    secret = os.environ.get("ASSISTENTE_SECRET", "")
    return {"cobrancas": [{"id": 1, "valor": 10, "cliente": cliente}], "segredo_sha256": hashlib.sha256(secret.encode()).hexdigest(),
            "tenant": os.environ.get("ASSISTENTE_TENANT_ID")}


@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def segredo() -> dict:
    secret = os.environ.get("ASSISTENTE_SECRET", "")
    return {"sha256": hashlib.sha256(secret.encode()).hexdigest(), "tenant": os.environ.get("ASSISTENTE_TENANT_ID")}


@app.tool(annotations=ToolAnnotations(read_only_hint=False))
def escreve(texto: str) -> dict:
    return {"escrito": texto}


@app.tool()
def sem_anotacao() -> dict:
    return {"ok": True}


@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def lento(segundos: float) -> dict:
    time.sleep(segundos)
    return {"fim": True}


@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def enorme(bytes_: int) -> dict:
    return {"dados": "x" * bytes_}


@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def falha() -> dict:
    raise RuntimeError("SEGREDO-INTERNO-NAO-DEVE-VAZAR")


@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def cai() -> dict:
    os._exit(3)


@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def texto_livre() -> str:
    return json.dumps({"lista": [1, 2, 3]})


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    app.run(transport=kind) if kind == "stdio" else app.run(transport=kind, host="127.0.0.1", port=port)
