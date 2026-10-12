"""Command replay, revision races, atomic journaling and committed notifications."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from local_ai_router.store import Store
from local_ai_router.workbench.events import Journal
from local_ai_router.workbench.policy import CAPABILITIES, Principal
from local_ai_router.workbench.schema import ActorType, WorkbenchEvent
from local_ai_router.workbench.service import WorkbenchService, payload_hash
from local_ai_router.workbench.store import WorkbenchError, identity


@pytest.fixture
def service(settings):
    router = Store(settings)
    service = WorkbenchService(router)
    yield service
    router.close()


@pytest.fixture
def human():
    return Principal("human", ActorType.HUMAN, CAPABILITIES, frozenset({"*"}), True)


def setup(service, human):
    ws = service.create("workspace", {"command_id": identity(), "name": "Test"}, human).body
    project = service.create(
        "project", {"command_id": identity(), "workspace_id": ws["id"], "key": "WB", "name": "Workbench"}, human
    ).body
    return ws, project


def item_request(ws, project):
    return {
        "command_id": identity(),
        "workspace_id": ws["id"],
        "project_id": project["id"],
        "title": "Task",
        "execution_kind": "MANUAL",
    }


def counts(service):
    return {
        table: service.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("wb_work_items", "wb_events", "wb_entity_versions", "wb_commands", "wb_operations")
    }


def test_same_command_replay_returns_original_once(service, human):
    ws, project = setup(service, human)
    request = item_request(ws, project)
    first = service.create("work_item", request, human)
    before = counts(service)
    assert service.create("work_item", request, human).body == first.body
    edit = {"command_id": identity(), "expected_revision": 1, "title": "Changed"}
    changed = service.update("work_item", ws["id"], first.body["id"], edit, human)
    replay = service.create("work_item", request, human)
    assert replay.replayed and replay.body == first.body
    assert service.update("work_item", ws["id"], first.body["id"], edit, human).body == changed.body
    assert counts(service) == {
        **before,
        "wb_events": before["wb_events"] + 1,
        "wb_entity_versions": before["wb_entity_versions"] + 1,
        "wb_commands": before["wb_commands"] + 1,
    }
    assert service.store.db.execute("SELECT next_number FROM wb_project_counters").fetchone()[0] == 2
    assert [r.revision for r in service.revisions("work_item", ws["id"], first.body["id"], human)] == [2, 1]


def test_reused_key_different_payload_conflicts(service, human):
    ws, project = setup(service, human)
    request = item_request(ws, project)
    service.create("work_item", request, human)
    before = counts(service)
    with pytest.raises(WorkbenchError) as caught:
        service.create("work_item", {**request, "title": "Different"}, human)
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    assert counts(service) == before


def test_two_edits_one_revision_winner(service, human, settings):
    ws, project = setup(service, human)
    item = service.create("work_item", item_request(ws, project), human).body
    router = Store(settings)
    second = WorkbenchService(router)
    barrier = Barrier(2)

    def edit(index):
        barrier.wait(timeout=10)
        try:
            return (
                (service, second)[index]
                .update(
                    "work_item",
                    ws["id"],
                    item["id"],
                    {"command_id": identity(), "expected_revision": 1, "title": f"Winner {index}"},
                    human,
                )
                .status
            )
        except WorkbenchError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(edit, range(2)))
        assert sorted(map(str, result)) == ["200", "REVISION_CONFLICT"]
        assert len(service.revisions("work_item", ws["id"], item["id"], human)) == 2
    finally:
        router.close()


@pytest.mark.parametrize(
    "table,operation",
    [
        ("wb_commands", "INSERT"),
        ("wb_work_items", "INSERT"),
        ("wb_events", "INSERT"),
        ("wb_entity_versions", "INSERT"),
        ("wb_commands", "UPDATE"),
        ("wb_work_items", "UPDATE"),
    ],
)
def test_failure_rolls_back_entity_revision_event_command(service, human, table, operation):
    ws, project = setup(service, human)
    item = service.create("work_item", item_request(ws, project), human).body
    before = counts(service)
    counter = service.store.db.execute("SELECT next_number FROM wb_project_counters").fetchone()[0]
    service.store.db.execute(
        f"CREATE TEMP TRIGGER injected BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'injected boundary failure'); END"
    )
    with pytest.raises(WorkbenchError, match="conflicts"):
        if operation == "UPDATE" and table == "wb_work_items":
            service.update(
                "work_item",
                ws["id"],
                item["id"],
                {"command_id": identity(), "expected_revision": 1, "title": "No commit"},
                human,
            )
        else:
            service.create("work_item", item_request(ws, project), human)
    assert counts(service) == before
    assert service.store.db.execute("SELECT next_number FROM wb_project_counters").fetchone()[0] == counter
    assert service.get("work_item", ws["id"], item["id"], human).revision == 1


def test_lost_response_and_scoped_replay(service, human, settings):
    ws, project = setup(service, human)
    request = item_request(ws, project)
    first = service.create("work_item", request, human)
    router = Store(settings)
    try:
        restarted = WorkbenchService(router)
        assert restarted.create("work_item", request, human).body == first.body
        revoked = replace(human, workspaces=frozenset(), local=False)
        with pytest.raises(WorkbenchError) as caught:
            restarted.create("work_item", request, revoked)
        assert caught.value.status == 404
        with pytest.raises(WorkbenchError) as caught:
            restarted.create("work_item", request, replace(human, capabilities=frozenset()))
        assert caught.value.status == 403
        with pytest.raises(WorkbenchError) as caught:
            restarted.create("work_item", request, None)
        assert caught.value.status == 401
        other = replace(human, id="other")
        assert restarted.create("work_item", request, other).body["number"] == 2
    finally:
        router.close()


def test_notifications_after_commit_and_failure_recoverable(service, human, settings, monkeypatch):
    ws, project = setup(service, human)
    request = item_request(ws, project)
    other = sqlite3.connect(settings.state / "router.sqlite3", isolation_level=None)

    def notify():
        assert not service.store.db.in_transaction
        assert other.execute("SELECT COUNT(*) FROM wb_work_items").fetchone()[0] == 1
        assert other.execute("SELECT COUNT(*) FROM wb_events").fetchone()[0] == 3
        raise RuntimeError("notification unavailable")

    try:
        monkeypatch.setattr(service.wake, "notify", notify)
        result = service.create("work_item", request, human)
        assert result.status == 201
        assert service.last_notification_error == "RuntimeError"
        assert service.create("work_item", request, human).body == result.body
        row = service.store.db.execute("SELECT * FROM wb_events ORDER BY sequence DESC LIMIT 1").fetchone()
        values = dict(row)
        values["payload"] = json.loads(values.pop("payload_json"))
        event = WorkbenchEvent.model_validate(values)
        assert event.payload.id == result.body["id"]
        assert event.actor_type == ActorType.HUMAN
    finally:
        other.close()


def test_operation_intent_committed_once_without_inference(service, human):
    ws, _ = setup(service, human)
    command_id, subject_id = identity(), identity()
    payload = {"subject": subject_id, "revision": 1}
    calls = []

    def apply():
        calls.append("intent")
        return service.operation_intent(ws["id"], "decompose", subject_id, 1, payload_hash(payload))

    first = service.command(human, ws["id"], "prd.write", command_id, "decompose", payload, apply)
    replay = service.command(human, ws["id"], "prd.write", command_id, "decompose", payload, apply)
    assert first.status == 202 and first.body == replay.body
    assert calls == ["intent"]
    assert counts(service)["wb_operations"] == 1


def test_repository_privilege_rechecked_before_replay(service, human, repo):
    ws, _ = setup(service, human)
    request = {
        "command_id": identity(),
        "workspace_id": ws["id"],
        "name": "Registered",
        "key": "RP",
        "repository_ref": next(iter(service.repositories())),
    }
    project = service.create("project", request, human).body
    restricted = replace(human, capabilities=CAPABILITIES - {"repository.admin"})
    with pytest.raises(WorkbenchError) as caught:
        service.create("project", request, restricted)
    assert caught.value.status == 403
    update = {"command_id": identity(), "expected_revision": 1, "repository_ref": None}
    service.update("project", ws["id"], project["id"], update, human)
    with pytest.raises(WorkbenchError) as caught:
        service.update("project", ws["id"], project["id"], update, restricted)
    assert caught.value.status == 403


def test_snapshot_pages_plus_replay_equal_current_state(service, human):
    ws, project = setup(service, human)
    created = [service.create("work_item", item_request(ws, project), human).body for _ in range(5)]
    journal = Journal(service)
    snapshot = journal.start_snapshot(ws["id"], human, {"kinds": ["work_item"]}, limit=2)
    at_sequence = snapshot["sequence"]
    reconstructed = {item["payload"]["id"]: item["payload"] for item in snapshot["items"]}
    service.update(
        "work_item",
        ws["id"],
        created[-1]["id"],
        {"command_id": identity(), "expected_revision": 1, "title": "Between pages"},
        human,
    )
    added = service.create("work_item", item_request(ws, project), human).body
    while snapshot["next_cursor"]:
        snapshot = journal.snapshot_page(snapshot["next_cursor"], human, limit=2)
        assert snapshot["sequence"] == at_sequence
        reconstructed.update({entry["payload"]["id"]: entry["payload"] for entry in snapshot["items"]})
    assert added["id"] not in reconstructed
    assert reconstructed[created[-1]["id"]]["title"] == "Task"
    for event in journal.events(ws["id"], human, after=at_sequence):
        reconstructed[event["entity_id"]] = event["payload"]
    assert len(reconstructed) == 6
    assert reconstructed[created[-1]["id"]]["title"] == "Between pages"
    assert all("spec" not in payload for payload in reconstructed.values())


def test_cross_connection_event_visibility(service, human, settings):
    ws, project = setup(service, human)
    journal = Journal(service)
    sequence = journal.sequence()
    other = Store(settings)
    try:
        second = WorkbenchService(other)
        item = second.create("work_item", item_request(ws, project), human).body
        event = journal.events(ws["id"], human, after=sequence)[0]
        assert event["entity_id"] == item["id"]
        assert event["sequence"] > sequence
    finally:
        other.close()


def test_duplicate_and_cursor_rules(service, human):
    ws, project = setup(service, human)
    journal = Journal(service)
    item = service.create("work_item", item_request(ws, project), human).body
    events = journal.events(ws["id"], human)
    assert journal.events(ws["id"], human) == events
    assert journal.events(ws["id"], human, after=events[-1]["sequence"]) == []
    with pytest.raises(WorkbenchError, match="ahead"):
        journal.events(ws["id"], human, after=journal.sequence() + 1)
    with pytest.raises(WorkbenchError, match="invalid"):
        journal.events(ws["id"], human, after=-1)
    service.store.db.execute("INSERT INTO metadata VALUES('wb_events_floor',?)", (str(events[-1]["sequence"]),))
    with pytest.raises(WorkbenchError, match="retired"):
        journal.events(ws["id"], human)
    service.store.db.execute("DELETE FROM metadata WHERE key='wb_events_floor'")
    service.store.db.execute("UPDATE wb_events SET schema_version=2 WHERE entity_id=?", (item["id"],))
    with pytest.raises(WorkbenchError, match="Unknown event"):
        journal.events(ws["id"], human)


async def test_slow_reader_bounded_and_resync(service, human):
    ws, project = setup(service, human)
    for _ in range(3):
        service.create("work_item", item_request(ws, project), human)
    journal = Journal(service)
    output = [entry async for entry in journal.stream(ws["id"], human, byte_budget=100)]
    assert len(output) == 1 and "DELIVERY_LIMIT" in output[0]
    assert not service.store.db.in_transaction
    count = 0

    async def stop():
        nonlocal count
        count += 1
        assert not service.store.db.in_transaction
        return count > 1

    output = [
        entry
        async for entry in journal.stream(
            ws["id"], human, journal.sequence(), disconnected=stop, heartbeat=0, poll=0.001
        )
    ]
    assert output == [": heartbeat\n\n"]


def test_snapshot_tombstone_and_expiry(service, human):
    ws, project = setup(service, human)
    items = [service.create("work_item", item_request(ws, project), human).body for _ in range(3)]
    journal = Journal(service)
    old = journal.start_snapshot(ws["id"], human, {"kinds": ["work_item"]}, limit=1)
    service.archive("work_item", ws["id"], items[0]["id"], {"command_id": identity(), "expected_revision": 1}, human)
    fresh = journal.start_snapshot(ws["id"], human, {"kinds": ["work_item"]})
    assert len(fresh["items"]) == 2
    seen = old["items"]
    token = old["next_cursor"]
    while token:
        page = journal.snapshot_page(token, human, limit=1)
        seen += page["items"]
        token = page["next_cursor"]
    assert len(seen) == 3
    with pytest.raises(WorkbenchError, match="not found"):
        journal.snapshot_page(old["next_cursor"], replace(human, id="other"))
    service.store.db.execute("UPDATE wb_snapshot_leases SET expires_at=0 WHERE id=?", (old["snapshot_id"],))
    with pytest.raises(WorkbenchError, match="expired"):
        journal.snapshot_page(old["next_cursor"], human)
    with pytest.raises(WorkbenchError, match="invalid"):
        journal.snapshot_page(old["next_cursor"] + "tamper", human)
