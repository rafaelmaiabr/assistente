"""Local store for outbound attachments. Only metadata lives in the message; bytes are checked again at send time.

Limits are deliberately small and the type list is an allowlist: documents and images, never executables, scripts,
archives or Office files with macros. A name is reduced to a safe file name (no path), the content hash is part of the
approved payload, and erasing a case removes its attachments.
"""
import hashlib
import os
import re
import shutil
from pathlib import Path

MAX_FILES = 3
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
ALLOWED = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".txt": "text/plain", ".csv": "text/csv"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_SIGNATURES = {".pdf": (b"%PDF-",), ".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",)}


class AttachmentRejected(ValueError):
    """The attachment is not allowed; the message is safe to show."""


def safe_name(name):
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    cleaned = _SAFE_NAME.sub("_", base).strip("._")[:100]
    if not cleaned:
        raise AttachmentRejected("Nome de arquivo invalido.")
    return cleaned


class AttachmentStore:
    def __init__(self, base_dir):
        self.root = Path(base_dir) / "attachments"

    def _folder(self, tenant_id, case_id):
        return self.root / tenant_id / case_id

    def validate(self, filename, data):
        """``(safe_name, content_type)`` for an allowed file, else ``AttachmentRejected``."""
        name = safe_name(filename)
        extension = Path(name).suffix.lower()
        if extension not in ALLOWED:
            raise AttachmentRejected("Tipo de arquivo nao permitido (aceitos: " + ", ".join(sorted(ALLOWED)) + ").")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise AttachmentRejected("Arquivo vazio.")
        if len(data) > MAX_FILE_BYTES:
            raise AttachmentRejected("Arquivo grande demais (limite de 2 MB).")
        signatures = _SIGNATURES.get(extension)
        if signatures and not any(bytes(data).startswith(s) for s in signatures):
            raise AttachmentRejected("O conteudo nao corresponde a extensao do arquivo.")
        if extension in (".txt", ".csv"):
            try:
                bytes(data).decode("utf-8")
            except UnicodeDecodeError:
                raise AttachmentRejected("Arquivo de texto precisa estar em UTF-8.") from None
        return name, ALLOWED[extension]

    def put_all(self, tenant_id, case_id, files):
        """Store several files and return their metadata (``filename``, ``content_type``, ``size``, ``sha256``)."""
        files = list(files or [])
        if len(files) > MAX_FILES:
            raise AttachmentRejected(f"No maximo {MAX_FILES} anexos.")
        prepared, total = [], 0
        for item in files:
            name, content_type = self.validate(item.get("filename"), item.get("data"))
            data = bytes(item["data"])
            total += len(data)
            prepared.append((name, content_type, data))
        if total > MAX_TOTAL_BYTES:
            raise AttachmentRejected("Anexos grandes demais (limite total de 5 MB).")
        folder = self._folder(tenant_id, case_id)
        folder.mkdir(parents=True, exist_ok=True)
        metadata = []
        for name, content_type, data in prepared:
            digest = hashlib.sha256(data).hexdigest()
            target = folder / digest
            if not target.exists():
                temp = target.with_suffix(".tmp")
                temp.write_bytes(data)
                try:
                    os.chmod(temp, 0o600)
                except OSError:
                    pass
                os.replace(temp, target)
            metadata.append({"filename": name, "content_type": content_type, "size": len(data), "sha256": digest})
        return metadata

    def read(self, tenant_id, case_id, metadata):
        """Bytes for a stored attachment, re-checked against the approved hash and size."""
        path = self._folder(tenant_id, case_id) / str(metadata.get("sha256", ""))
        try:
            data = path.read_bytes()
        except OSError:
            raise AttachmentRejected("Anexo nao encontrado no armazenamento local.") from None
        if hashlib.sha256(data).hexdigest() != metadata["sha256"] or len(data) != metadata["size"]:
            raise AttachmentRejected("O anexo foi alterado depois da aprovacao.")
        return data

    def delete_case(self, tenant_id, case_id):
        folder = self._folder(tenant_id, case_id)
        existed = folder.exists()
        shutil.rmtree(folder, ignore_errors=True)
        return existed
