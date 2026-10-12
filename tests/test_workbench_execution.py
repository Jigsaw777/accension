"""Actual bounded Engine edits with immutable Workbench attempts and post-await guards."""

import asyncio

import pytest
from test_workbench_agents import HUMAN, definition

from local_ai_router.contracts import ExecutionAborted
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.store import identity, now


@pytest.fixture
async def execution(engine, repo):
    (repo / "test_contract.py").write_text(
        'import unittest\nfrom greeting import greet\nclass Contract(unittest.TestCase):\n    def test_behavior(self):\n        self.assertEqual(greet(" Ada "), "Hello, Ada!")\n        self.assertEqual(greet(" "), "Hello, world!")\n'
    )
    runtime = WorkbenchRuntime(engine)
    ws = runtime.service.seed_defaults()
    reference = next(iter(runtime.service.repositories()))
    project = runtime.service.create(
        "project",
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "key": "EX",
            "name": "Execution",
            "repository_ref": reference,
        },
        HUMAN,
    ).body
    agent = runtime.agents.configure(definition(ws, repository_permissions=[reference]), HUMAN).body
    await runtime.start()
    yield runtime, ws, project, agent, repo
    await runtime.close()


def item(execution, path="greeting.py", **extra):
    runtime, ws, project, _, _ = execution
    return runtime.service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "project_id": project["id"],
            "title": "Greeting",
            "execution_kind": "ENGINE",
            "spec": {
                "objective": "Implement greeting",
                "acceptance_criteria": ["Blank input greets world"],
                "definition_of_done": ["Independent checks pass"],
                "allowed_paths": [path],
                "expected_artifacts": [path],
                "validation_checks": ["tests"],
            },
            **extra,
        },
        HUMAN,
    ).body


def queue(execution, work):
    runtime, ws, _, agent, _ = execution
    runtime.service.item_command(
        ws.id, work["id"], {"command_id": identity(), "expected_revision": work["revision"], "command": "ready"}, HUMAN
    )
    decision = runtime.agents.preview(ws.id, work["id"], HUMAN)
    assigned = runtime.agents.assign(
        ws.id,
        work["id"],
        {
            "command_id": identity(),
            "expected_revision": decision["item_revision"],
            "agent_id": agent["id"],
            "policy_revision_hash": decision["policy_revision_hash"],
        },
        HUMAN,
    ).body["item"]
    preflight = runtime.agents.execution_preflight(ws.id, work["id"], HUMAN)
    approved = runtime.service.item_command(
        ws.id,
        work["id"],
        {
            "command_id": identity(),
            "expected_revision": assigned["revision"],
            "command": "approve_execution",
            "contract_revision": assigned["contract_revision"],
            "contract_hash": preflight["contract_hash"],
            "policy_revision_hash": preflight["policy_revision_hash"],
            "reason": "Disposable repository scope reviewed",
        },
        HUMAN,
    ).body["item"]
    requested = runtime.service.item_command(
        ws.id,
        work["id"],
        {"command_id": identity(), "expected_revision": approved["revision"], "command": "request_execution"},
        HUMAN,
    )
    attempt_id = requested.body["attempt_id"]
    runtime.engine.store.db.execute(
        "UPDATE wb_attempts SET state='CLAIMED',owner_generation=?,lease_until=? WHERE id=?",
        (runtime.agents.generation, now() + 60000, attempt_id),
    )
    return attempt_id


def settle_fixture(runtime, ws, attempt_id):
    row, contract = runtime.execution.contract(attempt_id)
    with runtime.engine.store.transaction():
        runtime.engine.store.db.execute("UPDATE wb_attempts SET state='SUCCEEDED' WHERE id=?", (attempt_id,))
        runtime.engine.store.db.execute(
            "UPDATE wb_assignments SET status='RELEASED' WHERE id=?", (row["assignment_id"],)
        )
        runtime.engine.store.db.execute(
            "UPDATE wb_budget_reservations SET state='SETTLED' WHERE attempt_id=?", (attempt_id,)
        )
        work = runtime.service.store.get("work_item", ws.id, contract.item_id)
        runtime.service.changed_item(work, {"status": "DONE", "active_attempt_id": None}, HUMAN, "test_finished")


async def test_one_item_one_fresh_plan_and_run(execution):
    runtime, ws, _, _, repo = execution
    work = item(execution)
    attempt_id = queue(execution, work)
    result = await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    row, contract = runtime.execution.contract(attempt_id)
    assert result["status"] == "complete" and (repo / "greeting.py").read_text().startswith("def greet")
    assert row["run_id"] == contract.plan_id and result["request_id"] == contract.request_id
    assert result["tasks"] == 1 and work["id"] != contract.plan_id
    assert runtime.service.store.get("work_item", ws.id, work["id"]).status == "REVIEW"
    from local_ai_router.receipts import get

    assert get(runtime.engine, contract.plan_id)["status"] == "complete"


async def test_dependent_item_after_repo_fingerprint_changes(execution):
    runtime, ws, _, _, repo = execution
    first = item(execution)
    attempt1 = queue(execution, first)
    await runtime.execution.execute(attempt1, runtime.agents.generation, 1)
    settle_fixture(runtime, ws, attempt1)
    second = item(execution, "USAGE.md")
    runtime.service.dependencies(
        ws.id, second["id"], {"command_id": identity(), "expected_revision": 1, "add": [first["id"]]}, HUMAN
    )
    second = runtime.service.get("work_item", ws.id, second["id"], HUMAN).model_dump(mode="json")
    attempt2 = queue(execution, second)
    result = await runtime.execution.execute(attempt2, runtime.agents.generation, 1)
    row1, contract1 = runtime.execution.contract(attempt1)
    row2, contract2 = runtime.execution.contract(attempt2)
    assert result["status"] == "complete" and (repo / "USAGE.md").exists()
    assert row1["run_id"] != row2["run_id"] and contract1.request_id != contract2.request_id
    assert contract2.dependency_evidence[0]["item_id"] == first["id"]


async def test_unapproved_paths_or_stale_contract_denied(execution):
    runtime, ws, _, _, _ = execution
    with pytest.raises(ValueError):
        item(execution, spec={"allowed_paths": ["greeting.py"], "expected_artifacts": ["outside.py"]})
    work = item(execution)
    attempt_id = queue(execution, work)
    runtime.engine.store.db.execute("UPDATE wb_attempts SET fence=2 WHERE id=?", (attempt_id,))
    with pytest.raises(ExecutionAborted, match="fenced"):
        await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    assert not runtime.engine.store.db.execute("SELECT 1 FROM runs").fetchone()


async def test_stale_binding_keeps_engine_fingerprint_check(execution):
    runtime, _, _, _, repo = execution
    attempt_id = queue(execution, item(execution))
    plan = await runtime.execution.bind(attempt_id, runtime.agents.generation, 1)
    (repo / "user.py").write_text("value = 1\n")
    with pytest.raises(ValueError, match="changed since planning"):
        await runtime.engine.execute_plan(plan.plan_id, str(repo))
    assert not (repo / "greeting.py").exists()


async def test_binding_intent_crash_reconciles(execution, monkeypatch):
    runtime, _, _, _, _ = execution
    attempt_id = queue(execution, item(execution))
    original = runtime.engine.persist_plan

    def gap(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash after plan persistence")

    monkeypatch.setattr(runtime.engine, "persist_plan", gap)
    with pytest.raises(RuntimeError, match="crash"):
        await runtime.execution.bind(attempt_id, runtime.agents.generation, 1)
    row, contract = runtime.execution.contract(attempt_id)
    assert row["run_id"] is None
    assert (
        runtime.engine.store.db.execute("SELECT status FROM runs WHERE id=?", (contract.plan_id,)).fetchone()[0]
        == "planned"
    )
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


async def test_policy_tightening_during_provider_await_prevents_edit(execution, monkeypatch):
    runtime, ws, _, _, repo = execution
    work = item(execution)
    attempt_id = queue(execution, work)
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.engine.providers.generate

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(runtime.engine.providers, "generate", paused)
    task = asyncio.create_task(runtime.execution.execute(attempt_id, runtime.agents.generation, 1))
    await entered.wait()
    runtime.service.update(
        "workspace",
        ws.id,
        ws.id,
        {"command_id": identity(), "expected_revision": 1, "execution_policy": {"budget_ceiling": "0.001"}},
        HUMAN,
    )
    release.set()
    with pytest.raises(RuntimeError, match="ExecutionAborted|cancelled, expired or fenced"):
        await task
    assert not (repo / "greeting.py").exists()
    assert runtime.engine.store.costs()["calls"] == 1
