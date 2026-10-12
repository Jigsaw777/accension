"""Eligibility is deterministic authority; previews and model advice do not reserve."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_workbench_agents import HUMAN, definition
from test_workbench_imports import commit, preview
from test_workbench_imports import graph as graph

from local_ai_router.schema import Generation
from local_ai_router.workbench.policy import remaining_budget
from local_ai_router.workbench.store import WorkbenchError, identity


@pytest.fixture
async def assigned_graph(graph):
    planner, ws, project, _ = graph
    imported = preview(graph)
    created, _ = commit(graph, imported)
    ids = created.body["items"]
    for item_id in ids.values():
        # Remove only the fixture's dependency so independent capacity races can run.
        planner.store.db.execute("DELETE FROM wb_dependencies WHERE dependent_id=?", (item_id,))
        planner.service.item_command(
            ws.id, item_id, {"command_id": identity(), "expected_revision": 1, "command": "ready"}, HUMAN
        )
    runtime = planner.runtime
    ref = project["repository_ref"]
    agent = runtime.agents.configure(definition(ws, repository_permissions=[ref]), HUMAN).body
    await runtime.start()
    yield runtime, ws, project, ids, agent


def assign(runtime, ws, item_id, agent):
    decision = runtime.agents.preview(ws.id, item_id, HUMAN)
    return runtime.agents.assign(
        ws.id,
        item_id,
        {
            "command_id": identity(),
            "expected_revision": decision["item_revision"],
            "agent_id": agent["id"],
            "policy_revision_hash": decision["policy_revision_hash"],
        },
        HUMAN,
    )


async def test_ineligible_recommendation_never_assigned(assigned_graph):
    runtime, ws, _, ids, agent = assigned_graph
    item_id = ids["b0_n0"]
    bad = runtime.agents.configure(definition(ws, name="Suggested without permission"), HUMAN).body
    decision = runtime.agents.preview(ws.id, item_id, HUMAN)
    assert decision["selected"] == agent["id"]
    assert "REPOSITORY_DENIED" in next(row for row in decision["rejected"] if row["agent_id"] == bad["id"])["reasons"]
    with pytest.raises(WorkbenchError, match="eligible"):
        assign(runtime, ws, item_id, bad)
    assert runtime.engine.store.costs()["calls"] == 0
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_assignments").fetchone()[0] == 0


async def test_capacity_race_one_reservation(assigned_graph):
    runtime, ws, _, ids, agent = assigned_graph

    def claim(item_id):
        try:
            return assign(runtime, ws, item_id, agent).status
        except WorkbenchError:
            return 422

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, ids.values()))
    assert sorted(results) == [200, 422]
    assert (
        runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_assignments WHERE status='RESERVED'").fetchone()[0]
        == 1
    )
    runtime.agents.heartbeat()
    assert runtime.agents.list(ws.id, HUMAN)[0]["state"] == "BUSY"


async def test_privacy_quality_context_budget_reasons(assigned_graph):
    runtime, ws, _, ids, agent = assigned_graph
    item_id = ids["b0_n0"]
    decision = runtime.agents.preview(ws.id, item_id, HUMAN)
    assert decision["privacy"] == "LOCAL_ONLY" and decision["eligible"]
    runtime.engine.settings.models = [
        m.model_copy(update={"context_window": 1}) for m in runtime.engine.settings.models
    ]
    decision = runtime.agents.preview(ws.id, item_id, HUMAN)
    row = next(row for row in decision["rejected"] if row["agent_id"] == agent["id"])
    assert "NO_ELIGIBLE_MODEL" in row["reasons"]
    assert any("CONTEXT_TOO_SMALL" in candidate["reason_codes"] for candidate in row["models"]["candidates"])
    runtime.service.update(
        "work_item", ws.id, item_id, {"command_id": identity(), "expected_revision": 2, "budget_limit": "0"}, HUMAN
    )
    decision = runtime.agents.preview(ws.id, item_id, HUMAN)
    assert "BUDGET_EXHAUSTED" in decision["rejected"][0]["reasons"]


async def test_policy_changed_after_preview(assigned_graph):
    runtime, ws, project, ids, agent = assigned_graph
    item_id = ids["b0_n0"]
    decision = runtime.agents.preview(ws.id, item_id, HUMAN)
    runtime.service.update(
        "project",
        ws.id,
        project["id"],
        {"command_id": identity(), "expected_revision": 1, "max_active_runs": 2},
        HUMAN,
    )
    with pytest.raises(WorkbenchError, match="policy changed"):
        runtime.agents.assign(
            ws.id,
            item_id,
            {
                "command_id": identity(),
                "expected_revision": 2,
                "agent_id": agent["id"],
                "policy_revision_hash": decision["policy_revision_hash"],
            },
            HUMAN,
        )
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_assignments").fetchone()[0] == 0


async def test_quality_privacy_and_advisory_model_cannot_bypass(assigned_graph):
    runtime, ws, _, ids, _ = assigned_graph
    item_id = ids["b0_n0"]
    runtime.engine.settings.models = [
        m.model_copy(update={"quality": {"coding": 0.01}}) for m in runtime.engine.settings.models
    ]
    decision = runtime.agents.preview(ws.id, item_id, HUMAN)
    assert not decision["eligible"]
    assert any(
        "BELOW_QUALITY_SLO" in candidate["reason_codes"]
        for candidate in decision["rejected"][0]["models"]["candidates"]
    )
    runtime.engine.settings.models = [
        m.model_copy(update={"quality": {"coding": 1.0}}) for m in runtime.engine.settings.models
    ]
    item = runtime.service.store.get("work_item", ws.id, item_id)
    runtime.service.update(
        "work_item",
        ws.id,
        item_id,
        {"command_id": identity(), "expected_revision": item.revision, "preferred_model_id": "unauthorized-model"},
        HUMAN,
    )
    runtime.service.item_command(
        ws.id, item_id, {"command_id": identity(), "expected_revision": item.revision + 1, "command": "ready"}, HUMAN
    )
    decision = runtime.agents.preview(ws.id, item_id, HUMAN)
    assert decision["eligible"][0]["candidate_model_id"] != "unauthorized-model"
    assert decision["privacy"] == "LOCAL_ONLY"
    runtime.engine.settings.providers = {
        key: p.model_copy(update={"endpoint": "https://example.invalid"})
        for key, p in runtime.engine.settings.providers.items()
    }
    decision = runtime.agents.preview(ws.id, item_id, HUMAN)
    assert not decision["eligible"]


async def test_execution_grant_bound_to_human_contract(assigned_graph):
    from local_ai_router.workbench.policy import CAPABILITIES, Principal
    from local_ai_router.workbench.schema import ActorType

    runtime, ws, _, ids, agent = assigned_graph
    item_id = ids["b0_n0"]
    result = assign(runtime, ws, item_id, agent)
    preflight = runtime.agents.execution_preflight(ws.id, item_id, HUMAN)
    body = {
        "command_id": identity(),
        "expected_revision": result.body["item"]["revision"],
        "command": "approve_execution",
        "contract_revision": 1,
        "contract_hash": preflight["contract_hash"],
        "policy_revision_hash": preflight["policy_revision_hash"],
        "reason": "Explicit scope reviewed",
    }
    machine = Principal("machine", ActorType.MACHINE, CAPABILITIES, frozenset({"*"}), True)
    with pytest.raises(WorkbenchError, match="human"):
        runtime.service.item_command(ws.id, item_id, body, machine)
    granted = runtime.service.item_command(ws.id, item_id, body, HUMAN)
    assert granted.status == 200
    assert runtime.service.item_command(ws.id, item_id, body, HUMAN).replayed
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_execution_grants").fetchone()[0] == 1


async def test_retry_keeps_lifetime_budget(assigned_graph):
    runtime, ws, _, ids, agent = assigned_graph
    item_id = ids["b0_n0"]
    assignment = assign(runtime, ws, item_id, agent).body["assignment_id"]
    store = runtime.engine.store
    request_id, attempt_id = identity(), identity()
    store.db.execute(
        "INSERT INTO wb_attempts(id,workspace_id,item_id,ordinal,assignment_id,contract_revision,contract_hash,contract_json,state,request_id,fence) VALUES(?,?,?,1,?,1,?,'{}','FAILED',?,1)",
        (attempt_id, ws.id, item_id, assignment, "a" * 64, request_id),
    )
    store.db.execute("INSERT INTO wb_budget_reservations VALUES(?,?,?,'SETTLED',0,0)", (attempt_id, item_id, 100000))
    model = store.settings.models[0]
    call = store.reserve(request_id, "retry", "executor", model, 0.09, 0.10)
    store.settle(call, 0.09, {}, 0)
    item = runtime.service.store.get("work_item", ws.id, item_id)
    assert remaining_budget(runtime.service.store, item, 100000)["remaining_microusd"] == 10000
    store.db.execute("UPDATE wb_budget_reservations SET state='UNCERTAIN'")
    budget = remaining_budget(runtime.service.store, item, 100000)
    assert budget["remaining_microusd"] == 0 and budget["held_microusd"] == 10000


async def test_team_lead_advice_has_no_authority(assigned_graph, monkeypatch):
    runtime, ws, _, ids, _ = assigned_graph
    item_id = ids["b0_n0"]

    async def generate(*args, **kwargs):
        return Generation(
            text=json.dumps(
                {
                    "recommended_agent_id": identity(),
                    "action": "assign",
                    "risk": 20,
                    "complexity": 30,
                    "explanation": "Suggested only",
                }
            )
        )

    monkeypatch.setattr(runtime.engine.providers, "generate", generate)
    result = runtime.planning.team_lead(
        {"command_id": identity(), "workspace_id": ws.id, "item_id": item_id, "expected_revision": 2}, HUMAN
    )
    await asyncio.gather(*list(runtime.planning.tasks.values()))
    operation = runtime.planning.get(ws.id, result.body["id"], HUMAN)
    assert operation["state"] == "COMPLETED" and operation["result"]["authority"] is False
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_assignments").fetchone()[0] == 0
