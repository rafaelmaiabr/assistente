import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

from speech_service import encode_wav
from team.app import TeamApp
from team.vault import MemoryVault
from team.web import TEAM
from web_server import ALLOWED_HOSTS, create_app

CONFIG = {"model": "teste", "ollama_url": "http://127.0.0.1:11435/v1", "max_concurrent_generations": 1}
PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")


class FakeSpeech:
    def __init__(self):
        self.spoken = []

    async def transcribe(self, data):
        return "fala transcrita"

    async def synthesize(self, text, profile_id=None):
        self.spoken.append((text, profile_id))
        return encode_wav(b"\0\0" * 1600, 16000, 1)


class FakeAgent:
    instances = []

    def __init__(self, config, emit):
        self.emit, self.gate, self.inputs = emit, asyncio.Event(), []
        FakeAgent.instances.append(self)

    async def start(self):
        pass

    async def reply(self, text):
        self.inputs.append(text)
        await self.gate.wait()
        self.emit({"type": "message", "role": "assistant", "id": f"resp-{len(self.inputs)}", "text": f"resposta a {text}"})

    async def close(self):
        pass


class ConversationCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeAgent.instances = []
        self.sockets = []
        self.folder = tempfile.TemporaryDirectory()
        self.speech = FakeSpeech()
        team_app = TeamApp(Path(self.folder.name), CONFIG, vault=MemoryVault(), legacy_elevenlabs=None, voice_profiles=lambda: [{"id": "voz-suporte"}, {"id": "voz-financeiro"}])
        self.app = create_app(FakeAgent, CONFIG, self.speech, data_root=Path(self.folder.name), team_app=team_app)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.host = f"127.0.0.1:{self.runner.addresses[0][1]}"
        self.base = f"http://{self.host}"
        ALLOWED_HOSTS.add(self.host)
        self.client = ClientSession()
        async with self.client.post(self.base + "/api/persistence/enable", json={}, headers={"Origin": self.base}):
            pass
        s = self.s = self.app[TEAM].services()
        self.a = s.team.create_company(s.operator, "Empresa A")
        self.b = s.team.create_company(s.operator, "Empresa B")
        self.ca = s.access.tenant_context(s.operator, self.a["id"])
        self.dep = s.team.create(self.ca, "department", name="Atendimento", scope="x", status="ativo")
        self.suporte = s.agents.create_agent(self.ca, department_id=self.dep["id"], name="Suporte", voice_profile_ref="voz-suporte", **PROFILE)
        self.financeiro = s.agents.create_agent(self.ca, department_id=self.dep["id"], name="Financeiro", voice_profile_ref="voz-financeiro", **PROFILE)
        for agent, other in ((self.suporte, self.financeiro), (self.financeiro, self.suporte)):
            current = s.agents.get_agent(self.ca, agent["id"])
            s.agents.update_agent(self.ca, agent["id"], current["revision"], handoff_target_ids=[other["id"]])
            s.team.create(self.ca, "access_grant", principal_id=agent["id"], resource_kind="case", resource_id=None,
                          actions=["read", "write"], granted_by=s.operator.principal_id, expires_at=None)
        case = s.cases.open_case(self.ca, department_id=self.dep["id"], objective="Site fora do ar", customer_ref="c1")
        case = s.cases.transition(self.ca, case["id"], case["revision"], "triagem")
        case = s.cases.assign_owner(self.ca, case["id"], case["revision"], self.suporte["id"])
        self.case = s.cases.transition(self.ca, case["id"], case["revision"], "em_execucao")

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.client.close()
        await self.runner.cleanup()
        ALLOWED_HOSTS.discard(self.host)
        self.folder.cleanup()

    async def connect(self):
        ws = await self.client.ws_connect(self.base + "/ws", origin=self.base)
        self.sockets.append(ws)
        await self.until(ws, lambda e: e.get("state") == "ready")
        return ws

    async def until(self, ws, predicate, collect=None):
        async with asyncio.timeout(3):
            while True:
                event = await ws.receive_json()
                if collect is not None:
                    collect.append(event)
                if predicate(event):
                    return event

    async def attach(self, ws, case=None, company=None):
        await ws.send_json({"type": "attach", "company_id": (company or self.a)["id"], "case_id": (case or self.case)["id"]})
        return await self.until(ws, lambda e: e["type"] in ("binding", "rejected"))

    async def turn(self, ws, text="oi", speak=False):
        """Send a message, let the agent answer, and return every event of the turn."""
        events = []
        await ws.send_json({"type": "message", "text": text, "speak": speak})
        while not FakeAgent.instances[-1].gate.is_set() and False:
            await asyncio.sleep(0)
        FakeAgent.instances[-1].gate.set()
        await self.until(ws, lambda e: e.get("state") == "ready" and e.get("turn"), events)
        FakeAgent.instances[-1].gate.clear()
        return events


class BindingTests(ConversationCase):
    async def test_the_plain_assistant_announces_no_binding_and_tags_turns(self):
        ws = await self.connect()
        events = await self.turn(ws)
        self.assertEqual([e for e in events if e["type"] == "binding"], [])
        self.assertTrue(all(e["turn"] == 1 for e in events if e["type"] in ("status", "message")))
        self.assertEqual(next(e for e in events if e["type"] == "message" and e["role"] == "assistant")["binding"]["mode"], "local")
        self.assertEqual((await self.turn(ws))[-1]["turn"], 2)

    async def test_attaching_a_case_reports_company_case_and_agent_from_the_database(self):
        ws = await self.connect()
        event = await self.attach(ws)
        binding = event["binding"]
        self.assertEqual((event["reason"], binding["mode"], binding["company"]["name"], binding["case"]["id"]), ("vinculo", "equipe", "Empresa A", self.case["id"]))
        self.assertEqual((binding["agent"]["name"], binding["agent"]["voice_profile_ref"], binding["waiting_human"], binding["paused"]),
                         ("Suporte", "voz-suporte", False, False))
        events = await self.turn(ws, speak=True)
        message = next(e for e in events if e["type"] == "message" and e["role"] == "assistant")
        self.assertEqual(message["binding"], {"mode": "equipe", "company_id": self.a["id"], "case_id": self.case["id"],
                                              "agent_id": self.suporte["id"],
                                              "agent_version": self.s.agents.get_agent(self.ca, self.suporte["id"])["agent_version"]})
        self.assertEqual(self.speech.spoken[-1][1], "voz-suporte")  # the answer is spoken in the owner's voice
        self.assertEqual([e for e in events if e["type"] == "binding"], [])  # nothing changed, nothing re-announced

    async def test_a_handoff_changes_the_partner_and_the_voice_only_after_it_is_accepted(self):
        ws = await self.connect()
        await self.attach(ws)
        await self.turn(ws, speak=True)
        s = self.s
        sup = s.access.agent_context(s.operator, self.a["id"], s.agents.start_run(self.ca, self.suporte["id"], budget={"max_steps": 2, "token_budget": 100, "deadline_seconds": 60})["id"])
        proposal, _ = s.handoffs.propose_transfer(sup, self.case["id"], self.financeiro["id"], reason="Cobranca", summary="Resumo")
        events = await self.turn(ws, speak=True)  # proposed, not accepted: still Suporte, same voice
        self.assertEqual([e for e in events if e["type"] == "binding"], [])
        self.assertEqual(self.speech.spoken[-1][1], "voz-suporte")
        fin = s.access.agent_context(s.operator, self.a["id"], s.agents.start_run(self.ca, self.financeiro["id"], budget={"max_steps": 2, "token_budget": 100, "deadline_seconds": 60})["id"])
        s.handoffs.accept(fin, proposal["id"])
        events = await self.turn(ws, speak=True)
        announced = [e for e in events if e["type"] == "binding"]
        self.assertEqual([(e["reason"], e["binding"]["agent"]["name"]) for e in announced], [("mudou", "Financeiro")])
        self.assertLess(events.index(announced[0]), next(i for i, e in enumerate(events) if e["type"] == "message"))  # before the turn's first message
        self.assertEqual(self.speech.spoken[-1][1], "voz-financeiro")

    async def test_the_binding_is_taken_when_the_turn_starts_and_cannot_change_during_it(self):
        ws = await self.connect()
        await self.attach(ws)
        await ws.send_json({"type": "message", "text": "uma pergunta", "speak": False})
        await self.until(ws, lambda e: e.get("state") == "processing")
        await ws.send_json({"type": "detach"})  # a concurrent click during the turn
        rejected = await self.until(ws, lambda e: e["type"] == "rejected")
        self.assertTrue(rejected["busy"])
        await ws.send_json({"type": "attach", "company_id": self.a["id"], "case_id": self.case["id"]})
        self.assertTrue((await self.until(ws, lambda e: e["type"] == "rejected"))["busy"])
        FakeAgent.instances[-1].gate.set()
        done = []
        await self.until(ws, lambda e: e.get("state") == "ready" and e.get("turn"), done)
        message = next(e for e in done if e["type"] == "message" and e["role"] == "assistant")
        self.assertEqual(message["binding"]["case_id"], self.case["id"])  # the whole turn stayed with the original case

    async def test_unknown_foreign_or_unavailable_cases_are_refused(self):
        ws = await self.connect()
        for company, case in ((self.a["id"], "cas_" + "0" * 32), (self.b["id"], self.case["id"]), ("ten_" + "0" * 32, self.case["id"]), (1, 2), (None, None)):
            await ws.send_json({"type": "attach", "company_id": company, "case_id": case})
            self.assertEqual((await self.until(ws, lambda e: e["type"] in ("binding", "rejected")))["type"], "rejected", (company, case))
        events = await self.turn(ws)
        self.assertEqual([e for e in events if e["type"] == "binding"], [])  # still the plain assistant
        await self.attach(ws)
        await ws.send_json({"type": "detach"})
        self.assertEqual((await self.until(ws, lambda e: e["type"] == "binding"))["binding"]["mode"], "local")

    async def test_waiting_for_a_person_and_paused_cases_are_visible_in_the_binding(self):
        ws = await self.connect()
        self.s.approvals.request(self.ca, self.case["id"], "enviar_email", resource="con_x", recipients=["a@b.com"], amount_cents=1, currency="brl")
        event = await self.attach(ws)
        self.assertEqual((event["binding"]["waiting_human"], event["binding"]["case"]["phase"]), (True, "aguardando_humano"))
        case = self.s.cases.get_case(self.ca, self.case["id"])
        self.s.control.pause(self.ca, case["id"], case["revision"], "revisao")
        events = await self.turn(ws)
        changed = [e for e in events if e["type"] == "binding"]
        self.assertEqual(changed, [])  # same partner: the phase is refreshed by the client from the case, not re-announced as a new partner


class InterruptionTests(ConversationCase):
    async def test_cancel_interrupts_generation_without_touching_audio_or_the_microphone(self):
        ws = await self.connect()
        await ws.send_json({"type": "message", "text": "demorada", "speak": True})
        await self.until(ws, lambda e: e.get("state") == "processing")
        await ws.send_json({"type": "cancel"})
        seen = []
        await self.until(ws, lambda e: e.get("state") == "ready", seen)
        self.assertIn("cancelled", [e["type"] for e in seen])
        FakeAgent.instances[-1].gate.set()  # the agent would answer now: nothing may arrive
        await asyncio.sleep(0.2)
        self.assertEqual(self.speech.spoken, [])
        events = await self.turn(ws, "depois")  # the session is usable again
        self.assertEqual(next(e for e in events if e["type"] == "message" and e["role"] == "assistant")["text"], "resposta a depois")
        await ws.send_json({"type": "cancel"})
        self.assertEqual((await self.until(ws, lambda e: e["type"] == "rejected"))["message"], "Nao ha resposta em andamento.")

    async def test_reconnecting_starts_clean_and_must_attach_again(self):
        ws = await self.connect()
        await self.attach(ws)
        await ws.close()
        again = await self.connect()
        events = await self.turn(again)
        self.assertEqual(next(e for e in events if e["type"] == "message" and e["role"] == "assistant")["binding"]["mode"], "local")
        self.assertEqual((await self.attach(again))["binding"]["agent"]["name"], "Suporte")


if __name__ == "__main__":
    unittest.main()
