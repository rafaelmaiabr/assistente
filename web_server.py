"""Same-origin local chat. Each WebSocket owns one ephemeral agent session."""
import asyncio
import contextlib
import json
import logging
import uuid
from aiohttp import web, WSMsgType
from settings import DATA_ROOT, ROOT, load_config
from team import persistence
from team import web as team_web
from team.app import TeamApp
from speech_service import SpeechService, decode_audio

PORT = 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
CLIENTS = web.AppKey("clients", set)
CONFIG = web.AppKey("config", dict)
FACTORY = web.AppKey("factory", object)
SPEECH = web.AppKey("speech", object)
VOICE_OPERATION = web.AppKey('voice_operation', object)
DATA_DIR = web.AppKey('data_dir', object)
MAX_TEXT = 8000
TURN_TIMEOUT = 120


@web.middleware
async def local_only(request, handler):
    if request.host not in ALLOWED_HOSTS:
        raise web.HTTPForbidden(text="Host não permitido")
    if request.path.startswith(('/api/voices', '/api/persistence', '/api/team')):
        # Includes GET: another origin must not inspect local configuration.
        origin = request.headers.get('Origin')
        # Browsers omit Origin on same-site GETs; Sec-Fetch-Site (set by the browser, not scripts) proves the same.
        same_site = origin is None and request.headers.get('Sec-Fetch-Site') == 'same-origin'
        if not same_site and origin != f'http://{request.host}':
            raise web.HTTPForbidden(text='Origem não permitida')
        if request.method in ('POST', 'PUT', 'PATCH') and request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
    response = await handler(request)
    if not response.prepared:
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'",
        })
    return response


async def health(request):
    return web.json_response({"app": "assistente", "version": 3})


async def index(request):
    path = ROOT / "frontend/dist/index.html"
    if not path.exists():
        raise web.HTTPServiceUnavailable(text="Interface não compilada. Execute npm ci e npm run build na pasta frontend.")
    return web.FileResponse(path)


async def persistence_api(request):
    root = request.app[DATA_DIR]
    operation = request.match_info.get('operation', 'status')
    if request.method == 'GET' and operation == 'status':
        return web.json_response(persistence.status(root))
    if request.method != 'POST' or operation not in {'enable', 'disable'}:
        raise web.HTTPNotFound()
    lock = request.app[VOICE_OPERATION]
    if lock.locked():
        return web.json_response({'error': 'Já existe uma operação em andamento.'}, status=409)
    async with lock:
        result = await asyncio.to_thread(persistence.enable if operation == 'enable' else persistence.disable, root)
    return web.json_response(result)


async def voices(request):
    speech = request.app[SPEECH]
    operation = request.match_info.get('operation', '')
    try:
        if request.method == 'POST':
            if request.content_length is None or request.content_length > 20000:
                raise ValueError('Configuração excede o limite permitido.')
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('Configuração inválida.')
            if operation == 'status':
                pass
            elif operation == 'settings':
                await asyncio.to_thread(speech.voices.save, data)
            elif operation == 'credential':
                if set(data) != {'key'}:
                    raise ValueError('Informe apenas a chave.')
                if data['key'] is None:
                    await asyncio.to_thread(speech.voices.secrets.delete)
                else:
                    await asyncio.to_thread(speech.voices.secrets.set, data['key'])
            elif operation in {'preview', 'prepare'}:
                lock = request.app[VOICE_OPERATION]
                if lock.locked():
                    return web.json_response({'error': 'Já existe uma operação de voz em andamento.'}, status=409)
                async with lock:
                    if operation == 'prepare':
                        await asyncio.to_thread(speech.engine.prepare)
                    else:
                        profile_id = data.get('profile')
                        if not isinstance(profile_id, str):
                            raise ValueError('Selecione um perfil salvo.')
                        profile = speech.voices.profile(profile_id)
                        if profile['provider'] == 'elevenlabs' and data.get('allow_cloud') is not True:
                            raise ValueError('Autorize a prévia externa, que pode consumir créditos.')
                        wav = await asyncio.wait_for(speech.synthesize(
                            'Olá! Sou seu assistente. Posso responder suas perguntas e ajudar nas tarefas do dia a dia.',
                            profile_id), timeout=90)
                        return web.json_response({'wav': wav})
            else:
                raise web.HTTPNotFound()
        key = await asyncio.to_thread(speech.voices.secrets.get)
        return web.json_response({'settings': speech.voices.snapshot(), 'key_configured': bool(key),
                                  'kokoro_ready': speech.engine.ready()})
    except (ValueError, json.JSONDecodeError) as error:
        return web.json_response({'error': str(error)}, status=400)
    except Exception:
        return web.json_response({'error': 'Não foi possível concluir. Confira a instalação e tente novamente.'}, status=503)


async def chat(request):
    if request.headers.get("Origin") != f"http://{request.host}":
        raise web.HTTPForbidden(text="Origem não permitida")
    clients = request.app[CLIENTS]
    if len(clients) >= 4:
        raise web.HTTPServiceUnavailable(text="Limite de quatro conversas abertas")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1400000)
    clients.add(ws)  # Reserve a slot before awaiting the handshake.
    queue = asyncio.Queue()
    agent = None
    turn = None
    sender = None
    count = 0
    answers = []

    async def send():
        while True:
            await ws.send_json(await queue.get())

    def emit(event):
        if event.get('type') == 'message' and event.get('role') == 'assistant':
            answers.append(event['text'])
        queue.put_nowait(event)

    async def reply(text, audio=None, speak=False):
        answers.clear()
        try:
            if audio is not None:
                emit({'type': 'status', 'state': 'transcribing', 'label': 'Transcrevendo sua fala…'})
                text = await asyncio.wait_for(request.app[SPEECH].transcribe(audio), timeout=120)
                if not text:
                    emit({'type': 'warning', 'message': 'Não identifiquei fala. Confira o microfone e tente novamente.'})
                    emit({'type': 'status', 'state': 'ready', 'label': 'Pronto para conversar'})
                    return
            emit({"type": "message", "id": uuid.uuid4().hex, "role": "user", "text": text})
            emit({"type": "status", "state": "processing", "label": "Pensando…"})
            await asyncio.wait_for(agent.reply(text), timeout=TURN_TIMEOUT)
            if speak and answers:
                emit({'type': 'status', 'state': 'synthesizing', 'label': 'Preparando áudio…'})
                try:
                    audio_result = await asyncio.wait_for(request.app[SPEECH].synthesize('\n'.join(answers)), timeout=45)
                    emit({'type': 'audio', 'wav': audio_result})
                except Exception:
                    emit({'type': 'warning', 'message': 'Não foi possível gerar áudio. A resposta continua disponível em texto.'})
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
            if turn and not turn.done():
                emit({"type": "rejected", "message": "Aguarde a resposta em andamento.", "busy": True})
            elif count >= 100:
                emit({"type": "error", "message": "Limite de 100 mensagens. Inicie uma nova conversa.", "fatal": True})
            else:
                text, audio = None, None
                try:
                    if not isinstance(data, dict):
                        raise ValueError('Mensagem inválida.')
                    if data.get('type') == 'audio':
                        audio = decode_audio(data.get('wav'))
                    elif data.get('type') == 'message':
                        text = data.get('text')
                        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                            raise ValueError('Envie entre 1 e 8.000 caracteres.')
                        text = text.strip()
                    else:
                        raise ValueError('Mensagem inválida.')
                except ValueError as error:
                    emit({'type': 'rejected', 'message': str(error)})
                    continue
                count += 1
                turn = asyncio.create_task(reply(text, audio, data.get('speak') is True))
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


def create_app(factory=None, config=None, speech=None, data_root=None, team_app=None):
    if factory is None:
        from browser_agent import BrowserAgent
        factory = BrowserAgent
    app = web.Application(middlewares=[local_only])
    app[CLIENTS] = set()
    app[CONFIG] = config or load_config()
    app[FACTORY] = factory
    app[SPEECH] = speech or SpeechService(app[CONFIG])
    app[VOICE_OPERATION] = asyncio.Lock()
    app[DATA_DIR] = data_root or DATA_ROOT
    team_web.register(app, team_app or TeamApp(app[DATA_DIR], app[CONFIG]))
    app.router.add_get("/", index)
    app.router.add_get("/api/health", health)
    app.router.add_get('/api/voices', voices)
    app.router.add_post('/api/voices/{operation}', voices)
    app.router.add_get('/api/persistence', persistence_api)
    app.router.add_route('*', '/api/persistence/{operation}', persistence_api)
    app.router.add_get("/ws", chat)
    async def worklet(request):
        return web.FileResponse(ROOT / 'frontend/dist/microphone-worklet.js')
    app.router.add_get('/microphone-worklet.js', worklet)
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
