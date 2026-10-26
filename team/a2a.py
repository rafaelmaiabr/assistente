"""A2A (Agent2Agent) adapter: the wire contract between this team and agents outside this process.

MCP stays the boundary for *tools*; A2A is only a way to hand a task to another agent and read its answer back. This module is the
protocol layer and nothing else: it knows no repository, no company data and no credential. What it guarantees, whatever the peer says:

* **Pinned version.** Only A2A 1.0 (``A2A-Version: 1.0``); a card or a response for another version is refused (see docs/TEAM-A2A.md).
* **A card describes, it never grants.** A remote Agent Card is validated and reduced to a few short, escaped strings; ``grants_access``
  is always ``False`` and no field of a card widens a permission. Who may be called, with which credential, for which company, is
  decided by the gateway (T40), never by what the peer advertises.
* **Outbound is a small envelope.** The message carries the objective, the expected result, a short summary and short evidence lines the
  caller chose, never a secret and never the case history; a secret-looking value refuses the whole message.
* **Inbound is untrusted data.** Task ids, context ids and states are checked against what was asked and against the allowed state
  changes; a terminal task never goes back to work; results are redacted and capped, and an unknown outcome is not a success.
* **Handing a task over hands over nothing else.** A remote task has no say about who answers the customer: the local case owner stays
  responsible (docs/TEAM-HANDOFFS.md). Timeouts and lost connections surface as ``A2AUnavailable`` for the caller to settle.

The transport (``A2ATransport``) is an interface, as ``McpTransport`` is for tools: the tests use an in-memory peer and a real HTTP
binding plugs in later without changing these rules.
"""
import hashlib
import re
from abc import ABC, abstractmethod

from .prompting import escape
from .schema import find_secrets, redact_secrets

A2A_VERSION = "1.0"
VERSION_HEADER = "A2A-Version"
BINDING = "JSONRPC"
CARD_PATH = "/.well-known/agent-card.json"

MAX_TEXT = 4000          # one text part
MAX_PARTS = 8            # parts in one message
MAX_EVIDENCE = 5         # evidence lines in one message
MAX_ARTIFACT_PARTS = 20
MAX_RESULT_BYTES = 65536
MAX_SKILLS = 20
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# -- states --------------------------------------------------------------------------------------------------------------------
SUBMITTED, WORKING = "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"
INPUT_REQUIRED, AUTH_REQUIRED = "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"
COMPLETED, FAILED = "TASK_STATE_COMPLETED", "TASK_STATE_FAILED"
CANCELED, REJECTED = "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"
TERMINAL = frozenset({COMPLETED, FAILED, CANCELED, REJECTED})
INTERRUPTED = frozenset({INPUT_REQUIRED, AUTH_REQUIRED})
STATES = frozenset({SUBMITTED, WORKING}) | TERMINAL | INTERRUPTED

# What a peer may move a task to. A terminal task is final; the same state again is a repeat, not a change.
_NEXT = {
    SUBMITTED: {WORKING} | INTERRUPTED | TERMINAL,
    WORKING: INTERRUPTED | TERMINAL,
    INPUT_REQUIRED: {WORKING} | TERMINAL,
    AUTH_REQUIRED: {WORKING} | TERMINAL,
}

# Local task states (team.enums.TASK_STATES). A peer's "rejected" is a failure here; both interrupted states wait for a person.
LOCAL_STATE = {
    SUBMITTED: "pendente", WORKING: "em_execucao", INPUT_REQUIRED: "aguardando", AUTH_REQUIRED: "aguardando",
    COMPLETED: "concluida", FAILED: "falhou", CANCELED: "cancelada", REJECTED: "falhou",
}

# -- errors --------------------------------------------------------------------------------------------------------------------
ERRORS = {
    -32700: "JSONParseError", -32600: "InvalidRequestError", -32601: "MethodNotFoundError", -32602: "InvalidParamsError",
    -32603: "InternalError", -32001: "TaskNotFoundError", -32002: "TaskNotCancelableError",
    -32003: "PushNotificationNotSupportedError", -32004: "UnsupportedOperationError", -32005: "ContentTypeNotSupportedError",
    -32006: "InvalidAgentResponseError", -32007: "ExtendedAgentCardNotConfiguredError", -32008: "ExtensionSupportRequiredError",
    -32009: "VersionNotSupportedError",
}


class A2AError(Exception):
    """A refusal that is safe to show: ``name`` is stable and the message never carries the peer's text, data or secrets."""

    def __init__(self, name, message):
        super().__init__(message)
        self.name = name


class A2AUnavailable(A2AError):
    """The peer did not answer (timeout, connection lost, refused). The outcome of the call is **unknown**."""

    def __init__(self, message="O agente externo nao respondeu; o resultado e desconhecido."):
        super().__init__("Unavailable", message)


class A2ARefused(A2AError):
    """The peer refused the call (authentication, unsupported version or operation)."""


def _invalid(message):
    return A2AError("InvalidAgentResponseError", message)


# -- ids and text --------------------------------------------------------------------------------------------------------------
def _id(value, what):
    if not isinstance(value, str) or not _ID.match(value):
        raise A2AError("InvalidParamsError", f"{what} invalido.")
    return value


def _text(value, limit, what, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise A2AError("InvalidParamsError", f"Informe {what}.")
    value = value.strip()
    if len(value) > limit:
        raise A2AError("InvalidParamsError", f"{what} passa de {limit} caracteres.")
    return value


def _clean(value, limit):
    """Short, single-line, escaped text from a peer (a card or a status line)."""
    return escape(" ".join(str(value or "").split()))[:limit]


def message_id(*parts):
    """A stable id for a message, from the caller's own keys, so a retry sends the same id and the peer can deduplicate."""
    return "m-" + hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:32]


# -- Agent Card ----------------------------------------------------------------------------------------------------------------
def build_agent_card(*, name, description, url, version, skills=()):
    """The card this team would publish for one of its agents. It describes; it holds no credential, no company id and no endpoint of
    any other system, and publishing it does not let anyone call anything: the gateway still authenticates and authorizes every call."""
    card = {
        "name": _text(name, 120, "o nome do agente"),
        "description": _text(description, 500, "a descricao do agente"),
        "version": _text(version, 40, "a versao do agente"),
        "supportedInterfaces": [{"url": _text(url, 300, "o endereco do agente"), "protocolBinding": BINDING, "protocolVersion": A2A_VERSION}],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
        "skills": [],
    }
    for skill in list(skills)[:MAX_SKILLS]:
        card["skills"].append({"id": _id(skill["id"], "skill"), "name": _text(skill["name"], 120, "o nome da skill"),
                               "description": _text(skill.get("description") or skill["name"], 300, "a descricao da skill"), "tags": []})
    if find_secrets(card):
        raise A2AError("InvalidParamsError", "O cartao nao pode conter segredos.")
    return card


class RemoteCard:
    """A peer's card reduced to what is safe to keep and show. It is information, never permission: ``grants_access`` is ``False``."""
    grants_access = False

    def __init__(self, name, description, version, url, skills):
        self.name, self.description, self.version, self.url, self.skills = name, description, version, url, skills

    def as_dict(self):
        return {"name": self.name, "description": self.description, "version": self.version, "url": self.url,
                "skills": list(self.skills), "grants_access": False}


def read_remote_card(card):
    """Validate a peer's Agent Card and reduce it to a ``RemoteCard``. Required fields and a 1.0 JSON-RPC interface are mandatory; every
    string is escaped and cut short, and anything else in the card (security schemes, extensions, signatures) is ignored here."""
    if not isinstance(card, dict):
        raise _invalid("Cartao do agente invalido.")
    for field in ("name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes", "defaultOutputModes", "skills"):
        if field not in card:
            raise _invalid(f"O cartao do agente nao traz `{field}`.")
    if find_secrets({key: card[key] for key in ("name", "description", "version", "skills")}):
        raise _invalid("O cartao do agente traz segredos; recusado.")
    interfaces = card["supportedInterfaces"]
    chosen = next((i for i in interfaces if isinstance(i, dict) and i.get("protocolBinding") == BINDING
                   and i.get("protocolVersion") == A2A_VERSION and isinstance(i.get("url"), str)), None) if isinstance(interfaces, list) else None
    if chosen is None:
        raise A2ARefused("VersionNotSupportedError", f"O agente nao oferece A2A {A2A_VERSION} por {BINDING}.")
    skills = []
    for skill in card["skills"][:MAX_SKILLS] if isinstance(card["skills"], list) else []:
        if isinstance(skill, dict) and isinstance(skill.get("id"), str) and _ID.match(skill["id"]):
            skills.append({"id": skill["id"], "name": _clean(skill.get("name"), 120), "description": _clean(skill.get("description"), 300)})
    return RemoteCard(_clean(card["name"], 120), _clean(card["description"], 500), _clean(card["version"], 40), chosen["url"][:300], skills)


# -- outbound ------------------------------------------------------------------------------------------------------------------
def outbound_message(*, message_id, context_id, objective, expected_result=None, summary=None, evidence=()):
    """The ``SendMessage`` request for a delegated task: the objective, the expected result, a short summary and short evidence lines.

    Nothing else goes out: no case history, no customer record, no credential. A value that looks like a secret refuses the message.
    ``context_id`` is the local id that groups the exchange (opaque to the peer); ``message_id`` is stable across retries."""
    parts = [{"text": "Objetivo: " + _text(objective, MAX_TEXT, "o objetivo")}]
    if expected_result:
        parts.append({"text": "Resultado esperado: " + _text(expected_result, MAX_TEXT, "o resultado esperado")})
    if summary:
        parts.append({"text": "Resumo: " + _text(summary, MAX_TEXT, "o resumo")})
    lines = list(evidence)
    if len(lines) > MAX_EVIDENCE:
        raise A2AError("InvalidParamsError", f"No maximo {MAX_EVIDENCE} evidencias.")
    for line in lines:
        parts.append({"text": "Evidencia: " + _text(line, 500, "a evidencia")})
    request = {"message": {"messageId": _id(message_id, "messageId"), "contextId": _id(context_id, "contextId"),
                           "role": "ROLE_USER", "parts": parts[:MAX_PARTS]}}
    if len(parts) > MAX_PARTS or find_secrets(request):
        raise A2AError("InvalidParamsError", "A mensagem passa do limite ou contem algo parecido com segredo; nada foi enviado.")
    return request


# -- inbound -------------------------------------------------------------------------------------------------------------------
def _artifact_text(artifacts):
    texts = []
    for artifact in artifacts if isinstance(artifacts, list) else []:
        for part in (artifact.get("parts") or [])[:MAX_ARTIFACT_PARTS] if isinstance(artifact, dict) else []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return texts


def read_task(task, *, expect_task_id=None, expect_context_id=None, previous_state=None):
    """Validate a task the peer returned and reduce it to ``{task_id, context_id, state, local_state, terminal, text}``.

    * ``expect_context_id`` / ``expect_task_id``: a task for another context or another task is refused (never matched by guess);
    * ``previous_state``: a state change the protocol does not allow (a terminal task going back to work) is refused;
    * ``text`` is the artifacts' text, redacted and capped; it is data from outside and is never an instruction."""
    if not isinstance(task, dict):
        raise _invalid("Tarefa invalida na resposta do agente.")
    task_id, context_id = task.get("id"), task.get("contextId")
    if not (isinstance(task_id, str) and _ID.match(task_id) and isinstance(context_id, str) and _ID.match(context_id)):
        raise _invalid("A tarefa do agente nao traz identificadores validos.")
    if expect_context_id is not None and context_id != expect_context_id:
        raise _invalid("A tarefa pertence a outro contexto; recusada.")
    if expect_task_id is not None and task_id != expect_task_id:
        raise _invalid("A resposta e de outra tarefa; recusada.")
    status = task.get("status")
    state = status.get("state") if isinstance(status, dict) else None
    if state not in STATES:
        raise _invalid("Estado de tarefa desconhecido.")
    if previous_state is not None and state != previous_state and state not in _NEXT.get(previous_state, ()):
        raise _invalid("Mudanca de estado nao permitida para a tarefa.")
    text = redact_secrets("\n".join(_artifact_text(task.get("artifacts"))))
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        text = text.encode("utf-8")[:MAX_RESULT_BYTES].decode("utf-8", "ignore") + " [...truncado]"
    return {"task_id": task_id, "context_id": context_id, "state": state, "local_state": LOCAL_STATE[state],
            "terminal": state in TERMINAL, "text": text}


# -- transport and client ------------------------------------------------------------------------------------------------------
class A2ATransport(ABC):
    """One JSON-RPC call to a peer. Implementations raise ``A2AUnavailable`` when the call may or may not have arrived (timeout, lost
    connection) and ``A2ARefused`` for authentication or version refusals; they never put a credential in an exception message."""

    @abstractmethod
    def call(self, method, params, *, version=A2A_VERSION):
        """Return the JSON-RPC ``result`` (a dict) or raise ``A2AError``."""


class A2AClient:
    """Send a task to a peer, read it back and cancel it, applying every rule above. Holds no state between calls."""

    def __init__(self, transport):
        self.transport = transport

    def _call(self, method, params):
        try:
            result = self.transport.call(method, params, version=A2A_VERSION)
        except A2AError:
            raise
        except Exception as error:  # a transport bug or network error: only the kind is reported, never the text
            raise A2AUnavailable(f"Falha ao falar com o agente externo ({type(error).__name__}); o resultado e desconhecido.") from None
        if not isinstance(result, dict):
            raise _invalid("Resposta invalida do agente.")
        return result

    def send(self, request):
        """``SendMessage``: the peer answers with a task (this adapter does not accept a bare message as the result of a delegation)."""
        result = self._call("SendMessage", request)
        if ("task" in result) == ("message" in result):
            raise _invalid("A resposta deve trazer exatamente uma tarefa ou uma mensagem.")
        if "task" not in result:
            raise _invalid("O agente respondeu com mensagem, nao com tarefa; nada foi aceito.")
        return read_task(result["task"], expect_context_id=request["message"]["contextId"])

    def get(self, task_id, *, context_id, previous_state=None):
        result = self._call("GetTask", {"id": _id(task_id, "taskId")})
        return read_task(result, expect_task_id=task_id, expect_context_id=context_id, previous_state=previous_state)

    def cancel(self, task_id, *, context_id, previous_state=None):
        """``CancelTask`` is best effort: the peer may refuse (already terminal). The returned state is what to believe, not the request."""
        result = self._call("CancelTask", {"id": _id(task_id, "taskId")})
        return read_task(result, expect_task_id=task_id, expect_context_id=context_id, previous_state=previous_state)
