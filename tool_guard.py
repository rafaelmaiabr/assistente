"""One shape for tool errors and a stop for calls that keep failing (#134).

A small model that gets "An internal error occurred" retries blindly. Every failure goes back in the same shape: what failed, whether
trying again can help, and what to tell the person. The text is scrubbed first: no address, local path or credential-like string.
"""
import json
import re
import time

REPEATS = 2  # an identical call that failed this many times is not run a third time
WINDOW = 120.0  # seconds; an old failure does not block the person's later, deliberate retry
LIMIT_TEXT = "Cheguei ao limite de passos desta resposta."

_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+", re.I)
_PATH = re.compile(r"\b[A-Za-z]:[\\/]\S*|(?<![\w.])(?:~|\.{1,2})?/(?:[\w.-]+/)+[\w.-]*|\\\\\S+")
_SECRET = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def scrub(text, limit=300):
    """The message without addresses, local paths or long token-like strings."""
    text = _SECRET.sub("[omitido]", _PATH.sub("[caminho]", _URL.sub("[endereco]", str(text))))
    return text[:limit]


def std_error(what, retry=False, say=None):
    """The error as the model receives it. ``say`` is what to tell the person (defaults to the scrubbed ``what``)."""
    what = scrub(what)
    return {"erro": what, "pode_tentar_de_novo": bool(retry),
            "diga_a_pessoa": scrub(say) if say else what, "dados_nao_confiaveis": False}


class Guard:
    """Normalises tool failures and blocks an identical call that keeps failing. One per set of tools."""

    def __init__(self, clock=time.monotonic, repeats=REPEATS, window=WINDOW):
        self.clock, self.repeats, self.window = clock, repeats, window
        self.failures = {}  # (tool, arguments) -> [times of the failures]

    @staticmethod
    def _key(name, args):
        try:
            return name, json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return name, repr(args)

    def _recent(self, key):
        now = self.clock()
        times = [t for t in self.failures.get(key, []) if now - t < self.window]
        self.failures[key] = times
        return times

    async def call(self, name, args, run):
        """Awaits ``run()`` and returns its result; an exception or an ``erro`` result becomes the standard error."""
        key = self._key(name, args)
        if len(self._recent(key)) >= self.repeats:
            return std_error("Esta mesma chamada já falhou mais de uma vez e não foi repetida.", retry=False,
                             say="Não consegui concluir isso; tentar o mesmo pedido de novo não ajudou. Peça outra coisa ou tente mais tarde.")
        try:
            result = await run()
        except Exception:  # the cause is logged by the caller's own layers; the model gets no internals
            result = std_error("A ferramenta falhou por um motivo interno.", retry=True,
                               say="Tive um problema ao usar essa ferramenta agora.")
        if isinstance(result, dict) and "erro" in result:
            if "pode_tentar_de_novo" not in result:
                result = {**std_error(result["erro"], retry=True), **{k: scrub(v) if isinstance(v, str) else v for k, v in result.items() if k != "erro"}}
            self.failures[key].append(self.clock())
        else:
            self.failures.pop(key, None)
        return result


def limit_summary(steps):
    """What to say when the step budget ended without an answer. ``steps`` is a list of ``(tool, ok)`` in the order they ran."""
    done = [name for name, ok in steps if ok]
    failed = [name for name, ok in steps if not ok]
    parts = [LIMIT_TEXT]
    parts.append("Consultei: " + ", ".join(dict.fromkeys(done)) + "." if done else "Não consegui obter nenhum resultado.")
    if failed:
        parts.append("Falharam: " + ", ".join(dict.fromkeys(failed)) + ".")
    parts.append("Posso continuar se você pedir um recorte menor ou disser o que falta.")
    return " ".join(parts)
