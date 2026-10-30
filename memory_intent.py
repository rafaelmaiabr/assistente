"""Plain requests to write something down ("lembre que eu moro em Recife"), recognised by the server instead of left to a small model (#150).

With the real model the same sentence got "Claro!" and no tool call, so the person believed something was saved. Here the server only
*proposes* (the card on screen still needs the person's click) and the text goes through the same check as any memory entry.
"""
import re

MAX_PHRASE = 260  # the entry limit (200) plus the lead-in; longer messages go to the model

_LEAD = r"(?:voc[eê]\s+)?(?:(?:por favor|pode|poderia|consegue|quero que voc[eê]|preciso que voc[eê]|vamos)[,\s]+)*(?:voc[eê]\s+)?"
_VERB = (r"(?:lembr(?:e|a|ar)(?:-se|-te)?(?:\s+de)?|anot(?:e|a|ar)|guard(?:e|a|ar)|salv(?:e|a|ar)|memoriz(?:e|a|ar)|registr(?:e|a|ar)"
         r"|n[aã]o\s+esque[cç]a(?:\s+de)?)")
_WHERE = r"(?:\s+(?:isso\s+)?(?:na|em|para|pra)\s+(?:a\s+|sua\s+|minha\s+)*mem[oó]ria)?"
_REQUEST = re.compile(rf"^\s*{_LEAD}{_VERB}{_WHERE}\s*[,:]?\s+que\s+(?P<fact>.+?)\s*$", re.IGNORECASE | re.DOTALL)


def parse(text):
    """The fact to write down when ``text`` is a plain request to do it, otherwise ``None``. Questions are not requests."""
    if not isinstance(text, str) or len(text) > MAX_PHRASE or text.rstrip().endswith("?"):
        return None
    found = _REQUEST.match(text)
    if not found:
        return None
    fact = found.group("fact").strip().rstrip(".!;, ").strip()
    return fact or None
