"""Real process leadership and transactional claims without stealing uncertain writers."""

import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_workbench_execution import execution as execution
from test_workbench_execution import item, queue

from local_ai_router.contracts import ExecutionAborted
from local_ai_router.safety import repo_lock
from local_ai_router.workbench.scheduler import Scheduler
from local_ai_router.workbench.store import identity, now


def queued(execution):
    runtime = execution[0]
    attempt_id = queue(execution, item(execution))
    runtime.engine.store.db.execute(
        "UPDATE wb_attempts SET state='QUEUED',owner_generation=NULL,lease_until=NULL WHERE id=?", (attempt_id,)
    )
    return attempt_id


async def test_single_leader_two_processes(execution):
    runtime = execution[0]
    script = """
import sys
from pathlib import Path
from local_ai_router.config import load
from local_ai_router.engine import Engine
from local_ai_router.workbench.runtime import WorkbenchRuntime
engine = Engine(load(Path(sys.argv[1]), mock=True))
runtime = WorkbenchRuntime(engine)
print(runtime.scheduler.acquire(), flush=True)
sys.stdin.readline()
"""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    first = subprocess.Popen(
        [sys.executable, "-c", script, str(runtime.engine.settings.home)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **options,
    )
    second = None
    try:
        assert first.stdout.readline().strip() == "True"
        second = subprocess.Popen(
            [sys.executable, "-c", script, str(runtime.engine.settings.home)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **options,
        )
        assert second.stdout.readline().strip() == "False"
        assert not runtime.scheduler.acquire()
    finally:
        for process in (first, second):
            if process:
                process.communicate("stop\n", timeout=15)


async def test_claim_race_one_active_attempt(execution):
    runtime = execution[0]
    attempt_id = queued(execution)
    assert runtime.scheduler.acquire()
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: runtime.scheduler.claim(), range(2)))
    assert len([c for c in claims if c]) == 1
    assert claims[0] == (attempt_id, 2) or claims[1] == (attempt_id, 2)
    row, _ = runtime.execution.contract(attempt_id)
    assert row["state"] == "CLAIMED" and row["owner_generation"] == runtime.scheduler.generation
    assert (
        runtime.engine.store.db.execute(
            "SELECT COUNT(*) FROM wb_leases WHERE resource_key LIKE 'repository:%'"
        ).fetchone()[0]
        == 1
    )


async def test_expired_lease_not_proof_worker_dead(execution):
    runtime, ws, _, _, repo = execution
    attempt_id = queued(execution)
    assert runtime.scheduler.acquire()
    claim = runtime.scheduler.claim()
    assert claim
    with repo_lock(repo):
        runtime.engine.store.db.execute("UPDATE wb_attempts SET lease_until=? WHERE id=?", (now() - 1, attempt_id))
        runtime.scheduler.reconcile()
        row, contract = runtime.execution.contract(attempt_id)
        assert row["state"] == "RECONCILING" and row["fence"] == 3
        with pytest.raises(ExecutionAborted):
            runtime.execution.guard(attempt_id, runtime.scheduler.generation, 2)
        assert runtime.scheduler.claim() is None
        assert runtime.service.store.get("work_item", ws.id, contract.item_id).reason_code == "RECOVERY_REQUIRED"


async def test_repository_lock_and_capacity(execution):
    runtime, _, _, _, repo = execution
    attempt_id = queued(execution)
    assert runtime.scheduler.acquire()
    claim = runtime.scheduler.claim()
    assert claim
    with repo_lock(repo):
        await runtime.scheduler.work(*claim)
    row, _ = runtime.execution.contract(attempt_id)
    assert row["state"] == "RECONCILING" and row["error_code"] == "ValueError"
    assert not (repo / "greeting.py").exists()


async def test_pause_and_shutdown_no_new_claim(execution):
    from test_workbench_agents import HUMAN

    runtime, ws, _, _, _ = execution
    queued(execution)
    assert runtime.scheduler.acquire()
    response = runtime.scheduler.dispatch(
        "workspace", ws.id, ws.id, {"command_id": identity(), "expected_revision": 1, "action": "pause"}, HUMAN
    )
    assert response.body["paused"] and runtime.scheduler.claim() is None
    await runtime.scheduler.close()
    assert runtime.scheduler.claim() is None and runtime.scheduler.owner_lock is None


async def test_transient_runtime_never_dispatches(execution):
    runtime = execution[0]
    assert runtime.started and runtime.scheduler.loop_task is None and runtime.scheduler.fence is None
    assert not runtime.scheduler.leader()


async def test_daemon_loop_executes_queued_attempt(execution):
    runtime, ws, _, _, repo = execution
    attempt_id = queued(execution)
    await runtime.scheduler.start()
    async with asyncio.timeout(10):
        while runtime.execution.contract(attempt_id)[0]["state"] != "SUCCEEDED":
            await asyncio.sleep(0.05)
    assert (repo / "greeting.py").exists()
    assert (
        runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt_id)[1].item_id).status
        == "REVIEW"
    )
    assert runtime.scheduler.error is None


async def test_independent_connection_claim_race(execution):
    from local_ai_router.engine import Engine
    from local_ai_router.workbench.runtime import WorkbenchRuntime

    runtime = execution[0]
    attempt_id = queued(execution)
    assert runtime.scheduler.acquire()
    other_engine = Engine(runtime.engine.settings)
    other = WorkbenchRuntime(other_engine)
    other.scheduler.generation = runtime.scheduler.generation
    other.scheduler.fence = runtime.scheduler.fence
    try:
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda scheduler: scheduler.claim(), [runtime.scheduler, other.scheduler]))
        assert len([c for c in claims if c]) == 1
        assert runtime.execution.contract(attempt_id)[0]["state"] == "CLAIMED"
    finally:
        await other_engine.close()


async def test_expired_leader_os_lock_still_prevents_replacement(execution):
    runtime = execution[0]
    assert runtime.scheduler.acquire()
    runtime.engine.store.db.execute("UPDATE wb_leases SET expires_at=? WHERE resource_key='dispatcher'", (now() - 1,))
    replacement = Scheduler(runtime)
    replacement.generation = identity()
    assert not replacement.acquire()
    assert not runtime.scheduler.leader()
    with pytest.raises(ExecutionAborted):
        runtime.scheduler.heartbeat()
