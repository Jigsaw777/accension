"""Model reads respect Engine policy and never invoke or disclose provider transport."""

import json

import httpx
import pytest
from fastapi import FastAPI
from test_workbench_agents import HUMAN

from local_ai_router.schema import ModelDescriptor, Provider
from local_ai_router.workbench.api import install
from local_ai_router.workbench.model_options import model_options
from local_ai_router.workbench.policy import Principal
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import WorkbenchError, identity


@pytest.fixture
async def catalog(engine, monkeypatch):
    engine.settings.providers = {
        "fixture-local": Provider(kind="mock", local=True),
        "fixture-cloud": Provider(
            kind="openai", endpoint="https://secret.invalid/api", credential_ref="private-reference"
        ),
    }
    engine.settings.models = [
        ModelDescriptor(id="a-local", provider="fixture-local", deployment_name="local-deployment"),
        ModelDescriptor(
            id="b-cloud", provider="fixture-cloud", deployment_name="cloud-deployment", input_price=1, output_price=2
        ),
        ModelDescriptor(id="c-unknown-price", provider="fixture-cloud", deployment_name="unknown"),
        ModelDescriptor(id="d-disabled", provider="fixture-local", deployment_name="disabled", enabled=False),
        ModelDescriptor(id="e-text-only", provider="fixture-local", deployment_name="text", supports_code=False),
    ]
    engine.settings.routing.allow_below_slo = True
    engine.settings.routing.allow_unknown_pricing = False
    engine.settings.routing.planner_model_candidates = []
    engine.settings.control_plane.fully_local = False
    engine.settings.routing.preset = "balanced"

    def forbidden(*args, **kwargs):
        raise AssertionError("Read attempted inference or discovery")

    monkeypatch.setattr(engine.providers, "generate", forbidden)
    monkeypatch.setattr(engine, "generate_planning_document", forbidden)
    runtime = WorkbenchRuntime(engine)
    workspace = runtime.service.seed_defaults()
    yield runtime, workspace
    await runtime.close()


async def test_options_local_policy_unknown_pricing_and_no_transport(catalog):
    runtime, ws = catalog
    result = model_options(runtime, {"workspace_id": ws.id}, HUMAN)
    rows = {row["id"]: row for row in result["items"]}
    assert rows["a-local"]["eligible"] and rows["a-local"]["estimated_max_cost"] == 0
    assert rows["a-local"]["locality"] == "local"
    assert not rows["b-cloud"]["eligible"] and "PRIVACY_POLICY" in rows["b-cloud"]["reason_codes"]
    assert rows["c-unknown-price"]["estimated_max_cost"] is None
    assert "PRICE_UNKNOWN" in rows["c-unknown-price"]["reason_codes"]
    assert "UNAVAILABLE" in rows["d-disabled"]["reason_codes"]
    assert "MISSING_CODE" in rows["e-text-only"]["reason_codes"]
    encoded = json.dumps(result)
    assert "secret.invalid" not in encoded and "private-reference" not in encoded
    assert "credential" not in encoded and "endpoint" not in encoded
    assert runtime.engine.store.costs()["calls"] == 0


async def test_options_hybrid_budget_and_role_alias(catalog):
    runtime, ws = catalog
    hybrid = runtime.service.create(
        "workspace",
        {"command_id": identity(), "name": "Hybrid", "privacy_mode": "HYBRID", "inference_policy": "CLOUD_ALLOWED"},
        HUMAN,
    ).body
    result = model_options(runtime, {"workspace_id": hybrid["id"], "purpose": "repair"}, HUMAN)
    assert result["purpose"] == "repairer"
    cloud = next(row for row in result["items"] if row["id"] == "b-cloud")
    assert cloud["eligible"] and cloud["estimated_max_cost"] > 0
    denied = model_options(runtime, {"workspace_id": hybrid["id"], "budget": "0"}, HUMAN)
    assert "BUDGET_EXCEEDED" in next(row for row in denied["items"] if row["id"] == "b-cloud")["reason_codes"]


async def test_options_cursor_inventory_and_authorization(catalog):
    runtime, ws = catalog
    query = {"workspace_id": ws.id, "limit": 2}
    first = model_options(runtime, query, HUMAN)
    second = model_options(runtime, query, HUMAN, cursor=first["next_cursor"])
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
    for changed in ({**query, "purpose": "planner"}, {**query, "query": "local"}):
        with pytest.raises(WorkbenchError):
            model_options(runtime, changed, HUMAN, cursor=first["next_cursor"])
    runtime.engine.settings.models[0].enabled = False
    with pytest.raises(WorkbenchError):
        model_options(runtime, query, HUMAN, cursor=first["next_cursor"])
    scoped = Principal("remote", ActorType.MACHINE, frozenset({"workspace.read"}), frozenset({identity()}))
    with pytest.raises(WorkbenchError):
        model_options(runtime, query, scoped)


async def test_options_http_input_and_project_scope(catalog):
    runtime, ws = catalog
    other = runtime.service.create("workspace", {"command_id": identity(), "name": "Other"}, HUMAN).body
    project = runtime.service.create(
        "project", {"command_id": identity(), "workspace_id": other["id"], "key": "OT", "name": "Other project"}, HUMAN
    ).body
    app = FastAPI()
    install(app, runtime, principal_resolver=lambda request: HUMAN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/accs/v1/workbench/model-options", params={"workspace_id": ws.id})
        assert response.status_code == 200 and response.json()["items"]
        for params in ({"limit": 101}, {"purpose": "gateway"}, {"project_id": project["id"]}):
            bad = await client.get("/accs/v1/workbench/model-options", params={"workspace_id": ws.id, **params})
            assert bad.status_code in {400, 404, 422}


@pytest.mark.parametrize("model_id", ["missing", "d-disabled", "b-cloud", "e-text-only"])
async def test_agent_rejects_ineligible_pins(catalog, model_id):
    from test_workbench_agents import definition

    runtime, ws = catalog
    with pytest.raises(WorkbenchError) as error:
        runtime.agents.configure(
            definition(ws, model_bindings={"executor": {"mode": "pinned", "primary_model_id": model_id}}), HUMAN
        )
    assert error.value.code == "MODEL_INELIGIBLE"
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_agent_definitions").fetchone()


async def test_agent_valid_pin_and_auto_defaults(catalog):
    from test_workbench_agents import definition

    runtime, ws = catalog
    agent = runtime.agents.configure(
        definition(ws, model_bindings={"executor": {"mode": "pinned", "primary_model_id": "a-local"}}), HUMAN
    ).body
    persisted = runtime.agents.definition(agent["id"])
    assert persisted.model_bindings.executor.primary_model_id == "a-local"
    assert persisted.model_bindings.reviewer.mode == "auto"
