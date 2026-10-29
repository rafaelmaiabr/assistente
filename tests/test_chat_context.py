import asyncio
import json
import unittest

from livekit.agents import llm

import assistant_core
from chat_context import CUT_NOTE, MARKER, MAX_RESULT_CHARS, fit, prune

LONG = "mensagem muito longa " * 40  # well over the 200-character floor


def history(rounds, long=LONG):
    """rounds user turns, each with a tool call and its result, then the assistant's answer."""
    ctx = llm.ChatContext.empty()
    for n in range(rounds):
        ctx.add_message(role="user", content=f"pergunta {n}")
        ctx.insert(llm.FunctionCall(call_id=f"c{n}", name="listar_emails", arguments="{}"))
        ctx.insert(llm.FunctionCallOutput(call_id=f"c{n}", name="listar_emails", output=f"{long}{n}", is_error=False))
        ctx.add_message(role="assistant", content=f"resposta {n}")
    return ctx


def outputs(ctx):
    return [i.output for i in ctx.items if i.type == "function_call_output"]


class PruneTests(unittest.TestCase):
    def test_old_long_results_are_cleared_and_the_recent_turns_are_intact(self):
        ctx = history(5)
        pruned, count = prune(ctx)
        self.assertEqual(count, 3)
        self.assertEqual(outputs(pruned)[:3], [MARKER] * 3)
        self.assertEqual(outputs(pruned)[3:], outputs(ctx)[3:])  # the last two turns are untouched
        self.assertEqual(outputs(ctx)[0], f"{LONG}0")  # the input was not changed

    def test_what_was_said_and_answered_stays_and_calls_keep_their_results(self):
        pruned, _ = prune(history(5))
        self.assertEqual([i.content for i in pruned.items if i.type == "message"], [c for n in range(5) for c in ([f"pergunta {n}"], [f"resposta {n}"])])
        calls = [i.call_id for i in pruned.items if i.type == "function_call"]
        self.assertEqual(calls, [i.call_id for i in pruned.items if i.type == "function_call_output"])

    def test_short_results_stay_and_pruning_twice_changes_nothing(self):
        short = history(5, long="ok ")
        self.assertEqual(prune(short)[1], 0)
        once, _ = prune(history(5))
        self.assertEqual(prune(once)[1], 0)

    def test_a_short_conversation_is_never_touched(self):
        ctx = history(2)
        self.assertEqual(prune(ctx), (ctx, 0))

    def test_a_tool_round_not_answered_yet_is_in_the_kept_tail(self):
        ctx = history(4)
        ctx.add_message(role="user", content="e agora?")
        ctx.insert(llm.FunctionCall(call_id="x", name="listar_emails", arguments="{}"))
        ctx.insert(llm.FunctionCallOutput(call_id="x", name="listar_emails", output=LONG, is_error=False))  # the model has not answered yet
        pruned, _ = prune(ctx)
        self.assertEqual(outputs(pruned)[-1], LONG)
        self.assertEqual(outputs(pruned)[-2], f"{LONG}3")


class FitTests(unittest.TestCase):
    def test_a_small_result_is_returned_as_it_is(self):
        result = {"mensagens": [{"uid": 1}]}
        self.assertIs(fit(result), result)

    def test_a_big_result_is_cut_and_says_so(self):
        cut = fit({"mensagens": ["x" * 500] * 50})
        self.assertEqual(cut["aviso"], CUT_NOTE)
        self.assertTrue(cut["dados_nao_confiaveis"])
        self.assertEqual(len(cut["parcial"]), MAX_RESULT_CHARS)

    def test_a_result_that_cannot_be_shown_becomes_a_plain_error(self):
        self.assertIn("erro", fit({"x": object()}))
        self.assertEqual(json.loads(json.dumps(fit({"erro": "falhou"}))), {"erro": "falhou"})


class AgentHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_hook_clears_the_turn_context_and_the_stored_history(self):
        agent = assistant_core.ChatAgent(instructions="x")
        agent._chat_ctx = history(5)
        turn = agent.chat_ctx.copy()
        turn.add_message(role="user", content="nova pergunta")
        await agent.on_user_turn_completed(turn, turn.items[-1])
        self.assertEqual(outputs(turn), [MARKER] * 4 + [f"{LONG}4"])  # the new message counts as a turn: only the last one before it stays
        self.assertEqual(outputs(agent.chat_ctx)[:3], [MARKER] * 3)  # the stored history, without the new message

    async def test_a_short_history_is_left_alone(self):
        agent = assistant_core.ChatAgent(instructions="x")
        agent._chat_ctx = history(2)
        turn = agent.chat_ctx.copy()
        before = outputs(turn)
        await agent.on_user_turn_completed(turn, turn.items[-1])
        self.assertEqual(outputs(turn), before)

    async def test_the_email_tool_cuts_a_big_result(self):
        class Mail:
            def configured(self):
                return True

            def list_recent(self, *_):
                return {"mensagens": ["y" * 500] * 40}

        tools = assistant_core.email_tools(Mail(), None)
        listar = next(t for t in tools if t.info.name == "listar_emails")
        result = await listar(limite=5, desde="")
        self.assertEqual(result["aviso"], CUT_NOTE)


if __name__ == "__main__":
    unittest.main()
