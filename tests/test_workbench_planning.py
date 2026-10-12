"""Planning uses the existing provider path without granting execution authority."""

import asyncio
import json

import pytest

from local_ai_router.providers import ProviderError
from local_ai_router.schema import Generation
from local_ai_router.workbench.policy import CAPABILITIES, Principal
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import WorkbenchError, identity

HUMAN = Principal("operator", ActorType.HUMAN, CAPABILITIES, frozenset({"*"}), True)
SPEC = {
    "problem_statement": "Greeting behavior is unclear",
    "objectives": ["Greeting"],
    "scope": ["Greeting"],
    "functional_requirements": [{"id": "R1", "text": "Implement greet"}],
    "acceptance_criteria": [{"id": "A1", "text": "Blank input greets world"}],
}


@pytest.fixture
async def planning(engine):
    runtime = WorkbenchRuntime(engine)
    service = runtime.service
    workspace = service.seed_defaults()
    project = service.create(
        "project", {"command_id": identity(), "workspace_id": workspace.id, "name": "Planning", "key": "PL"}, HUMAN
    ).body
    yield runtime.planning, workspace, project
    await runtime.close()


def request(workspace, project, **extra):
    return {
        "workspace_id": workspace.id,
        "project_id": project["id"],
        "requirement": "Specify greeting behavior",
        "constraints": ["No network"],
        **extra,
    }


def enqueue(planning, body, command_id=None):
    quote = planning.quote(body, HUMAN)
    return planning.enqueue({**body, "command_id": command_id or identity(), "quote": quote["quote"]}, HUMAN)


async def drain(planning):
    await asyncio.gather(*list(planning.tasks.values()))


async def test_prd_generation_uses_existing_provider_path(planning, monkeypatch):
    planner, workspace, project = planning
    calls = []

    async def generate(model, messages, role, rid, session, budget, **kwargs):
        assert not planner.store.db.in_transaction
        assert role == "planner" and kwargs["schema"]["title"] == "PRDSpec"
        assert "No network" in messages[-1]["content"]
        calls.append((model.id, rid))
        return Generation(text=json.dumps(SPEC))

    monkeypatch.setattr(planner.engine.providers, "generate", generate)
    result = enqueue(planner, request(workspace, project))
    await drain(planner)
    operation = planner.get(workspace.id, result.body["id"], HUMAN)
    assert operation["state"] == "COMPLETED" and len(calls) == 1
    prd = planner.service.get("prd", workspace.id, operation["result"]["prd_id"], HUMAN)
    assert prd.status == "DRAFT" and prd.spec.constraints == ["No network"]
    assert not planner.engine.store.db.execute("SELECT 1 FROM runs").fetchone()


async def test_pinned_planner_actual_call_and_quote_inventory(planning, monkeypatch):
    from local_ai_router.schema import ModelDescriptor

    planner, workspace, project = planning
    planner.engine.settings.models = [
        ModelDescriptor(id=name, provider="mock", deployment_name=name) for name in ("first", "chosen")
    ]
    planner.engine.settings.routing.allow_below_slo = True
    planner.engine.settings.routing.planner_model_candidates = []
    body = request(workspace, project, model_selection={"mode": "pinned", "primary_model_id": "chosen"})
    calls = []

    async def capture(model, *args, **kwargs):
        calls.append(model.id)
        return Generation(text=json.dumps(SPEC))

    monkeypatch.setattr(planner.engine.providers, "_generate_checked", capture)
    quote = planner.quote(body, HUMAN)
    assert quote["eligible_models"] == ["chosen"]
    result = planner.enqueue({**body, "command_id": identity(), "quote": quote["quote"]}, HUMAN)
    await drain(planner)
    assert calls == ["chosen"] and planner.get(workspace.id, result.body["id"], HUMAN)["state"] == "COMPLETED"
    changed = {**body, "model_selection": {"mode": "pinned", "primary_model_id": "first"}}
    with pytest.raises(WorkbenchError):
        planner.enqueue({**changed, "command_id": identity(), "quote": quote["quote"]}, HUMAN)
    planner.engine.settings.models[0].enabled = False
    with pytest.raises(WorkbenchError):
        planner.enqueue({**body, "command_id": identity(), "quote": quote["quote"]}, HUMAN)
    assert not planner.store.db.execute("SELECT 1 FROM wb_attempts").fetchone()


async def test_duplicate_generation_one_operation(planning, monkeypatch):
    planner, workspace, project = planning
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def generate(*args, **kwargs):
        calls.append(1)
        entered.set()
        await release.wait()
        return Generation(text=json.dumps(SPEC))

    monkeypatch.setattr(planner.engine.providers, "generate", generate)
    body = request(workspace, project)
    command = {**body, "command_id": identity(), "quote": planner.quote(body, HUMAN)["quote"]}
    first = planner.enqueue(command, HUMAN)
    await entered.wait()
    second = planner.enqueue(command, HUMAN)
    assert second.body == first.body and second.replayed
    assert planner.get(workspace.id, first.body["id"], HUMAN)["state"] == "RUNNING"
    assert calls == [1]
    release.set()
    await drain(planner)
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_operations").fetchone()[0] == 1
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_prds").fetchone()[0] == 1


@pytest.mark.parametrize("output", ["invalid", "{}", json.dumps({**SPEC, "actor_type": "human"})])
async def test_invalid_model_output_visible(planning, monkeypatch, output):
    planner, workspace, project = planning

    async def generate(*args, **kwargs):
        return Generation(text=output)

    monkeypatch.setattr(planner.engine.providers, "generate", generate)
    result = enqueue(planner, request(workspace, project))
    await drain(planner)
    operation = planner.get(workspace.id, result.body["id"], HUMAN)
    assert operation["state"] == "FAILED" and operation["error_code"] == "PLANNING_FAILED"
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_prds").fetchone()[0] == 0


async def test_constraints_and_privacy_preserved(planning, monkeypatch):
    planner, workspace, project = planning
    body = request(workspace, project)
    quote = planner.quote(body, HUMAN)
    assert quote["privacy"] == "LOCAL_ONLY" and not quote["inference"]
    assert planner.engine.store.costs()["calls"] == 0

    async def generate(*args, **kwargs):
        planner.service.update(
            "project",
            workspace.id,
            project["id"],
            {"command_id": identity(), "expected_revision": 1, "description": "Changed policy context"},
            HUMAN,
        )
        return Generation(text=json.dumps(SPEC))

    monkeypatch.setattr(planner.engine.providers, "generate", generate)
    result = planner.enqueue({**body, "command_id": identity(), "quote": quote["quote"]}, HUMAN)
    await drain(planner)
    operation = planner.get(workspace.id, result.body["id"], HUMAN)
    assert operation["state"] == "FAILED" and operation["error_code"] == "POLICY_CHANGED"
    with pytest.raises(WorkbenchError, match="quote"):
        planner.enqueue({**body, "command_id": identity(), "quote": quote["quote"]}, HUMAN)


async def test_provider_failure_and_unknown_cost_visible(planning, monkeypatch):
    planner, workspace, project = planning

    async def generate(model, messages, role, rid, session, budget, **kwargs):
        call = planner.engine.store.reserve(rid, session, role, model, 0.001, budget)
        planner.engine.store.settle(call, None, {}, 0, failed=True)
        raise ProviderError("Timed out")

    monkeypatch.setattr(planner.engine.providers, "generate", generate)
    result = enqueue(planner, request(workspace, project))
    await drain(planner)
    operation = planner.get(workspace.id, result.body["id"], HUMAN)
    assert operation["state"] == "UNCERTAIN" and operation["cost"]["uncertain_calls"] > 0


async def test_planning_never_executes(planning, repo, monkeypatch):
    planner, workspace, project = planning
    ref = next(iter(planner.service.repositories()))
    planner.service.update(
        "project",
        workspace.id,
        project["id"],
        {"command_id": identity(), "expected_revision": 1, "repository_ref": ref},
        HUMAN,
    )
    prd = planner.service.create(
        "prd",
        {
            "command_id": identity(),
            "workspace_id": workspace.id,
            "project_id": project["id"],
            "title": "Greeting",
            "spec": SPEC,
        },
        HUMAN,
    ).body
    for revision, command in [(1, "submit_review"), (2, "approve")]:
        prd = planner.service.prd_command(
            workspace.id,
            prd["id"],
            {"command_id": identity(), "expected_revision": revision, "command": command},
            HUMAN,
        ).body

    async def forbidden(*args, **kwargs):
        raise AssertionError("Planning executed work")

    monkeypatch.setattr(planner.engine, "execute_plan", forbidden)
    body = request(
        workspace,
        project,
        kind="decompose",
        prd_id=prd["id"],
        expected_revision=3,
        allowed_paths=["greeting.py", "USAGE.md", "test_greeting.py"],
    )
    result = enqueue(planner, body)
    await drain(planner)
    operation = planner.get(workspace.id, result.body["id"], HUMAN)
    assert operation["state"] == "COMPLETED", operation
    preview = operation["result"]
    assert len(preview["items"]) == 3 and preview["coverage"] == ["R1"]
    assert all(item["privacy_policy"] == "LOCAL_ONLY" for item in preview["items"])
    assert all("No network" in item["spec"]["constraints"] for item in preview["items"])
    assert not any((repo / path).exists() for path in body["allowed_paths"])
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_work_items").fetchone()[0] == 0


async def test_restart_never_blindly_repeats_inference(planning, monkeypatch):
    planner, workspace, project = planning
    entered = asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(planner.engine.providers, "generate", generate)
    body = request(workspace, project)
    command = {**body, "command_id": identity(), "quote": planner.quote(body, HUMAN)["quote"]}
    operation = planner.enqueue(command, HUMAN)
    await entered.wait()
    await planner.close()
    planner.reconcile_startup()
    assert planner.get(workspace.id, operation.body["id"], HUMAN)["state"] == "UNCERTAIN"
    assert planner.enqueue(command, HUMAN).replayed
    assert not planner.tasks


async def test_planning_http_quote_operation_reads(engine, monkeypatch):
    import httpx

    from local_ai_router.app import create_app

    app = create_app(engine.settings, engine)
    prefix = "/accs/v1/workbench"
    planner = app.state.workbench.planning
    workspace = planner.service.seed_defaults()
    project = planner.service.create(
        "project", {"command_id": identity(), "workspace_id": workspace.id, "name": "HTTP", "key": "HT"}, HUMAN
    ).body

    async def generate(*args, **kwargs):
        return Generation(text=json.dumps(SPEC))

    monkeypatch.setattr(engine.providers, "generate", generate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer " + engine.settings.token},
    ) as client:
        body = request(workspace, project)
        quote = await client.post(prefix + "/planning/quote", json=body)
        assert quote.status_code == 200 and engine.store.costs()["calls"] == 0
        response = await client.post(
            prefix + "/prds/generate", json={**body, "command_id": identity(), "quote": quote.json()["quote"]}
        )
        assert response.status_code == 202
        await drain(planner)
        operation = await client.get(
            prefix + "/operations/" + response.json()["id"], params={"workspace_id": workspace.id}
        )
        assert operation.status_code == 200 and operation.json()["state"] == "COMPLETED"
        denied = await client.get(prefix + "/operations/" + response.json()["id"], params={"workspace_id": identity()})
        assert denied.status_code == 404
