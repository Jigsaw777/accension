"""Adversarial trust, evidence and lifecycle checks over real disposable runtime."""

import asyncio
import hashlib
import json

import pytest
from test_workbench_agents import HUMAN
from test_workbench_execution import execution as execution
from test_workbench_execution import item, queue
from test_workbench_interfaces import interfaces as interfaces
from test_workbench_lan_access import lan as lan
from test_workbench_lan_access import pair
from test_workbench_scheduler import queued
from test_workbench_store import project, request, workspace
from test_workbench_store import wb as wb

from local_ai_router.contracts import ExecutionAborted
from local_ai_router.workbench.lan import LAN_PREFIX, safe_projection
from local_ai_router.workbench.store import WorkbenchError, identity, now


async def test_read_permission_does_not_imply_artifact_permission(lan):
    runtime, ws, project, _, client = lan
    work = runtime.service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "project_id": project["id"],
            "title": "Private evidence",
            "execution_kind": "MANUAL",
        },
        HUMAN,
    ).body
    device = pair(runtime, ws, ["workspace.read"], name="Metadata only")
    headers = {"authorization": "Bearer " + device["token"]}
    path = LAN_PREFIX + "/work-items/" + work["id"]
    assert (await client.get(path, params={"workspace_id": ws.id}, headers=headers)).status_code == 200
    response = await client.get(path + "/evidence", params={"workspace_id": ws.id}, headers=headers)
    assert response.status_code == 403


async def test_device_revoke_fences_issued_execution_grant(execution):
    from local_ai_router.config import WorkbenchLAN

    runtime, ws, _, _, _ = execution
    runtime.service.update(
        "workspace",
        ws.id,
        ws.id,
        {"command_id": identity(), "expected_revision": ws.revision, "privacy_mode": "PRIVATE_LAN"},
        HUMAN,
    )
    runtime.engine.settings.workbench_lan = WorkbenchLAN(origins=["https://localhost:8766"], workspaces=[ws.id])
    device = pair(runtime, ws, ["workspace.read", "execution.approve"], True, name="Grant owner")
    attempt = queue(execution, item(execution))
    runtime.service.store.db.execute(
        "UPDATE wb_execution_grants SET principal_id=?", ("device-" + device["device_id"],)
    )
    runtime.devices.revoke(ws.id, device["device_id"], 1, HUMAN)
    with pytest.raises(ExecutionAborted, match="revoked"):
        runtime.execution.guard(attempt, runtime.agents.generation, 1)


@pytest.mark.parametrize(
    "failure", [ExecutionAborted("fenced"), RuntimeError("storage failed")], ids=["fence", "storage"]
)
async def test_dispatch_loop_failure_cancels_owned_worker(execution, monkeypatch, failure):
    runtime, _, _, _, _ = execution
    task = asyncio.create_task(asyncio.sleep(10))
    runtime.scheduler.workers["synthetic"] = (task, 1)

    def fail():
        raise failure

    monkeypatch.setattr(runtime.scheduler, "heartbeat", fail)
    await runtime.scheduler.loop()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.scheduler.error
    runtime.scheduler.workers.clear()


def test_store_page_filters_and_cursors_fail_closed(wb):
    ws = workspace(wb)
    p = project(wb, ws)
    with wb.router.transaction():
        work = wb.allocate_item(request(ws, p), "human")
    assert wb.list(
        "work_item", ws.id, priority=work.priority, item_type=work.type, project_id=p.id, status=work.status
    )[0]
    for kind, fields in (
        ("project", {"priority": 1}),
        ("project", {"project_id": p.id}),
        ("project", {"status": "READY"}),
        ("work_item", {"after": "bad"}),
        ("project", {"after": (1, p.id)}),
        ("agent", {}),
        ("work_item", {"search": "x" * 201}),
    ):
        with pytest.raises(WorkbenchError):
            wb.list(kind, ws.id, **fields)
    assert wb.list("project", ws.id, search=p.name, after="", include_archived=True)[0]


def test_store_rejects_corrupt_references_and_unowned_writes(wb):
    ws = workspace(wb)
    p = project(wb, ws)
    with pytest.raises(RuntimeError):
        wb.insert("project", p)
    with pytest.raises(RuntimeError):
        wb.replace("project", p, 1)
    with pytest.raises(RuntimeError):
        wb.allocate_item(request(ws, p), "human")
    with wb.router.transaction():
        work = wb.allocate_item(request(ws, p), "human")
        with pytest.raises(WorkbenchError, match="identity"):
            wb.replace("work_item", work.model_copy(update={"created_by": "forged", "revision": 2}), 1)
        with pytest.raises(WorkbenchError, match="display key"):
            wb._references("work_item", work.model_copy(update={"display_key": "BAD-1"}), writable=True)
        with pytest.raises(WorkbenchError, match="Workspace does not exist"):
            wb._references("project", p.model_copy(update={"workspace_id": identity()}), writable=True)
        with pytest.raises(WorkbenchError, match="parent_item_id"):
            wb._references("work_item", work.model_copy(update={"parent_item_id": identity()}), writable=True)
        wb.db.execute("UPDATE wb_project_counters SET next_number=1 WHERE project_id=?", (p.id,))
        wb.replace("project", p.model_copy(update={"key": "CHANGED", "revision": 2}), 1)
        wb.db.execute("DELETE FROM wb_project_counters WHERE project_id=?", (p.id,))
        with pytest.raises(WorkbenchError, match="counter"):
            wb.allocate_item(request(ws, p), "human")
        wb.db.execute("UPDATE wb_workspaces SET archived_at=? WHERE id=?", (now(), ws.id))
        with pytest.raises(WorkbenchError, match="Workspace is archived"):
            wb._references("project", p, writable=True)


@pytest.mark.parametrize("outcome", ["failure", "cancel", "changed"], ids=["failure", "cancel", "changed"])
async def test_team_lead_failure_has_no_assignment_authority(execution, monkeypatch, outcome):
    from local_ai_router.providers import ProviderError
    from local_ai_router.workbench.schema import TeamLeadAdvice

    runtime, ws, _, _, _ = execution
    work = item(execution)
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise ProviderError("failure")
        return TeamLeadAdvice(action="assign", risk=1, complexity=1, explanation="Advisory only")

    monkeypatch.setattr(runtime.engine, "generate_planning_document", generate)
    operation = runtime.planning.team_lead(
        {"command_id": identity(), "workspace_id": ws.id, "item_id": work["id"], "expected_revision": work["revision"]},
        HUMAN,
    )
    task = runtime.planning.tasks[operation.body["id"]]
    await entered.wait()
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if outcome == "changed":
            runtime.service.update(
                "work_item",
                ws.id,
                work["id"],
                {"command_id": identity(), "expected_revision": work["revision"], "title": "Changed"},
                HUMAN,
            )
        release.set()
        await task
    result = runtime.planning.get(ws.id, operation.body["id"], HUMAN)
    assert result["state"] == ("UNCERTAIN" if outcome == "cancel" else "FAILED")
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_assignments").fetchone()[0] == 0
    await runtime.planning.run_advice(operation.body["id"], HUMAN)


async def test_receipt_corruption_rejected_and_scoped_sections(execution):
    runtime, ws, _, _, _ = execution
    attempt = queue(execution, item(execution))
    with pytest.raises(WorkbenchError):
        runtime.execution.read_attempt(ws.id, attempt, HUMAN, "receipt")
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    assert runtime.execution.read_attempt(ws.id, attempt, HUMAN, "verification")["items"]
    assert runtime.execution.read_attempt(ws.id, attempt, HUMAN, "artifacts")["items"]
    assert runtime.execution.read_attempt(ws.id, attempt, HUMAN)["models"]
    with pytest.raises(WorkbenchError):
        runtime.execution.read_attempt(identity(), attempt, HUMAN)
    runtime.service.store.db.execute("UPDATE wb_receipt_snapshots SET receipt_json='{}' WHERE attempt_id=?", (attempt,))
    with pytest.raises(WorkbenchError, match="integrity"):
        runtime.execution.read_attempt(ws.id, attempt, HUMAN, "receipt")


async def test_artifact_content_scope_size_encoding_and_hash(execution):
    runtime, ws, _, _, repo = execution
    attempt = queue(execution, item(execution))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    artifact = runtime.execution.read_attempt(ws.id, attempt, HUMAN, "artifacts")["items"][0]
    aid = artifact["id"]
    assert "greet" in runtime.execution.artifact_content(ws.id, aid, HUMAN)["content"]
    with pytest.raises(WorkbenchError):
        runtime.execution.artifact_content(identity(), aid, HUMAN)
    path = repo / artifact["relative_path_or_external_reference"]
    path.write_bytes(b"changed")
    with pytest.raises(WorkbenchError, match="changed"):
        runtime.execution.artifact_content(ws.id, aid, HUMAN)
    runtime.service.store.db.execute("UPDATE wb_artifacts SET size_bytes=256001 WHERE id=?", (aid,))
    with pytest.raises(WorkbenchError, match="limit"):
        runtime.execution.artifact_content(ws.id, aid, HUMAN)
    path.write_bytes(b"\xff")
    runtime.service.store.db.execute(
        "UPDATE wb_artifacts SET size_bytes=1,content_hash=? WHERE id=?", (hashlib.sha256(b"\xff").hexdigest(), aid)
    )
    with pytest.raises(WorkbenchError, match="UTF-8"):
        runtime.execution.artifact_content(ws.id, aid, HUMAN)
    for fields in ({"limit": 101}, {"before": -1}):
        with pytest.raises(WorkbenchError):
            runtime.execution.attempts(ws.id, artifact["item_id"], HUMAN, **fields)


async def test_corrupt_contract_and_invalid_recovery_fail_closed(execution, monkeypatch):
    runtime, ws, _, _, _ = execution
    attempt = queue(execution, item(execution))
    with pytest.raises(WorkbenchError):
        runtime.execution.contract(identity())
    with pytest.raises(WorkbenchError, match="fenced"):
        await runtime.execution.recover(ws.id, attempt, "resume", HUMAN)
    await runtime.execution.bind(attempt, runtime.agents.generation, 1)
    runtime.service.store.db.execute(
        "UPDATE wb_attempts SET state='RECONCILING',lease_until=? WHERE id=?", (now() - 1, attempt)
    )
    with pytest.raises(WorkbenchError, match="Unknown"):
        await runtime.execution.recover(ws.id, attempt, "invalid", HUMAN)

    async def fail(*args, **kwargs):
        raise RuntimeError("Interrupted resume")

    monkeypatch.setattr("local_ai_router.recovery.recover", fail)
    try:
        with pytest.raises(RuntimeError):
            await runtime.execution.recover(ws.id, attempt, "resume", HUMAN)
        assert runtime.execution.contract(attempt)[0]["state"] == "RECONCILING"
    finally:
        runtime.scheduler.workers.pop(attempt, None)
    runtime.service.store.db.execute("UPDATE wb_attempts SET contract_json='{}' WHERE id=?", (attempt,))
    with pytest.raises(WorkbenchError, match="integrity"):
        runtime.execution.contract(attempt)


async def test_corrupt_revision_command_and_missing_skill_fail_closed(execution):
    from local_ai_router.workbench.schema import PlanningInput
    from local_ai_router.workbench.service import payload_hash

    runtime, ws, project, _, _ = execution
    work = item(execution)
    with pytest.raises(RuntimeError):
        runtime.service.operation_intent(ws.id, "manual", work["id"], 1, "hash")
    for fields in ({"limit": 0}, {"before": 0}):
        with pytest.raises(WorkbenchError):
            runtime.service.revisions("work_item", ws.id, work["id"], HUMAN, **fields)
    runtime.service.store.db.execute(
        "UPDATE wb_entity_versions SET snapshot_json='{}' WHERE entity_id=?", (work["id"],)
    )
    with pytest.raises(WorkbenchError, match="revision"):
        runtime.service.revisions("work_item", ws.id, work["id"], HUMAN)
    runtime.service.store.db.execute(
        "UPDATE wb_commands SET response_json='invalid' WHERE command_id=?", (work.get("command_id", "none"),)
    )
    command = {
        "command_id": identity(),
        "workspace_id": ws.id,
        "project_id": project["id"],
        "title": "Replay",
        "execution_kind": "MANUAL",
    }
    runtime.service.create("work_item", command, HUMAN)
    runtime.service.store.db.execute(
        "UPDATE wb_commands SET response_json='invalid' WHERE command_id=?", (command["command_id"],)
    )
    with pytest.raises(WorkbenchError, match="command result"):
        runtime.service.create("work_item", command, HUMAN)
    runtime.service.store.db.execute(
        "UPDATE wb_commands SET state='PENDING' WHERE command_id=?", (command["command_id"],)
    )
    with pytest.raises(WorkbenchError, match="reconciliation"):
        runtime.service.create("work_item", command, HUMAN)
    current = runtime.service.store.get("work_item", ws.id, work["id"])
    for skill in ("missing-skill",):
        with pytest.raises(WorkbenchError, match="skill"):
            runtime.service.readiness(
                current.model_copy(update={"spec": current.spec.model_copy(update={"required_skills": [skill]})})
            )
    inputs = {"workspace_id": ws.id, "project_id": project["id"]}
    for extra in (
        {"requirement": ""},
        {"requirement": "Plan", "template_id": "work-item-default"},
        {"kind": "decompose"},
    ):
        with pytest.raises(WorkbenchError):
            runtime.planning.context(PlanningInput.model_validate({**inputs, **extra}), HUMAN)
    with runtime.service.store.router.transaction():
        op = runtime.service.operation_intent(ws.id, "manual", work["id"], 1, payload_hash({}))
        runtime.service.store.db.execute("UPDATE wb_work_items SET status='VERIFYING' WHERE id=?", (work["id"],))
        runtime.service.store.db.execute(
            "UPDATE wb_operations SET result_json=? WHERE id=?", (json.dumps({}), op.body["id"])
        )
    runtime.planning.reconcile_startup()
    assert runtime.service.store.get("work_item", ws.id, work["id"]).reason_code == "MANUAL_VERIFICATION_INTERRUPTED"


async def test_local_api_protocol_rejects_mismatched_operations(execution, interfaces):
    runtime, ws, _, _, _ = execution
    client, _, _ = interfaces
    prefix = "/accs/v1/workbench"
    for path, body in (
        ("/prds/generate", {"kind": "decompose"}),
        ("/prds/" + identity() + "/decompose", {"prd_id": identity(), "kind": "decompose"}),
        ("/prds/" + identity() + "/reconcile", {"prd_id": identity()}),
    ):
        assert (await client.post(prefix + path, json=body)).status_code == 400
    assert (await client.get(prefix + "/lan/setup")).status_code == 200
    assert (await client.get(prefix + "/templates/prd-default/export")).status_code == 200
    assert (await client.get(prefix + "/workspaces", params={"limit": 0})).status_code == 422
    assert (
        await client.get(prefix + "/work-items", params={"workspace_id": ws.id, "cursor": "invalid"})
    ).status_code == 422


async def test_claim_stale_grant_and_queued_cancel_release(execution):
    runtime, ws, _, _, _ = execution
    attempt = queued(execution)
    assert runtime.scheduler.acquire()
    runtime.service.store.db.execute("UPDATE wb_execution_grants SET revoked_at=?", (now(),))
    assert runtime.scheduler.claim() is None
    assert runtime.execution.contract(attempt)[0]["state"] == "RECONCILING"


async def test_queued_cancellation_atomic_and_budget_settled(execution):
    runtime, ws, _, _, _ = execution
    attempt = queued(execution)
    runtime.service.store.db.execute("UPDATE wb_attempts SET cancel_requested_at=? WHERE id=?", (now(), attempt))
    runtime.scheduler.cancel_owned()
    row, contract = runtime.execution.contract(attempt)
    assert row["state"] == "CANCELLED"
    assert runtime.service.store.get("work_item", ws.id, contract.item_id).active_attempt_id is None
    assert (
        runtime.service.store.db.execute(
            "SELECT state FROM wb_budget_reservations WHERE attempt_id=?", (attempt,)
        ).fetchone()[0]
        == "SETTLED"
    )
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


async def test_shutdown_timeout_preserves_lock_and_fences(execution, monkeypatch):
    runtime, _, _, _, _ = execution
    attempt = queued(execution)
    assert runtime.scheduler.acquire()
    claim = runtime.scheduler.claim()
    release = asyncio.Event()

    async def stubborn():
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    runtime.scheduler.workers[attempt] = (task, claim[1])
    original = asyncio.wait

    async def short_wait(tasks, **kwargs):
        return await original(tasks, timeout=0.01)

    monkeypatch.setattr(asyncio, "wait", short_wait)
    with pytest.raises(WorkbenchError, match="database must remain open"):
        await runtime.scheduler.close()
    assert runtime.scheduler.owner_lock is not None
    row, _ = runtime.execution.contract(attempt)
    assert row["state"] == "RECONCILING" and row["fence"] > claim[1]
    with pytest.raises(ExecutionAborted):
        runtime.execution.guard(attempt, runtime.scheduler.generation, claim[1])
    release.set()
    await task
    runtime.scheduler.workers.clear()
    await runtime.scheduler.close()


async def test_lan_pair_claim_session_csrf_logout_and_bearer_rotate(lan):
    runtime, ws, _, _, client = lan
    issued = runtime.devices.invitation(ws.id, HUMAN)
    pending = await client.post("/pairing/request", json={"code": issued["code"], "name": "Protocol"})
    assert pending.status_code == 200
    proof = {"id": pending.json()["id"], "challenge": pending.json()["challenge"], "browser": True}
    status = await client.post("/pairing/status", json=proof)
    assert status.json()["status"] == "PENDING_APPROVAL"
    runtime.devices.approve(proof["id"], ws.id, ["workspace.read"], pending.json()["verification_phrase"], HUMAN)
    claimed = await client.post("/pairing/claim", json=proof)
    assert "token" not in claimed.json()
    assert all(value in claimed.headers["set-cookie"].lower() for value in ("httponly", "secure", "samesite=strict"))
    credential = client.cookies.get("accension_device")
    actor, _ = runtime.devices.authenticate(credential, True)
    stored = runtime.devices.store.db.execute(
        "SELECT secret_digest FROM wb_device_sessions WHERE id=?", (actor.credential_id,)
    ).fetchone()[0]
    assert stored == hashlib.sha256(credential.split(".")[1].encode()).hexdigest()
    assert credential != runtime.engine.settings.token and credential not in claimed.text
    session = await client.get("/session")
    assert session.status_code == 200 and session.json()["csrf"]
    headers = {"origin": "https://localhost:8766", "x-csrf-token": session.json()["csrf"]}
    logout = await client.post("/session/logout", json={}, headers=headers)
    assert logout.status_code == 200 and "max-age=0" in logout.headers["set-cookie"].lower()
    assert (await client.get(LAN_PREFIX + "/workspaces")).status_code == 401
    bearer = pair(runtime, ws, ["workspace.read"], name="Rotate")
    rotated = await client.post("/credentials/rotate", headers={"authorization": "Bearer " + bearer["token"]})
    assert rotated.status_code == 200 and rotated.json()["token"]
    assert (
        await client.get(LAN_PREFIX + "/workspaces", headers={"authorization": "Bearer " + bearer["token"]})
    ).status_code == 401
    assert (
        await client.get("/session", headers={"authorization": "Bearer " + rotated.json()["token"]})
    ).status_code == 403
    assert (await client.get("/assets/app.js")).status_code == 404
    assert (await client.get("/assets/workbench-lan.js")).status_code == 200
    assert (await client.get("/")).status_code == 200


async def test_lan_authentication_does_not_expose_exception_details(lan, monkeypatch):
    runtime, _, _, _, client = lan

    def fail(*args, **kwargs):
        raise WorkbenchError("UNAUTHENTICATED", "injected private credential and path", 401)

    monkeypatch.setattr(runtime.devices, "authenticate", fail)
    response = await client.get(LAN_PREFIX + "/workspaces")
    assert response.status_code == 401 and response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "private credential" not in response.text


async def test_device_approval_deny_revision_scope_and_ambiguous_auth(lan):
    runtime, ws, _, _, client = lan
    issued = runtime.devices.invitation(ws.id, HUMAN)
    pending = runtime.devices.request(issued["code"], "Denied", "deny-source")
    with pytest.raises(WorkbenchError):
        runtime.devices.approve(pending["id"], ws.id, ["workspace.read"], "wrong", HUMAN)
    with pytest.raises(WorkbenchError):
        runtime.devices.approve(
            pending["id"], ws.id, ["workspace.read"], pending["verification_phrase"], HUMAN, expected_revision=1
        )
    runtime.devices.approve(
        pending["id"], ws.id, ["workspace.read"], pending["verification_phrase"], HUMAN, reject=True
    )
    with pytest.raises(WorkbenchError):
        runtime.devices.claim(pending["id"], pending["challenge"], False, "deny-source")
    with pytest.raises(WorkbenchError):
        runtime.devices.grants(ws.id, ["workspace.read", "device.admin"])
    device = pair(runtime, ws, ["workspace.read", "review.approve"], True, name="Ambiguous")
    client.cookies.set("accension_device", device["token"])
    assert (await client.get(LAN_PREFIX + "/workspaces", headers={"authorization": "Bearer x"})).status_code == 401
    actor, _ = runtime.devices.authenticate(device["token"], True)
    with pytest.raises(WorkbenchError):
        runtime.devices.rotate(actor)
    with pytest.raises(WorkbenchError):
        runtime.devices.revoke(ws.id, device["device_id"], 99, HUMAN)
    for token in (None, "invalid", "x.y"):
        with pytest.raises(WorkbenchError):
            runtime.devices.authenticate(token)
    assert safe_projection(
        {"repository_ref": "private", "nested": {"token": "hidden", "path": "C:\\private\\file", "safe": "ok"}}
    ) == {"nested": {"path": "[LOCAL PATH]", "safe": "ok"}}


async def test_current_evidence_and_archive_guards(execution):
    from local_ai_router.workbench.policy import Principal
    from local_ai_router.workbench.schema import ActorType

    runtime, ws, project, _, _ = execution
    work = item(execution)
    current = runtime.service.store.get("work_item", ws.id, work["id"])
    with pytest.raises(WorkbenchError, match="verified evidence"):
        runtime.service.evidence(current)
    attempt = queue(execution, work)
    current = runtime.service.store.get("work_item", ws.id, work["id"])
    with pytest.raises(WorkbenchError, match="Active"):
        runtime.service.archive(
            "work_item", ws.id, current.id, {"command_id": identity(), "expected_revision": current.revision}, HUMAN
        )
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    current = runtime.service.store.get("work_item", ws.id, work["id"])
    metadata = Principal("metadata", ActorType.HUMAN, frozenset({"workspace.read"}), frozenset({ws.id}), False)
    assert runtime.service.allowed_actions(current, metadata) == []
    runtime.service.store.db.execute(
        "UPDATE wb_verifications SET evidence_hash=? WHERE attempt_id=?", ("0" * 64, attempt)
    )
    with pytest.raises(WorkbenchError, match="integrity"):
        runtime.service.evidence(current)
    assert "approve_completion" not in runtime.service.allowed_actions(current, HUMAN)
    for fields in ({"required_tools": ["unknown"]}, {"validation_checks": ["unknown"]}):
        with pytest.raises(WorkbenchError):
            runtime.service.readiness(current.model_copy(update={"spec": current.spec.model_copy(update=fields)}))
    runtime.service.skills = None
    with pytest.raises(WorkbenchError, match="registry"):
        runtime.service.readiness(
            current.model_copy(update={"spec": current.spec.model_copy(update={"required_skills": ["missing"]})})
        )
    for kind in ("prd", "plan_import"):
        with pytest.raises(WorkbenchError):
            runtime.service.archive(kind, ws.id, identity(), {"command_id": identity(), "expected_revision": 1}, HUMAN)
    runtime.service.archive(
        "project", ws.id, project["id"], {"command_id": identity(), "expected_revision": project["revision"]}, HUMAN
    )
    with pytest.raises(WorkbenchError, match="already"):
        runtime.service.archive(
            "project",
            ws.id,
            project["id"],
            {"command_id": identity(), "expected_revision": project["revision"] + 1},
            HUMAN,
        )


async def test_frozen_skill_and_execution_command_guards(execution, monkeypatch):
    from local_ai_router.schema import SkillBinding, SkillContract

    runtime, ws, _, _, _ = execution
    work = item(execution)
    with pytest.raises(WorkbenchError, match="Expected"):
        runtime.execution.request(
            ws.id, work["id"], {"command_id": identity(), "expected_revision": 1, "command": "cancel"}, HUMAN
        )
    attempt = queue(execution, work)
    monkeypatch.setattr(
        runtime.engine.skills,
        "freeze",
        lambda *args, **kwargs: SkillContract(skills=[SkillBinding(id="changed", content_hash="0" * 64)]),
    )
    with pytest.raises(ExecutionAborted, match="Skill content changed"):
        await runtime.execution.bind(attempt, runtime.agents.generation, 1)
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


async def test_absent_operation_and_recovery_prerequisites(execution):
    runtime, ws, _, _, _ = execution
    await runtime.execution.manual_run(identity(), HUMAN)
    await runtime.execution.recovery_run(identity(), HUMAN)
    attempt = queue(execution, item(execution))
    with pytest.raises(WorkbenchError):
        await runtime.execution.recover(identity(), attempt, "inspect", HUMAN)
    runtime.service.store.db.execute("UPDATE wb_attempts SET state='RECONCILING' WHERE id=?", (attempt,))
    current = runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt)[1].item_id)
    with pytest.raises(WorkbenchError, match="owning daemon"):
        runtime.execution.recovery_request(
            ws.id,
            attempt,
            {"command_id": identity(), "expected_revision": current.revision, "action": "inspect"},
            HUMAN,
        )
