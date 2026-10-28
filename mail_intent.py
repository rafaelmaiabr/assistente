"""Short phrases that ask to list the mailbox ("liste meus emails", "tenho email novo?", "qual o ultimo email").

Recognized in the backend so the answer does not depend on a small model deciding to call the tool. It only lists
(read only, the same call the ``listar_emails`` tool makes). Anything that is not a plain request to list goes to the model.
"""
import re
import unicodedata

from personal_mail import MAX_ITEMS

DEFAULT_LIMIT = 5
MAX_PHRASE = 120
_NUMBERS = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10}
# "emails" as people say it and as a speech model writes it (emeio, imeio, e-meio, e mail).
_OBJECT = r"(?:e-?\s?mails?|[ei]-?meios?|mensagens?|correio|caixa de entrada|inbox)"
_FILLER = (r"(?:(?:os|as|o|a|meus|minhas|meu|minha|seus|ultimos?|ultimas?|novos?|novas?|recentes?|mais recentes?|de hoje|\d+|"
           + "|".join(_NUMBERS) + r")\s+)*")
_AFTER = r"(?:\s+(?:recentes?|novos?|novas?|de hoje|da caixa de entrada|da minha caixa(?: de entrada)?|do meu correio))*"
_POLITE = r"(?:(?:por favor|pf|voce|vc|pode|poderia|consegue|quero|preciso|gostaria de|me|vamos|agora)\s+)*"
_READ_VERBS = (r"(?:listar|liste|lista|mostrar|mostre|mostra|exibir|exiba|ler|leia|le|ver|veja|consultar|consulte|conferir|confira|"
               r"verificar|verifique|checar|cheque|abrir|abra|falar|fala|diga|diz)")
_PATTERNS = [
    re.compile(rf"^{_POLITE}{_READ_VERBS}\s+(?:me\s+)?{_FILLER}{_OBJECT}{_AFTER}$"),
    re.compile(rf"^(?:eu\s+)?(?:tenho|tem|ha|chegou|chegaram|recebi)\s+(?:(?:algum|alguma|alguns|algumas|novo|novos|nova|novas|um|uma)\s+)*"
               rf"{_OBJECT}(?:\s+(?:novo|novos|nova|novas|hoje))?$"),
    re.compile(rf"^qual\s+(?:e\s+|foi\s+)?o\s+ultimo\s+{_OBJECT}(?:\s+que\s+(?:eu\s+)?recebi)?$"),
]


def _plain(text):
    """Lower case, no accents, no punctuation: the words only."""
    base = "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", base)).strip()


def parse(text):
    """``{"limit": n}`` when ``text`` is a plain request to list the mailbox, otherwise ``None``."""
    if not isinstance(text, str) or len(text) > MAX_PHRASE:
        return None
    words = _plain(text)
    if not any(pattern.match(words) for pattern in _PATTERNS):
        return None
    last = re.search(r"\bultim[oa]\b", words) is not None  # "ultimo email" (one), not "ultimos emails"
    number = re.search(r"\b(\d+)\b", words) or re.search(r"\b(" + "|".join(_NUMBERS) + r")\b", words)
    if number:
        value = int(number.group(1)) if number.group(1).isdigit() else _NUMBERS[number.group(1)]
    else:
        value = 1 if last else DEFAULT_LIMIT
    return {"limit": max(1, min(value, MAX_ITEMS))}


def _clean(value, high):
    """One line of plain text: the content of a message is not ours, so no line breaks, no control characters, bounded."""
    text = re.sub(r"[\x00-\x1f\x7f  ]+", " ", str(value if value is not None else ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= high else text[: high - 1] + "…"


def render(result):
    """The assistant's text for a ``PersonalMail.list_recent`` result. Fields from the mailbox go inside a code block,
    which the screen shows as plain text, so a hostile subject cannot become a link or a heading."""
    if not isinstance(result, dict) or "erro" in result:
        reason = _clean((result or {}).get("erro") if isinstance(result, dict) else "", 200)
        return "Não consegui ler a caixa de email." + (f" {reason}" if reason else "")
    messages = result.get("mensagens") or []
    folder = _clean(result.get("pasta") or "INBOX", 60)
    if not messages:
        return f"Não há mensagens na pasta {folder} da sua caixa real."
    lines = [f"{n}. {_clean(m.get('de'), 80) or '(sem remetente)'} | {_clean(m.get('assunto'), 140) or '(sem assunto)'} | {_clean(m.get('data'), 40)}"
             for n, m in enumerate(messages, 1)]
    more = "\nHá mais mensagens além destas." if result.get("ha_mais") else ""
    return f"Da sua caixa real, pasta {folder} (remetente | assunto | data):\n\n```\n" + "\n".join(lines) + "\n```" + more
