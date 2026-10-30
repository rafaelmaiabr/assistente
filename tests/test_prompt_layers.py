import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from team.connectors.http_routes import RouteStore

LIVEKIT = importlib.util.find_spec("livekit") is not None
CONFIG = {"mcp_servers": [], "model": "x", "ollama_url": "http://x"}
# Size of the whole prompt in characters before #135 (1707, 2112 and 3053); the ceilings keep it from growing back.
CEILING = {"demo": 1707, "real": 2112, "full": 3053}  # exactly the sizes before #135: persona, memory and triage lines were added since and it is still below


@unittest.skipUnless(LIVEKIT, "livekit is not installed here")
class PromptLayerTests(unittest.TestCase):
    """Stable identity first, then the index of what is registered, then what changes with the state (#135)."""

    def setUp(self):
        import assistant_core
        self.core = assistant_core
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RouteStore(Path(tmp.name) / "http-routes.json")

    def mail(self, configured, smtp=False, note=""):
        mail = mock.Mock()
        mail.configured.return_value, mail.smtp_config.return_value, mail.prompt_note = configured, (object() if smtp else None), note
        return mail

    def prompt(self, mail, card=False):
        return self.core.agent_parts(CONFIG, mail, mock.Mock(), on_integration=(lambda *a: {}) if card else None, routes=self.store)[0]

    def test_the_stable_layer_is_the_same_prefix_in_every_state(self):
        self.store.enable("registro-dominio")
        states = [self.prompt(self.mail(False)), self.prompt(self.mail(True)), self.prompt(self.mail(True, smtp=True)),
                  self.prompt(self.mail(True, smtp=True, note="Caixa da empresa X. "), card=True)]
        for text in states:
            self.assertTrue(text.startswith(self.core.BASE))
        self.assertEqual(len({text[:len(self.core.BASE)] for text in states}), 1)

    def test_the_order_is_stable_then_index_then_the_mailbox_then_card_then_routes(self):
        self.store.enable("registro-dominio")
        text = self.prompt(self.mail(True, smtp=True), card=True)
        marks = [text.index(self.core.BASE), text.index("Capacidades reais nesta sessão"), text.index(self.core.REAL),
                 text.index(self.core.INTEGRATIONS), text.index("Consulta de rotas")]
        self.assertEqual(marks, sorted(marks))

    def test_the_prompt_stays_below_the_size_it_had_before(self):
        self.assertLessEqual(len(self.prompt(self.mail(False))), CEILING["demo"])
        self.assertLessEqual(len(self.prompt(self.mail(True, smtp=True))), CEILING["real"])
        self.store.enable("registro-dominio")
        self.assertLessEqual(len(self.prompt(self.mail(True, smtp=True), card=True)), CEILING["full"])

    def test_the_long_instructions_live_in_the_tool_descriptions(self):
        tools = {t.info.name: t.info.description for t in self.core.agent_parts(CONFIG, self.mail(True, smtp=True), mock.Mock(),
                                                                                  on_integration=lambda *a: {}, routes=self.store)[1] if getattr(t, "info", None)}
        self.assertIn("nunca diga que enviou", tools["preparar_resposta"])
        self.assertIn("nunca escolhe a empresa", tools["solicitar_integracao"])
        self.assertIn("uidvalidity", tools["ler_email"])


@unittest.skipUnless(LIVEKIT, "livekit is not installed here")
class RefreshTests(unittest.IsolatedAsyncioTestCase):
    """Changing the mailbox changes the volatile part and the tools; the stable part is not rewritten in the middle of a conversation."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.routes = RouteStore(Path(tmp.name) / "http-routes.json")

    async def make(self, mails):
        import assistant_core
        from browser_agent import BrowserAgent
        state = iter(mails)
        browser = BrowserAgent(CONFIG, lambda event: None)
        browser.mail_source = lambda: _async(next(state))
        calls = []

        class FakeAgent:
            instructions = ""

            async def update_tools(self, tools):
                calls.append(("tools", len(tools)))

            async def update_instructions(self, text):
                calls.append(("instructions", text))
                self.instructions = text

        browser.agent = FakeAgent()
        with mock.patch.object(assistant_core, "RouteStore", return_value=self.routes), mock.patch.object(assistant_core, "DATA_ROOT", Path(".")):
            browser.agent.instructions = assistant_core.agent_parts(CONFIG, mails[0][0], mails[0][1], routes=self.routes,
                                                                 memory=browser.memory, on_memory=browser.on_memory,
                                                                 rules=browser.rules, on_rule=browser.on_rule)[0]
            mails_after = mails[1:]
            state = iter(mails_after)
            browser.mail_source = lambda: _async(next(state))
            return browser, calls, assistant_core

    def mail(self, configured, note=""):
        mail = mock.Mock()
        mail.configured.return_value, mail.smtp_config.return_value, mail.prompt_note = configured, None, note
        return mail, mock.Mock()

    async def test_switching_the_mailbox_updates_the_tools_and_only_the_volatile_text(self):
        browser, calls, core = await self.make([self.mail(False), self.mail(True, "Caixa nova. ")])
        before = browser.agent.instructions
        with mock.patch.object(core, "RouteStore", return_value=self.routes):
            await browser.refresh()
        sent = [c for c in calls if c[0] == "instructions"]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1].startswith(core.BASE))
        self.assertEqual(sent[0][1][:len(core.BASE)], before[:len(core.BASE)])  # the stable prefix is byte for byte the same
        self.assertIn("Caixa nova.", sent[0][1])

    async def test_nothing_is_rewritten_when_the_text_did_not_change(self):
        browser, calls, core = await self.make([self.mail(True), self.mail(True)])
        with mock.patch.object(core, "RouteStore", return_value=self.routes):
            await browser.refresh()
        self.assertEqual([c[0] for c in calls], ["tools"])


async def _async(value):
    return value


if __name__ == "__main__":
    unittest.main()
