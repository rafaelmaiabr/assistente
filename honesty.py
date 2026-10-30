"""What the assistant can and cannot do, said by the server instead of left to a small model (#121).

Three pieces, none of which adds a capability: the prompt text listing what the registered tools really allow, short
deterministic answers for requests that are known to be out of reach, and two checks on the model's own text (a button that
no card backs, an action announced when no tool ran).
"""
import re
import unicodedata

from mail_intent import _plain

MAX_PHRASE = 120

_READS = {"listar_emails", "buscar_emails", "ler_email"}
_CAN = [
    (_READS, "ler a caixa de email (listar, buscar e ler, somente leitura)"),
    ({"preparar_resposta"}, "preparar o rascunho de uma resposta, que só sai quando o usuário aprova na tela"),
    ({"configurar_caixa_de_email"}, "abrir o formulário seguro de configuração da caixa de email"),
    ({"solicitar_integracao"}, "mostrar o cartão para o usuário conectar um sistema (o usuário decide nos botões do cartão)"),
    ({"consultar_rota"}, "consultar as rotas de internet que o operador liberou, e só elas"),
    ({"triar_emails"}, "examinar a caixa de entrada e apontar mensagens prioritárias ou suspeitas, com as evidências, só lendo (nada é movido, apagado nem marcado)"),
    ({"propor_regra_de_caixa"}, "propor uma regra de triagem da caixa (endereço completo ou domínio exato); ela só é criada se a pessoa escolher criar no cartão"),
    ({"propor_memoria"}, "propor uma anotação para a memória sobre a pessoa; ela só é gravada se a pessoa aprovar na tela e só vale na próxima conversa"),
]
_CANNOT = ("acessar a internet em geral ou abrir endereços que o usuário digitar (fora das rotas liberadas, se houver); "
           "mover, apagar, arquivar ou marcar emails como lidos ou spam; enviar email sem a aprovação do usuário na tela; "
           "criar botões, cartões ou links por conta própria, pois só as ferramentas os mostram")


def capabilities_text(names):
    """The prompt paragraph for the tools that are really registered in this session (``names``), so it cannot drift from the code."""
    names = set(names)
    can = [label for tools, label in _CAN if names & tools]
    return ("Capacidades reais nesta sessão: " + ("; ".join(can) if can else "apenas conversar, sem nenhuma ferramenta") + ". "
            "Você NÃO consegue: " + _CANNOT + ". Quando pedirem algo fora disso, diga com clareza que não consegue; "
            "nunca diga que está fazendo algo sem chamar a ferramenta, e nunca descreva botões ou cartões que a ferramenta não mostrou. ")


_ASSISTANT = r"(?:(?:voce|vc)\s+)?"
_INTERNET = re.compile(rf"^{_ASSISTANT}(?:consegue|pode|sabe|tem\s+acesso|acessa|navega|esta\s+conectad[oa]|tem\s+internet)\b.*\binternet\b")
_URL = re.compile(r"(?i)https?://\S{3,}")
_OPEN_URL = re.compile(rf"^(?:(?:por favor|pode|poderia|consegue|quero|preciso)\s+)*{_ASSISTANT}"
                       r"(?:acessar|acesse|acessa|abrir|abra|ler|leia|consultar|consulte|visitar|visite|entrar|entre)\b")
_MAIL_WRITE = re.compile(r"^(?:(?:por favor|pode|poderia|consegue|quero|preciso|vamos)\s+)*"
                         r"(?:mov(?:er|a|am)|apag(?:ar|ue|am)|exclu(?:ir|a)|delet(?:ar|e)|arquiv(?:ar|e)|(?:marcar|marque)|limp(?:ar|e))\b"
                         r".*\b(?:e-?mails?|mensagens?|spam|lixeira|caixa|lid[oa]s?)\b")
_SEND = re.compile(r"\b(?:envi(?:e|ar)|mand(?:e|ar))\b.*\bsem\s+(?:aprovacao|aprovar|confirmar|confirmacao|perguntar|revisar|revisao)\b")


def parse(text):
    """The key of the known out-of-reach request ``text`` is, or ``None``. Only short, plain requests: anything else goes to the model."""
    if not isinstance(text, str) or len(text) > MAX_PHRASE:
        return None
    words = _plain(text)
    if _SEND.search(words):
        return "enviar"
    if _URL.search(text) and _OPEN_URL.match(words):
        return "endereco"
    if _MAIL_WRITE.match(words):
        return "emails"
    if _INTERNET.match(words):
        return "internet"
    return None


def _routes(routes):
    if not routes:
        return "Nenhuma rota de consulta está liberada."
    return "Rotas liberadas pelo operador: " + ", ".join(f"{r.name} ({r.label})" for r in routes) + "."


def answer(key, routes=()):
    """The assistant's text for a key from :func:`parse`. Nothing was done, and the text says what is possible instead."""
    if key == "emails":
        return ("Não consigo mover, apagar, arquivar nem marcar emails: minha leitura da caixa é somente leitura, e nada foi alterado. "
                "Posso listar, buscar e ler mensagens, e preparar o rascunho de uma resposta para você aprovar. "
                "Para listar, peça: \"liste meus emails\".")
    if key == "enviar":
        return ("Não envio email por conta própria. Posso preparar o rascunho de uma resposta; ele só sai quando você revisa e clica em "
                "Aprovar e enviar na tela. Nada foi enviado.")
    if key == "endereco":
        return ("Não abro endereços digitados na conversa, e nada foi acessado. Só consulto rotas que o operador liberou antes, pelo nome. "
                + _routes(routes))
    return "Não tenho acesso livre à internet. " + _routes(routes) + " Fora disso, não consigo consultar páginas nem serviços externos."


_BUTTON = re.compile(r"\[\s*(?:conectar|configurar|abrir|aprovar|enviar|autorizar|ativar|integrar|clique|botao)[^\]\n]{0,50}\]"
                     r"|\bclique\s+(?:no|nos|em)\s+(?:o\s+)?(?:cartao|botao|botoes|link|formulario)\b"
                     r"|\b(?:cartao|botao|botoes)\s+abaixo\b")
_ANNOUNCE = re.compile(r"\b(?:estou|estarei|vou|vamos|farei|irei)\s+(?:agora\s+)?"
                       r"(?:listando|consultando|verificando|buscando|procurando|pesquisando|acessando|lendo|checando|conferindo|movendo|apagando|marcando"
                       r"|listar|consultar|verificar|buscar|procurar|pesquisar|acessar|ler|checar|conferir|mover|apagar|marcar)\b")


def fake_button(text):
    """True when the model's text points at a button or card (which only the server can show)."""
    if not isinstance(text, str):
        return False
    folded = "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn")  # keeps the brackets
    return _BUTTON.search(folded) is not None


def announces_action(text):
    """True when the model says it is doing (or about to do) a lookup or change."""
    return isinstance(text, str) and _ANNOUNCE.search(_plain(text)) is not None


def turn_warning(text, tools, shown):
    """The notice for a finished turn, or ``None``. ``tools``: how many tools ran; ``shown``: whether the server showed a card or form."""
    notes = []
    if fake_button(text) and not shown:
        notes.append("A resposta fala de um botão ou cartão, mas nenhum foi mostrado na tela. Ignore essa parte.")
    if announces_action(text) and not tools:
        notes.append("A resposta anunciou uma ação, mas nenhuma ferramenta foi executada: nada foi feito. "
                     "Para ver seus emails, peça: \"liste meus emails\".")
    return " ".join(notes) or None
