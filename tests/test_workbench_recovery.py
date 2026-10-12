"""Crash boundaries, real validation child cancellation, and hash-safe cleanup."""

import asyncio
import os
import subprocess
import sys

import pytest
from test_workbench_agents import HUMAN
from test_workbench_execution import execution as execution
from test_workbench_execution import item, queue
from test_workbench_scheduler import queued

from local_ai_router.workbench.store import identity, now


@pytest.mark.parametrize("changed", [False, True], ids=["resume", "changed-policy"])
async def test_resume_preserves_attempt_and_rejects_changed_policy(execution, changed):
    runtime, ws, _, _, _ = execution
    attempt = queue(execution, item(execution))
    await runtime.execution.bind(attempt, runtime.agents.generation, 1)
    runtime.service.store.db.execute(
        "UPDATE wb_attempts SET state='RECONCILING',lease_until=? WHERE id=?", (now() - 1, attempt)
    )
    row, contract = runtime.execution.contract(attempt)
    if changed:
        runtime.service.store.db.execute(
            "UPDATE wb_workspaces SET execution_policy_json=json_set(execution_policy_json,'$.max_active_runs',2) WHERE id=?",
            (ws.id,),
        )
        from local_ai_router.workbench.store import WorkbenchError

        with pytest.raises(WorkbenchError, match="changed policy"):
            await runtime.execution.recover(ws.id, attempt, "resume", HUMAN)
    else:
        try:
            result = await runtime.execution.recover(ws.id, attempt, "resume", HUMAN)
            assert result["status"] == "complete"
            assert runtime.execution.contract(attempt)[0]["state"] == "SUCCEEDED"
            assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_attempts").fetchone()[0] == 1
            assert runtime.service.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        finally:
            runtime.scheduler.workers.pop(attempt, None)


async def test_cancel_validation_child_tree(execution):
    runtime, ws, _, _, repo = execution
    (repo / "test_slow.py").write_text(
        "import unittest, subprocess, sys, pathlib, time\n"
        "class Slow(unittest.TestCase):\n"
        "    def test_child(self):\n"
        "        child=subprocess.Popen([sys.executable,'-c',\"import time,pathlib; pathlib.Path('child-live').write_text('yes'); time.sleep(3); pathlib.Path('child-survived').write_text('unsafe')\"])\n"
        "        pathlib.Path('child-pid').write_text(str(child.pid))\n"
        "        time.sleep(30)\n"
    )
    attempt_id = queued(execution)
    assert runtime.scheduler.acquire()
    claim = runtime.scheduler.claim()
    task = asyncio.create_task(runtime.scheduler.work(*claim))
    runtime.scheduler.workers[attempt_id] = (task, claim[1])
    for _ in range(150):
        if (repo / "child-live").exists():
            break
        await asyncio.sleep(0.05)
    assert (repo / "child-live").exists()
    work = runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt_id)[1].item_id)
    result = runtime.service.item_command(
        ws.id, work.id, {"command_id": identity(), "expected_revision": work.revision, "command": "cancel"}, HUMAN
    )
    assert result.status == 202 and result.body["status"] != "CANCELLED"
    runtime.scheduler.cancel_owned()
    await asyncio.wait_for(task, 10)
    row, contract = runtime.execution.contract(attempt_id)
    assert row["state"] == "CANCELLED"
    assert runtime.service.store.get("work_item", ws.id, contract.item_id).active_attempt_id is None
    assert not (repo / "greeting.py").exists()
    await asyncio.sleep(3.2)
    assert not (repo / "child-survived").exists()


@pytest.mark.parametrize("boundary", ["intent", "plan", "link", "edit", "validation", "terminal", "receipt"])
async def test_crash_each_intent_to_receipt_boundary(execution, boundary):
    runtime, ws, _, _, repo = execution
    attempt_id = queue(execution, item(execution))
    script = r"""
import asyncio, os, sys
from pathlib import Path
from local_ai_router.config import load, Repository
from local_ai_router.engine import Engine
from local_ai_router.safety import Edits
from local_ai_router import receipts
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.store import now
home,root,attempt,boundary=sys.argv[1:]
settings=load(Path(home),mock=True)
settings.discovery.enabled=False
settings.repositories=[Repository(path=root,validation={"tests":["{python}","-m","unittest","discover","-v"]})]
engine=Engine(settings)
runtime=WorkbenchRuntime(engine)
engine.store.db.execute("UPDATE wb_attempts SET owner_generation=?,fence=2,lease_until=? WHERE id=?",(runtime.agents.generation,now()+60000,attempt))
if boundary=="intent": os._exit(9)
if boundary=="plan":
    original=engine.persist_plan
    def persist(*a,**k):
        original(*a,**k); os._exit(9)
    engine.persist_plan=persist
if boundary=="edit":
    original=Edits.apply
    def apply(*a,**k):
        original(*a,**k); os._exit(9)
    Edits.apply=apply
if boundary=="validation":
    original=runtime.execution.observe
    def observe(a,g,f,s):
        original(a,g,f,s)
        if s.kind=="validation_end" and s.task_id is None: os._exit(9)
    runtime.execution.observe=observe
if boundary in {"terminal","receipt"}:
    original=receipts.save
    def save(*a,**k):
        if boundary=="receipt": original(*a,**k)
        os._exit(9)
    receipts.save=save
async def run():
    if boundary=="link":
        await runtime.execution.bind(attempt,runtime.agents.generation,2); os._exit(9)
    await runtime.execution.execute(attempt,runtime.agents.generation,2)
asyncio.run(run())
"""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(runtime.engine.settings.home), str(repo), attempt_id, boundary],
        capture_output=True,
        timeout=30,
        **options,
    )
    assert result.returncode == 9, result.stderr.decode(errors="replace")
    assert runtime.scheduler.acquire()
    runtime.scheduler.reconcile()
    row, contract = runtime.execution.contract(attempt_id)
    work = runtime.service.store.get("work_item", ws.id, contract.item_id)
    if boundary == "receipt":
        assert row["state"] == "SUCCEEDED" and work.status == "REVIEW"
        assert work.active_attempt_id is None
    else:
        assert row["state"] == "RECONCILING" and work.active_attempt_id == attempt_id
        assert work.reason_code == "RECOVERY_REQUIRED"
    count = runtime.engine.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    runtime.scheduler.reconcile()
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == count


async def test_terminal_receipt_replayed_without_execution(execution, monkeypatch):
    runtime, ws, _, _, _ = execution
    attempt_id = queue(execution, item(execution))
    original = runtime.execution.observe

    def observe(a, g, f, s):
        if s.kind != "receipt_finalized":
            original(a, g, f, s)

    monkeypatch.setattr(runtime.execution, "observe", observe)
    await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    runtime.engine.store.db.execute("UPDATE wb_attempts SET lease_until=? WHERE id=?", (now() - 1, attempt_id))
    runtime.scheduler.reconcile()
    assert runtime.execution.contract(attempt_id)[0]["state"] == "SUCCEEDED"
    versions = runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_events").fetchone()[0]
    runtime.scheduler.reconcile()
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_events").fetchone()[0] == versions
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_receipt_snapshots").fetchone()[0] == 1


async def test_user_edits_survive_rollback_conflict(execution, monkeypatch):
    runtime, ws, _, _, repo = execution
    attempt_id = queue(execution, item(execution))
    original = runtime.execution.observe

    def observe(a, g, f, s):
        original(a, g, f, s)
        if s.kind == "validation_start":
            (repo / "greeting.py").write_text("user = 'keep me'\n")
            raise RuntimeError("user interrupted")

    monkeypatch.setattr(runtime.execution, "observe", observe)
    await runtime.scheduler.work(attempt_id, 1)
    assert runtime.execution.contract(attempt_id)[0]["state"] == "RECONCILING"
    result = await runtime.execution.recover(ws.id, attempt_id, "rollback", HUMAN)
    assert result["status"] == "rollback_conflicts"
    assert (repo / "greeting.py").read_text() == "user = 'keep me'\n"
    assert runtime.execution.contract(attempt_id)[0]["state"] == "RECONCILING"


async def test_retry_new_attempt_budget_and_grant(execution, monkeypatch):
    runtime, ws, _, _, repo = execution
    work = item(execution)
    attempt_id = queue(execution, work)

    async def fail(*a, **k):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(runtime.engine, "_worker", fail)
    await runtime.scheduler.work(attempt_id, 1)
    first, contract = runtime.execution.contract(attempt_id)
    assert first["state"] == "FAILED" and not (repo / "greeting.py").exists()
    current = runtime.service.store.get("work_item", ws.id, work["id"])
    runtime.service.item_command(
        ws.id, current.id, {"command_id": identity(), "expected_revision": current.revision, "command": "retry"}, HUMAN
    )
    decision = runtime.agents.preview(ws.id, current.id, HUMAN)
    assigned = runtime.agents.assign(
        ws.id,
        current.id,
        {
            "command_id": identity(),
            "expected_revision": decision["item_revision"],
            "agent_id": execution[3]["id"],
            "policy_revision_hash": decision["policy_revision_hash"],
        },
        HUMAN,
    ).body["item"]
    requested = runtime.service.item_command(
        ws.id,
        current.id,
        {"command_id": identity(), "expected_revision": assigned["revision"], "command": "request_execution"},
        HUMAN,
    )
    assert requested.body["item"]["status"] == "WAITING_FOR_HUMAN"
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_attempts").fetchone()[0] == 1
    preflight = runtime.agents.execution_preflight(ws.id, current.id, HUMAN)
    approved = runtime.service.item_command(
        ws.id,
        current.id,
        {
            "command_id": identity(),
            "expected_revision": requested.body["item"]["revision"],
            "command": "approve_execution",
            "contract_revision": current.contract_revision,
            "contract_hash": preflight["contract_hash"],
            "policy_revision_hash": preflight["policy_revision_hash"],
            "reason": "Retry needs a new explicit grant",
        },
        HUMAN,
    ).body["item"]
    second = runtime.service.item_command(
        ws.id,
        current.id,
        {"command_id": identity(), "expected_revision": approved["revision"], "command": "request_execution"},
        HUMAN,
    ).body["attempt_id"]
    other, other_contract = runtime.execution.contract(second)
    assert other_contract.plan_id != contract.plan_id and other_contract.request_id != contract.request_id
    assert other_contract.grant_id != contract.grant_id and other["ordinal"] == 2
    assert other_contract.budget_microusd <= contract.budget_microusd


async def test_recovery_command_idempotent_and_scope(execution):
    runtime, ws, _, _, _ = execution
    attempt_id = queue(execution, item(execution))
    runtime.engine.store.db.execute("UPDATE wb_attempts SET lease_until=? WHERE id=?", (now() - 1, attempt_id))
    runtime.scheduler.reconcile()
    assert runtime.scheduler.acquire()
    current = runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt_id)[1].item_id)
    body = {"command_id": identity(), "expected_revision": current.revision, "action": "discard"}
    first = runtime.execution.recovery_request(ws.id, attempt_id, body, HUMAN)
    assert runtime.execution.recovery_request(ws.id, attempt_id, body, HUMAN).replayed
    await runtime.planning.tasks[first.body["id"]]
    assert runtime.planning.get(ws.id, first.body["id"], HUMAN)["state"] == "COMPLETED"
    assert runtime.execution.contract(attempt_id)[0]["state"] == "FAILED"
