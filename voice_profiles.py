"""Voice preferences contain no secrets; credentials live in the Windows vault."""
import copy
import hashlib
import json
import math
import re
import threading
from pathlib import Path
from settings import DATA_ROOT


def defaults():
    return {'active': 'windows', 'profiles': [
        {'id': 'windows', 'name': 'Windows · português', 'provider': 'windows', 'voice': '', 'speed': 1.0},
        *[{'id': voice, 'name': name, 'provider': 'kokoro', 'voice': voice, 'speed': 1.0}
          for voice, name in [('pf_dora', 'Dora · português'), ('pm_alex', 'Alex · português'), ('pm_santa', 'Santa · português')]],
        {'id': 'elevenlabs', 'name': 'ElevenLabs · configurar', 'provider': 'elevenlabs', 'voice': '', 'speed': 1.0,
         'model': 'eleven_multilingual_v2', 'stability': 0.5, 'similarity': 0.75},
    ]}


def validate_settings(data):
    if not isinstance(data, dict) or set(data) != {'active', 'profiles'}:
        raise ValueError('Configuração de voz inválida.')
    profiles = data['profiles']
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 20:
        raise ValueError('Use entre 1 e 20 perfis de voz.')
    ids = set()
    for p in profiles:
        if not isinstance(p, dict) or p.get('provider') not in {'windows', 'kokoro', 'elevenlabs'}:
            raise ValueError('Provedor de voz inválido.')
        required = {'id', 'name', 'provider', 'voice', 'speed'}
        allowed = required | ({'model', 'stability', 'similarity'} if p['provider'] == 'elevenlabs' else set())
        if set(p) != allowed:
            raise ValueError('Campos de perfil inválidos. Chaves não pertencem à configuração.')
        if not isinstance(p['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', p['id']) or p['id'] in ids:
            raise ValueError('Identificador de perfil inválido ou repetido.')
        ids.add(p['id'])
        if not isinstance(p['name'], str) or not 1 <= len(p['name'].strip()) <= 80:
            raise ValueError('Nome do perfil deve ter entre 1 e 80 caracteres.')
        if not isinstance(p['voice'], str) or len(p['voice']) > 200:
            raise ValueError('Identificador de voz inválido.')
        if p['provider'] == 'kokoro' and p['voice'] not in {'pf_dora', 'pm_alex', 'pm_santa'}:
            raise ValueError('Escolha uma voz Kokoro em português.')
        if p['provider'] == 'windows' and p['voice']:
            raise ValueError('Windows usa a voz em português instalada no sistema.')
        if p['provider'] == 'elevenlabs':
            if p['voice'] and not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', p['voice']):
                raise ValueError('Voice ID ElevenLabs inválido.')
            if p['model'] not in {'eleven_multilingual_v2', 'eleven_flash_v2_5', 'eleven_turbo_v2_5'}:
                raise ValueError('Modelo ElevenLabs inválido.')
            for key in ['stability', 'similarity']:
                number(p[key], 0, 1)
            number(p['speed'], 0.7, 1.2)
        else:
            number(p['speed'], 0.5, 2)
    if not isinstance(data['active'], str) or data['active'] not in ids:
        raise ValueError('Selecione um perfil existente.')
    return copy.deepcopy(data)


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Valor numérico deve estar entre {low} e {high}.')


class WindowsSecrets:
    def __init__(self, root=DATA_ROOT):
        scope = hashlib.sha256(str(Path(root).resolve()).lower().encode()).hexdigest()[:16]
        self.target = f'Assistente/{scope}/elevenlabs'

    def get(self):
        try:
            import win32cred
            credential = win32cred.CredRead(self.target, win32cred.CRED_TYPE_GENERIC)
            blob = credential['CredentialBlob']
            return blob.decode('utf-16-le') if isinstance(blob, bytes) else blob
        except ImportError:
            return None
        except Exception as error:
            if getattr(error, 'winerror', None) == 1168:
                return None
            raise ValueError('Não foi possível acessar o cofre de credenciais do Windows.') from None

    def set(self, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 1000 or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError('Chave inválida. Cole apenas a chave de API.')
        try:
            import win32cred
            win32cred.CredWrite({'Type': win32cred.CRED_TYPE_GENERIC, 'TargetName': self.target,
                                'UserName': 'elevenlabs', 'CredentialBlob': key,
                                'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)
        except Exception:
            raise ValueError('Não foi possível salvar no cofre do Windows. Nenhuma cópia em texto foi criada.') from None

    def delete(self):
        try:
            import win32cred
            win32cred.CredDelete(self.target, win32cred.CRED_TYPE_GENERIC, 0)
        except Exception as error:
            if getattr(error, 'winerror', None) != 1168:
                raise ValueError('Não foi possível remover a chave do cofre do Windows.') from None


class VoiceProfiles:
    def __init__(self, root=DATA_ROOT, secrets=None):
        self.path = Path(root) / 'voice-settings.json'
        self.secrets = secrets if secrets is not None else WindowsSecrets(root)
        self.lock = threading.RLock()
        self.data = defaults()
        if self.path.exists():
            self.data = validate_settings(json.loads(self.path.read_text(encoding='utf-8')))

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def save(self, data):
        clean = validate_settings(data)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.path)
            self.data = clean

    def profile(self, profile_id=None):
        data = self.snapshot()
        for p in data['profiles']:
            if p['id'] == (profile_id or data['active']):
                return p
        raise ValueError('Perfil de voz não encontrado.')
