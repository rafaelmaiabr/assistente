"""Bounded local audio conversion. No browser speech service or cloud API."""
import asyncio
import base64
import binascii
import io
import threading
import wave
from settings import ROOT

MAX_WAV_BYTES = 960044


def decode_audio(encoded):
    if not isinstance(encoded, str) or len(encoded) > 1280060:
        raise ValueError('Áudio excede o limite de 30 segundos.')
    try:
        data = base64.b64decode(encoded, validate=True)
        if len(data) > MAX_WAV_BYTES:
            raise ValueError('Áudio muito grande.')
        with wave.open(io.BytesIO(data), 'rb') as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise ValueError('Use áudio PCM mono de 16 kHz.')
            frames = wav.getnframes()
            if not 1600 <= frames <= 480000 or len(wav.readframes(frames)) != frames * 2:
                raise ValueError('Áudio vazio, incompleto ou longo demais.')
    except (binascii.Error, wave.Error, EOFError) as error:
        raise ValueError('Áudio inválido. Tente gravar novamente.') from error
    return data


def encode_wav(pcm, rate, channels):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return base64.b64encode(output.getvalue()).decode('ascii')


class SpeechService:
    def __init__(self, config):
        self.config = config
        self.model = None
        # Lock remains held even if the awaiting coroutine is cancelled.
        self.lock = threading.Lock()

    def _transcribe(self, data):
        from faster_whisper import WhisperModel
        with self.lock:
            if self.model is None:
                self.model = WhisperModel(self.config['whisper_model'], device='cpu', compute_type='int8', download_root=str(ROOT / '.models'))
            segments, _ = self.model.transcribe(io.BytesIO(data), language=self.config['language'], beam_size=1, vad_filter=True)
            return ' '.join(part.text.strip() for part in segments).strip()

    async def transcribe(self, data):
        return await asyncio.to_thread(self._transcribe, data)

    async def synthesize(self, text):
        if len(text) > 4000:
            raise ValueError('Resposta longa demais para síntese nesta versão.')
        from local_audio import windows_wav
        pcm, rate, channels = await asyncio.to_thread(windows_wav, text)
        return encode_wav(pcm, rate, channels)
