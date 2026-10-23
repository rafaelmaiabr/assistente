"""Optional local check of the real model server used by the agent runtime.

    .venv/Scripts/python.exe scripts/smoke_ollama.py

Sends one short prompt to the Ollama endpoint from config.json (or config.example.json), prints the
model, the elapsed time and the reply length. It needs Ollama running with the configured model and
never sends data anywhere else. Exit code 0 on success, 2 when Ollama is not reachable.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from settings import load_config  # noqa: E402
from team.llm import LlmError, OpenAICompatClient  # noqa: E402


async def main() -> int:
    config = load_config()
    client = OpenAICompatClient(config["ollama_url"], timeout=120)
    started = time.monotonic()
    try:
        reply = await client.complete(
            [{"role": "user", "content": "Responda apenas com a palavra OK."}], [], model=config["model"], max_tokens=16)
    except LlmError as error:
        print(f"Ollama indisponivel em {config['ollama_url']} ({error.code}). Nada foi validado.")
        return 2
    print(f"OK: modelo {config['model']} respondeu em {time.monotonic() - started:.1f}s "
          f"({len(reply.text)} caracteres, uso={reply.usage}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
