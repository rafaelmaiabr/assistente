import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from export_team_schemas import outputs  # noqa: E402
from team import ContractError, is_id, kinds, new_id, validate, validate_bundle  # noqa: E402
from team.enums import CASE_STATES, CASE_TRANSITIONS, can_transition  # noqa: E402
from team.legacy import import_legacy_config  # noqa: E402
from team.samples import IDS, bundle  # noqa: E402


def doc(kind):
    return copy.deepcopy(next(item for item in bundle() if item["kind"] == kind))


class ContractTests(unittest.TestCase):
    def assertRejects(self, kind, document, text, code=None):
        with self.assertRaises(ContractError) as caught:
            validate(kind, document)
        self.assertIn(text, str(caught.exception))
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_examples_cover_every_kind_and_validate(self):
        docs = validate_bundle(bundle())
        self.assertEqual({item["kind"] for item in docs}, set(kinds()))

    def test_committed_schemas_and_examples_match_specs(self):
        for name, text in outputs().items():
            self.assertEqual((ROOT / "team" / name).read_text(encoding="utf-8"), text, name)

    def test_definition_run_skill_grant_and_connection_are_distinct_shapes(self):
        shapes = [set(doc(kind)) for kind in ("agent_definition", "agent_run", "skill_definition", "skill_grant", "connection")]
        for index, first in enumerate(shapes):
            for second in shapes[index + 1:]:
                self.assertNotEqual(first, second)
        self.assertNotIn("instructions", doc("skill_grant"))  # a grant carries no instructions
        self.assertIn("credential_ref", doc("connection"))

    def test_ids_are_opaque_and_prefixed(self):
        self.assertTrue(is_id(new_id("case"), "cas"))
        self.assertFalse(is_id("financeiro"))
        agent = doc("agent_definition")
        agent["id"] = "financeiro"
        self.assertRejects("agent_definition", agent, "$.id")

    def test_unknown_schema_version_and_unknown_field(self):
        future = doc("case")
        future["schema_version"] = 2
        self.assertRejects("case", future, "schema_version", "unsupported_schema_version")
        extra = doc("case")
        extra["surprise"] = 1
        self.assertRejects("case", extra, "campo desconhecido")

    def test_tenant_and_case_are_required_for_business_data(self):
        for kind in ("department", "case", "task", "approval_request", "handoff", "evidence", "event"):
            missing = doc(kind)
            del missing["tenant_id"]
            self.assertRejects(kind, missing, "tenant_id")
        for kind in ("task", "approval_request", "handoff", "evidence"):
            missing = doc(kind)
            del missing["case_id"]
            self.assertRejects(kind, missing, "case_id")

    def test_envelope_requires_case_except_config_messages(self):
        event = doc("event")
        event["case_id"] = None
        self.assertRejects("event", event, "case_id")
        event["type"] = "config.agent-published"
        validate("event", event)

    def test_envelope_traceability_and_revision(self):
        event = doc("event")
        for field in ("correlation_id", "causation_id", "actor_id", "revision"):
            self.assertIn(field, event)
        event["actor_id"] = IDS["case"]  # not an actor id
        self.assertRejects("event", event, "$.actor_id")
        command = doc("command")
        command["expected_revision"] = 0
        self.assertRejects("command", command, "expected_revision")

    def test_inline_secrets_are_rejected_but_references_are_allowed(self):
        validate("connection", doc("connection"))
        for field, value in (("api_key", "abc"), ("password", "x"), ("label", "usa sk-abcdefghijklmnopqrstuv")):
            leaky = doc("connection")
            leaky[field] = value
            self.assertRejects("connection", leaky, "segredo embutido", "secret")
        userinfo = doc("connection")
        userinfo["endpoint"] = "https://user:pass@host.example/mcp"
        self.assertRejects("connection", userinfo, "segredo embutido", "secret")
        nested = doc("command")
        nested["payload"] = {"headers": {"Authorization": "Bearer abcdefghijklmnop"}}
        self.assertRejects("command", nested, "segredo embutido")
        endpoint = doc("connection")
        endpoint["endpoint"] = "https://host.example/mcp"
        endpoint["credential_ref"] = "senha123"  # a raw value is not a vault reference
        self.assertRejects("connection", endpoint, "credential_ref")

    def test_invalid_state_and_timestamp_without_zone(self):
        case = doc("case")
        case["state"] = "inventado"
        self.assertRejects("case", case, "$.state")
        case = doc("case")
        case["created_at"] = "2026-01-01T10:00:00"
        self.assertRejects("case", case, "$.created_at")

    def test_bundle_rejects_dangling_and_cross_tenant_references(self):
        broken = bundle()
        next(item for item in broken if item["kind"] == "task")["assignee_agent_id"] = new_id("agent_definition")
        with self.assertRaisesRegex(ContractError, "referencia invalida"):
            validate_bundle(broken)
        other = new_id("tenant")
        crossed = bundle() + [dict(doc("tenant"), id=other)]
        foreign = dict(doc("department"), id=new_id("department"), tenant_id=other)
        crossed.append(foreign)
        next(item for item in crossed if item["kind"] == "agent_definition")["department_id"] = foreign["id"]
        with self.assertRaisesRegex(ContractError, "outra empresa"):
            validate_bundle(crossed)

    def test_bundle_rejects_duplicate_ids_and_wrong_kind_target(self):
        with self.assertRaisesRegex(ContractError, "duplicado"):
            validate_bundle(bundle() + [doc("department")])
        wrong = bundle()
        next(item for item in wrong if item["kind"] == "task")["case_id"] = IDS["dep_fin"].replace("dep_", "cas_")
        with self.assertRaisesRegex(ContractError, "referencia invalida"):
            validate_bundle(wrong)

    def test_case_state_graph_is_closed_and_reopening_is_explicit(self):
        for source, targets in CASE_TRANSITIONS.items():
            self.assertIn(source, CASE_STATES)
            self.assertTrue(set(targets) <= set(CASE_STATES))
        self.assertEqual(set(CASE_TRANSITIONS), set(CASE_STATES))
        self.assertFalse(can_transition("cancelada", "em_execucao"))
        self.assertFalse(can_transition("recebida", "resolvida"))
        self.assertTrue(can_transition("resolvida", "em_execucao"))


class LegacyImportTests(unittest.TestCase):
    CONFIG = {"ollama_url": "http://127.0.0.1:11435/v1", "model": "qwen2.5:7b", "language": "pt",
              "mcp_servers": [{"url": "http://127.0.0.1:8000/mcp"}]}

    def test_import_builds_valid_bundle_without_touching_input(self):
        original = copy.deepcopy(self.CONFIG)
        docs = import_legacy_config(self.CONFIG)
        self.assertEqual(self.CONFIG, original)
        self.assertEqual([item["kind"] for item in docs], ["tenant", "department", "agent_definition", "connection"])
        self.assertEqual(docs[3]["endpoint"], "http://127.0.0.1:8000/mcp")
        validate_bundle(docs)

    def test_import_without_servers_and_with_secret_url(self):
        self.assertEqual(len(import_legacy_config({"mcp_servers": []})), 3)
        with self.assertRaises(ContractError):
            import_legacy_config({"mcp_servers": [{"url": "https://u:p@host.example/mcp"}]})

    def test_example_config_is_importable_and_file_is_unchanged(self):
        path = ROOT / "config.example.json"
        before = path.read_bytes()
        import_legacy_config(json.loads(before.decode("utf-8-sig")))
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
