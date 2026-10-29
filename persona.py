"""Who the assistant is (tone), apart from what it can do, and the answer styles the person picks per conversation (#136).

The persona is a short local file written by the operator. It can only describe tone: anything that tries to change rules or
permissions, to reveal secrets, or to claim a capability is dropped as a whole and the built-in text is used. The drop is logged
without the text. The style presets only shorten the answers: they never touch the honesty block or what the tools allow.
"""
import logging
import re

from mail_intent import _plain
from team.triage import looks_like_injection

logger = logging.getLogger("persona")

PERSONA_FILE = "persona.txt"  # in the data folder, which is outside the repository when ASSISTENTE_DATA_DIR points elsewhere
MAX_CHARS = 600
DEFAULT = "Tom: direto e cordial; diga com franqueza quando não souber ou não tiver certeza. "

STYLES = {
    "padrao": "",
    "curto": ("Estilo desta conversa: respostas curtas, no máximo três frases, sem repetir a pergunta. Isso só encurta o texto: "
              "todos os avisos, limites e regras acima continuam valendo por inteiro. "),
}
DEFAULT_STYLE = "padrao"

# What a tone note has no business saying. Checked on the accent-less lower-case text, line by line.
_CLAIMS = re.compile(r"\b(voce|vc|assistente)\s+(agora\s+)?(pode|consegue|sabe|tem acesso|acessa|envia|manda|apaga|move|exclui|executa|ignora|deve|nao precisa)\b")
_RULES = re.compile(r"\b(permissao|permissoes|autoriza\w*|aprovacao|aprovar|sem pedir|sem perguntar|regras?|restricoes?|limites?|politicas?|ferramentas?|honestidade)\b")
_SECRETS = re.compile(r"\b(senha|senhas|credencia\w*|token|tokens|chave|chaves|segredo|segredos|api key|cofre)\b|[A-Za-z0-9_\-]{24,}|https?://|\\\\|[a-z]:[\\/]")


def _problem(text):
    """Why a persona is refused (a short code, never the text), or ``None``."""
    if len(text) > MAX_CHARS:
        return "grande_demais"
    for line in text.splitlines():
        plain = _plain(line)
        if looks_like_injection(line) or looks_like_injection(plain):
            return "instrucao"
        if _CLAIMS.search(plain):
            return "capacidade"
        if _RULES.search(plain):
            return "regra"
        if _SECRETS.search(plain) or _SECRETS.search(line):
            return "segredo"
    return None


def load(data_dir):
    """The persona text to use. Missing, empty, unreadable or refused files give the built-in one."""
    try:
        raw = (data_dir / PERSONA_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return DEFAULT
    except (OSError, UnicodeDecodeError):
        logger.warning("persona ignorada: arquivo ilegivel")
        return DEFAULT
    if not raw:
        return DEFAULT
    why = _problem(raw)
    if why:
        logger.warning("persona descartada (%s); o texto padrao foi usado", why)
        return DEFAULT
    return " ".join(raw.split()) + " "


def style_text(name):
    """The prompt text of a preset; an unknown name is the default one, never an error that could leave the chat without a prompt."""
    return STYLES.get(name, STYLES[DEFAULT_STYLE])
