"""Actual provider calls remain in scoped ordered bindings, including concurrent tasks."""

import asyncio
import json

import pytest
from test_workbench_agents import HUMAN, definition
from test_workbench_execution import execution, item, queue  # noqa: F401

from local_ai_router.contracts import ExecutionAborted, execution_scope, model_order, model_selection_scope
from local_ai_router.schema import Classification, ModelDescriptor


def pinned(primary, *fallbacks):
    return {"mode": "pinned", "primary_model_id": primary, "fallback_model_ids": list(fallbacks)}


@pytest.fixture
def models(engine):
    engine.settings.models = [
        ModelDescriptor(id=name, provider="mock", deployment_name=name) for name in ("alpha", "beta", "gamma")
    ]
    engine.settings.routing.allow_below_slo = True
    engine.settings.routing.planner_model_candidates = []
    return engine.settings.models


async def test_concurrent_bindings_capture_actual_provider_calls(engine, models, monkeypatch):
    calls = []
    original = engine.providers._generate_checked

    async def capture(model, *args, **kwargs):
        await asyncio.sleep(0)
        calls.append((args[2], model.id))
        return await original(model, *args, **kwargs)

    monkeypatch.setattr(engine.providers, "_generate_checked", capture)

    async def run(name):
        with execution_scope(lambda: None, ["executor"], model_bindings={"executor": pinned(name)}):
            await asyncio.sleep(0)
            chosen, _ = engine.router.roles.select(Classification(), "executor")
            assert [model.id for model in chosen] == [name]
            capsule = {"task": {"title": "Greeting", "expected_artifacts": []}, "files": []}
            await engine.providers.generate(
                chosen[0], [{"role": "user", "content": json.dumps(capsule)}], "executor", name, name, 1
            )

    await asyncio.gather(run("alpha"), run("beta"))
    assert sorted(calls) == [("alpha", "alpha"), ("beta", "beta")]
    assert model_order("executor") is None


async def test_boundary_denies_undeclared_models_and_resets_exception(engine, models):
    with pytest.raises(ExecutionAborted):
        with model_selection_scope({"executor": pinned("alpha")}):
            await engine.providers.generate(models[1], [], "executor", "denied", "denied", 1)
    assert engine.store.costs()["calls"] == 0 and model_order("executor") is None
    with execution_scope(lambda: None, ["executor"], models=["beta"], model_bindings={"executor": pinned("alpha")}):
        assert not engine.router.roles.select(Classification(), "executor")[0]


async def test_order_unavailable_and_independent_reviewer(engine, models):
    with model_selection_scope({"executor": pinned("beta", "alpha"), "reviewer": pinned("beta")}):
        assert [m.id for m in engine.router.roles.select(Classification(), "executor")[0]] == ["beta", "alpha"]
        assert engine.router.roles.select(Classification(), "reviewer", excluded={"beta"})[0] == []
        models[1].enabled = False
        assert [m.id for m in engine.router.roles.select(Classification(), "executor")[0]] == ["alpha"]
    with model_selection_scope({"executor": pinned("beta")}):
        assert engine.router.roles.select(Classification(), "executor")[0] == []


async def test_engine_attempt_freezes_and_uses_selected_model(execution, models, monkeypatch):  # noqa: F811
    runtime, ws, _, agent, _ = execution
    updated = runtime.agents.configure(
        definition(
            ws,
            repository_permissions=agent["repository_permissions"],
            expected_revision=1,
            model_bindings={"executor": pinned("beta"), "repairer": pinned("beta"), "reviewer": pinned("alpha")},
        ),
        HUMAN,
        agent["id"],
    ).body
    fixture = (*execution[:3], updated, execution[4])
    attempt_id = queue(fixture, item(fixture))
    calls = []
    original = runtime.engine.providers._generate_checked

    async def capture(model, *args, **kwargs):
        calls.append((model.id, args[1]))
        return await original(model, *args, **kwargs)

    monkeypatch.setattr(runtime.engine.providers, "_generate_checked", capture)
    result = await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    assert result["status"] == "complete"
    assert calls and all(model == "beta" for model, role in calls if role in {"executor", "repair"})
    assert runtime.execution.contract(attempt_id)[1].model_bindings.executor.primary_model_id == "beta"


async def test_actual_failure_uses_only_declared_fallback(execution, models, monkeypatch):  # noqa: F811
    from local_ai_router.errors import ProviderUnavailable

    runtime, ws, _, agent, _ = execution
    updated = runtime.agents.configure(
        definition(
            ws,
            repository_permissions=agent["repository_permissions"],
            expected_revision=1,
            model_bindings={"executor": pinned("beta", "alpha"), "repairer": pinned("beta", "alpha")},
        ),
        HUMAN,
        agent["id"],
    ).body
    fixture = (*execution[:3], updated, execution[4])
    attempt_id = queue(fixture, item(fixture))
    calls = []
    original = runtime.engine.providers._generate_checked

    async def capture(model, *args, **kwargs):
        calls.append((model.id, args[1]))
        if model.id == "beta":
            raise ProviderUnavailable("synthetic primary failure")
        return await original(model, *args, **kwargs)

    monkeypatch.setattr(runtime.engine.providers, "_generate_checked", capture)
    result = await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    assert result["status"] == "complete"
    assert calls == [("beta", "executor"), ("alpha", "repair")]
