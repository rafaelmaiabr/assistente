"""Same-origin local chat. Each WebSocket owns one ephemeral agent session."""
import asyncio
import contextlib
import json
import logging
import re
import uuid
import mail_intent
from aiohttp import web, WSMsgType
from settings import DATA_ROOT, ROOT, load_config
from team import conversation, persistence
from team.access import NotFound
from team import web as team_web
from team.app import TeamApp
from team.connectors.conversation import IntegrationSession, disabled_card
from speech_service import SpeechService, decode_audio
from personal_mail import PersonalMail
from personal_outbox import PersonalOutbox

PORT = 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
TURN_EVENTS = ('status', 'message', 'audio', 'tool', 'warning', 'error')
CLIENTS = web.AppKey("clients", set)
CONFIG = web.AppKey("config", dict)
FACTORY = web.AppKey("factory", object)
SPEECH = web.AppKey("speech", object)
VOICE_OPERATION = web.AppKey('voice_operation', object)
DATA_DIR = web.AppKey('data_dir', object)
MAX_TEXT = 8000
TURN_TIMEOUT = 120
SETUP_INTENT = re.compile(r'(?i)\b(configur\w*|conect\w*|ativ\w*|troc\w*)\b.*\b(e-?mail|caixa)\b')
INTEGRATION_INTENT = re.compile(
    r'(?i)^\s*(?:(?:quero|preciso|pode|vamos|gostaria de)\s+)?(?:conectar|integrar|ligar)\s+(?:(?:o|a|os|as|ao|à|no|na|com)\s+)*(?P<alvo>[^\n?!.]{2,60})[?!.]?\s*$')
SETUP_LOCK = asyncio.Lock()


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


async def auth_session(request):
    """The interface asks how it is being served: here it is the local application, with the local owner and no sign-in."""
    return web.json_response({"mode": "local", "authenticated": True})


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
        if operation == 'enable':
            from team.vault import WindowsVault
            result = await asyncio.to_thread(persistence.enable, root, request.app[CONFIG], WindowsVault(root))
        else:
            result = await asyncio.to_thread(persistence.disable, root)
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
    def mail_account():
        return PersonalMail(request.app[DATA_DIR], request.app[team_web.TEAM].vault())

    outbox = PersonalOutbox(request.app[DATA_DIR], mail_account())
    decisions = set()
    integration = {"session": None, "last_text": None}  # the cards of this conversation; the text that led to the last one
    loop = asyncio.get_running_loop()

    def integrations():
        services = request.app[team_web.TEAM].services()
        if services is None:
            return None
        if integration["session"] is None or integration["session"].services is not services:
            integration["session"] = IntegrationSession(services)
        return integration["session"]

    def card_for(vendor, capability):
        """Called by the agent tool in a worker thread: the model names a system, the backend builds the card."""
        session = integrations()
        card = disabled_card() if session is None else session.request(capability, vendor, original=integration["last_text"])
        loop.call_soon_threadsafe(emit, {"type": "integration_card", "card": card})
        return card
    attachment = None  # (company id, case id) the conversation is attached to, or None for the plain local assistant
    announced = conversation.fingerprint(conversation.local_binding())  # the partner the client was last told about
    active = {"turn": 0, "binding": conversation.local_binding()}  # captured at the start of each turn, never changed mid-turn
    turns = 0

    def snapshot():
        """The binding as the database says it is right now (not as any screen shows it)."""
        if attachment is None:
            return conversation.local_binding()
        services = request.app[team_web.TEAM].services()
        if services is None:
            raise NotFound()
        return conversation.team_binding(services, *attachment)

    async def send():
        while True:
            await ws.send_json(await queue.get())

    def emit(event):
        if event.get('type') == 'message' and event.get('role') == 'assistant':
            answers.append(event['text'])
        if active['turn'] and event.get('type') in TURN_EVENTS:
            event = {**event, 'turn': active['turn']}  # lets the client drop anything that arrives after its turn is over
            if event['type'] in ('message', 'audio'):
                event['binding'] = conversation.compact(active['binding'])
        queue.put_nowait(event)

    def announce(binding, reason):
        nonlocal announced
        announced = conversation.fingerprint(binding)
        emit({'type': 'binding', 'binding': binding, 'reason': reason})

    async def reply(text, audio=None, speak=False, profile=None):
        answers.clear()
        try:
            if audio is not None:
                emit({'type': 'status', 'state': 'transcribing', 'label': 'Transcrevendo sua fala…'})
                text = await asyncio.wait_for(request.app[SPEECH].transcribe(audio), timeout=120)
                if not text:
                    emit({'type': 'warning', 'message': 'Não identifiquei fala. Confira o microfone e tente novamente.'})
                    emit({'type': 'status', 'state': 'ready', 'label': 'Pronto para conversar'})
                    return
            integration["last_text"] = text[:300]
            emit({"type": "message", "id": uuid.uuid4().hex, "role": "user", "text": text})
            emit({"type": "status", "state": "processing", "label": "Pensando…"})
            await asyncio.wait_for(agent.reply(text), timeout=TURN_TIMEOUT)
            if speak and answers:
                emit({'type': 'status', 'state': 'synthesizing', 'label': 'Preparando áudio…'})
                try:
                    voice = {'profile_id': profile} if profile else {}  # the owner's voice, decided when the turn began
                    audio_result = await asyncio.wait_for(request.app[SPEECH].synthesize('\n'.join(answers), **voice), timeout=45)
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

    async def configure_mail(data):
        fields = {key: data.get(key) for key in ('email', 'password', 'imap_host', 'smtp_host')}
        try:
            if not all(isinstance(fields[key], str) for key in ('email', 'password')) or any(
                    fields[key] is not None and not isinstance(fields[key], str) for key in ('imap_host', 'smtp_host')):
                raise ValueError('Preencha o email e a senha de aplicativo.')
            async with SETUP_LOCK:
                result = await asyncio.to_thread(
                    mail_account().setup, fields['email'].strip(), fields['password'],
                    imap_host=(fields['imap_host'] or '').strip() or None, smtp_host=(fields['smtp_host'] or '').strip() or None,
                    read_only=data.get('read_only') is True)
            if 'erro' in result:
                emit({'type': 'setup_result', 'ok': False, 'message': result['erro']})
                return
            refresh = getattr(agent, 'refresh', None)
            if refresh is not None:
                with contextlib.suppress(Exception):
                    await refresh()
            emit({'type': 'setup_result', 'ok': True, 'reply': result['envio'], 'user': result['usuario'],
                  'message': 'Caixa conectada. A habilidade de email está ativa' + (' e você aprova cada resposta antes do envio.' if result['envio'] else ' (somente leitura).')})
            emit({'type': 'skill', 'id': 'email', 'state': 'ativa'})
        except ValueError as error:
            emit({'type': 'setup_result', 'ok': False, 'message': str(error)})
        except Exception:
            emit({'type': 'setup_result', 'ok': False, 'message': 'Não foi possível configurar a caixa. Confira os dados e tente novamente.'})

    async def decide(data):
        draft_id, decision = data.get('id'), data.get('decision')
        try:
            if not isinstance(draft_id, str) or decision not in ('approve', 'discard'):
                raise ValueError('Decisão inválida.')
            if decision == 'discard':
                result = await asyncio.to_thread(outbox.discard, draft_id)
            else:
                emit({'type': 'draft_result', 'draft': {'id': draft_id, 'state': 'enviando'}})
                result = await asyncio.to_thread(outbox.approve, draft_id, data.get('hash'))
            emit({'type': 'draft_result', 'draft': {'id': draft_id, **result}})  # an error result still names its draft
        except ValueError as error:
            emit({'type': 'rejected', 'message': str(error)})
        except Exception:
            emit({'type': 'draft_result', 'draft': {'id': draft_id, 'state': 'desconhecida',
                                                    'detail': 'Erro inesperado. Confira a pasta Enviados antes de tentar de novo.'}})

    def integration_result(request_id, result):
        emit({'type': 'integration_result', 'request_id': request_id, 'ok': bool(result.get('ok')), 'message': result.get('message', ''),
              'next': result.get('next'), 'resume': bool(result.get('original')), 'label': result.get('label')})

    async def refresh_card(request_id):
        # the rows of a card must show what is true now, whatever the attempt did
        with contextlib.suppress(Exception):
            emit({'type': 'integration_card', 'card': await asyncio.to_thread(integrations().refresh, request_id)})

    async def wait_oauth(request_id):
        try:
            integration_result(request_id, await asyncio.to_thread(integrations().oauth_wait, request_id))
            await refresh_card(request_id)
        except Exception:
            integration_result(request_id, {'ok': False, 'message': 'A autorização não foi concluída.'})

    async def integrate(data):
        """The person's clicks on an integration card. The model cannot produce these messages; secrets live only in ``values``."""
        kind, request_id, company = data.get('type'), data.get('request_id'), data.get('company_id')
        session = integrations()
        try:
            if session is None or not isinstance(request_id, str):
                raise NotFound()
            if kind == 'integration_pick':
                emit({'type': 'integration_card', 'card': await asyncio.to_thread(session.pick, request_id, data.get('provider'))})
            elif kind == 'integration_open':
                emit({'type': 'integration_form', 'form': await asyncio.to_thread(session.open, request_id, company)})
            elif kind == 'integration_use':
                integration_result(request_id, await asyncio.to_thread(session.use, request_id, company))
            elif kind == 'integration_submit':
                values, label = data.get('values'), data.get('label')
                if not isinstance(values, dict) or not (label is None or isinstance(label, str)):
                    raise ValueError('formulario')
                emit({'type': 'integration_busy', 'request_id': request_id, 'text': 'Salvando e testando a conexão…'})
                integration_result(request_id, await asyncio.to_thread(session.submit, request_id, company, label, values))
                await refresh_card(request_id)
            elif kind == 'integration_oauth':
                begun = await asyncio.to_thread(session.oauth_begin, request_id, company)
                if not begun.get('ok'):
                    integration_result(request_id, begun)
                else:
                    emit({'type': 'integration_oauth', 'request_id': request_id, 'url': begun['url']})
                    task = asyncio.create_task(wait_oauth(request_id))
                    decisions.add(task)
                    task.add_done_callback(decisions.discard)
            elif kind == 'integration_cancel':
                await asyncio.to_thread(session.oauth_cancel, request_id)
                integration_result(request_id, {'ok': False, 'message': 'A autorização foi cancelada.'})
            else:
                raise ValueError('tipo')
        except NotFound:
            emit({'type': 'integration_error', 'request_id': request_id if isinstance(request_id, str) else None,
                  'message': 'Este cartão expirou. Peça a integração de novo.'})
        except Exception:
            emit({'type': 'integration_error', 'request_id': request_id if isinstance(request_id, str) else None,
                  'message': 'Não foi possível concluir. Tente novamente.'})

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
        if hasattr(agent, 'on_integration'):
            agent.on_integration = card_for  # the tool exists only where the team services can answer it
        await asyncio.wait_for(agent.start(), timeout=30)
        mail = mail_account()
        emit({"type": "session", "model": request.app[CONFIG]["model"], "mail": "real" if mail.configured() else "demo",
              "reply": mail.configured() and mail.smtp_config() is not None})
        pending = await asyncio.to_thread(outbox.pending)
        if pending:  # drafts still waiting after a reload; the approval is the person's, so they come back to the screen
            emit({"type": "drafts", "drafts": pending})
        emit({"type": "status", "state": "ready", "label": "Pronto para conversar"})
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(message.data)
            except json.JSONDecodeError:
                emit({"type": "rejected", "message": "Mensagem inválida."})
                continue
            if isinstance(data, dict) and data.get('type') == 'cancel':
                if turn and not turn.done():  # interrupting generation is not the same as stopping speech or muting the microphone
                    turn.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await turn
                    emit({'type': 'cancelled'})
                    emit({'type': 'status', 'state': 'ready', 'label': 'Pronto para conversar'})
                else:
                    emit({'type': 'rejected', 'message': 'Nao ha resposta em andamento.'})
            elif isinstance(data, dict) and data.get('type') == 'setup_mail':
                # The form posts the credentials straight here: they never pass through the model, the history or the logs.
                task = asyncio.create_task(configure_mail(data))
                decisions.add(task)
                task.add_done_callback(decisions.discard)
            elif isinstance(data, dict) and str(data.get('type', '')).startswith('integration_'):
                task = asyncio.create_task(integrate(data))
                decisions.add(task)
                task.add_done_callback(decisions.discard)
            elif isinstance(data, dict) and data.get('type') == 'draft_decision':
                # The only way an email leaves: the person's click, as a message the model cannot produce.
                task = asyncio.create_task(decide(data))
                decisions.add(task)
                task.add_done_callback(decisions.discard)
            elif turn and not turn.done():
                emit({"type": "rejected", "message": "Aguarde a resposta em andamento.", "busy": True})
            elif isinstance(data, dict) and data.get('type') in ('attach', 'detach'):
                try:
                    if data['type'] == 'attach':
                        company, case = data.get('company_id'), data.get('case_id')
                        services = request.app[team_web.TEAM].services()
                        if services is None:
                            raise ValueError('O atendimento persistente esta desativado.')
                        attachment = None
                        candidate = conversation.team_binding(services, company, case)
                        attachment = (company, case)
                        announce(candidate, 'vinculo')
                    else:
                        attachment = None
                        announce(conversation.local_binding(), 'vinculo_removido')
                except NotFound:
                    emit({'type': 'rejected', 'message': 'Demanda nao encontrada.'})
                except ValueError as error:
                    emit({'type': 'rejected', 'message': str(error)})
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
                if text and len(text) <= 120 and SETUP_INTENT.search(text):
                    # "Configurar caixa de email": no model needed to open the secure form (a small model may not call the tool).
                    emit({'type': 'message', 'id': uuid.uuid4().hex, 'role': 'user', 'text': text})
                    emit({'type': 'setup', 'skill': 'email'})
                    emit({'type': 'message', 'id': uuid.uuid4().hex, 'role': 'assistant', 'text':
                          'Abri o formulário seguro para configurar sua caixa de email. A senha vai direto da tela para o cofre do Windows, sem passar pela conversa.'})
                    emit({'type': 'status', 'state': 'ready', 'label': 'Pronto para conversar'})
                    continue
                listing = mail_intent.parse(text) if text else None
                if listing is not None:
                    # "Liste meus emails": read only, answered here so it does not depend on a small model calling the tool.
                    emit({'type': 'message', 'id': uuid.uuid4().hex, 'role': 'user', 'text': text})
                    if mail.configured():
                        emit({'type': 'status', 'state': 'executing', 'label': 'Consultando seus e-mails…'})
                        try:
                            answer = mail_intent.render(await asyncio.to_thread(mail.list_recent, None, listing['limit'], None))
                        except Exception:
                            answer = 'Não consegui ler a caixa de email agora. Tente de novo em instantes.'
                    else:
                        emit({'type': 'setup', 'skill': 'email'})
                        answer = ('Sua caixa de email ainda não está configurada. Abri o formulário seguro; a senha vai direto da tela '
                                  'para o cofre do Windows, sem passar pela conversa.')
                    emit({'type': 'message', 'id': uuid.uuid4().hex, 'role': 'assistant', 'text': answer})
                    emit({'type': 'status', 'state': 'ready', 'label': 'Pronto para conversar'})
                    continue
                intent = INTEGRATION_INTENT.search(text) if text and len(text) <= 120 else None
                session = integrations() if intent else None
                if session is not None and await asyncio.to_thread(session.match, '', intent.group('alvo').strip()):
                    # "Conectar ao <sistema instalado>": the card needs no model (a small one may not call the tool).
                    card = await asyncio.to_thread(session.request, '', intent.group('alvo').strip())
                    emit({'type': 'message', 'id': uuid.uuid4().hex, 'role': 'user', 'text': text})
                    emit({'type': 'integration_card', 'card': card})
                    emit({'type': 'message', 'id': uuid.uuid4().hex, 'role': 'assistant',
                          'text': 'Mostrei na tela o cartão da integração. Os dados de acesso são digitados no formulário seguro, nunca na conversa.'})
                    emit({'type': 'status', 'state': 'ready', 'label': 'Pronto para conversar'})
                    continue
                count += 1
                turns += 1
                try:
                    current = snapshot()
                except NotFound:  # the case disappeared or the persistent mode was switched off: back to the plain assistant
                    attachment, current = None, conversation.local_binding()
                active.update(turn=turns, binding=current)
                if conversation.fingerprint(current) != announced:  # e.g. a transfer was accepted since the last turn
                    announce(current, 'mudou')
                voice_profile = (current.get('agent') or {}).get('voice_profile_ref')
                turn = asyncio.create_task(reply(text, audio, data.get('speak') is True, voice_profile))
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
    app.router.add_get("/auth/session", auth_session)
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


async def serve():
    """The local application on loopback; plus, only when ``remote.enabled`` is set and valid, the separate remote listener."""
    import sys
    from team.login import ConfigError, GoogleLogin, RemoteConfig
    from team.remote import build_app, ssl_context
    config = load_config()
    team_app = TeamApp(DATA_ROOT, config)
    runners = [(web.AppRunner(create_app(config=config, team_app=team_app), access_log=None), "127.0.0.1", PORT, None)]
    try:
        remote = RemoteConfig.from_config(config)
    except ConfigError as error:
        print(f"Acesso remoto NAO iniciado: {error}", file=sys.stderr)
        remote = RemoteConfig()
    if remote.enabled:
        login = GoogleLogin(remote, team_app.vault())
        runners.append((web.AppRunner(build_app(remote, team_app, login), access_log=None), remote.bind, remote.port, ssl_context(remote)))
    for runner, host, port, ssl in runners:
        await runner.setup()
        await web.TCPSite(runner, host, port, ssl_context=ssl).start()
    try:
        await asyncio.Event().wait()
    finally:
        for runner, *_ in runners:
            await runner.cleanup()


if __name__ == "__main__":
    # Do not log user messages or raw provider errors to disk.
    logging.basicConfig(level=logging.CRITICAL)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
