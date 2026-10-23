"""Secret storage behind a small interface. Secret values never enter documents, logs or errors.

``WindowsVault`` uses the Windows Credential Manager (same mechanism as the ElevenLabs key);
``MemoryVault`` is the fake used by automated tests. Entries are keyed by (tenant, credential id),
so one company can never read another's secret and nothing is exposed through global variables.
"""
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path

MAX_SECRET = 4096


class VaultError(Exception):
    """Sanitized failure: the message never contains the secret or the platform error text."""


def clean_secret(secret) -> str:
    if not isinstance(secret, str) or not 1 <= len(secret) <= MAX_SECRET \
            or any(ord(char) < 32 or ord(char) == 127 for char in secret) or secret != secret.strip():
        raise VaultError("Segredo invalido: use 1 a 4096 caracteres, sem quebras de linha e sem espacos nas pontas.")
    return secret


class SecretVault(ABC):
    @abstractmethod
    def put(self, tenant_id, credential_id, secret): ...

    @abstractmethod
    def get(self, tenant_id, credential_id): ...

    @abstractmethod
    def delete(self, tenant_id, credential_id): ...


class MemoryVault(SecretVault):
    """In-process fake for tests; it can simulate an unavailable vault."""

    def __init__(self):
        self.items, self.broken = {}, False

    def _check(self):
        if self.broken:
            raise VaultError("Cofre de credenciais indisponivel.")

    def put(self, tenant_id, credential_id, secret):
        self._check()
        self.items[(tenant_id, credential_id)] = clean_secret(secret)

    def get(self, tenant_id, credential_id):
        self._check()
        return self.items.get((tenant_id, credential_id))

    def delete(self, tenant_id, credential_id):
        self._check()
        self.items.pop((tenant_id, credential_id), None)


class WindowsVault(SecretVault):
    """Windows Credential Manager entries named ``Assistente/<install scope>/team/<tenant>/<credential>``."""

    def __init__(self, root):
        self.scope = hashlib.sha256(str(Path(root).resolve()).lower().encode()).hexdigest()[:16]

    def _target(self, tenant_id, credential_id):
        return f"Assistente/{self.scope}/team/{tenant_id}/{credential_id}"

    def put(self, tenant_id, credential_id, secret):
        value = clean_secret(secret)
        try:
            import win32cred
            win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": self._target(tenant_id, credential_id),
                                 "UserName": "assistente", "CredentialBlob": value,
                                 "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)
        except Exception:
            raise VaultError("Nao foi possivel salvar no cofre do Windows. Nenhuma copia em texto foi criada.") from None

    def get(self, tenant_id, credential_id):
        try:
            import win32cred
            blob = win32cred.CredRead(self._target(tenant_id, credential_id), win32cred.CRED_TYPE_GENERIC)["CredentialBlob"]
            return blob.decode("utf-16-le") if isinstance(blob, bytes) else blob
        except Exception as error:
            if getattr(error, "winerror", None) == 1168:  # ERROR_NOT_FOUND
                return None
            raise VaultError("Nao foi possivel acessar o cofre de credenciais do Windows.") from None

    def delete(self, tenant_id, credential_id):
        try:
            import win32cred
            win32cred.CredDelete(self._target(tenant_id, credential_id), win32cred.CRED_TYPE_GENERIC, 0)
        except Exception as error:
            if getattr(error, "winerror", None) != 1168:
                raise VaultError("Nao foi possivel remover a credencial do cofre do Windows.") from None


class LegacyElevenLabsAdapter:
    """Read-only view of the existing ElevenLabs entry, so voice profiles keep working unchanged."""

    def __init__(self, secrets):
        self.secrets = secrets  # voice_profiles.WindowsSecrets (or a fake with get())

    def get(self):
        try:
            return self.secrets.get()
        except Exception:
            raise VaultError("Nao foi possivel acessar o cofre de credenciais do Windows.") from None
