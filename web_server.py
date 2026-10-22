"""Same-origin local chat. Each WebSocket owns one ephemeral agent session."""
import asyncio
import contextlib
import json
import logging
import uuid
from aiohttp import web, WSMsgType
from settings import ROOT, load_config

PORT = 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
CLIENTS = web.AppKey("clients", set)
CONFIG = web.AppKey("config", dict)
FACTORY = web.AppKey("factory", object)
MAX_TEXT = 8000
TURN_TIMEOUT = 120


@web.middleware
async def local_only(request, handler):
    if request.host not in ALLOWED_HOSTS:
        raise web.HTTPForbidden(text="Host não permitido")
    response = await handler(request)
    if not response.prepared:
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        })
    return response


async def health(request):
    return web.json_response({"app": "assistente", "version": 1})


async def index(request):
    path = ROOT / "frontend/dist/index.html"
    if not path.exists():
        raise web.HTTPServiceUnavailable(text="Interface não compilada. Execute npm ci e npm run build na pasta frontend.")
    return web.FileResponse(path)


async def chat(request):
    if request.headers.get("Origin") != f"http://{request.host}":
        raise web.HTTPForbidden(text="Origem não permitida")
    clients = request.app[CLIENTS]
    if len(clients) >= 4:
        raise web.HTTPServiceUnavailable(text="Limite de quatro conversas abertas")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=40000)
    clients.add(ws)  # Reserve a slot before awaiting the handshake.
    queue = asyncio.Queue()
    agent = None
    turn = None
    sender = None
    count = 0

    async def send():
        while True:
            await ws.send_json(await queue.get())

    def emit(event):
        queue.put_nowait(event)

    async def reply(text):
        emit({"type": "message", "id": uuid.uuid4().hex, "role": "user", "text": text})
        emit({"type": "status", "state": "processing", "label": "Pensando…"})
        try:
            await asyncio.wait_for(agent.reply(text), timeout=TURN_TIMEOUT)
        except asyncio.TimeoutError:
            emit({"type": "error", "message": "A resposta excedeu dois minutos. Inicie uma nova conversa para tentar novamente.", "fatal": True})
            await queue_drained()
            await ws.close()
        except Exception:
            emit({"type": "error", "message": "Não foi possível obter a resposta. Confira o Ollama e o modelo configurado e tente novamente.", "fatal": False})
        else:
            emit({"type": "status", "state": "ready", "label": "Pronto para conversar"})

    async def queue_drained():
        # Give the sole sender a chance to flush terminal events before closing.
        for _ in range(20):
            if queue.empty():
                break
            await asyncio.sleep(0.01)

    try:
        await ws.prepare(request)
        sender = asyncio.create_task(send())
        emit({"type": "status", "state": "connecting", "label": "Preparando assistente…"})
        agent = request.app[FACTORY](request.app[CONFIG], emit)
        await asyncio.wait_for(agent.start(), timeout=30)
        emit({"type": "session", "model": request.app[CONFIG]["model"]})
        emit({"type": "status", "state": "ready", "label": "Pronto para conversar"})
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(message.data)
            except json.JSONDecodeError:
                emit({"type": "rejected", "message": "Mensagem inválida."})
                continue
            text = data.get("text") if isinstance(data, dict) and data.get("type") == "message" else None
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                emit({"type": "rejected", "message": "Envie entre 1 e 8.000 caracteres."})
            elif turn and not turn.done():
                emit({"type": "rejected", "message": "Aguarde a resposta em andamento."})
            elif count >= 100:
                emit({"type": "error", "message": "Limite de 100 mensagens. Inicie uma nova conversa.", "fatal": True})
            else:
                count += 1
                turn = asyncio.create_task(reply(text.strip()))
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception:
        if sender and not ws.closed:
            emit({"type": "error", "message": "Não foi possível iniciar o assistente. Verifique a configuração do modelo e dos servidores MCP.", "fatal": True})
            await queue_drained()
    finally:
        if turn and not turn.done():
            turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await turn
        if agent:
            with contextlib.suppress(Exception):
                await agent.close()
        if sender:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
        clients.discard(ws)
        await ws.close()
    return ws


def create_app(factory=None, config=None):
    if factory is None:
        from browser_agent import BrowserAgent
        factory = BrowserAgent
    app = web.Application(middlewares=[local_only])
    app[CLIENTS] = set()
    app[CONFIG] = config or load_config()
    app[FACTORY] = factory
    app.router.add_get("/", index)
    app.router.add_get("/api/health", health)
    app.router.add_get("/ws", chat)
    assets = ROOT / "frontend/dist/assets"
    if assets.exists():
        app.router.add_static("/assets/", assets)

    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001) for ws in list(app[CLIENTS])))
    app.on_shutdown.append(shutdown)
    return app


if __name__ == "__main__":
    # Do not log user messages or raw provider errors to disk.
    logging.basicConfig(level=logging.CRITICAL)
    web.run_app(create_app(), host="127.0.0.1", port=PORT, access_log=None)
