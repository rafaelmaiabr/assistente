"""Translate actual LiveKit events into the small browser protocol."""
import asyncio
from livekit.agents import AgentSession, APIConnectOptions
from livekit.agents.voice.agent_session import SessionConnectOptions
from assistant_core import SKILL_OF, agent_parts, make_agent
from tool_guard import limit_summary


class BrowserAgent:
    def __init__(self, config, emit):
        self.config, self.emit = config, emit
        self.session = AgentSession(vad=None,
            conn_options=SessionConnectOptions(llm_conn_options=APIConnectOptions(timeout=90, max_retry=0)),
            turn_handling={"turn_detection": "manual", "interruption": {"enabled": False}})
        self.failed = False
        self.agent = None
        self.on_integration = None  # set by the server when the team services exist; the tool is absent otherwise
        self.mail_source = None  # async callable -> (mail, outbox) when the chat reads a company mailbox (#117); None keeps the personal one
        self.answers = 0
        self.steps = []  # (tool, ok) of the turn in progress, for the summary when the step budget ends without an answer
        self.running = None
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
            self.running = name
            self.emit({"type": "status", "state": "executing", "label": "Mostrando o cartão de integração…" if name == "solicitar_integracao" else "Consultando e-mails de demonstração…" if name == "verificar_caixa_email" else "Consultando seus e-mails…" if name in ("listar_emails", "buscar_emails", "ler_email") else "Consultando a rota liberada…" if name == "consultar_rota" else "Preparando o rascunho…" if name == "preparar_resposta" else "Executando ferramenta…"})
            self.emit({"type": "tool", "name": name, "state": "started"})
            if name in SKILL_OF:
                self.emit({"type": "skill", "id": SKILL_OF[name], "state": "usando"})
        elif update.type == "tool_call_ended":
            self.steps.append((self.running, update.status == "done"))
            self.emit({"type": "tool", "state": update.status})
            self.emit({"type": "status", "state": "processing", "label": "Preparando resposta…"})

    def on_error(self, event):
        if not getattr(event.error, "recoverable", False):
            self.failed = True

    async def _mail(self):
        return await self.mail_source() if self.mail_source else (None, None)

    async def start(self):
        mail, outbox = await self._mail()
        self.agent = make_agent(self.config, mail=mail, outbox=outbox, on_draft=lambda draft: self.emit({"type": "draft", "draft": draft}),
                                on_setup=lambda: self.emit({"type": "setup", "skill": "email"}), on_integration=self.on_integration)
        await self.session.start(agent=self.agent)

    async def refresh(self):
        """After a skill was set up from the form: new instructions and tools for the running conversation (history is kept)."""
        mail, outbox = await self._mail()
        instructions, tools = agent_parts(self.config, mail=mail, outbox=outbox, on_draft=lambda draft: self.emit({"type": "draft", "draft": draft}),
                                          on_setup=lambda: self.emit({"type": "setup", "skill": "email"}), on_integration=self.on_integration)
        await self.agent.update_tools(tools)
        await self.agent.update_instructions(instructions)

    async def reply(self, text):
        self.failed = False
        self.steps = []
        before = self.answers
        await self.session.generate_reply(user_input=text)
        if self.steps and not self.failed and self.answers == before:  # tools ran and the model said nothing: do not leave silence
            self.answers += 1
            self.emit({"type": "message", "role": "assistant", "id": f"limite-{self.answers}", "text": limit_summary(self.steps)})
            return
        if self.failed or self.answers == before:
            raise RuntimeError("O modelo não retornou uma resposta. Verifique se o Ollama está ativo e o modelo está instalado.")

    async def close(self):
        await asyncio.wait_for(self.session.aclose(), timeout=15)
