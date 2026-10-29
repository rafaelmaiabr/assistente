import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web, ClientSession

from team.app import TeamApp
from team.connectors.http_routes import ROUTES_FILE, RouteStore
from team.vault import MemoryVault
from tests.test_web import FakeSpeech
from web_server import ALLOWED_HOSTS, create_app


class ScriptedAgent:
    """Says what the test tells it to, and emits the tool events a real turn would."""
    instances, script = [], {}

    def __init__(self, config, emit):
        self.emit, self.inputs = emit, []
        self.instances.append(self)

    async def start(self):
        pass

    async def reply(self, text):
        self.inputs.append(text)
        tools, say = self.script[text]
        for name in tools:
            self.emit({"type": "tool", "name": name, "state": "started"})
        self.emit({"type": "message", "role": "assistant", "id": "a", "text": say})

    async def close(self):
        pass


class WebHonestyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ScriptedAgent.instances, ScriptedAgent.script = [], {}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runner = web.AppRunner(create_app(ScriptedAgent, {"model": "test"}, FakeSpeech(), data_root=self.root,
                                               team_app=TeamApp(self.root, {"model": "test"}, vault=MemoryVault())))
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        port = self.runner.addresses[0][1]
        self.base, self.host = f"http://127.0.0.1:{port}", f"127.0.0.1:{port}"
        ALLOWED_HOSTS.add(self.host)
        self.client, self.seen = ClientSession(), []

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        ALLOWED_HOSTS.discard(self.host)
        self.tmp.cleanup()

    async def until(self, ws, predicate):
        try:
            async with asyncio.timeout(10):
                while True:
                    event = await ws.receive_json()
                    self.seen.append(event)
                    if predicate(event):
                        return event
        except TimeoutError:
            raise AssertionError("never arrived; saw: " + "; ".join(json.dumps(e) for e in self.seen[-8:])) from None

    async def connect(self):
        ws = await self.client.ws_connect(self.base + "/ws", origin=self.base)
        await self.until(ws, lambda e: e.get("state") == "ready")
        return ws

    async def turn(self, ws, text):
        """Everything the screen gets until the turn is over."""
        self.seen = []
        await ws.send_json({"type": "message", "text": text})
        await self.until(ws, lambda e: e.get("type") == "status" and e.get("state") == "ready")
        return self.seen

    def kinds(self, events, kind):
        return [e for e in events if e.get("type") == kind]

    async def test_requests_out_of_reach_get_a_fixed_answer_and_the_model_is_never_asked(self):
        ws = await self.connect()
        for text, expected in (("Consegue acessar a internet?", "Não tenho acesso livre"), ("Mova os spam", "nada foi alterado"),
                               ("Apague meus emails", "nada foi alterado"), ("Marque como lido", "nada foi alterado"),
                               ("Envie a resposta sem aprovação", "Nada foi enviado"),
                               ("acesse https://exemplo.com.br/x", "nada foi acessado")):
            with self.subTest(text=text):
                events = await self.turn(ws, text)
                answers = [e["text"] for e in self.kinds(events, "message") if e["role"] == "assistant"]
                self.assertEqual(len(answers), 1)
                self.assertIn(expected, answers[0])
                self.assertEqual(self.kinds(events, "setup") + self.kinds(events, "integration_card") + self.kinds(events, "draft"), [])
        self.assertEqual(ScriptedAgent.instances[0].inputs, [])  # no model call, so no tool call: nothing could have happened

    async def test_the_internet_answer_lists_the_routes_the_operator_turned_on(self):
        RouteStore(self.root / ROUTES_FILE).enable("registro-dominio")
        ws = await self.connect()
        events = await self.turn(ws, "Consegue acessar a internet?")
        answer = [e["text"] for e in self.kinds(events, "message") if e["role"] == "assistant"][0]
        self.assertIn("registro-dominio", answer)
        self.assertNotIn("Nenhuma rota", answer)

    async def test_other_requests_still_go_to_the_model(self):
        ScriptedAgent.script = {"Qual a capital da França?": ([], "Paris."), "Como faço para apagar um email?": ([], "Depende do seu provedor.")}
        ws = await self.connect()
        for text in ScriptedAgent.script:
            events = await self.turn(ws, text)
            self.assertEqual(self.kinds(events, "warning"), [])
        self.assertEqual(ScriptedAgent.instances[0].inputs, list(ScriptedAgent.script))

    async def test_a_turn_that_announces_an_action_without_running_a_tool_is_flagged(self):
        ScriptedAgent.script = {"olá": ([], "Estou listando os emails agora.")}
        ws = await self.connect()
        events = await self.turn(ws, "olá")
        warnings = self.kinds(events, "warning")
        self.assertEqual(len(warnings), 1)
        self.assertIn("nada foi feito", warnings[0]["message"])
        self.assertIn("liste meus emails", warnings[0]["message"])

    async def test_the_same_words_after_a_real_tool_call_are_not_flagged(self):
        ScriptedAgent.script = {"olá": (["listar_emails"], "Estou listando: 1. Reunião."), "outra": ([], "Tudo certo.")}
        ws = await self.connect()
        self.assertEqual(self.kinds(await self.turn(ws, "olá"), "warning"), [])
        self.assertEqual(self.kinds(await self.turn(ws, "outra"), "warning"), [])  # and the counter starts again each turn

    async def test_a_button_in_the_model_text_with_no_card_is_flagged(self):
        ScriptedAgent.script = {"conecte": ([], "Clique em [Conectar sistema de consulta de domínio] para continuar.")}
        ws = await self.connect()
        warnings = self.kinds(await self.turn(ws, "conecte"), "warning")
        self.assertEqual(len(warnings), 1)
        self.assertIn("nenhum foi mostrado", warnings[0]["message"])

    async def test_the_flag_of_one_turn_does_not_leak_into_the_next(self):
        ScriptedAgent.script = {"um": ([], "Estou verificando."), "dois": ([], "Pronto, sem nada a anunciar.")}
        ws = await self.connect()
        self.assertEqual(len(self.kinds(await self.turn(ws, "um"), "warning")), 1)
        self.assertEqual(self.kinds(await self.turn(ws, "dois"), "warning"), [])


if __name__ == "__main__":
    unittest.main()
