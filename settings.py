"""Portable configuration; local settings never belong in Git."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get('ASSISTENTE_DATA_DIR', str(ROOT))).resolve()


def load_config(root: Path = ROOT) -> dict:
    defaults = json.loads((root / "config.example.json").read_text(encoding="utf-8-sig"))
    local = (DATA_ROOT if root == ROOT else root) / "config.json"
    if local.exists():
        try:
            override = json.loads(local.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError(f"config.json invalido: linha {error.lineno}, coluna {error.colno}") from error
        if not isinstance(override, dict):
            raise ValueError("config.json deve conter um objeto JSON")
        defaults.update(override)
    for key in ("ollama_url", "model", "whisper_model", "language"):
        if not isinstance(defaults.get(key), str) or not defaults[key].strip():
            raise ValueError(f"Configuracao '{key}' deve ser um texto nao vazio")
    if urlparse(defaults["ollama_url"]).scheme not in ("http", "https"):
        raise ValueError("ollama_url deve usar http ou https")
    limit = defaults.get("max_concurrent_generations")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 8:
        raise ValueError("max_concurrent_generations deve ser um inteiro entre 1 e 8")
    servers = defaults.get("mcp_servers")
    if not isinstance(servers, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("url"), str)
        or urlparse(item["url"]).scheme not in ("http", "https") for item in servers
    ):
        raise ValueError("mcp_servers deve ser uma lista de objetos com url HTTP")
    return defaults
