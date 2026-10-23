"""Local HTTP API for the team panel (companies, departments, agents, skills, connections).

Same-origin only (the web server checks the exact Origin for every ``/api/team`` request, GET
included), JSON bodies only, small limits. Every company-scoped route carries the company id in the
path and builds its access context on the server; a company the operator does not have, or any id
that is not visible, answers with the same 404 and never reveals another company's data. Credentials
are write-only: no response contains a secret, only its state. Errors are sanitized sentences.
"""
import dataclasses
import json
import tempfile
from pathlib import Path

from aiohttp import web

from .access import AccessError, NotFound, PermissionDenied
from .agents import ProfileRejected
from . import overview
from .approvals import ApprovalInvalid, ApprovalRejected
from .cases import CaseRejected, InvalidTransition
from .handoffs import HandoffRejected
from .outbound import OutboundError
from .connections import ConnectionRejected
from .llm import PolicyRejected
from .repository import RevisionConflict
from .schema import ContractError
from .skills import SkillRejected

MAX_BODY = 64 * 1024
TEAM = web.AppKey("team_app", object)
DEMOS = Path(__file__).resolve().parent / "skills_demo"
SAFE_ERRORS = (ProfileRejected, SkillRejected, ConnectionRejected, PolicyRejected, CaseRejected, ApprovalRejected,
               HandoffRejected, OutboundError)


def _json(data, status=200):
    return web.json_response(data, status=status)


def _error(message, status, code=None):
    return web.json_response({"error": message, **({"code": code} if code else {})}, status=status)


def api(handler):
    """Translate domain errors into short, safe JSON answers."""

    async def wrapper(request):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except NotFound:
            return _error("Recurso nao encontrado.", 404)
        except PermissionDenied:
            return _error("Acao nao permitida.", 403)
        except RevisionConflict:
            return _error("Os dados mudaram; recarregue e tente novamente.", 409, "conflict")
        except ContractError as error:
            return web.json_response({"error": "Dados invalidos.", "details": error.errors[:6]}, status=400)
        except SAFE_ERRORS as error:
            return _error(str(error), 400)
        except AccessError as error:
            return _error(str(error), 400)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return _error("Requisicao invalida.", 400)
        except Exception:
            return _error("Nao foi possivel concluir. Tente novamente.", 500)

    return wrapper


async def _body(request):
    if request.content_length is None or request.content_length > MAX_BODY:
        raise ValueError("body")
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError("body")
    return data


def _services(request):
    services = request.app[TEAM].services()
    if services is None:
        raise web.HTTPConflict(text=json.dumps({"error": "Ative o atendimento persistente para usar a equipe.",
                                                "code": "persistence_disabled"}), content_type="application/json")
    return services


def _tenant(request):
    services = _services(request)
    return services, services.access.tenant_context(services.operator, request.match_info["tenant"])


def _fields(data, allowed):
    unknown = set(data) - set(allowed)
    if unknown:
        raise ValueError("campos")
    return {key: data[key] for key in allowed if key in data}


# -- state and companies ------------------------------------------------------------------------------------------------
@api
async def state(request):
    app = request.app[TEAM]
    if not app.enabled():
        return _json({"enabled": False, "companies": []})
    services = _services(request)
    return _json({"enabled": True, "companies": services.team.list_companies(services.operator),
                  "voices": app.voice_profiles()})


@api
async def create_company(request):
    services = _services(request)
    data = _fields(await _body(request), ("name",))
    return _json(services.team.create_company(services.operator, data["name"]), 201)


@api
async def update_company(request):
    services = _services(request)
    data = await _body(request)
    revision = data.pop("expected_revision")
    company = services.team.update_company(services.operator, request.match_info["tenant"], revision,
                                           **_fields(data, ("name", "status")))
    return _json(company)


# -- departments ---------------------------------------------------------------------------------------------------------
@api
async def list_departments(request):
    services, ctx = _tenant(request)
    return _json(services.team.list(ctx, "department"))


@api
async def create_department(request):
    services, ctx = _tenant(request)
    data = _fields(await _body(request), ("name", "scope"))
    return _json(services.team.create(ctx, "department", status="ativo", **data), 201)


@api
async def update_department(request):
    services, ctx = _tenant(request)
    data = await _body(request)
    revision = data.pop("expected_revision")
    return _json(services.team.update(ctx, "department", request.match_info["id"], revision,
                                      **_fields(data, ("name", "scope", "status"))))


# -- agents ----------------------------------------------------------------------------------------------------------------
AGENT_FIELDS = ("department_id", "name", "goals", "personality", "scope", "out_of_scope", "routing_policy",
                "model_policy_ref", "voice_profile_ref", "handoff_target_ids", "capabilities", "status")


@api
async def agent_options(request):
    services, ctx = _tenant(request)
    return _json({"voices": request.app[TEAM].voice_profiles(),
                  "model_policies": sorted(services.policies.names_for(ctx.tenant_id)),
                  "routing_policies": ["encaminhar", "esclarecer"]})


@api
async def list_agents(request):
    services, ctx = _tenant(request)
    return _json(services.agents.list_agents(ctx))


@api
async def create_agent(request):
    services, ctx = _tenant(request)
    data = _fields(await _body(request), AGENT_FIELDS)
    return _json(services.agents.create_agent(ctx, **{k: v for k, v in data.items() if k != "status"}), 201)


@api
async def validate_agent(request):
    services, ctx = _tenant(request)
    data = await _body(request)
    agent_id = data.pop("agent_id", None)
    services.agents.validate(ctx, agent_id=agent_id, **_fields(data, AGENT_FIELDS))
    return _json({"valid": True})


@api
async def update_agent(request):
    services, ctx = _tenant(request)
    data = await _body(request)
    revision = data.pop("expected_revision")
    return _json(services.agents.update_agent(ctx, request.match_info["id"], revision, **_fields(data, AGENT_FIELDS)))


@api
async def agent_versions(request):
    services, ctx = _tenant(request)
    versions = services.agents.list_versions(ctx, request.match_info["id"])
    return _json([{k: v[k] for k in ("id", "agent_version", "status", "content_hash", "created_at", "definition")}
                  for v in versions])


@api
async def set_version_status(request):
    services, ctx = _tenant(request)
    data = _fields(await _body(request), ("status",))
    version = services.agents.set_version_status(ctx, request.match_info["id"], int(request.match_info["version"]),
                                                data["status"])
    return _json({k: version[k] for k in ("id", "agent_version", "status")})


# -- skills ----------------------------------------------------------------------------------------------------------------
def _skill_view(skill):
    keys = ("id", "skill_key", "skill_version", "description", "risks", "tool_operations", "operations", "settings",
            "fictitious", "status", "content_hash", "revision")
    return {k: skill.get(k) for k in keys}


def _package(data, directory):
    """A reviewable package from a built-in demo name or a manifest object sent by the panel."""
    if "demo" in data:
        demo = DEMOS / str(data["demo"])
        if not (demo.is_dir() and demo.parent == DEMOS):
            raise NotFound()
        return demo
    manifest = data.get("manifest")
    if not isinstance(manifest, dict) or "instructions_file" in manifest:
        raise ValueError("manifest")
    folder = Path(directory)
    (folder / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def _review(skills, data):
    with tempfile.TemporaryDirectory() as directory:
        review = skills.review_package(_package(data, directory))
    return dataclasses.asdict(review)


@api
async def list_skills(request):
    services, _ = _tenant(request)
    return _json([_skill_view(s) for s in services.skills.list_skills(services.operator)])


@api
async def demo_skills(request):
    services, _ = _tenant(request)
    return _json([_review(services.skills, {"demo": d.name}) | {"demo": d.name} for d in sorted(DEMOS.iterdir())])


@api
async def review_skill(request):
    services, _ = _tenant(request)
    return _json(_review(services.skills, await _body(request)))


@api
async def install_skill(request):
    services, _ = _tenant(request)
    data = await _body(request)
    with tempfile.TemporaryDirectory() as directory:
        skill = services.skills.install(services.operator, _package(data, directory), data["approved_hash"],
                                        accept_new_permissions=data.get("accept_new_permissions") is True)
    return _json(_skill_view(skill), 201)


@api
async def set_skill_status(request):
    services, _ = _tenant(request)
    data = _fields(await _body(request), ("status",))
    return _json(_skill_view(services.skills.set_skill_status(services.operator, request.match_info["id"], data["status"])))


@api
async def list_grants(request):
    services, ctx = _tenant(request)
    return _json(services.skills.list_grants(ctx, request.query.get("agent_id")))


@api
async def create_grant(request):
    services, ctx = _tenant(request)
    data = _fields(await _body(request), ("agent_id", "skill_id", "operations", "connection_id", "resource", "attach"))
    return _json(services.skills.grant_skill(ctx, **data), 201)


@api
async def revoke_grant(request):
    services, ctx = _tenant(request)
    return _json(services.skills.revoke_grant(ctx, request.match_info["id"]))


# -- connections -------------------------------------------------------------------------------------------------------------
@api
async def list_connections(request):
    services, ctx = _tenant(request)
    return _json(services.connections.list_connections(ctx))


@api
async def create_connection(request):
    services, ctx = _tenant(request)
    data = _fields(await _body(request), ("label", "provider", "connection_kind", "catalog_entry_id", "scopes"))
    return _json(services.connections.create_connection(ctx, **data), 201)


@api
async def update_connection(request):
    services, ctx = _tenant(request)
    data = await _body(request)
    revision = data.pop("expected_revision")
    return _json(services.connections.update_connection(ctx, request.match_info["id"], revision,
                                                         **_fields(data, ("label", "status", "scopes"))))


@api
async def set_credential(request):
    services, ctx = _tenant(request)
    data = _fields(await _body(request), ("secret",))
    return _json(services.connections.set_credential(ctx, request.match_info["id"], data["secret"]))


@api
async def credential_action(request):
    services, ctx = _tenant(request)
    action = request.match_info["action"]
    if action == "revoke":
        return _json(services.connections.revoke_credential(ctx, request.match_info["id"]))
    if action == "status":
        return _json(services.connections.credential_status(ctx, request.match_info["id"]))
    raise NotFound()


@api
async def remove_credential(request):
    services, ctx = _tenant(request)
    return _json(services.connections.remove_credential(ctx, request.match_info["id"]))


@api
async def mcp_catalog(request):
    services, ctx = _tenant(request)
    return _json([{k: e[k] for k in ("id", "catalog_key", "entry_version", "transport", "url", "tools", "status", "revision")}
                  for e in services.connections.list_mcp_catalog(ctx)])


@api
async def approve_mcp(request):
    services, _ = _tenant(request)
    data = _fields(await _body(request), ("catalog_key", "entry_version", "transport", "url", "tools", "tool_specs"))
    return _json(services.connections.approve_mcp_server(services.operator, **data), 201)


@api
async def set_mcp_status(request):
    services, _ = _tenant(request)
    data = await _body(request)
    entry = services.connections.set_mcp_status(services.operator, request.match_info["id"], data["expected_revision"],
                                                data["status"])
    return _json({"id": entry["id"], "status": entry["status"], "revision": entry["revision"]})


# -- cases center -----------------------------------------------------------------------------------------------------------
@api
async def cases_list(request):
    services, ctx = _tenant(request)
    q = request.query
    return _json(overview.list_cases(services, ctx, state=q.get("state"), phase=q.get("phase")))


@api
async def case_open(request):
    services, ctx = _tenant(request)
    data = _fields(await _body(request), ("department_id", "objective", "customer_ref", "priority", "origin"))
    return _json(services.cases.open_case(ctx, **data), 201)


@api
async def case_snapshot(request):
    services, ctx = _tenant(request)
    return _json(overview.snapshot(services, ctx, request.match_info["id"]))


@api
async def events_feed(request):
    services, ctx = _tenant(request)
    return _json(overview.events_after(services, ctx, int(request.query.get("after", "0")), request.query.get("case_id")))


@api
async def attention_inbox(request):
    services, ctx = _tenant(request)
    return _json(overview.attention(services, ctx))


@api
async def case_control(request):
    """Pause, take over, reject or return a case. Always with the expected revision and a reason."""
    services, ctx = _tenant(request)
    action = request.match_info["action"]
    data = _fields(await _body(request), ("expected_revision", "reason"))
    method = {"pause": services.control.pause, "take-over": services.control.take_over, "reject": services.control.reject,
              "return": services.control.return_to_team}.get(action)
    if method is None:
        raise NotFound()
    method(ctx, request.match_info["id"], data["expected_revision"], data["reason"])
    return _json(overview.snapshot(services, ctx, request.match_info["id"]))


@api
async def case_assign(request):
    services, ctx = _tenant(request)
    data = _fields(await _body(request), ("expected_revision", "agent_id"))
    services.cases.assign_owner(ctx, request.match_info["id"], data["expected_revision"], data["agent_id"])
    return _json(overview.snapshot(services, ctx, request.match_info["id"]))


@api
async def approval_decision(request):
    services, ctx = _tenant(request)
    action, approval_id = request.match_info["action"], request.match_info["id"]
    data = await _body(request)
    if action == "approve":
        done = services.approvals.approve(ctx, approval_id, data["shown_hash"], data.get("comment"))
    elif action == "deny":
        done = services.approvals.deny(ctx, approval_id, data["reason"])
    elif action == "revoke":
        done = services.approvals.revoke(ctx, approval_id, data["reason"])
    elif action == "resolve-effect":
        done = services.approvals.resolve_effect(ctx, approval_id, data["outcome"], data["detail"])
    else:
        raise NotFound()
    return _json(overview.snapshot(services, ctx, done["case_id"]))


@api
async def message_resolve(request):
    services, ctx = _tenant(request)
    data = _fields(await _body(request), ("outcome", "provider_id", "detail"))
    message = services.outbound.resolve_unknown(ctx, request.match_info["id"], data["outcome"],
                                                data.get("provider_id"), data.get("detail"))
    return _json(overview.snapshot(services, ctx, message["case_id"]))


def register(app, team_app):
    app[TEAM] = team_app
    base = "/api/team"
    r = app.router
    r.add_get(f"{base}/state", state)
    r.add_post(f"{base}/companies", create_company)
    r.add_patch(f"{base}/companies/{{tenant}}", update_company)
    t = f"{base}/companies/{{tenant}}"
    r.add_get(f"{t}/departments", list_departments)
    r.add_post(f"{t}/departments", create_department)
    r.add_patch(f"{t}/departments/{{id}}", update_department)
    r.add_get(f"{t}/agent-options", agent_options)
    r.add_get(f"{t}/agents", list_agents)
    r.add_post(f"{t}/agents", create_agent)
    r.add_post(f"{t}/agents/validate", validate_agent)
    r.add_patch(f"{t}/agents/{{id}}", update_agent)
    r.add_get(f"{t}/agents/{{id}}/versions", agent_versions)
    r.add_patch(f"{t}/agents/{{id}}/versions/{{version}}", set_version_status)
    r.add_get(f"{t}/skills", list_skills)
    r.add_get(f"{t}/skills/demos", demo_skills)
    r.add_post(f"{t}/skills/review", review_skill)
    r.add_post(f"{t}/skills/install", install_skill)
    r.add_patch(f"{t}/skills/{{id}}", set_skill_status)
    r.add_get(f"{t}/grants", list_grants)
    r.add_post(f"{t}/grants", create_grant)
    r.add_delete(f"{t}/grants/{{id}}", revoke_grant)
    r.add_get(f"{t}/connections", list_connections)
    r.add_post(f"{t}/connections", create_connection)
    r.add_patch(f"{t}/connections/{{id}}", update_connection)
    r.add_put(f"{t}/connections/{{id}}/credential", set_credential)
    r.add_post(f"{t}/connections/{{id}}/credential/{{action}}", credential_action)
    r.add_delete(f"{t}/connections/{{id}}/credential", remove_credential)
    r.add_get(f"{t}/cases", cases_list)
    r.add_post(f"{t}/cases", case_open)
    r.add_get(f"{t}/cases/{{id}}", case_snapshot)
    r.add_post(f"{t}/cases/{{id}}/assign", case_assign)
    r.add_post(f"{t}/cases/{{id}}/{{action}}", case_control)
    r.add_get(f"{t}/events", events_feed)
    r.add_get(f"{t}/attention", attention_inbox)
    r.add_post(f"{t}/approvals/{{id}}/{{action}}", approval_decision)
    r.add_post(f"{t}/messages/{{id}}/resolve", message_resolve)
    r.add_get(f"{t}/mcp-catalog", mcp_catalog)
    r.add_post(f"{t}/mcp-catalog", approve_mcp)
    r.add_patch(f"{t}/mcp-catalog/{{id}}", set_mcp_status)
