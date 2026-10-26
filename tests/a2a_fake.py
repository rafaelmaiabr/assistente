"""An in-memory A2A peer for the tests: a small server (tasks by id, messages by messageId) and the transport that reaches it.

The peer advertises a card, requires a bearer token and the 1.0 version header, deduplicates by ``messageId`` and lets a test drive the
task through its states (``advance``), so the client's rules are exercised against a peer that behaves, and against one that does not.
"""
from team import a2a


class FakePeer:
    def __init__(self, token="token-de-teste", skills=("analise",)):
        self.token = token
        self.tasks, self.by_message, self.calls = {}, {}, []
        self.card = {
            "name": "Agente Parceiro", "description": "Agente de teste", "version": "1.0.0",
            "supportedInterfaces": [{"url": "https://parceiro.invalid/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
            "capabilities": {"streaming": False, "pushNotifications": False}, "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"], "skills": [{"id": s, "name": s, "description": f"Skill {s}"} for s in skills]}

    def handle(self, method, params, *, version, token):
        self.calls.append((method, params))
        if version != "1.0":
            raise a2a.A2ARefused("VersionNotSupportedError", "Versao nao suportada.")
        if token != self.token:
            raise a2a.A2ARefused("Unauthenticated", "Credencial recusada.")
        if method == "SendMessage":
            message = params["message"]
            if message["messageId"] in self.by_message:  # a retry: the same task, not a second one
                return {"task": self.tasks[self.by_message[message["messageId"]]]}
            task_id = f"t-{len(self.tasks) + 1}"
            self.by_message[message["messageId"]] = task_id
            self.tasks[task_id] = {"id": task_id, "contextId": message["contextId"], "status": {"state": a2a.SUBMITTED}, "artifacts": []}
            return {"task": self.tasks[task_id]}
        task = self.tasks.get(params.get("id"))
        if task is None:
            raise a2a.A2AError("TaskNotFoundError", "Tarefa nao encontrada.")
        if method == "GetTask":
            return task
        if method == "CancelTask":
            if task["status"]["state"] in a2a.TERMINAL:
                raise a2a.A2AError("TaskNotCancelableError", "Tarefa ja encerrada.")
            task["status"]["state"] = a2a.CANCELED
            return task
        raise a2a.A2AError("MethodNotFoundError", "Metodo desconhecido.")

    def advance(self, task_id, state, text=None):
        task = self.tasks[task_id]
        task["status"]["state"] = state
        if text is not None:
            task["artifacts"] = [{"artifactId": "a1", "parts": [{"text": text}]}]


class MemoryTransport(a2a.A2ATransport):
    def __init__(self, peer, token="token-de-teste", online=True):
        self.peer, self.token, self.online = peer, token, online

    def call(self, method, params, *, version=a2a.A2A_VERSION):
        if not self.online:
            raise a2a.A2AUnavailable()
        return self.peer.handle(method, params, version=version, token=self.token)
