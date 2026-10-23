import tempfile
import unittest
from pathlib import Path
from aiohttp import web, ClientSession
from web_server import create_app, ALLOWED_HOSTS


class PersistenceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.runner = web.AppRunner(create_app(object, {'model': 'test'}, object(), data_root=Path(self.folder.name)))
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', 0).start()
        self.host = f'127.0.0.1:{self.runner.addresses[0][1]}'
        self.base = f'http://{self.host}'
        ALLOWED_HOSTS.add(self.host)
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        ALLOWED_HOSTS.discard(self.host)
        self.folder.cleanup()

    def call(self, method, path, origin=True):
        headers = {'Origin': self.base} if origin else {}
        return self.client.request(method, self.base + path, json={} if method == 'POST' else None, headers=headers)

    async def test_default_is_off_then_enable_and_disable_inform_the_user(self):
        async with self.call('GET', '/api/persistence') as response:
            data = await response.json()
        self.assertFalse(data['enabled'])
        async with self.call('POST', '/api/persistence/enable') as response:
            data = await response.json()
        self.assertTrue(data['enabled'])
        self.assertIn('Conversas anteriores não são importadas', data['notice'])
        async with self.call('POST', '/api/persistence/disable') as response:
            self.assertFalse((await response.json())['enabled'])
        self.assertTrue((Path(self.folder.name) / 'team-data' / 'team.sqlite3').exists())

    async def test_requires_same_origin_and_known_operation(self):
        async with self.call('GET', '/api/persistence', origin=False) as response:
            self.assertEqual(response.status, 403)
        async with self.call('POST', '/api/persistence/enable', origin=False) as response:
            self.assertEqual(response.status, 403)
        async with self.call('POST', '/api/persistence/delete') as response:
            self.assertEqual(response.status, 404)
        async with self.call('GET', '/api/persistence/enable') as response:
            self.assertEqual(response.status, 404)
        self.assertFalse((Path(self.folder.name) / 'team-data').exists())

    async def test_failed_migration_reports_without_enabling(self):
        folder = Path(self.folder.name) / 'team-data'
        folder.mkdir()
        (folder / 'team.sqlite3').write_bytes(b'lixo' * 100)
        async with self.call('POST', '/api/persistence/enable') as response:
            data = await response.json()
        self.assertFalse(data['enabled'])
        self.assertIn('nao foi alterado', data['error'])


if __name__ == '__main__':
    unittest.main()
