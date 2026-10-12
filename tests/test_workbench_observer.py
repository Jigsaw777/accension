"""Actual execution stages, retained repair evidence and immutable receipts."""

import asyncio
import json

import pytest
from test_workbench_execution import execution as execution
from test_workbench_execution import item, queue

from local_ai_router import receipts
from local_ai_router.contracts import ExecutionSignal
from local_ai_router.schema import Generation


async def test_validation_stage_is_actual_activity(execution, monkeypatch):
    from local_ai_router import engine as engine_module

    runtime, ws, _, _, _ = execution
    work = item(execution)
    attempt_id = queue(execution, work)
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine_module.validate

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(engine_module, "validate", paused)
    task = asyncio.create_task(runtime.execution.execute(attempt_id, runtime.agents.generation, 1))
    await entered.wait()
    assert runtime.service.get("work_item", ws.id, work["id"], runtime_principal()).status == "VERIFYING"
    assert (
        runtime.engine.store.db.execute("SELECT state FROM wb_attempts WHERE id=?", (attempt_id,)).fetchone()[0]
        == "VERIFYING"
    )
    release.set()
    await task
    assert runtime.service.get("work_item", ws.id, work["id"], runtime_principal()).status == "REVIEW"


def runtime_principal():
    from test_workbench_agents import HUMAN

    return HUMAN


async def test_repairs_preserve_check_evidence(execution, monkeypatch):
    runtime, ws, _, _, _ = execution
    work = item(execution)
    attempt_id = queue(execution, work)
    original = runtime.engine.providers.generate
    calls = []

    async def generate(*args, **kwargs):
        calls.append(args[2])
        result = await original(*args, **kwargs)
        if len(calls) == 1:
            output = json.loads(result.text)
            output["files_changed"][0]["content"] = 'def greet(name):\n    return "wrong"\n'
            return Generation(text=json.dumps(output), usage=result.usage)
        return result

    monkeypatch.setattr(runtime.engine.providers, "generate", generate)
    await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    rows = runtime.engine.store.db.execute(
        "SELECT status,checks_json FROM wb_verifications WHERE attempt_id=? ORDER BY created_at,rowid", (attempt_id,)
    ).fetchall()
    assert [row[0] for row in rows] == ["FAILED", "PASSED", "PASSED", "VERIFIED"]
    assert "wrong" in json.loads(rows[0][1])["checks"][0]["output"]
    assert calls == ["executor", "repair"]
    stages = [
        json.loads(row[0])["status"]
        for row in runtime.engine.store.db.execute(
            "SELECT payload_json FROM wb_events WHERE entity_id=? ORDER BY sequence", (work["id"],)
        )
    ]
    assert ["RUNNING", "VERIFYING", "RUNNING", "VERIFYING"] == [s for s in stages if s in {"RUNNING", "VERIFYING"}]


async def test_receipt_snapshot_immutable_after_recovery(execution):
    runtime, _, _, _, _ = execution
    attempt_id = queue(execution, item(execution))
    await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    row, contract = runtime.execution.contract(attempt_id)
    before = tuple(
        runtime.engine.store.db.execute(
            "SELECT receipt_hash,receipt_json FROM wb_receipt_snapshots WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
    )
    runtime.engine.store.db.execute("UPDATE receipts SET data='{}' WHERE run=?", (contract.plan_id,))
    after = tuple(
        runtime.engine.store.db.execute(
            "SELECT receipt_hash,receipt_json FROM wb_receipt_snapshots WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
    )
    assert before == after and json.loads(after[1])["status"] == "complete" and row["state"] == "SUCCEEDED"


async def test_missing_receipt_blocks_completion(execution, monkeypatch):
    runtime, ws, _, _, _ = execution
    work = item(execution)
    attempt_id = queue(execution, work)

    def missing(*args, **kwargs):
        raise OSError("receipt persistence failed")

    monkeypatch.setattr(receipts, "save", missing)
    with pytest.raises(OSError, match="receipt"):
        await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    assert runtime.service.get("work_item", ws.id, work["id"], runtime_principal()).status == "VERIFYING"
    assert not runtime.engine.store.db.execute("SELECT 1 FROM wb_receipt_snapshots").fetchone()
    assert runtime.engine.store.db.execute("SELECT status FROM runs").fetchone()[0] == "complete"


async def test_trace_listener_not_replaced(execution):
    runtime, _, _, _, _ = execution
    listener, traces = lambda trace: traces.append(trace), []
    runtime.engine.store.trace_listener = listener
    attempt_id = queue(execution, item(execution))
    await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    assert runtime.engine.store.trace_listener is listener and traces


async def test_observer_failure_aborts_and_preserves_recovery_evidence(execution, monkeypatch):
    runtime, ws, _, _, repo = execution
    work = item(execution)
    attempt_id = queue(execution, work)
    original = runtime.execution.observe

    def fail(attempt, generation, fence, signal):
        if signal.kind == "validation_end":
            raise RuntimeError("authority event failed")
        return original(attempt, generation, fence, signal)

    monkeypatch.setattr(runtime.execution, "observe", fail)
    with pytest.raises(RuntimeError, match="authority event failed"):
        await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    assert not (repo / "greeting.py").exists()
    assert runtime.service.get("work_item", ws.id, work["id"], runtime_principal()).status != "DONE"
    row, _ = runtime.execution.contract(attempt_id)
    assert row["state"] != "SUCCEEDED"


def test_signal_is_typed():
    signal = ExecutionSignal("validation_start", task_id="task")
    assert signal.kind == "validation_start" and signal.checks == ()
