import asyncio
import json
import time
from pathlib import Path
from livekit.agents import AgentSession
from agent import make_agent
from local_audio import LocalSTT, LocalTTS, windows_wav
from livekit import rtc

async def main():
    pcm, rate, channels = await asyncio.to_thread(windows_wav, "Ola. Este e um teste do assistente local.")
    print(f"Voz Windows: {len(pcm)} bytes, {rate} Hz, {channels} canal", flush=True)
    recognizer = LocalSTT()
    result = await recognizer.recognize(rtc.AudioFrame(pcm, rate, channels, len(pcm)//2//channels))
    print("Transcricao:", result.alternatives[0].text, flush=True)
    synthesizer = LocalTTS()
    frames = 0
    async with synthesizer.synthesize("Teste de audio.") as stream:
        async for event in stream:
            frames += 1
    print("Frames TTS LiveKit:", frames, flush=True)
    session = AgentSession()
    try:
        await session.start(agent=make_agent())
        started = time.perf_counter()
        await session.generate_reply(user_input="Verifique a caixa de email X usando a ferramenta e resuma.")
        history = session.history.to_dict()
        Path("validacao.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(history, ensure_ascii=False), flush=True)
        print(f"Tempo do turno com MCP: {time.perf_counter()-started:.1f}s", flush=True)
    finally:
        await session.aclose()

asyncio.run(main())
