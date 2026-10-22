"""Small Windows-only local adapters for the validation prototype."""
import asyncio
import io
import tempfile
import uuid
import wave
from pathlib import Path

from livekit import rtc
from livekit.agents import stt, tts, DEFAULT_API_CONNECT_OPTIONS
from faster_whisper import WhisperModel
from settings import DATA_ROOT


class LocalSTT(stt.STT):
    def __init__(self, model="base", language="pt"):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._whisper = WhisperModel(model, device="cpu", compute_type="int8", download_root=str(DATA_ROOT / ".models"))
        self.language = language

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        data = rtc.combine_audio_frames(buffer).to_wav_bytes()
        def transcribe():
            segments, _ = self._whisper.transcribe(io.BytesIO(data), language=self.language, beam_size=1)
            return " ".join(segment.text.strip() for segment in segments)
        text = await asyncio.to_thread(transcribe)
        return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                               alternatives=[stt.SpeechData(language=self.language, text=text)])


def windows_wav(text, speed=1.0):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    path = Path(tempfile.gettempdir()) / f"assistente-{uuid.uuid4().hex}.wav"
    stream = None
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        voice.Rate = max(-10, min(10, round((speed - 1) * 10)))
        for token in voice.GetVoices():
            if "416" in token.GetAttribute("Language").lower():
                voice.Voice = token
                break
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Format.Type = 22  # 22.05 kHz, 16 bit, mono
        stream.Open(str(path), 3)
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()
        stream = None
        with wave.open(str(path), "rb") as wav:
            return wav.readframes(wav.getnframes()), wav.getframerate(), wav.getnchannels()
    finally:
        if stream is not None:
            stream.Close()
        path.unlink(missing_ok=True)
        pythoncom.CoUninitialize()


class LocalTTS(tts.TTS):
    def __init__(self):
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=22050, num_channels=1)

    def synthesize(self, text, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return LocalStream(tts=self, input_text=text, conn_options=conn_options)


class LocalStream(tts.ChunkedStream):
    async def _run(self, output_emitter):
        data, rate, channels = await asyncio.to_thread(windows_wav, self.input_text)
        output_emitter.initialize(request_id=uuid.uuid4().hex, sample_rate=rate, num_channels=channels, mime_type="audio/pcm")
        output_emitter.push(data)
        output_emitter.flush()

