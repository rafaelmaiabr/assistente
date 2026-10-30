"""Read-only triage of the mailbox: priority and suspicion, each with its evidence (#138).

The rules follow the reference in docs/REFERENCIA-HERMES.md (preserve before cleaning; fraud needs two independent evidences, one
tied to the malicious action; weak signals alone do not count; a real conversation or a known sender keeps a message; commercial
mail is never junk; a deadline is only what the text says). Everything here is plain code on parsed messages: the text of a
message is *data*, it cannot change a rule, and nothing is moved, deleted or marked. Reading is done by the mailbox objects with
``BODY.PEEK``. The report names its coverage, so "I looked at everything" is never said when there was a limit.
"""
import re
from urllib.parse import urlparse

import mailbox_rules
from mail_intent import _clean, _plain

DEFAULT_LIMIT = 10
MAX_PRAZO = 60

EXECUTABLE = (".exe", ".scr", ".js", ".vbs", ".bat", ".cmd", ".lnk", ".msi", ".jar", ".ps1", ".com", ".iso", ".img")
_DOUBLE = re.compile(r"\.(pdf|docx?|xlsx?|jpe?g|png|txt)\.(exe|scr|js|vbs|bat|cmd|lnk|com)$", re.I)
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "cutt.ly", "rb.gy", "ow.ly", "shorturl.at", "tiny.cc"}
BRANDS = ("paypal", "itau", "bradesco", "santander", "nubank", "banco do brasil", "caixa economica", "receita federal", "correios",
          "microsoft", "google", "apple", "amazon", "netflix", "mercado livre", "serasa", "detran")
_SECOND_LEVEL = {"com.br", "org.br", "net.br", "gov.br", "edu.br", "co.uk", "com.ar", "com.pt"}
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

# Evidence: id -> (group, strong, why). A strong one is tied to the action the sender wants; weak ones never count alone.
EVIDENCE = {
    "pede_credencial": ("acao", True, "pede senha, código, token ou dados do cartão"),
    "pagamento_novo_destino": ("acao", True, "pede pagamento para uma conta ou chave nova"),
    "anexo_executavel": ("destino", True, "anexo executável ou com extensão dupla"),
    "link_dominio_diferente": ("destino", False, "link para um domínio diferente do remetente"),
    "encurtador": ("destino", False, "link encurtado"),
    "reply_to_diferente": ("destino", False, "Reply-To de outro domínio"),
    "nome_se_passa_por": ("identidade", False, "nome de exibição de uma marca que o domínio não confirma"),
    "urgencia_ameaca": ("pressao", False, "urgência com ameaça"),
}
PRIORITY = {
    "conversa_real": "faz parte de uma conversa (cabeçalhos In-Reply-To/References)",
    "incidente": "fala de incidente ou serviço fora do ar",
    "cliente_conhecido": "vem de um remetente que você marcou como conhecido",
    "prazo_explicito": "traz um prazo escrito no texto",
    "regra_prioridade": "uma regra que você aprovou marca como prioridade",
}
CLASS_LABEL = {"prioridade": "Prioridade", "suspeito": "Suspeita", "revisao": "Para você revisar", "normal": "Sem alerta"}

_ASKS_CREDENTIAL = re.compile(r"\b(confirm\w*|inform\w*|digit\w*|envi\w*|atualiz\w*|valid\w*|redefin\w*|verific\w*|fornec\w*)\b.{0,25}"
                              r"\b(senha|codigo de (seguranca|verificacao|acesso)|token|cvv|dados do cartao|numero do cartao|dados bancarios|login e senha)\b")
_NEW_PAYMENT_DEST = re.compile(r"\b(novos? dados bancarios|nova conta|conta nova|nova chave( pix)?|chave pix nova|alterei (minha |a )?(conta|chave)|"
                               r"mudei (a |minha )?(conta|chave)|troquei (a |minha )?(conta|chave)|atualizacao dos dados bancarios)\b")
_PAYMENT_WORD = re.compile(r"\b(pag\w*|transfer\w*|pix|deposit\w*)\b")
_URGENT = re.compile(r"\b(urgente|imediat\w*|ultimo aviso|em ate \d+ ?(h|horas)|ainda hoje|hoje)\b")
_THREAT = re.compile(r"\b(bloque\w*|suspens\w*|cancel\w*|encerr\w*|multa|perder\w*|desativ\w*)\b")
_COMMERCIAL = re.compile(r"\b(boleto|nota fiscal|nfe?|fatura|renovacao|cobranca|confirmacao de pagamento|comprovante|recibo|assinatura|pedido \d+)\b")
_INCIDENT = re.compile(r"\b(fora do ar|indisponibilidade|indisponivel|incidente|instabilidade|queda d[oe]|servidor (caiu|fora)|"
                       r"erro em producao|sistema parou|parou de funcionar)\b")
_DEADLINE = re.compile(r"(?i)\b(?:at[ée]|prazo(?: final)?(?: [ée]| de| para)?:?|vence(?:ndo)?(?: em| dia)?|vencimento:?|limite:?)\s*"
                       r"(?:o\s+dia\s+|dia\s+|as\s+|às\s+)?(\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}h(?:\d{2})?|\d{1,2}:\d{2}|\d{1,2} de [a-zç]+(?: de \d{4})?)")
_ASKS = (
    (re.compile(r"\b(pode|poderia|consegue)\b.{0,12}\bconfirmar\b|\bfavor confirmar\b|\bconfirme\b"), "confirmar"),
    (re.compile(r"\b(pode|poderia|consegue)\b.{0,12}\b(enviar|mandar|encaminhar)\b|\bfavor (enviar|mandar)\b"), "enviar algo"),
    (re.compile(r"\b(pode|poderia|consegue)\b.{0,12}\b(responder|retornar|informar|avisar)\b|\baguardo (seu |sua )?(retorno|resposta)\b|\bpreciso que\b"), "responder"),
    (re.compile(r"\b(pode|poderia)\b.{0,12}\b(ligar|telefonar)\b|\bme ligue\b"), "ligar"),
)
_BULK_PRECEDENCE = {"bulk", "junk", "list"}


def _text(value, limit):
    """Text from a message for the screen: one line, bounded, and no backtick, so it cannot close the code block it is shown in."""
    return _clean(value, limit).replace("`", "'")


def base_domain(host):
    """The registrable part of a host name (``mail.exemplo.com.br`` -> ``exemplo.com.br``)."""
    labels = [part for part in str(host or "").lower().strip(".").split(".") if part]
    if len(labels) <= 2:
        return ".".join(labels)
    return ".".join(labels[-3:]) if ".".join(labels[-2:]) in _SECOND_LEVEL else ".".join(labels[-2:])


def _domain_of(address):
    return base_domain(str(address or "").rsplit("@", 1)[-1]) if "@" in str(address or "") else ""


def _hosts(text):
    found = []
    for url in _URL.findall(text or ""):
        host = (urlparse(url).hostname or "").lower()
        if host:
            found.append(host)
    return found[:30]


def _is_bulk(message):
    auto = message.get("auto") or {}
    submitted = str(auto.get("auto_submitted") or "no")
    return submitted != "no" or str(auto.get("precedence") or "") in _BULK_PRECEDENCE or bool(auto.get("list_id") or auto.get("list_unsubscribe"))


def _ids(message):
    ids = message.get("ids") or {}
    return bool(ids.get("in_reply_to") or ids.get("references"))


def evidences(message):
    """The ids of the evidences found in a parsed message (see ``EVIDENCE``). Pure function of the message."""
    sender = str(message.get("sender") or "")
    own = _domain_of(sender)
    text = f"{message.get('subject') or ''}\n{message.get('text') or ''}"
    plain = _plain(text)
    found = []
    if _ASKS_CREDENTIAL.search(plain):
        found.append("pede_credencial")
    if _NEW_PAYMENT_DEST.search(plain) and _PAYMENT_WORD.search(plain):
        found.append("pagamento_novo_destino")
    for attachment in message.get("attachments") or []:
        name = str((attachment or {}).get("filename") or "").lower()
        if name.endswith(EXECUTABLE) or _DOUBLE.search(name):
            found.append("anexo_executavel")
            break
    hosts = _hosts(message.get("text") or "")
    if any(base_domain(host) in SHORTENERS for host in hosts):
        found.append("encurtador")
    if own and any(base_domain(host) not in SHORTENERS and base_domain(host) != own for host in hosts):
        found.append("link_dominio_diferente")
    reply_to = _domain_of(str((message.get("auto") or {}).get("reply_to") or "").strip("<> ").split("<")[-1])
    if own and reply_to and reply_to != own:
        found.append("reply_to_diferente")
    name_plain = _plain(str(message.get("sender_name") or ""))
    if own and any(brand in name_plain and brand.split()[0] not in own for brand in BRANDS):
        found.append("nome_se_passa_por")
    if _URGENT.search(plain) and _THREAT.search(plain):
        found.append("urgencia_ameaca")
    return found


def is_suspect(found):
    """Two independent evidences (different groups), at least one strong. Two of the same group count as one."""
    groups = {EVIDENCE[item][0] for item in found}
    return any(EVIDENCE[item][1] for item in found) and len(groups) >= 2


def deadline(message):
    """The deadline exactly as the text writes it, or ``None``. Never inferred from words like "urgente"."""
    found = _DEADLINE.search(f"{message.get('subject') or ''}\n{message.get('text') or ''}")
    return _text(found.group(0), MAX_PRAZO) if found else None


def asked(message):
    plain = _plain(message.get("text") or "")
    for pattern, label in _ASKS:
        if pattern.search(plain):
            return label
    return None


def analyse(message, known_senders=(), rules=()):
    """The report line for one parsed message. ``known_senders``: addresses or domains the person marked as known. ``rules``: the
    active mailbox rules of this account (#139); they only move a message toward priority or preservation, never toward a verdict of fraud."""
    found = evidences(message)
    sender = str(message.get("sender") or "").lower()
    plain = _plain(f"{message.get('subject') or ''} {message.get('text') or ''}")
    bulk = _is_bulk(message)
    known = bool(sender) and (sender in known_senders or _domain_of(sender) in known_senders)
    actions, rule_ids, clash = mailbox_rules.applied(rules, message)
    known = known or "preservar" in actions
    real_conversation = _ids(message) and not bulk
    commercial = bool(_COMMERCIAL.search(plain)) or bool((message.get("auto") or {}).get("list_unsubscribe"))
    limit = deadline(message)
    reasons = []
    if real_conversation:
        reasons.append("conversa_real")
    if known:
        reasons.append("cliente_conhecido")
    if _INCIDENT.search(plain) and not bulk:
        reasons.append("incidente")
    if limit:
        reasons.append("prazo_explicito")
    if "prioridade" in actions:
        reasons.append("regra_prioridade")
    suspect = is_suspect(found)
    conflict = real_conversation or known
    if clash:
        kind, why = "revisao", "Regras suas em conflito para esta mensagem: foi preservada; decida qual vale."
    elif suspect and conflict:
        kind, why = "revisao", "Há indícios de golpe, mas é uma conversa real ou um remetente conhecido: foi preservada; confira você."
    elif suspect and reasons:
        kind, why = "prioridade", "Parece importante, mas tem indícios de golpe: não confie nos links nem nos pedidos antes de conferir."
    elif suspect:
        kind, why = "suspeito", "Há duas evidências independentes de golpe, uma ligada ao que o remetente pede."
    elif reasons:
        kind, why = "prioridade", "; ".join(PRIORITY[item] for item in reasons).capitalize() + "."
    else:
        kind, why = "normal", "Nenhum sinal de prioridade ou de golpe."
    ask = asked(message)
    if commercial and ask is None and re.search(r"\b(boleto|fatura|cobranca|vencimento)\b", plain):
        ask = "pagar"
    return {
        "uid": message.get("uid"), "de": _text(message.get("sender"), 80), "assunto": _text(message.get("subject"), 120),
        "classe": kind, "motivo": why, "suspeita": suspect, "destaque": suspect and kind in ("prioridade", "revisao"),
        "prioridade_por": reasons, "evidencias": [{"tipo": item, "grupo": EVIDENCE[item][0], "forte": EVIDENCE[item][1],
                                                  "porque": EVIDENCE[item][2]} for item in found],
        "acao_pedida": ask, "prazo": limit, "aguarda_resposta": ask in ("confirmar", "enviar algo", "responder", "ligar") and not bulk,
        "preservar": commercial or known or real_conversation,
        "regras_aplicadas": rule_ids, "conflito_de_regras": clash,
        "re_sem_cabecalhos": bool(re.match(r"(?i)\s*(re|enc|fwd?)\s*:", str(message.get("subject") or ""))) and not _ids(message),
    }


def coverage(requested, listed, examined, failed, more, folder):
    """What was really looked at, in words. Says "all" only when nothing was left out."""
    complete = not more and failed == 0
    if complete:
        text = f"Examinei todas as {examined} mensagens que há na pasta {folder}."
    else:
        parts = [f"Examinei {examined} das mensagens mais recentes da pasta {folder}"]
        if failed:
            parts.append(f"{failed} não puderam ser lidas")
        if more:
            parts.append("há mensagens mais antigas que não examinei")
        text = "; ".join(parts) + "."
    return {"pasta": folder, "pedidas": requested, "listadas": listed, "examinadas": examined, "falharam": failed, "ha_mais": bool(more),
            "completa": complete, "texto": text}


def triage(mail, limit=DEFAULT_LIMIT, since=None, known_senders=(), account="pessoal", rules=()):
    """Lists the latest messages and reads each one without marking it, then reports. Writes nothing to the mailbox."""
    page = mail.list_recent(None, limit, since)
    if not isinstance(page, dict) or "erro" in page:
        return page if isinstance(page, dict) else {"erro": "Não foi possível ler a caixa agora."}
    items, results, failed = page.get("mensagens") or [], [], 0
    for item in items:
        message = mail.original(item.get("uid"), page.get("uidvalidity"))
        if not isinstance(message, dict) or "erro" in message:
            failed += 1
            continue
        row = analyse(message, known_senders, rules)
        row["conta"] = account
        results.append(row)
    folder = _text(page.get("pasta") or "INBOX", 60)
    order = {"suspeito": 0, "revisao": 1, "prioridade": 2, "normal": 3}
    results.sort(key=lambda row: (order[row["classe"]], not row["destaque"]))
    return {"dados_nao_confiaveis": True, "somente_leitura": True, "cobertura": coverage(limit, len(items), len(results), failed, page.get("ha_mais"), folder),
            "mensagens": results}


_ASK_TRIAGE = re.compile(
    r"^(?:(?:por favor|pode|poderia|consegue|quero que voc[eê]|vamos)[,\s]+)*(?:voc[eê]\s+)?"
    r"(?:(?:fa[cç]a|faz|fazer|fa[cç]amos)\s+(?:a\s+|uma\s+)?tria?gem|tri(?:e|ar|a)\s+(?:(?:a|os|as|meus|minha|minhas)\s+)*(?:caixa|e-?mails?|mensagens)|"
    r"(?:quais|que)\s+(?:dos\s+)?(?:meus\s+)?(?:e-?mails?|mensagens)\s+(?:s[aã]o|est[aã]o|parecem)\s+(?:s[aã]o\s+)?(?:priori\w+|importantes?|urgentes?|suspeit\w+|golpes?)|"
    r"(?:tem|h[aá]|existe)\s+(?:algum[a]?\s+)?(?:e-?mail|mensagem)\s+(?:suspeit\w+|de golpe|de phishing|importante|urgente|priori\w+)|"
    r"(?:algum|algum[a]?)\s+(?:e-?mail|mensagem)\s+(?:suspeit\w+|de golpe|de phishing))"
    r".{0,40}$", re.I | re.DOTALL)


def parse_request(text):
    """True when ``text`` is a short, plain request to triage the mailbox (answered by the server, not left to the model)."""
    return isinstance(text, str) and len(text) <= 120 and bool(_ASK_TRIAGE.match(text.strip().rstrip("?!. ")))


def render(report):
    """The assistant's text. Fields from the mailbox sit in a code block so a hostile subject cannot become a link or a heading."""
    if not isinstance(report, dict) or "erro" in report:
        reason = _text((report or {}).get("erro") if isinstance(report, dict) else "", 200)
        return "Não consegui examinar a caixa de email." + (f" {reason}" if reason else "") + " Nada foi alterado."
    rows, cover = report.get("mensagens") or [], report["cobertura"]
    head = f"Triagem somente leitura: nada foi movido, apagado nem marcado como lido. {cover['texto']}"
    if not rows:
        return head + " Não há mensagens para examinar."
    sections = []
    for kind in ("suspeito", "revisao", "prioridade", "normal"):
        group = [row for row in rows if row["classe"] == kind]
        if not group:
            continue
        if kind == "normal":
            sections.append(f"{CLASS_LABEL[kind]}: {len(group)} mensagem(ns).")
            continue
        lines = []
        for row in group:
            extra = [row["motivo"]]
            if row["evidencias"]:
                extra.append("evidências: " + ", ".join(item["porque"] for item in row["evidencias"]))
            if row["prazo"]:
                extra.append(f"prazo no texto: {row['prazo']}")
            if row["acao_pedida"]:
                extra.append(f"pede: {row['acao_pedida']}" + (" (aguarda resposta)" if row["aguarda_resposta"] else ""))
            lines.append(f"uid {row['uid']} | {row['de'] or '(sem remetente)'} | {row['assunto'] or '(sem assunto)'}\n    " + "\n    ".join(extra))
        sections.append(f"{CLASS_LABEL[kind]} ({len(group)}):\n```\n" + "\n".join(lines) + "\n```")
    return head + "\n\n" + "\n\n".join(sections) + "\n\nIsto é uma análise por regras e pode errar; nada foi feito com as mensagens."
