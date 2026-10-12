"""Atomic bounded activity, stable backfill and historical identity."""

import json

import pytest
from test_workbench_agents import HUMAN, definition

from local_ai_router.workbench.activity import append_activity, backfill_activity
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import identity, now


@pytest.fixture
async def activity(engine):
    runtime = WorkbenchRuntime(engine)
    ws = runtime.service.seed_defaults()
    yield runtime, ws
    await runtime.close()


async def test_activity_owning_transaction_rollback_and_replay(activity):
    runtime, ws = activity
    store = runtime.service.store
    fields = {
        "source_key": "test:atomic",
        "workspace_id": ws.id,
        "occurred_at": now(),
        "actor_type": ActorType.SYSTEM,
        "actor_id": "test",
        "event_type": "test_event",
    }
    with pytest.raises(RuntimeError, match="owning transaction"):
        append_activity(store, **fields)
    with pytest.raises(RuntimeError, match="rollback"):
        with store.router.transaction():
            append_activity(store, **fields)
            raise RuntimeError("rollback")
    assert not store.db.execute("SELECT 1 FROM wb_activity_entries WHERE source_key='test:atomic'").fetchone()
    with store.router.transaction():
        append_activity(store, **fields)
        append_activity(store, **fields)
    assert (
        store.db.execute("SELECT COUNT(*) FROM wb_activity_entries WHERE source_key='test:atomic'").fetchone()[0] == 1
    )


async def test_timeline_bucket_window_and_cursor_binding(activity):
    from local_ai_router.workbench.activity import timeline
    from local_ai_router.workbench.store import WorkbenchError

    runtime, ws = activity
    with runtime.engine.store.transaction():
        for stamp in (1000, 1500, 2000):
            append_activity(
                runtime.service.store,
                source_key="bucket:" + str(stamp),
                workspace_id=ws.id,
                occurred_at=stamp,
                actor_type=ActorType.SYSTEM,
                actor_id="test",
                event_type="bucket_fixture",
            )
    first = timeline(runtime, ws.id, HUMAN, start=1000, end=2000, limit=1)
    assert [x["occurred_at"] for x in first["items"]] == [1500]
    next_page = timeline(runtime, ws.id, HUMAN, start=1000, end=2000, limit=1, cursor=first["next_cursor"])
    assert [x["occurred_at"] for x in next_page["items"]] == [1000]
    with pytest.raises(WorkbenchError, match="scope changed"):
        timeline(runtime, ws.id, HUMAN, start=1000, end=3000, cursor=first["next_cursor"])


async def test_agent_rename_keeps_frozen_activity(activity):
    runtime, ws = activity
    agent = runtime.agents.configure(definition(ws, name="First name"), HUMAN).body
    runtime.agents.configure(definition(ws, name="New name", expected_revision=1), HUMAN, agent["id"])
    rows = list(
        runtime.service.store.db.execute(
            "SELECT metadata_json FROM wb_activity_entries WHERE agent_definition_id=? ORDER BY id", (agent["id"],)
        )
    )
    assert [json.loads(row[0])["agent_name"] for row in rows] == ["First name", "New name"]


async def test_backfill_is_bounded_restartable_and_preserves_journal(activity):
    runtime, ws = activity
    for index in range(5):
        runtime.service.create(
            "project",
            {"command_id": identity(), "workspace_id": ws.id, "name": "History " + str(index), "key": "H" + str(index)},
            HUMAN,
        )
    db = runtime.service.store.db
    original = [tuple(row) for row in db.execute("SELECT * FROM wb_events")]
    db.execute("DELETE FROM wb_activity_entries")
    assert backfill_activity(runtime.service.store, limit=2) == 2
    assert backfill_activity(runtime.service.store, limit=2) == 2
    while backfill_activity(runtime.service.store, limit=2):
        pass
    count = db.execute("SELECT COUNT(*) FROM wb_activity_entries").fetchone()[0]
    assert count == len(original) and backfill_activity(runtime.service.store) == 0
    assert [tuple(row) for row in db.execute("SELECT * FROM wb_events")] == original


async def test_activity_metadata_is_strict_redacted_and_bounded(activity):
    from pydantic import ValidationError

    runtime, ws = activity
    store = runtime.service.store
    fields = {
        "source_key": "test:redacted",
        "workspace_id": ws.id,
        "occurred_at": now(),
        "actor_type": ActorType.SYSTEM,
        "actor_id": "test",
        "event_type": "test_event",
    }
    with store.router.transaction():
        append_activity(store, **fields, metadata={"summary": "sk-" + "x" * 40})
        with pytest.raises(ValidationError):
            append_activity(store, **{**fields, "source_key": "forbidden"}, metadata={"prompt": "private"})
    row = store.db.execute("SELECT metadata_json FROM wb_activity_entries WHERE source_key='test:redacted'").fetchone()
    assert "x" * 40 not in row[0]


async def test_timeline_high_water_scope_and_new_events(activity):
    from local_ai_router.workbench.activity import timeline
    from local_ai_router.workbench.policy import Principal
    from local_ai_router.workbench.store import WorkbenchError

    runtime, ws = activity
    agent = runtime.agents.configure(definition(ws), HUMAN).body
    for revision in range(2, 7):
        runtime.agents.configure(
            definition(ws, name="Name " + str(revision), expected_revision=revision - 1), HUMAN, agent["id"]
        )
    first = timeline(runtime, ws.id, HUMAN, agent_id=agent["id"], limit=2)
    runtime.agents.configure(definition(ws, expected_revision=6), HUMAN, agent["id"])
    seen = [row["id"] for row in first["items"]]
    page = first
    while page["has_more"]:
        page = timeline(runtime, ws.id, HUMAN, agent_id=agent["id"], limit=2, cursor=page["next_cursor"])
        seen.extend(row["id"] for row in page["items"])
    assert len(seen) == len(set(seen)) == 6
    restricted = Principal("other", ActorType.MACHINE, frozenset({"workspace.read"}), frozenset({identity()}), True)
    with pytest.raises(WorkbenchError):
        timeline(runtime, ws.id, restricted, agent_id=agent["id"])
    with pytest.raises(WorkbenchError):
        timeline(runtime, identity(), HUMAN, agent_id=agent["id"], cursor=first["next_cursor"])


async def test_removed_membership_history_has_no_current_configuration(activity):
    from local_ai_router.workbench.activity import timeline
    from local_ai_router.workbench.policy import Principal

    runtime, ws = activity
    other = runtime.service.create("workspace", {"command_id": identity(), "name": "Second"}, HUMAN).body
    agent = runtime.agents.configure(
        definition(ws, name="Historical", allowed_workspaces=[ws.id, other["id"]]), HUMAN
    ).body
    runtime.agents.configure(
        definition(ws, name="Private current", allowed_workspaces=[other["id"]], expected_revision=1),
        HUMAN,
        agent["id"],
    )
    scoped = Principal("scoped", ActorType.MACHINE, frozenset({"workspace.read"}), frozenset({ws.id}))
    history = timeline(runtime, ws.id, scoped, agent_id=agent["id"])
    assert "Historical" in json.dumps(history)
    assert "Private current" not in json.dumps(history)
    assert "allowed_workspaces" not in json.dumps(history)


async def test_corrupt_timeline_metadata_fails_explicitly(activity):
    from local_ai_router.workbench.activity import timeline
    from local_ai_router.workbench.store import WorkbenchError

    runtime, ws = activity
    agent = runtime.agents.configure(definition(ws), HUMAN).body
    runtime.service.store.db.execute(
        "UPDATE wb_activity_entries SET metadata_json='{' WHERE agent_definition_id=?", (agent["id"],)
    )
    with pytest.raises(WorkbenchError) as failure:
        timeline(runtime, ws.id, HUMAN, agent_id=agent["id"])
    assert failure.value.code == "CORRUPT_DATA"
