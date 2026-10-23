"""State and vocabulary enums. Values are stable wire strings (pt-BR, as in the docs)."""

CASE_STATES = (
    "recebida", "triagem", "em_execucao", "aguardando_cliente", "aguardando_aprovacao",
    "aguardando_subtarefa", "pausada", "resolvida", "falhou", "cancelada",
)
# Allowed edges. T09 enforces them with revision checks; reopening a closed case is explicit.
_WAITING = ("aguardando_cliente", "aguardando_aprovacao", "aguardando_subtarefa")
CASE_TRANSITIONS = {
    "recebida": ("triagem", "cancelada"),
    "triagem": ("em_execucao", "aguardando_cliente", "pausada", "falhou", "cancelada"),
    "em_execucao": _WAITING + ("pausada", "resolvida", "falhou", "cancelada"),
    "aguardando_cliente": ("em_execucao", "pausada", "resolvida", "cancelada"),
    "aguardando_aprovacao": ("em_execucao", "pausada", "falhou", "cancelada"),
    "aguardando_subtarefa": ("em_execucao", "pausada", "falhou", "cancelada"),
    "pausada": ("em_execucao", "triagem", "cancelada"),
    "resolvida": ("em_execucao",),
    "falhou": ("triagem", "em_execucao"),
    "cancelada": (),
}
TASK_STATES = ("pendente", "em_execucao", "aguardando", "concluida", "falhou", "cancelada")
RUN_STATES = ("enfileirada", "em_execucao", "pausada", "concluida", "falhou", "cancelada", "expirada")
APPROVAL_STATES = ("pendente", "aprovada", "negada", "expirada", "consumida", "cancelada")
EFFECT_OUTCOMES = ("desconhecido", "confirmado", "falhou")
HANDOFF_KINDS = ("delegacao", "transferencia")
HANDOFF_STATES = ("proposto", "aceito", "recusado", "expirado", "devolvido", "cancelado")
OPERATIONS = ("read", "write", "send", "spend")
CONNECTION_KINDS = ("mcp", "imap", "smtp", "http", "outro")
MCP_TRANSPORTS = ("http", "sse", "stdio", "local")
STATUSES = ("ativo", "inativo")
CREDENTIAL_STORES = ("cofre-local", "elevenlabs-legado")
CREDENTIAL_STATES = ("ativa", "revogada")
ROUTING_POLICIES = ("encaminhar", "esclarecer")
RISK_TAGS = ("dados_pessoais", "dados_financeiros", "leitura_email", "envio_externo", "pagamento", "alteracao_dominio")
LLM_PROVIDERS = ("ollama", "externo")
PRIORITIES = ("baixa", "normal", "alta", "urgente")
ORIGINS = ("operador", "chat", "email", "agendamento", "agente")
EVIDENCE_TYPES = ("fato", "resultado_ferramenta", "recibo", "documento")
RELIABILITY = ("verificada", "declarada", "inferida")
ENTRY_TYPES = ("nota_interna", "mensagem_externa", "resumo_ia")
DIRECTIONS = ("entrada", "saida")
TRIAGE_DECISIONS = ("rotear", "misto", "esclarecer", "fila_humana")
TRIAGE_METHODS = ("regra", "llm")
MESSAGE_STATES = ("rascunho", "aguardando_aprovacao", "enviando", "enviada", "falhou", "desconhecido", "bloqueada", "cancelada")
OUTBOUND_CHANNELS = ("email", "chat")
DOC_STATUSES = ("publicado", "substituido", "revogado", "expirado", "excluido")
MEMORY_SCOPES = ("empresa", "departamento", "cliente")
MEMORY_KINDS = ("preferencia", "fato")
MEMORY_STATES = ("proposta", "confirmada", "rejeitada", "expirada")
ENTRY_CHANNELS = ("chat", "email", "telefone", "interno")
CHANNELS = ("email", "telefone")
# observado: remetente visto numa mensagem, sem identidade; vinculado: o operador associou a um contato.
LINK_STATES = ("observado", "vinculado")
RELATIONS = ("cliente", "fornecedor", "interno", "outro")
ACCESS_ACTIONS = ("read", "write", "relate", "export")
# Recursos que uma concessao pode alcancar (empresa e a propria concessao nunca sao concediveis a agentes).
RESOURCE_KINDS = (
    "department", "agent_definition", "agent_run", "skill_grant", "connection", "case", "task",
    "approval_request", "handoff", "evidence", "case_entry", "outbound_message", "agent_profile_version", "contact", "contact_address", "contact_link", "event",
)

ENUMS = {
    "CaseState": CASE_STATES, "TaskState": TASK_STATES, "RunState": RUN_STATES,
    "ApprovalState": APPROVAL_STATES, "HandoffKind": HANDOFF_KINDS, "HandoffState": HANDOFF_STATES,
    "Operation": OPERATIONS, "ConnectionKind": CONNECTION_KINDS, "McpTransport": MCP_TRANSPORTS,
    "Status": STATUSES, "DocStatus": DOC_STATUSES, "MemoryScope": MEMORY_SCOPES, "MemoryKind": MEMORY_KINDS,
    "MemoryState": MEMORY_STATES, "MessageState": MESSAGE_STATES, "OutboundChannel": OUTBOUND_CHANNELS, "EffectOutcome": EFFECT_OUTCOMES, "TriageDecision": TRIAGE_DECISIONS, "TriageMethod": TRIAGE_METHODS, "Priority": PRIORITIES, "Origin": ORIGINS, "EvidenceType": EVIDENCE_TYPES,
    "Reliability": RELIABILITY, "EntryType": ENTRY_TYPES, "Direction": DIRECTIONS, "EntryChannel": ENTRY_CHANNELS, "RiskTag": RISK_TAGS, "RoutingPolicy": ROUTING_POLICIES, "CredentialStore": CREDENTIAL_STORES, "CredentialState": CREDENTIAL_STATES, "Channel": CHANNELS, "LinkState": LINK_STATES, "Relation": RELATIONS,
    "AccessAction": ACCESS_ACTIONS, "ResourceKind": RESOURCE_KINDS,
}


def can_transition(current: str, target: str) -> bool:
    return target in CASE_TRANSITIONS.get(current, ())
