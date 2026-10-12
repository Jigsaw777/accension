"""Definitions cannot fabricate daemon capacity or grant arbitrary adapters."""

from uuid import UUID

import pytest

from local_ai_router.workbench.policy import CAPABILITIES, Principal
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import WorkbenchError, identity, now

HUMAN = Principal("operator", ActorType.HUMAN, CAPABILITIES, frozenset({"*"}), True)


@pytest.fixture
async def agents(engine):
    runtime = WorkbenchRuntime(engine)
    yield runtime, runtime.service.seed_defaults()
    await runtime.close()


def definition(workspace, **extra):
    return {"command_id": identity(), "name": "Built-in worker", "allowed_workspaces": [workspace.id], **extra}


async def test_workspace_agents_not_hidden_by_global_limit(agents):
    runtime, workspace = agents
    other = runtime.service.create("workspace", {"command_id": identity(), "name": "Other"}, HUMAN).body
    wanted = runtime.agents.configure(definition(workspace), HUMAN).body
    other_agent = runtime.agents.configure(definition(workspace, allowed_workspaces=[other["id"]]), HUMAN).body
    db = runtime.service.store.db
    sample = dict(db.execute("SELECT * FROM wb_agent_definitions WHERE id=?", (other_agent["id"],)).fetchone())
    columns = tuple(sample)
    with runtime.engine.store.transaction():
        for number in range(1, 202):
            row = {**sample, "id": str(UUID(int=number, version=4)), "name": "Other " + str(number)}
            db.execute(
                "INSERT INTO wb_agent_definitions("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(row[column] for column in columns),
            )
    assert [row["id"] for row in runtime.agents.list(workspace.id, HUMAN)] == [wanted["id"]]


async def test_agent_cursor_pages_are_scoped_and_searchable(agents):
    runtime, workspace = agents
    for number in range(5):
        runtime.agents.configure(definition(workspace, name="Worker " + str(number)), HUMAN)
    page = runtime.agents.list_page(workspace.id, HUMAN, limit=2)
    seen = [row["id"] for row in page["items"]]
    cursor = page["next_cursor"]
    while page["has_more"]:
        page = runtime.agents.list_page(workspace.id, HUMAN, limit=2, cursor=page["next_cursor"])
        seen.extend(row["id"] for row in page["items"])
    assert len(seen) == len(set(seen)) == 5
    assert len(runtime.agents.list_page(workspace.id, HUMAN, query="Worker 4")["items"]) == 1
    with pytest.raises(WorkbenchError):
        runtime.agents.list_page(workspace.id, HUMAN, cursor=cursor, query="Worker 4")


async def test_definition_does_not_imply_running_instance(agents):
    runtime, workspace = agents
    created = runtime.agents.configure(definition(workspace), HUMAN)
    rows = runtime.agents.list(workspace.id, HUMAN)
    assert len(rows) == 1 and rows[0]["state"] == "OFFLINE" and rows[0]["instances"] == []
    assert created.body["adapter_kind"] == "engine"
    assert runtime.engine.store.costs()["calls"] == 0


async def test_agent_updates_and_removal_reach_all_granted_workspaces(agents):
    runtime, left = agents
    right = runtime.service.create("workspace", {"command_id": identity(), "name": "Second"}, HUMAN).body
    created = runtime.agents.configure(definition(left, allowed_workspaces=[left.id, right["id"]]), HUMAN).body
    for wid in (left.id, right["id"]):
        snapshot = runtime.journal.start_snapshot(wid, HUMAN, filters={"kinds": ["agent"]})
        assert snapshot["items"][0]["payload"]["id"] == created["id"]
    cursor = runtime.journal.sequence()
    runtime.agents.configure(definition(left, expected_revision=1), HUMAN, created["id"])
    for wid in (left.id, right["id"]):
        assert runtime.journal.events(wid, HUMAN, after=cursor)[0]["entity_revision"] == 2
    assert runtime.journal.start_snapshot(right["id"], HUMAN, filters={"kinds": ["agent"]})["items"] == []
    assert (
        runtime.journal.start_snapshot(left.id, HUMAN, filters={"kinds": ["agent"]})["items"][0]["payload"]["revision"]
        == 2
    )


async def test_instance_lifecycle_and_disable(agents):
    runtime, workspace = agents
    first = runtime.agents.configure(definition(workspace), HUMAN)
    await runtime.start()
    runtime.agents.heartbeat()
    row = runtime.agents.list(workspace.id, HUMAN)[0]
    assert row["state"] == "AVAILABLE" and row["instances"][0]["owner_generation"] == runtime.agents.generation
    command = definition(workspace, enabled=False, expected_revision=1)
    changed = runtime.agents.configure(command, HUMAN, first.body["id"])
    assert changed.body["revision"] == 2
    assert runtime.agents.list(workspace.id, HUMAN)[0]["state"] == "DISABLED"
    assert not runtime.agents.instances
    runtime.agents.configure(definition(workspace, expected_revision=2), HUMAN, first.body["id"])
    assert runtime.agents.list(workspace.id, HUMAN)[0]["state"] == "AVAILABLE"
    await runtime.close()
    assert runtime.agents.list(workspace.id, HUMAN)[0]["state"] == "OFFLINE"
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_agent_instances").fetchone()[0] == 2


async def test_expired_heartbeat_stops_new_work(agents):
    runtime, workspace = agents
    runtime.agents.configure(definition(workspace), HUMAN)
    await runtime.start()
    instance_id = next(iter(runtime.agents.instances.values()))
    runtime.engine.store.db.execute("UPDATE wb_agent_instances SET expires_at=? WHERE id=?", (now() - 1, instance_id))
    assert runtime.agents.list(workspace.id, HUMAN)[0]["state"] == "OFFLINE"


async def test_provider_unavailable_distinct_from_worker_health(agents):
    runtime, workspace = agents
    runtime.engine.settings.models = []
    runtime.agents.configure(definition(workspace), HUMAN)
    await runtime.start()
    row = runtime.agents.list(workspace.id, HUMAN)[0]
    assert row["state"] == "AVAILABLE" and row["provider_eligibility"] == "checked per work item"
    assert runtime.engine.store.costs()["calls"] == 0


@pytest.mark.parametrize("adapter", ["codex", "claude", "shell", "python"])
async def test_unverified_adapter_rejected(agents, adapter):
    runtime, workspace = agents
    with pytest.raises(ValueError):
        runtime.agents.configure(definition(workspace, adapter_kind=adapter), HUMAN)
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_agent_definitions").fetchone()[0] == 0


async def test_agent_grants_and_revision_fail_closed(agents):
    runtime, workspace = agents
    limited = Principal("limited", ActorType.HUMAN, frozenset({"agent.admin"}), frozenset({workspace.id}), False)
    with pytest.raises(WorkbenchError):
        runtime.agents.configure(definition(workspace), limited)
    for changes in (
        {"repository_permissions": ["a" * 64]},
        {"tools": ["shell"]},
        {"skills": ["untrusted"]},
        {"allowed_workspaces": [identity()]},
        {"allowed_projects": [identity()]},
    ):
        with pytest.raises(WorkbenchError):
            runtime.agents.configure(definition(workspace, **changes), HUMAN)
    created = runtime.agents.configure(definition(workspace), HUMAN)
    with pytest.raises(WorkbenchError, match="changed"):
        runtime.agents.configure(definition(workspace, expected_revision=5), HUMAN, created.body["id"])
    assert runtime.agents.list(identity(), HUMAN) == []


async def test_agent_configure_idempotency_and_api(agents):
    import httpx
    from fastapi import FastAPI

    from local_ai_router.workbench.api import install

    runtime, workspace = agents
    app = FastAPI()
    install(app, runtime, prefixes=("/test",), principal_resolver=lambda _: HUMAN)
    body = definition(workspace)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        first = await client.post("/test/agents", json=body)
        again = await client.post("/test/agents", json=body)
        assert first.status_code == 201 and again.json() == first.json()
        rows = await client.get("/test/agents", params={"workspace_id": workspace.id})
        assert rows.json()["items"][0]["state"] == "OFFLINE"
