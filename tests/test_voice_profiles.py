import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from aiohttp import ClientSession, web
from voice_profiles import VoiceProfiles, WindowsSecrets, defaults, validate_settings
from voice_engine import VoiceEngine, ASSETS
from speech_service import SpeechService
from web_server import create_app, ALLOWED_HOSTS


class MemorySecrets:
    def __init__(self): self.key = None
    def get(self): return self.key
    def set(self, value): self.key = value
    def delete(self): self.key = None


class ProfilesTests(unittest.TestCase):
    def test_persistence_rejects_secrets_and_invalid_numbers(self):
        with tempfile.TemporaryDirectory() as folder:
            store = VoiceProfiles(folder, MemorySecrets())
            data = store.snapshot()
            data['active'] = 'pf_dora'
            data['profiles'][1]['speed'] = 0.9
            store.save(data)
            self.assertEqual(VoiceProfiles(folder).snapshot(), data)
            for invalid in [float('nan'), float('inf'), True, -1, '1']:
                bad = copy.deepcopy(data)
                bad['profiles'][1]['speed'] = invalid
                with self.assertRaises(ValueError): store.save(bad)
            bad = copy.deepcopy(data)
            bad['profiles'][4]['api_key'] = 'private'
            with self.assertRaises(ValueError): store.save(bad)
            self.assertNotIn('private', store.path.read_text(encoding='utf-8'))
            self.assertEqual(store.snapshot(), data)

    def test_rejects_duplicates_paths_unknown_models_and_active(self):
        for modify in [lambda d: d.update(active='missing'),
                       lambda d: d['profiles'].append(d['profiles'][0]),
                       lambda d: d['profiles'][4].update(voice='../bad'),
                       lambda d: d['profiles'][4].update(model='unlisted'),
                       lambda d: d['profiles'][1].update(voice='not-portuguese')]:
            data = defaults(); modify(data)
            with self.assertRaises(ValueError): validate_settings(data)

    def test_windows_vault_round_trip_and_no_plaintext_fallback(self):
        fake = MagicMock()
        fake.CRED_TYPE_GENERIC = 1; fake.CRED_PERSIST_LOCAL_MACHINE = 2
        fake.CredRead.return_value = {'CredentialBlob': 'fake-secret'.encode('utf-16-le')}
        with patch.dict('sys.modules', {'win32cred': fake}):
            vault = WindowsSecrets('test-voice-scope')
            vault.set('fake-secret')
            self.assertEqual(vault.get(), 'fake-secret')
            vault.delete()
            self.assertEqual(fake.CredWrite.call_args[0][0]['CredentialBlob'], 'fake-secret')
            fake.CredWrite.side_effect = RuntimeError('raw provider secret')
            with self.assertRaisesRegex(ValueError, 'Nenhuma cópia em texto'):
                vault.set('fake-secret')
            with self.assertRaises(ValueError): vault.set('bad key\n')

    def test_kokoro_requires_explicit_preparation(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = VoiceEngine(VoiceProfiles(folder, MemorySecrets()), folder)
            self.assertFalse(engine.ready())
            with self.assertRaisesRegex(ValueError, 'Prepare o Kokoro'):
                engine.local('teste', defaults()['profiles'][1])
            with patch('voice_engine.urllib.request.urlopen') as opening:
                opening.return_value.__enter__.return_value.read.side_effect = [b'corrupt', b'']
                with self.assertRaisesRegex(ValueError, 'verificação'):
                    engine.prepare()
            self.assertEqual(list(engine.folder.iterdir()), [])


class VoiceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = VoiceProfiles(self.folder.name, MemorySecrets())
        self.speech = SpeechService({}, self.store)
        self.speech.engine = VoiceEngine(self.store, self.folder.name)
        self.speech.synthesize = AsyncMock(return_value='test-wav')
        self.runner = web.AppRunner(create_app(lambda *_: None, {'model': 'fake'}, self.speech))
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', 0).start()
        self.host = '127.0.0.1:' + str(self.runner.addresses[0][1])
        ALLOWED_HOSTS.add(self.host)
        self.base = 'http://' + self.host
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close(); await self.runner.cleanup()
        ALLOWED_HOSTS.discard(self.host); self.folder.cleanup()

    async def post(self, op, body):
        return await self.client.post(self.base + '/api/voices/' + op, json=body, headers={'Origin': self.base})

    async def test_origin_and_content_type_required(self):
        for origin in [None, 'http://evil.test']:
            headers = {'Origin': origin} if origin else {}
            response = await self.client.post(self.base + '/api/voices/settings', json=defaults(), headers=headers)
            self.assertEqual(response.status, 403)
        response = await self.client.post(self.base + '/api/voices/settings', data='{}', headers={'Origin': self.base})
        self.assertEqual(response.status, 415)

    async def test_key_not_returned_or_persisted_and_removable(self):
        response = await self.post('credential', {'key': 'fake-private-key'})
        data = await response.json()
        self.assertTrue(data['key_configured'])
        self.assertNotIn('fake-private-key', json.dumps(data))
        response = await self.post('settings', defaults())
        self.assertEqual(response.status, 200)
        self.assertNotIn('fake-private-key', self.store.path.read_text())
        response = await self.post('credential', {'key': None})
        self.assertFalse((await response.json())['key_configured'])

    async def test_preview_does_not_switch_active_and_cloud_requires_explicit_opt_in(self):
        response = await self.post('preview', {'profile': 'pf_dora'})
        self.assertEqual((await response.json())['wav'], 'test-wav')
        self.assertEqual(self.store.snapshot()['active'], 'windows')
        response = await self.post('preview', {'profile': 'elevenlabs'})
        self.assertEqual(response.status, 400)
        self.speech.synthesize.assert_awaited_once()
        response = await self.post('preview', {'profile': 'elevenlabs', 'allow_cloud': True})
        self.assertEqual(response.status, 200)

    async def test_provider_errors_do_not_expose_details(self):
        with patch.object(self.store.secrets, 'set', side_effect=RuntimeError('raw-secret')):
            response = await self.post('credential', {'key': 'test'})
        self.assertEqual(response.status, 503)
        self.assertNotIn('raw-secret', await response.text())


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_elevenlabs_fixed_origin_no_redirects_and_bounded_pcm(self):
        with tempfile.TemporaryDirectory() as folder:
            secrets = MemorySecrets(); secrets.set('fake-private-key')
            store = VoiceProfiles(folder, secrets)
            data = store.snapshot(); data['active'] = 'elevenlabs'; data['profiles'][-1]['voice'] = 'voice123'
            store.save(data)
            engine = VoiceEngine(store, folder)
            response = MagicMock(); response.status = 200
            async def chunks(_): yield b'\x00\x00' * 20
            response.content.iter_chunked = chunks
            session = MagicMock()
            session.post.return_value.__aenter__ = AsyncMock(return_value=response)
            with patch('voice_engine.ClientSession') as client:
                client.return_value.__aenter__ = AsyncMock(return_value=session)
                pcm, rate, channels = await engine.synthesize('teste')
            self.assertEqual((len(pcm), rate, channels), (40, 22050, 1))
            args, kwargs = session.post.call_args
            self.assertEqual(args[0], 'https://api.elevenlabs.io/v1/text-to-speech/voice123?output_format=pcm_22050')
            self.assertFalse(kwargs['allow_redirects'])
            self.assertEqual(kwargs['headers'], {'xi-api-key': 'fake-private-key'})
            response.status = 401
            with patch('voice_engine.ClientSession') as client:
                client.return_value.__aenter__ = AsyncMock(return_value=session)
                with self.assertRaisesRegex(ValueError, 'recusou'):
                    await engine.synthesize('teste')
