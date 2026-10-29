import importlib.util
import unittest
from types import SimpleNamespace

from tool_guard import Guard, LIMIT_TEXT, REPEATS, limit_summary, scrub, std_error

LIVEKIT = importlib.util.find_spec("livekit") is not None
SECRETS = ["https://api.exemplo.com.br/v1/rota?chave=abc", "C:\\Users\\fulano9\\dados\\config.json", "/home/fulano9/.config/app/config.json",
           "token_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ScrubTests(unittest.TestCase):
    def test_no_address_path_or_token_survives(self):
        for secret in SECRETS:
            cleaned = scrub(f"falhou ao abrir {secret} agora")
            self.assertNotIn(secret, cleaned)
            self.assertNotIn("fulano9", cleaned)
            self.assertNotIn("exemplo.com", cleaned)
            self.assertNotIn("ABCDEFGHIJ", cleaned)
            self.assertTrue(cleaned.startswith("falhou ao abrir"))

    def test_the_standard_error_has_the_three_parts_and_is_scrubbed(self):
        error = std_error(f"nao abriu {SECRETS[0]}", retry=False, say=f"veja {SECRETS[1]}")
        self.assertEqual(set(error), {"erro", "pode_tentar_de_novo", "diga_a_pessoa", "dados_nao_confiaveis"})
        self.assertIs(error["pode_tentar_de_novo"], False)
        self.assertNotIn("exemplo.com", repr(error))
        self.assertNotIn("fulano9", repr(error))


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_exception_becomes_a_standard_error_without_internals(self):
        async def boom():
            raise RuntimeError(f"conexao recusada em {SECRETS[0]} ({SECRETS[1]})")

        result = await Guard().call("t", (1,), boom)
        self.assertTrue(result["pode_tentar_de_novo"])
        self.assertNotIn("exemplo.com", repr(result))
        self.assertNotIn("fulano9", repr(result))
        self.assertNotIn("recusada", repr(result))

    async def test_an_error_result_gets_the_standard_fields_and_a_scrubbed_message(self):
        async def failed():
            return {"erro": f"rota fora do ar: {SECRETS[0]}"}

        result = await Guard().call("t", (1,), failed)
        self.assertIn("rota fora do ar", result["erro"])
        self.assertNotIn("exemplo.com", repr(result))
        self.assertIn("diga_a_pessoa", result)

    async def test_a_good_result_is_returned_as_it_is(self):
        data = {"mensagens": [1]}

        async def fine():
            return data

        self.assertIs(await Guard().call("t", (1,), fine), data)

    async def test_an_identical_call_that_keeps_failing_is_blocked_and_not_run_again(self):
        guard, runs = Guard(), []

        async def failed():
            runs.append(1)
            return {"erro": "fora do ar"}

        for _ in range(REPEATS):
            await guard.call("t", ("a",), failed)
        blocked = await guard.call("t", ("a",), failed)
        self.assertEqual(len(runs), REPEATS)  # the third call did not run
        self.assertIs(blocked["pode_tentar_de_novo"], False)
        await guard.call("t", ("b",), failed)  # other arguments are another call
        self.assertEqual(len(runs), REPEATS + 1)

    async def test_a_success_clears_the_count_and_an_old_failure_expires(self):
        clock, guard, runs = Clock(), None, []
        guard = Guard(clock=clock)
        outcomes = iter([{"erro": "x"}, {"ok": 1}, {"erro": "x"}, {"erro": "x"}, {"ok": 2}])

        async def step():
            runs.append(1)
            return next(outcomes)

        for _ in range(4):
            await guard.call("t", ("a",), step)
        self.assertEqual(len(runs), 4)  # fail, ok (cleared), fail, fail
        blocked = await guard.call("t", ("a",), step)
        self.assertEqual(len(runs), 4)
        self.assertIn("erro", blocked)
        clock.now += 1000  # the person tries again later
        self.assertEqual(await guard.call("t", ("a",), step), {"ok": 2})


class LimitSummaryTests(unittest.TestCase):
    def test_it_says_what_was_done_and_what_failed(self):
        text = limit_summary([("listar_emails", True), ("ler_email", False), ("listar_emails", True)])
        self.assertIn(LIMIT_TEXT, text)
        self.assertIn("Consultei: listar_emails.", text)
        self.assertIn("Falharam: ler_email.", text)

    def test_with_nothing_obtained_it_says_so(self):
        self.assertIn("Não consegui obter nenhum resultado.", limit_summary([("ler_email", False)]))


@unittest.skipUnless(LIVEKIT, "livekit is not installed here")
class StepBudgetTests(unittest.IsolatedAsyncioTestCase):
    """The chat answers with a summary when the model runs out of steps without saying anything."""

    def agent(self, script):
        from browser_agent import BrowserAgent
        sent = []
        agent = BrowserAgent({"model": "x", "ollama_url": "http://x", "mcp_servers": []}, sent.append)

        async def generate_reply(user_input):
            for event in script(agent):
                agent.on_tool(event)

        agent.session = SimpleNamespace(generate_reply=generate_reply)
        return agent, sent

    @staticmethod
    def started(name):
        return SimpleNamespace(update=SimpleNamespace(type="tool_call_started", function_call=SimpleNamespace(name=name)))

    @staticmethod
    def ended(status):
        return SimpleNamespace(update=SimpleNamespace(type="tool_call_ended", status=status))

    async def test_tools_ran_and_the_model_said_nothing_so_a_summary_is_sent(self):
        agent, sent = self.agent(lambda a: [self.started("listar_emails"), self.ended("done"), self.started("ler_email"), self.ended("error")])
        await agent.reply("resuma meus emails")
        messages = [m for m in sent if m["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn(LIMIT_TEXT, messages[0]["text"])
        self.assertIn("listar_emails", messages[0]["text"])
        self.assertIn("ler_email", messages[0]["text"])

    async def test_without_tools_and_without_an_answer_it_still_fails_loudly(self):
        agent, _ = self.agent(lambda a: [])
        with self.assertRaises(RuntimeError):
            await agent.reply("oi")

    async def test_a_normal_answer_gets_no_summary(self):
        def script(agent):
            yield self.started("listar_emails")
            yield self.ended("done")
            agent.on_message(SimpleNamespace(item=SimpleNamespace(type="message", role="assistant", text_content="Aqui estão.", id="m1")))

        agent, sent = self.agent(script)
        await agent.reply("oi")
        self.assertEqual([m["text"] for m in sent if m["type"] == "message"], ["Aqui estão."])


@unittest.skipUnless(LIVEKIT, "livekit is not installed here")
class ToolWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_email_tool_blocks_a_call_that_keeps_failing_and_hides_the_cause(self):
        import assistant_core

        class Mail:
            runs = 0

            def configured(self):
                return True

            def list_recent(self, *_):
                Mail.runs += 1
                raise OSError(f"sem rede para {SECRETS[0]} em {SECRETS[1]}")

        listar = next(t for t in assistant_core.email_tools(Mail(), None) if t.info.name == "listar_emails")
        results = [await listar(limite=5, desde="") for _ in range(REPEATS + 1)]
        self.assertEqual(Mail.runs, REPEATS)
        for result in results:
            self.assertIn("diga_a_pessoa", result)
            self.assertNotIn("exemplo.com", repr(result))
            self.assertNotIn("fulano9", repr(result))


if __name__ == "__main__":
    unittest.main()
