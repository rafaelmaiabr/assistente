"""Optional local Kokoro and explicit cloud synthesis. Never log provider errors."""
import asyncio
import hashlib
import threading
import urllib.request
from pathlib import Path
from aiohttp import ClientSession, ClientTimeout
from settings import DATA_ROOT

ASSETS = {
    'kokoro-v1.0.onnx': ('beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a', 325505369),
    'voices-v1.0.bin': ('bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d', 28214398),
}
RELEASE = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/'


class VoiceEngine:
    def __init__(self, profiles, root=DATA_ROOT):
        self.profiles = profiles
        self.folder = Path(root) / '.models/kokoro'
        self.model = None
        self.lock = threading.Lock()

    def ready(self):
        return all((self.folder / name).is_file() and (self.folder / name).stat().st_size == size
                   for name, (_, size) in ASSETS.items())

    def prepare(self):
        # Downloads are explicit; no synthesis request downloads a model.
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            for name, (digest, size) in ASSETS.items():
                target = self.folder / name
                if target.exists() and target.stat().st_size == size and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                    continue
                partial = target.with_suffix(target.suffix + '.part')
                try:
                    checksum = hashlib.sha256()
                    total = 0
                    request = urllib.request.Request(RELEASE + name, headers={'User-Agent': 'Assistente-Local'})
                    with urllib.request.urlopen(request, timeout=120) as source, partial.open('wb') as output:
                        while chunk := source.read(1024 * 1024):
                            total += len(chunk)
                            if total > size:
                                raise ValueError('Arquivo Kokoro excede o tamanho esperado.')
                            checksum.update(chunk)
                            output.write(chunk)
                    if total != size or checksum.hexdigest() != digest:
                        raise ValueError('Falha na verificação dos arquivos Kokoro. Tente preparar novamente.')
                    partial.replace(target)
                finally:
                    partial.unlink(missing_ok=True)

    def local(self, text, p):
        with self.lock:
            if p['provider'] == 'windows':
                from local_audio import windows_wav
                return windows_wav(text, speed=p['speed'])
            if not self.ready():
                raise ValueError('Prepare o Kokoro nas configurações de voz antes de usar.')
            import numpy as np
            if self.model is None:
                from kokoro_onnx import Kokoro
                import onnxruntime as ort
                options = ort.SessionOptions()
                options.intra_op_num_threads = 4
                session = ort.InferenceSession(str(self.folder / 'kokoro-v1.0.onnx'), sess_options=options,
                                               providers=['CPUExecutionProvider'])
                self.model = Kokoro.from_session(session, str(self.folder / 'voices-v1.0.bin'))
            samples, rate = self.model.create(text, voice=p['voice'], speed=p['speed'], lang='pt-br')
            return (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes(), rate, 1

    async def synthesize(self, text, profile_id=None):
        p = self.profiles.profile(profile_id)
        if p['provider'] != 'elevenlabs':
            return await asyncio.to_thread(self.local, text, p)
        if not p['voice']:
            raise ValueError('Informe o Voice ID do ElevenLabs neste perfil.')
        key = await asyncio.to_thread(self.profiles.secrets.get)
        if not key:
            raise ValueError('Salve a chave ElevenLabs no cofre do Windows antes de usar.')
        # Fixed HTTPS origin, no redirects, no retries (which could duplicate charges).
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{p['voice']}?output_format=pcm_22050"
        payload = {'text': text, 'model_id': p['model'], 'voice_settings': {
            'stability': p['stability'], 'similarity_boost': p['similarity'], 'speed': p['speed']}}
        try:
            async with ClientSession(timeout=ClientTimeout(total=40)) as session:
                async with session.post(url, headers={'xi-api-key': key}, json=payload, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError('ElevenLabs recusou a síntese. Confira chave, Voice ID, modelo e créditos.')
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > 5292000:
                            raise ValueError('Áudio ElevenLabs excede dois minutos.')
                    if not data or len(data) % 2:
                        raise ValueError('ElevenLabs retornou áudio inválido.')
                    return bytes(data), 22050, 1
        except ValueError:
            raise
        except Exception:
            raise ValueError('Falha de conexão com ElevenLabs. Tente novamente quando houver conexão.') from None
