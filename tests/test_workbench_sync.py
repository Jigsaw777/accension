"""Independent paired clients reconstruct committed state and reject stale authority."""

import asyncio

import httpx
import pytest
from test_workbench_agents import HUMAN
from test_workbench_lan_access import lan as lan
from test_workbench_lan_access import pair

from local_ai_router.store import Store
from local_ai_router.workbench.devices import Devices
from local_ai_router.workbench.events import Journal
from local_ai_router.workbench.lan import LAN_PREFIX
from local_ai_router.workbench.service import WorkbenchService
from local_ai_router.workbench.store import WorkbenchError, identity


def item(runtime, ws, project, title="Original"):
    return runtime.service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "project_id": project["id"],
            "title": title,
            "execution_kind": "MANUAL",
        },
        HUMAN,
    ).body


async def test_two_clients_snapshot_replay_converge(lan):
    runtime, ws, project, app, client = lan
    first = pair(runtime, ws, ["workspace.read", "work.write"], name="One")
    second = pair(runtime, ws, ["workspace.read"], name="Two")
    entries = [item(runtime, ws, project) for _ in range(4)]
    snapshots = []
    for device in (first, second):
        actor, _ = runtime.devices.authenticate(device["token"])
        snapshots.append((actor, runtime.journal.start_snapshot(ws.id, actor, {"kinds": ["work_item"]}, limit=1)))
    runtime.service.update(
        "work_item",
        ws.id,
        entries[-1]["id"],
        {"command_id": identity(), "expected_revision": 1, "title": "Between pages"},
        HUMAN,
    )
    added = item(runtime, ws, project, "Later")
    views = []
    for actor, page in snapshots:
        sequence = page["sequence"]
        seen = {entry["payload"]["id"]: entry["payload"] for entry in page["items"]}
        while page["next_cursor"]:
            page = runtime.journal.snapshot_page(page["next_cursor"], actor, limit=1)
            assert page["sequence"] == sequence
            seen.update({entry["payload"]["id"]: entry["payload"] for entry in page["items"]})
        assert added["id"] not in seen
        for event in runtime.journal.events(ws.id, actor, after=sequence):
            if event["entity_type"] == "work_item":
                seen[event["entity_id"]] = event["payload"]
        views.append({key: (value["revision"], value["title"]) for key, value in seen.items()})
    assert views[0] == views[1] and len(views[0]) == 5
    assert views[0][entries[-1]["id"]] == (2, "Between pages")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://localhost:8766"
    ) as independent:
        read = await independent.get(
            LAN_PREFIX + "/work-items",
            params={"workspace_id": ws.id},
            headers={"authorization": "Bearer " + second["token"]},
        )
        assert read.status_code == 200 and len(read.json()["items"]) == 5


async def test_revoke_closes_sse_and_snapshot(lan):
    runtime, ws, project, _, _ = lan
    device = pair(runtime, ws, ["workspace.read"])
    actor, _ = runtime.devices.authenticate(device["token"])
    for _ in range(2):
        item(runtime, ws, project)
    snapshot = runtime.journal.start_snapshot(ws.id, actor, {"kinds": ["work_item"]}, limit=1)
    stream = runtime.journal.stream(ws.id, actor, runtime.journal.sequence(), heartbeat=0, poll=0.001)
    assert await anext(stream) == ": heartbeat\n\n"
    runtime.devices.revoke(ws.id, device["device_id"], 1, HUMAN)
    output = await asyncio.wait_for(anext(stream), 1)
    assert "UNAUTHENTICATED" in output
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    with pytest.raises(WorkbenchError):
        runtime.journal.snapshot_page(snapshot["next_cursor"], actor)
    assert (
        runtime.service.store.db.execute(
            "SELECT COUNT(*) FROM wb_snapshot_leases WHERE principal_id=?", (actor.id,)
        ).fetchone()[0]
        == 0
    )


async def test_restart_and_missed_event_recovery(lan):
    from types import SimpleNamespace

    runtime, ws, project, _, _ = lan
    device = pair(runtime, ws, ["workspace.read"])
    actor, _ = runtime.devices.authenticate(device["token"])
    cursor = runtime.journal.sequence()
    created = item(runtime, ws, project, "While disconnected")
    # A separately reopened SQLite connection has no in-memory client/worker state.
    other = Store(runtime.engine.settings)
    try:
        service = WorkbenchService(other)
        devices = Devices(SimpleNamespace(service=service, engine=runtime.engine))
        restarted, _ = devices.authenticate(device["token"])
        assert restarted.id == actor.id
        replay = Journal(service).events(ws.id, restarted, after=cursor)
        assert replay[-1]["entity_id"] == created["id"]
    finally:
        other.close()


async def test_out_of_order_duplicate_and_stale_cursor(lan):
    runtime, ws, project, _, _ = lan
    device = pair(runtime, ws, ["workspace.read"])
    actor, _ = runtime.devices.authenticate(device["token"])
    created = item(runtime, ws, project)
    runtime.service.update(
        "work_item", ws.id, created["id"], {"command_id": identity(), "expected_revision": 1, "title": "Latest"}, HUMAN
    )
    events = [event for event in runtime.journal.events(ws.id, actor) if event["entity_id"] == created["id"]]
    assert [event["entity_revision"] for event in events] == [1, 2]
    assert runtime.journal.events(ws.id, actor, after=runtime.journal.sequence()) == []
    runtime.service.store.db.execute("INSERT INTO metadata VALUES('wb_events_floor',?)", (str(events[0]["sequence"]),))
    with pytest.raises(WorkbenchError) as error:
        runtime.journal.events(ws.id, actor)
    assert error.value.status == 410
    runtime.service.store.db.execute("DELETE FROM metadata WHERE key='wb_events_floor'")
    with pytest.raises(WorkbenchError):
        runtime.journal.events(ws.id, actor, after=runtime.journal.sequence() + 1)


async def test_command_retry_after_network_loss(lan):
    runtime, ws, project, _, client = lan
    device = pair(runtime, ws, ["workspace.read", "work.write"])
    headers = {"authorization": "Bearer " + device["token"]}
    body = {
        "command_id": identity(),
        "workspace_id": ws.id,
        "project_id": project["id"],
        "title": "Lost response",
        "execution_kind": "MANUAL",
    }
    await client.post(LAN_PREFIX + "/work-items", json=body, headers=headers)
    retry = await client.post(LAN_PREFIX + "/work-items", json=body, headers=headers)
    assert retry.status_code == 201
    assert (
        runtime.service.store.db.execute(
            "SELECT COUNT(*) FROM wb_work_items WHERE project_id=?", (project["id"],)
        ).fetchone()[0]
        == 1
    )
    assert (
        await client.post(LAN_PREFIX + "/work-items", json={**body, "title": "Different"}, headers=headers)
    ).status_code == 409
    runtime.devices.revoke(ws.id, device["device_id"], 1, HUMAN)
    assert (await client.post(LAN_PREFIX + "/work-items", json=body, headers=headers)).status_code == 401
