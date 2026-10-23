"""Capability gaps: what the team was asked to do and could not, and the person's decision about it.

An agent that has no instruction (a skill in .md) and no tool for a request does not improvise: it records a gap, the case
pauses and waits for a person. The person chooses between writing a skill, building a tool (a follow-up task, tracked outside
this module) and declining; a refusal is kept as a confirmed memory so triage recognises the same request next time.
Recording a gap executes nothing and grants nothing. Grouping is by shared terms of the topic, not by meaning.
"""
import re
import unicodedata

from .access import AccessContext, NotFound, PermissionDenied
from .cases import TERMINAL_STATES, CaseRejected, _now, clean_text
from .enums import GAP_DECISIONS, GAP_TYPES, can_transition
from .ids import new_id
from .repository import RevisionConflict
from .schema import find_secrets

MAX_RELATED = 20
MAX_TRIED = 10
TOPIC_TERMS = 4
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset("""para como com sem uma uns umas dos das nos nas por que quando onde isso esse essa este esta aqui
    ele ela eles elas meu minha seu sua nao sim mais muito pode preciso preciso quero queria favor ajuda cliente
    sobre ser tem tenho foi era the and for with""".split())


class GapRejected(ValueError):
    """The request is refused; the message is safe to show."""


def terms(text):
    """Distinct significant words of ``text``, lower-case and without accents."""
    plain = unicodedata.normalize("NFKD", str(text).lower()).encode("ascii", "ignore").decode()
    found = []
    for word in _WORD.findall(plain):
        if word not in _STOP and word not in found:
            found.append(word)
    return found


def topic_of(requested, topic=None):
    """Stable key of the subject: the first significant terms, sorted. An explicit short topic wins over the text."""
    words = terms(topic if topic else requested)[:TOPIC_TERMS]
    if not words:
        raise GapRejected("Informe o assunto do pedido com palavras que identifiquem a necessidade.")
    return "-".join(sorted(words))[:64].strip("-")


def declined_match(session, text):
    """The declined gap whose topic terms all appear in ``text`` and whose memory is still confirmed, else ``None``."""
    present = set(terms(text))
    for gap in session.list("capability_gap", state="decidida", limit=1000):
        if gap.get("decision") != "nao_atender" or not gap.get("memory_id"):
            continue
        memory = session.get("memory_item", gap["memory_id"])
        if memory is None or memory["state"] != "confirmada":
            continue
        wanted = set(gap["topic"].split("-"))
        if wanted and wanted <= present:
            return gap
    return None


class GapService:
    def __init__(self, repository, access, cases, knowledge):
        self.repository, self.access, self.cases, self.knowledge = repository, access, cases, knowledge

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _ctx(self, ctx):
        if not isinstance(ctx, AccessContext):
            raise PermissionDenied()

    def _operator(self, ctx):
        self._ctx(ctx)
        if not self.access.is_operator(ctx, "admin"):
            raise PermissionDenied("apenas o operador confiavel ou um administrador decide sobre lacunas")

    @staticmethod
    def _text(value, limit, what):
        if not isinstance(value, str):
            raise GapRejected(f"{what} invalido.")
        try:
            text = clean_text(value)[:limit]
        except CaseRejected:
            raise GapRejected(f"{what} vazio.") from None
        if find_secrets({"texto": text}):
            raise GapRejected(f"{what} parece conter um segredo; remova-o.")
        return text

    def _gap(self, session, gap_id):
        gap = session.get("capability_gap", gap_id) if isinstance(gap_id, str) else None
        if gap is None:
            raise NotFound()
        return gap

    def _save(self, session, gap, **changes):
        return session.save({**gap, **changes, "revision": gap["revision"] + 1, "updated_at": _now()})

    def _note(self, ctx, session, case_id, text):
        self.cases.commit_note(ctx, session, case_id, text)

    def _each_case(self, session, gap):
        for case_id in gap["related_case_ids"]:
            case = session.get("case", case_id)
            if case is not None and case["state"] not in TERMINAL_STATES:
                yield case

    # -- reporting -----------------------------------------------------------------------------------------------------
    def report(self, ctx, case_id, requested, *, tried=(), gap_type="ferramenta", topic=None):
        """Declare that the team cannot do ``requested``. The case pauses; nothing else happens.

        Only the case's current owner (an agent) or the trusted operator may declare it. An open gap on the same topic is
        reused: it counts one more occurrence and links this case, so repeated needs show up as one decision to make.
        """
        self._ctx(ctx)
        if gap_type not in GAP_TYPES:
            raise GapRejected("Tipo de lacuna desconhecido.")
        requested = self._text(requested, 500, "O pedido")
        if not isinstance(tried, (list, tuple)) or len(tried) > MAX_TRIED:
            raise GapRejected("Informe no maximo %d tentativas." % MAX_TRIED)
        tried = [self._text(item, 200, "A tentativa") for item in tried]
        key = topic_of(requested, topic)
        with self.repository.transaction(ctx.tenant_id) as session:
            case = session.get("case", case_id) if isinstance(case_id, str) else None
            if case is None or not self.access.allowed(ctx, session, "write", "case", case_id):
                raise NotFound()
            if ctx.role == "operator" and not self.access.is_operator(ctx, "atendente"):
                raise PermissionDenied("apenas o operador confiavel ou quem atende registra lacunas")
            self.cases._owner_only(ctx, case)
            if case["state"] in TERMINAL_STATES:
                raise GapRejected("A demanda esta encerrada.")
            now = _now()
            existing = next((g for g in session.list("capability_gap", state="aberta", topic=key, limit=1000)), None)
            if existing is not None:
                related = existing["related_case_ids"]
                if case_id in related:
                    gap = existing  # the same case reporting again changes nothing
                else:
                    if len(related) >= MAX_RELATED:
                        raise GapRejected("Esta lacuna ja reune demandas demais; decida-a antes de registrar outra.")
                    gap = self._save(session, existing, occurrences=existing["occurrences"] + 1,
                                     related_case_ids=related + [case_id])
            else:
                gap = session.save({
                    "kind": "capability_gap", "schema_version": 1, "id": new_id("capability_gap"), "tenant_id": ctx.tenant_id,
                    "revision": 1, "created_at": now, "updated_at": now, "case_id": case_id, "reported_by": ctx.principal_id,
                    "requested": requested, "tried": tried, "gap_type": gap_type, "topic": key, "occurrences": 1,
                    "related_case_ids": [case_id], "state": "aberta", "decision": None, "decision_note": None,
                    "decided_by": None, "decided_at": None, "follow_up": None, "memory_id": None, "fulfilled_at": None})
            self._pause(ctx, session, case, gap)
            return gap

    def _pause(self, ctx, session, case, gap):
        """Stop the case until a person decides. A case without an owner (initial queue) keeps its state and only gets the note."""
        note = f"Lacuna de capacidade ({gap['gap_type']}): {gap['requested'][:300]}. Aguardando decisao de uma pessoa."
        if case["owner_agent_id"] is None:
            pass  # still in the initial queue: it already waits for a person, and a paused case needs an owner
        elif case["state"] != "pausada":
            if not can_transition(case["state"], "pausada"):
                raise GapRejected("A demanda nao pode ser pausada neste estado.")
            self.cases.commit_change(ctx, session, case, {"state": "pausada"}, "case.state-changed",
                                     {"from": case["state"], "to": "pausada", "reason": "lacuna de capacidade",
                                      "gap_id": gap["id"]})
        self._note(ctx, session, case["id"], note)

    # -- deciding -----------------------------------------------------------------------------------------------------
    def decide(self, ctx, gap_id, expected_revision, decision, note, *, follow_up=None):
        """The operator's call: write a skill, build a tool or do not serve this request. Always with a reason."""
        self._operator(ctx)
        if decision not in GAP_DECISIONS:
            raise GapRejected("Decisao desconhecida.")
        note = self._text(note, 1000, "A justificativa")
        follow_up = self._text(follow_up, 200, "A referencia") if follow_up else None
        with self.repository.transaction(ctx.tenant_id) as session:
            gap = self._gap(session, gap_id)
        if gap["state"] != "aberta":
            raise GapRejected("Esta lacuna ja foi decidida.")
        if gap["revision"] != expected_revision:
            raise RevisionConflict("revisao desatualizada")
        memory_id = None
        if decision == "nao_atender":
            memory = self.knowledge.remember(
                ctx, scope="empresa", memory_kind="preferencia", source=f"lacuna:{gap_id}",
                text=f"Fora do escopo ({gap['topic']}): {gap['requested'][:300]}. Nao atender; encaminhar a uma pessoa.")
            memory_id = memory["id"]
        try:
            with self.repository.transaction(ctx.tenant_id) as session:
                fresh = self._gap(session, gap_id)
                if fresh["revision"] != expected_revision or fresh["state"] != "aberta":
                    raise RevisionConflict("revisao desatualizada")
                saved = self._save(session, fresh, state="decidida", decision=decision, decision_note=note,
                                   decided_by=ctx.principal_id, decided_at=_now(), follow_up=follow_up, memory_id=memory_id)
                for case in self._each_case(session, saved):
                    self._apply(ctx, session, case, saved, decision, note)
                return saved
        except Exception:
            if memory_id is not None:
                self.knowledge.forget(ctx, memory_id)
            raise

    def _apply(self, ctx, session, case, gap, decision, note):
        if decision == "nao_atender":
            reason = f"Fora do escopo: {note}"[:400]
            self.cases.commit_change(ctx, session, case, {"state": "cancelada", "closed_at": _now()}, "case.state-changed",
                                     {"from": case["state"], "to": "cancelada", "reason": reason, "gap_id": gap["id"]})
            self._note(ctx, session, case["id"], f"Decisao sobre a lacuna: nao atender. {note}")
        else:
            what = "criar uma skill (instrucao)" if decision == "criar_skill" else "criar uma ferramenta"
            tail = f" Acompanhamento: {gap['follow_up']}." if gap.get("follow_up") else ""
            self._note(ctx, session, case["id"], f"Decisao sobre a lacuna: {what}. {note}{tail} A demanda continua pausada.")

    def fulfill(self, ctx, gap_id, expected_revision, note, *, resume=True):
        """The skill or tool now exists: close the gap and, if asked, resume the cases that were waiting for it."""
        self._operator(ctx)
        note = self._text(note, 1000, "A observacao")
        with self.repository.transaction(ctx.tenant_id) as session:
            gap = self._gap(session, gap_id)
            if gap["state"] != "decidida" or gap["decision"] == "nao_atender":
                raise GapRejected("So uma lacuna com skill ou ferramenta decididas pode ser marcada como atendida.")
            if gap["revision"] != expected_revision:
                raise RevisionConflict("revisao desatualizada")
            saved = self._save(session, gap, state="atendida", fulfilled_at=_now())
            for case in self._each_case(session, saved):
                if resume and case["state"] == "pausada" and not case.get("operator_control"):
                    self.cases.commit_change(ctx, session, case, {"state": "em_execucao"}, "case.state-changed",
                                             {"from": "pausada", "to": "em_execucao", "reason": "capacidade disponivel",
                                              "gap_id": gap_id})
                self._note(ctx, session, case["id"], f"Capacidade disponivel. {note}")
            return saved

    # -- reading --------------------------------------------------------------------------------------------------------
    def get(self, ctx, gap_id):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            return self._gap(session, gap_id)

    def list_gaps(self, ctx, state=None):
        self._operator(ctx)
        with self.repository.transaction(ctx.tenant_id) as session:
            gaps = session.list("capability_gap", limit=1000, **({"state": state} if state else {}))
        return sorted(gaps, key=lambda g: (-g["occurrences"], g["created_at"], g["id"]))
