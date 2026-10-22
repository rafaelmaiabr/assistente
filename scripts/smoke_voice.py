"""Real local round trip using a synthesized fixture, not a user's recording."""
import asyncio
import base64
import io
import sys
import wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from aiohttp import ClientSession
from local_audio import windows_wav
from speech_service import encode_wav


async def main():
    pcm, rate, _ = await asyncio.to_thread(windows_wav, 'Verifique a caixa de email X e resuma as mensagens.')
    samples = np.frombuffer(pcm, dtype=np.int16)
    resampled = np.interp(np.arange(0, len(samples), rate/16000), np.arange(len(samples)), samples).astype('<i2')
    origin = 'http://127.0.0.1:8765'
    async with ClientSession() as client, client.ws_connect(origin + '/ws', origin=origin) as ws:
        async def receive_turn():
            events=[]
            async with asyncio.timeout(180):
                while True:
                    event=await ws.receive_json(); events.append(event)
                    if event['type'] in ('error','rejected','warning'): raise AssertionError(event)
                    if event.get('state')=='ready': return events
        await receive_turn()
        await ws.send_json({'type':'audio', 'wav': encode_wav(resampled.tobytes(),16000,1), 'speak':True})
        events=await receive_turn()
        assert any(e.get('role')=='user' and 'email' in e['text'].lower().replace('-', '') for e in events), events
        assert any(e.get('type')=='tool' and e.get('state')=='done' for e in events), events
        audio=next(e['wav'] for e in events if e['type']=='audio')
        with wave.open(io.BytesIO(base64.b64decode(audio)), 'rb') as wav:
            assert wav.getnframes()>1000
            print(f'PASS: WAV -> Whisper -> Ollama -> MCP -> texto + audio ({wav.getnframes()/wav.getframerate():.1f}s).')

if __name__=='__main__': asyncio.run(main())
