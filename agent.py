import asyncio
from livekit.agents import AgentServer, AgentSession, JobContext, cli
from livekit.plugins import silero
from local_audio import LocalSTT, LocalTTS
from settings import load_config
from assistant_core import make_agent

CONFIG = load_config()

server = AgentServer()

@server.rtc_session()
async def entrypoint(ctx: JobContext):
    recognizer = await asyncio.to_thread(LocalSTT, CONFIG["whisper_model"], CONFIG["language"])
    session = AgentSession(stt=recognizer, tts=LocalTTS(), vad=silero.VAD.load(),
                           turn_handling={"turn_detection": "vad", "interruption": {"mode": "vad"}})
    await session.start(agent=make_agent(), room=ctx.room)

if __name__ == "__main__":
    cli.run_app(server)
