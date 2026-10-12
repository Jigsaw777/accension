"""Delivery migration defaults and strict bounded selections."""

import pytest
from test_workbench_agents import HUMAN, definition
from test_workbench_execution import execution, item, queue  # noqa: F401
from test_workbench_imports import commit, graph, plan, preview  # noqa: F401

from local_ai_router.workbench.schema import DeliveryPreview
from local_ai_router.workbench.store import identity, now


async def test_individual_attempt_keeps_null_batch(execution):  # noqa: F811
    runtime, _, _, _, _ = execution
    attempt = queue(execution, item(execution))
    row, contract = runtime.execution.contract(attempt)
    assert row["batch_id"] is None and contract.batch_id is None
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_delivery_batches").fetchone()


def test_delivery_preview_exact_selection():
    item_id = identity()
    fields = {
        "workspace_id": identity(),
        "project_id": identity(),
        "import_id": identity(),
        "item_ids": [item_id],
        "contract_revisions": {item_id: 1},
        "agents": {item_id: identity()},
        "budget": "0.10",
        "expires_at": now() + 10000,
    }
    assert DeliveryPreview.model_validate(fields).item_ids == [item_id]
    for change in ({"item_ids": [item_id, item_id]}, {"agents": {}}, {"contract_revisions": {}}, {"budget": "NaN"}):
        with pytest.raises(ValueError):
            DeliveryPreview.model_validate({**fields, **change})


@pytest.fixture
async def delivery(graph):  # noqa: F811
    planner, ws, project, prd = graph
    runtime = planner.runtime
    staged = preview(graph)
    result, _ = commit(graph, staged)
    agent = runtime.agents.configure(definition(ws, repository_permissions=[project["repository_ref"]]), HUMAN).body
    items = list(result.body["items"].values())
    body = {
        "workspace_id": ws.id,
        "project_id": project["id"],
        "import_id": staged["id"],
        "item_ids": items,
        "contract_revisions": {key: 1 for key in items},
        "agents": {key: agent["id"] for key in items},
        "budget": "0.20",
        "expires_at": now() + 60000,
    }
    return runtime, ws, body


async def test_human_batch_confirm_replay_and_active_conflict(delivery):
    runtime, ws, body = delivery
    reviewed = runtime.delivery.preview(body, HUMAN)
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_delivery_batches").fetchone()
    command = {
        "command_id": identity(),
        "workspace_id": ws.id,
        "preview": reviewed["preview"],
        "selection_hash": reviewed["selection_hash"],
    }
    first = runtime.delivery.confirm(command, HUMAN)
    repeat = runtime.delivery.confirm(command, HUMAN)
    assert repeat.replayed and repeat.body == first.body
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_attempts").fetchone()
    from local_ai_router.workbench.store import WorkbenchError

    with pytest.raises(WorkbenchError):
        runtime.delivery.confirm({**command, "command_id": identity()}, HUMAN)
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_delivery_batches").fetchone()[0] == 1


async def test_machine_or_stale_preview_cannot_approve(delivery):
    from local_ai_router.workbench.policy import CAPABILITIES, Principal
    from local_ai_router.workbench.schema import ActorType
    from local_ai_router.workbench.store import WorkbenchError

    runtime, ws, body = delivery
    reviewed = runtime.delivery.preview(body, HUMAN)
    command = {
        "command_id": identity(),
        "workspace_id": ws.id,
        "preview": reviewed["preview"],
        "selection_hash": reviewed["selection_hash"],
    }
    machine = Principal("operator", ActorType.MACHINE, CAPABILITIES, frozenset({"*"}), True)
    with pytest.raises(WorkbenchError) as failure:
        runtime.delivery.confirm(command, machine)
    assert failure.value.code == "HUMAN_REQUIRED"
    agent = runtime.agents.definition(next(iter(body["agents"].values())))
    runtime.agents.configure(
        definition(ws, repository_permissions=agent.repository_permissions, expected_revision=1, name="Changed agent"),
        HUMAN,
        agent.id,
    )
    with pytest.raises(WorkbenchError):
        runtime.delivery.confirm(command, HUMAN)
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_delivery_batches").fetchone()


def approve_delivery(runtime, ws, body, principal=HUMAN):
    reviewed = runtime.delivery.preview(body, principal)
    return runtime.delivery.confirm(
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "preview": reviewed["preview"],
            "selection_hash": reviewed["selection_hash"],
        },
        principal,
    ).body


@pytest.mark.parametrize("invalidate", ["device", "session", "expiry", "permissions"])
async def test_browser_delivery_grant_revalidates_origin(delivery, invalidate):
    from local_ai_router.workbench.store import WorkbenchError

    runtime, ws, body = delivery
    runtime.service.update(
        "workspace",
        ws.id,
        ws.id,
        {"command_id": identity(), "expected_revision": ws.revision, "privacy_mode": "PRIVATE_LAN"},
        HUMAN,
    )
    runtime.engine.settings.workbench_lan.workspaces = [ws.id]
    devices = runtime.devices
    invitation = devices.invitation(ws.id, HUMAN)
    pairing = devices.request(invitation["code"], "Operator", "synthetic")
    devices.approve(
        pairing["id"], ws.id, ["workspace.read", "execution.approve"], pairing["verification_phrase"], HUMAN
    )
    claimed = devices.claim(pairing["id"], pairing["challenge"], True, "synthetic")
    operator, _ = devices.authenticate(claimed["token"], True)
    batch = approve_delivery(runtime, ws, body, operator)
    runtime.delivery.authorization(batch["id"], body["item_ids"][0])
    if invalidate == "device":
        devices.revoke(ws.id, claimed["device_id"], 1, HUMAN)
    elif invalidate == "permissions":
        import json

        runtime.service.store.db.execute(
            "UPDATE wb_devices SET grants_json=? WHERE id=?",
            (json.dumps({ws.id: ["workspace.read"]}), claimed["device_id"]),
        )
    else:
        column = "revoked_at" if invalidate == "session" else "expires_at"
        runtime.service.store.db.execute(
            f"UPDATE wb_device_sessions SET {column}=? WHERE id=?", (now() - 1, operator.credential_id)
        )
    with pytest.raises(WorkbenchError):
        runtime.delivery.authorization(batch["id"], body["item_ids"][0])
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_attempts").fetchone()


async def test_preview_tampering_and_expiry_are_rejected(delivery):
    from local_ai_router.workbench.store import WorkbenchError

    runtime, ws, body = delivery
    reviewed = runtime.delivery.preview(body, HUMAN)
    command = {
        "command_id": identity(),
        "workspace_id": ws.id,
        "preview": reviewed["preview"],
        "selection_hash": reviewed["selection_hash"],
    }
    with pytest.raises(WorkbenchError):
        runtime.delivery.confirm({**command, "preview": command["preview"] + "x"}, HUMAN)
    signed = runtime.journal.decode(command["preview"], max_length=500000)
    signed["input"]["budget"] = "0.15"
    with pytest.raises(WorkbenchError, match="changed"):
        runtime.delivery.confirm({**command, "preview": runtime.journal.token(signed)}, HUMAN)
    signed["input"]["expires_at"] = now() - 1
    with pytest.raises(WorkbenchError):
        runtime.delivery.confirm({**command, "preview": runtime.journal.token(signed)}, HUMAN)


async def test_tick_queues_once_under_human_grant_and_waits_dependency(delivery, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    runtime, ws, body = delivery
    leader = runtime.scheduler.leader

    def locked_leader():
        assert runtime.engine.store.lock._is_owned()
        return leader()

    monkeypatch.setattr(runtime.scheduler, "leader", locked_leader)
    batch = approve_delivery(runtime, ws, body)
    await runtime.start()
    assert runtime.scheduler.acquire()
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: runtime.delivery.tick(), range(2)))
    rows = runtime.service.store.db.execute("SELECT * FROM wb_attempts").fetchall()
    assert len(rows) == 1 and rows[0]["state"] == "QUEUED"
    _, contract = runtime.execution.contract(rows[0]["id"])
    assert contract.batch_id == batch["id"] and contract.grant_id is None
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_execution_grants").fetchone()[0] == 0
    members = runtime.delivery.get(ws.id, batch["id"], HUMAN)["members"]
    assert sorted(m["member_state"] for m in members) == ["queued", "waiting"]
    assert next(m for m in members if m["member_state"] == "waiting")["blocked_reason"] == "DEPENDENCY_NOT_DONE"
    actors = runtime.service.store.db.execute(
        "SELECT actor_type FROM wb_events WHERE event_type='execution_queued'"
    ).fetchall()
    assert [row[0] for row in actors] == ["SYSTEM"]


async def test_pause_resume_cancel_retains_live_members_until_cleanup(delivery):
    runtime, ws, body = delivery
    batch = approve_delivery(runtime, ws, body)
    await runtime.start()
    assert runtime.scheduler.acquire()
    runtime.delivery.tick()
    batch = runtime.delivery.get(ws.id, batch["id"], HUMAN)

    def command(action):
        return runtime.delivery.command(
            ws.id,
            batch["id"],
            {
                "command_id": identity(),
                "expected_revision": batch["revision"],
                "action": action,
                "reason": "Reviewed synthetic delivery",
            },
            HUMAN,
        ).body

    batch = command("pause")
    assert runtime.scheduler.claim() is None
    batch = command("resume")
    assert batch["state"] == "RUNNING"
    batch = command("cancel")
    runtime.delivery.tick()
    assert (
        runtime.service.store.db.execute(
            "SELECT COUNT(*) FROM wb_delivery_members WHERE batch_id=? AND active=1", (batch["id"],)
        ).fetchone()[0]
        == 1
    )
    assert runtime.service.store.db.execute("SELECT state FROM wb_delivery_reservations").fetchone()[0] == "reserved"
    runtime.scheduler.cancel_owned()
    runtime.delivery.tick()
    assert (
        runtime.service.store.db.execute(
            "SELECT COUNT(*) FROM wb_delivery_members WHERE batch_id=? AND active=1", (batch["id"],)
        ).fetchone()[0]
        == 0
    )
    assert (
        runtime.service.store.db.execute("SELECT state FROM wb_delivery_batches WHERE id=?", (batch["id"],)).fetchone()[
            0
        ]
        == "CANCELLED"
    )


async def test_batch_cap_rolls_back_assignment_attempt_and_reservation(delivery):
    runtime, ws, body = delivery
    batch = approve_delivery(runtime, ws, {**body, "budget": "0.001"})
    await runtime.start()
    assert runtime.scheduler.acquire()
    runtime.delivery.tick()
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_attempts").fetchone()
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_assignments").fetchone()
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_delivery_reservations").fetchone()
    assert runtime.delivery.get(ws.id, batch["id"], HUMAN)["state"] == "NEEDS_REVIEW"


async def test_real_scheduler_delivery_waits_human_completion_before_dependency(graph):  # noqa: F811
    planner, ws, project, _ = graph
    runtime = planner.runtime
    from pathlib import Path

    root = Path(runtime.service.repository(project["repository_ref"]).path)
    (root / "test_contract.py").write_text(
        'import unittest\nfrom greeting import greet\nclass Contract(unittest.TestCase):\n    def test_behavior(self):\n        self.assertEqual(greet(" Ada "), "Hello, Ada!")\n        self.assertEqual(greet(" "), "Hello, world!")\n'
    )
    proposal = plan()
    proposal["tasks"][0]["expected_artifacts"] = ["greeting.py"]
    proposal["tasks"][1]["expected_artifacts"] = ["USAGE.md"]
    staged = preview(graph, [proposal])
    result, _ = commit(graph, staged)
    agent = runtime.agents.configure(definition(ws, repository_permissions=[project["repository_ref"]]), HUMAN).body
    ids = list(result.body["items"].values())
    body = {
        "workspace_id": ws.id,
        "project_id": project["id"],
        "import_id": staged["id"],
        "item_ids": ids,
        "contract_revisions": {key: 1 for key in ids},
        "agents": {key: agent["id"] for key in ids},
        "budget": "0.20",
        "expires_at": now() + 60000,
    }
    batch = approve_delivery(runtime, ws, body)
    await runtime.start()
    assert runtime.scheduler.acquire()
    runtime.delivery.tick()
    claim = runtime.scheduler.claim()
    assert claim is not None
    await runtime.scheduler.work(*claim)
    first = runtime.service.store.get("work_item", ws.id, ids[0])
    assert first.status == "REVIEW" and first.active_attempt_id is None
    runtime.delivery.tick()
    assert runtime.delivery.get(ws.id, batch["id"], HUMAN)["state"] == "RUNNING"
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_attempts").fetchone()[0] == 1
    runtime.service.item_command(
        ws.id,
        first.id,
        {
            "command_id": identity(),
            "expected_revision": first.revision,
            "command": "approve_completion",
            "contract_revision": first.contract_revision,
            "evidence_hash": runtime.service.evidence(first)[0]["evidence_hash"],
            "reason": "Actual independent checks and artifacts reviewed",
        },
        HUMAN,
    )
    runtime.delivery.tick()
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_attempts").fetchone()[0] == 2
    claim = runtime.scheduler.claim()
    assert claim is not None
    await runtime.scheduler.work(*claim)
    second = runtime.service.store.get("work_item", ws.id, ids[1])
    assert second.status == "REVIEW"
    runtime.service.item_command(
        ws.id,
        second.id,
        {
            "command_id": identity(),
            "expected_revision": second.revision,
            "command": "approve_completion",
            "contract_revision": second.contract_revision,
            "evidence_hash": runtime.service.evidence(second)[0]["evidence_hash"],
            "reason": "Actual independent checks and artifacts reviewed",
        },
        HUMAN,
    )
    runtime.delivery.tick()
    final = runtime.delivery.get(ws.id, batch["id"], HUMAN)
    assert final["state"] == "COMPLETED" and all(m["status"] == "DONE" for m in final["members"])
    assert (
        runtime.service.store.db.execute(
            "SELECT COUNT(*) FROM wb_delivery_reservations WHERE state='settled'"
        ).fetchone()[0]
        == 2
    )


async def test_delivery_intent_crash_rollback_and_committed_restart(delivery, monkeypatch):
    runtime, ws, body = delivery
    batch = approve_delivery(runtime, ws, body)
    await runtime.start()
    assert runtime.scheduler.acquire()
    original = runtime.execution.request

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic crash before transaction commit")

    monkeypatch.setattr(runtime.execution, "request", interrupted)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        runtime.delivery.tick()
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_attempts").fetchone()
    assert not runtime.service.store.db.execute("SELECT 1 FROM wb_delivery_reservations").fetchone()
    monkeypatch.setattr(runtime.execution, "request", original)
    runtime.delivery.tick()
    attempt = runtime.service.store.db.execute("SELECT id FROM wb_attempts").fetchone()[0]
    runtime.delivery.tick()
    assert [r[0] for r in runtime.service.store.db.execute("SELECT id FROM wb_attempts")] == [attempt]
    assert runtime.delivery.get(ws.id, batch["id"], HUMAN)["state"] == "RUNNING"


async def test_changed_grant_requires_fresh_review_and_preserves_audit(delivery):
    from local_ai_router.workbench.store import WorkbenchError

    runtime, ws, body = delivery
    batch = approve_delivery(runtime, ws, body)
    await runtime.start()
    assert runtime.scheduler.acquire()
    runtime.service.store.db.execute(
        "UPDATE wb_delivery_grants SET expires_at=? WHERE batch_id=?", (now() - 1, batch["id"])
    )
    runtime.delivery.tick()
    current = runtime.delivery.get(ws.id, batch["id"], HUMAN)
    assert current["state"] == "NEEDS_REVIEW"
    with pytest.raises(WorkbenchError):
        runtime.delivery.command(
            ws.id,
            batch["id"],
            {
                "command_id": identity(),
                "expected_revision": current["revision"],
                "action": "resume",
                "reason": "Cannot silently replace approval",
            },
            HUMAN,
        )
    reviewed = runtime.delivery.preview(body, HUMAN)
    result = runtime.delivery.reapprove(
        ws.id,
        batch["id"],
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "expected_revision": current["revision"],
            "preview": reviewed["preview"],
            "selection_hash": reviewed["selection_hash"],
        },
        HUMAN,
    )
    assert result.body["state"] == "APPROVED"
    assert (
        runtime.service.store.db.execute(
            "SELECT COUNT(*) FROM wb_delivery_grants WHERE batch_id=?", (batch["id"],)
        ).fetchone()[0]
        == 2
    )
    assert (
        runtime.service.store.db.execute(
            "SELECT COUNT(*) FROM wb_delivery_grants WHERE batch_id=? AND revoked_at IS NULL", (batch["id"],)
        ).fetchone()[0]
        == 1
    )
    runtime.delivery.tick()
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_attempts").fetchone()[0] == 1
