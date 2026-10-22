import asyncio
import unittest
from unittest.mock import patch
from aiohttp import web, ClientSession, WSServerHandshakeError
from web_server import create_app, ALLOWED_HOSTS
from speech_service import encode_wav


class FakeSpeech:
    def __init__(self): self.transcript = 'fala transcrita'; self.spoken = []; self.fail = False
    async def transcribe(self, data): return self.transcript
    async def synthesize(self, text):
        if self.fail: raise RuntimeError('tts unavailable')
        self.spoken.append(text)
        return encode_wav(b'\0\0' * 16000, 16000, 1)


class FakeAgent:
    instances = []

    def __init__(self, config, emit):
        self.emit, self.closed = emit, False
        self.inputs = []
        self.gate = asyncio.Event()
        self.instances.append(self)

    async def start(self):
        pass

    async def reply(self, text):
        self.inputs.append(text)
        await self.gate.wait()
        if text == 'falhar':
            raise RuntimeError('provider failure')
        self.emit({'type': 'message', 'role': 'assistant', 'id': 'answer', 'text': text})

    async def close(self):
        self.closed = True


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeAgent.instances = []
        self.speech = FakeSpeech()
        self.runner = web.AppRunner(create_app(FakeAgent, {'model': 'test'}, self.speech))
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.base = f'http://127.0.0.1:{port}'
        self.host = f'127.0.0.1:{port}'
        ALLOWED_HOSTS.add(self.host)
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        ALLOWED_HOSTS.discard(self.host)

    async def connect(self):
        ws = await self.client.ws_connect(self.base + '/ws', origin=self.base)
        await self.until(ws, lambda e: e.get('state') == 'ready')
        return ws

    async def until(self, ws, predicate):
        async with asyncio.timeout(3):
            while True:
                event = await ws.receive_json()
                if predicate(event):
                    return event

    async def test_origin_and_host_are_restricted(self):
        with self.assertRaises(WSServerHandshakeError) as error:
            await self.client.ws_connect(self.base + '/ws', origin='https://evil.example')
        self.assertEqual(error.exception.status, 403)
        response = await self.client.get(self.base + '/api/health', headers={'Host': 'evil.example'})
        self.assertEqual(response.status, 403)

    async def test_concurrent_send_is_rejected_and_cleanup_runs(self):
        ws = await self.connect()
        await ws.send_json({'type': 'message', 'text': 'primeira'})
        await self.until(ws, lambda e: e.get('state') == 'processing')
        await ws.send_json({'type': 'message', 'text': 'segunda'})
        event = await self.until(ws, lambda e: e['type'] == 'rejected')
        self.assertIn('Aguarde', event['message'])
        agent = FakeAgent.instances[0]
        agent.gate.set()
        await self.until(ws, lambda e: e.get('state') == 'ready')
        self.assertEqual(agent.inputs, ['primeira'])
        await ws.close()
        for _ in range(30):
            if agent.closed:
                break
            await asyncio.sleep(.01)
        self.assertTrue(agent.closed)

    async def test_validation_and_failure_keep_session_usable(self):
        ws = await self.connect()
        await ws.send_str('bad json')
        await self.until(ws, lambda e: e['type'] == 'rejected')
        await ws.send_json({'type': 'message', 'text': 'x' * 8001})
        await self.until(ws, lambda e: e['type'] == 'rejected')
        FakeAgent.instances[0].gate.set()
        await ws.send_json({'type': 'message', 'text': 'falhar'})
        error = await self.until(ws, lambda e: e['type'] == 'error')
        self.assertFalse(error['fatal'])
        self.assertNotIn('provider failure', error['message'])
        await ws.send_json({'type': 'message', 'text': 'recuperar'})
        answer = await self.until(ws, lambda e: e.get('role') == 'assistant')
        self.assertEqual(answer['text'], 'recuperar')
        await ws.close()

    async def test_tabs_have_independent_sessions(self):
        one, two = await self.connect(), await self.connect()
        self.assertEqual(len(FakeAgent.instances), 2)
        await one.close()
        self.assertFalse(two.closed)
        FakeAgent.instances[1].gate.set()
        await two.send_json({'type': 'message', 'text': 'outra aba'})
        await self.until(two, lambda e: e.get('role') == 'assistant')
        self.assertEqual(FakeAgent.instances[1].inputs, ['outra aba'])
        await two.close()

    async def test_timeout_closes_stuck_session(self):
        with patch('web_server.TURN_TIMEOUT', .05):
            ws = await self.connect()
            await ws.send_json({'type': 'message', 'text': 'lenta'})
            event = await self.until(ws, lambda e: e['type'] == 'error')
            self.assertTrue(event['fatal'])
            await ws.close()

    async def test_disconnect_cancels_inflight_reply(self):
        ws = await self.connect()
        await ws.send_json({'type': 'message', 'text': 'em andamento'})
        await self.until(ws, lambda e: e.get('state') == 'processing')
        await ws.close()
        for _ in range(30):
            if FakeAgent.instances[0].closed:
                break
            await asyncio.sleep(.01)
        self.assertTrue(FakeAgent.instances[0].closed)

    async def test_audio_transcribes_into_same_session_and_returns_speech(self):
        ws = await self.connect()
        FakeAgent.instances[0].gate.set()
        await ws.send_json({'type': 'audio', 'wav': encode_wav(b'\0\0'*16000,16000,1), 'speak': True})
        await self.until(ws, lambda e: e.get('state') == 'transcribing')
        item = await self.until(ws, lambda e: e.get('role') == 'user')
        self.assertEqual(item['text'], 'fala transcrita')
        audio = await self.until(ws, lambda e: e['type'] == 'audio')
        self.assertTrue(audio['wav'])
        await self.until(ws, lambda e: e.get('state') == 'ready')
        self.assertEqual(self.speech.spoken, ['fala transcrita'])
        await ws.close()

    async def test_audio_validation_silence_and_tts_failure(self):
        ws = await self.connect()
        FakeAgent.instances[0].gate.set()
        await ws.send_json({'type': 'audio', 'wav': 'bad'})
        await self.until(ws, lambda e: e['type'] == 'rejected')
        self.assertEqual(FakeAgent.instances[0].inputs, [])
        self.speech.transcript = ''
        await ws.send_json({'type': 'audio', 'wav': encode_wav(b'\0\0'*16000,16000,1)})
        await self.until(ws, lambda e: e.get('state') == 'ready')
        self.assertEqual(FakeAgent.instances[0].inputs, [])
        self.speech.fail = True
        await ws.send_json({'type': 'message', 'text': 'teste', 'speak': True})
        await self.until(ws, lambda e: e.get('role') == 'assistant')
        await self.until(ws, lambda e: e['type'] == 'warning')
        await self.until(ws, lambda e: e.get('state') == 'ready')
        await ws.close()
