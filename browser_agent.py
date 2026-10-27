"""Translate actual LiveKit events into the small browser protocol."""
import asyncio
from livekit.agents import AgentSession, APIConnectOptions
from livekit.agents.voice.agent_session import SessionConnectOptions
from assistant_core import SKILL_OF, agent_parts, make_agent


class BrowserAgent:
    def __init__(self, config, emit):
        self.config, self.emit = config, emit
        self.session = AgentSession(vad=None,
            conn_options=SessionConnectOptions(llm_conn_options=APIConnectOptions(timeout=90, max_retry=0)),
            turn_handling={"turn_detection": "manual", "interruption": {"enabled": False}})
        self.failed = False
        self.agent = None
        self.answers = 0
        self.session.on("conversation_item_added", self.on_message)
        self.session.on("tool_execution_updated", self.on_tool)
        self.session.on("error", self.on_error)

    def on_message(self, event):
        item = event.item
        if item.type == "message" and item.role == "assistant" and item.text_content:
            self.answers += 1
            self.emit({"type": "message", "role": "assistant", "id": item.id, "text": item.text_content})

    def on_tool(self, event):
        update = event.update
        if update.type == "tool_call_started":
            name = update.function_call.name
            self.emit({"type": "status", "state": "executing", "label": "Consultando e-mails de demonstração…" if name == "verificar_caixa_email" else "Consultando seus e-mails…" if name in ("listar_emails", "buscar_emails", "ler_email") else "Preparando o rascunho…" if name == "preparar_resposta" else "Executando ferramenta…"})
            self.emit({"type": "tool", "name": name, "state": "started"})
            if name in SKILL_OF:
                self.emit({"type": "skill", "id": SKILL_OF[name], "state": "usando"})
        elif update.type == "tool_call_ended":
            self.emit({"type": "tool", "state": update.status})
            self.emit({"type": "status", "state": "processing", "label": "Preparando resposta…"})

    def on_error(self, event):
        if not getattr(event.error, "recoverable", False):
            self.failed = True

    async def start(self):
        self.agent = make_agent(self.config, on_draft=lambda draft: self.emit({"type": "draft", "draft": draft}),
                                on_setup=lambda: self.emit({"type": "setup", "skill": "email"}))
        await self.session.start(agent=self.agent)

    async def refresh(self):
        """After a skill was set up from the form: new instructions and tools for the running conversation (history is kept)."""
        instructions, tools = agent_parts(self.config, on_draft=lambda draft: self.emit({"type": "draft", "draft": draft}),
                                          on_setup=lambda: self.emit({"type": "setup", "skill": "email"}))
        await self.agent.update_tools(tools)
        await self.agent.update_instructions(instructions)

    async def reply(self, text):
        self.failed = False
        before = self.answers
        await self.session.generate_reply(user_input=text)
        if self.failed or self.answers == before:
            raise RuntimeError("O modelo não retornou uma resposta. Verifique se o Ollama está ativo e o modelo está instalado.")

    async def close(self):
        await asyncio.wait_for(self.session.aclose(), timeout=15)
