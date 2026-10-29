"""Keep the chat's context small for a small local model (#133): old tool results are cleared and big ones are cut.

Both are deterministic and need no model. A tool call is never separated from its result: an old result keeps its place in the
history and only its (long) text becomes a short marker, so what the person said and what the assistant answered stay intact.
"""
import json

KEEP_TURNS = 2  # the last N user messages, and everything after the first of them, are never touched
MIN_CHARS = 200  # shorter results are cheap and stay
MAX_RESULT_CHARS = 6000  # a result handed to the model is cut here
MARKER = "[resultado antigo removido para economizar contexto; consulte a ferramenta de novo se precisar]"
CUT_NOTE = "Resultado cortado por ser grande demais; peça um recorte menor (menos itens, outra pasta ou um filtro)."


def prune(chat_ctx, keep_turns=KEEP_TURNS, min_chars=MIN_CHARS):
    """``(new_context, count)``: ``chat_ctx`` is not changed. Results before the last ``keep_turns`` user messages that are longer
    than ``min_chars`` are replaced by ``MARKER``. A turn whose tool round was not answered yet is inside the kept tail."""
    items = list(chat_ctx.items)
    users = [i for i, item in enumerate(items) if item.type == "message" and item.role == "user"]
    if keep_turns < 1 or len(users) <= keep_turns:
        return chat_ctx, 0
    boundary = users[-keep_turns]
    count = 0
    pruned = chat_ctx.copy()
    for i in range(boundary):
        item = pruned.items[i]
        if item.type == "function_call_output" and len(item.output) > min_chars and item.output != MARKER:
            pruned.items[i] = item.model_copy(update={"output": MARKER})
            count += 1
    return (pruned, count) if count else (chat_ctx, 0)


def fit(result, max_chars=MAX_RESULT_CHARS):
    """The tool result as it goes to the model: unchanged when small, otherwise a cut version that says it was cut."""
    try:
        text = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"erro": "Resultado da ferramenta nao pode ser mostrado."}
    if len(text) <= max_chars:
        return result
    return {"aviso": CUT_NOTE, "dados_nao_confiaveis": True, "parcial": text[:max_chars]}
