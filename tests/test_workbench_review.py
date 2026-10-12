"""Completion depends on real current evidence, never model or manual labels."""

from dataclasses import replace

import pytest
from test_workbench_agents import HUMAN
from test_workbench_execution import execution as execution
from test_workbench_execution import item, queue

from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import WorkbenchError, identity


def policy(runtime, ws, review):
    current = runtime.service.store.get("workspace", ws.id, ws.id)
    runtime.service.update(
        "workspace",
        ws.id,
        ws.id,
        {"command_id": identity(), "expected_revision": current.revision, "review_policy": review},
        HUMAN,
    )


@pytest.mark.parametrize(
    "review,expected",
    [("AUTO_VERIFY", "DONE"), ("AI_REVIEW", "WAITING_FOR_HUMAN"), ("HUMAN_APPROVAL_REQUIRED", "REVIEW")],
)
async def test_all_review_policy_branches(execution, review, expected):
    runtime, ws, _, _, _ = execution
    policy(runtime, ws, review)
    attempt_id = queue(execution, item(execution, review_policy=review))
    await runtime.execution.execute(attempt_id, runtime.agents.generation, 1)
    current = runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt_id)[1].item_id)
    assert current.status == expected
    if review == "AI_REVIEW":
        assert current.reason_code == "NO_ELIGIBLE_REVIEWER"


async def test_no_reviewer_does_not_satisfy_ai_review(execution):
    runtime, ws, _, _, _ = execution
    policy(runtime, ws, "AI_REVIEW")
    attempt = queue(execution, item(execution, review_policy="AI_REVIEW"))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    assert runtime.execution.contract(attempt)[0]["state"] == "SUCCEEDED"
    assert (
        runtime.engine.store.db.execute("SELECT status FROM wb_verifications WHERE kind='AI_REVIEW'").fetchone()[0]
        == "MISSING"
    )
    assert (
        runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt)[1].item_id).status != "DONE"
    )


async def verified(execution):
    runtime = execution[0]
    attempt = queue(execution, item(execution))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    current = runtime.service.store.get("work_item", execution[1].id, runtime.execution.contract(attempt)[1].item_id)
    evidence, _ = runtime.service.evidence(current)
    return current, evidence


def approve(runtime, ws, current, evidence, principal=HUMAN, command="approve_completion"):
    return runtime.service.item_command(
        ws.id,
        current.id,
        {
            "command_id": identity(),
            "expected_revision": current.revision,
            "command": command,
            "contract_revision": current.contract_revision,
            "evidence_hash": evidence["evidence_hash"],
            "reason": "Exact current evidence inspected",
        },
        principal,
    )


async def test_human_approval_requires_current_evidence(execution):
    runtime, ws, _, _, repo = execution
    current, evidence = await verified(execution)
    with pytest.raises(WorkbenchError, match="human"):
        approve(runtime, ws, current, evidence, replace(HUMAN, actor_type=ActorType.MACHINE))
    (repo / "greeting.py").write_text("changed = True\n")
    with pytest.raises(WorkbenchError, match="artifact changed"):
        approve(runtime, ws, current, evidence)
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_approvals").fetchone()[0] == 0


async def test_approval_and_rejection_retain_files(execution):
    runtime, ws, _, _, repo = execution
    current, evidence = await verified(execution)
    result = approve(runtime, ws, current, evidence, command="reject_completion")
    assert result.body["status"] == "FAILED" and (repo / "greeting.py").exists()
    assert runtime.engine.store.db.execute("SELECT decision FROM wb_approvals").fetchone()[0] == "REJECT"


async def test_failed_checks_cannot_done(execution, monkeypatch):
    runtime, ws, _, _, repo = execution
    (repo / "test_contract.py").write_text(
        "import unittest\nclass Fail(unittest.TestCase):\n    def test_fail(self): self.fail('contract failed')\n"
    )
    attempt = queue(execution, item(execution))
    await runtime.scheduler.work(attempt, 1)
    current = runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt)[1].item_id)
    assert current.status == "FAILED"
    with pytest.raises(WorkbenchError):
        runtime.service.evidence(current)


async def test_manual_attestation_not_fake_test_or_run(execution):
    runtime, ws, _, _, _ = execution
    work = runtime.service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "project_id": execution[2]["id"],
            "title": "Read design",
            "execution_kind": "MANUAL",
            "spec": {
                "objective": "Read design",
                "acceptance_criteria": ["Decision recorded"],
                "definition_of_done": ["Human confirms review"],
            },
        },
        HUMAN,
    ).body
    result = runtime.service.item_command(
        ws.id,
        work["id"],
        {
            "command_id": identity(),
            "expected_revision": work["revision"],
            "command": "manual_submit",
            "evidence": {"checklist": ["Read design"], "references": ["Decision notes"]},
        },
        HUMAN,
    )
    await runtime.planning.tasks[result.body["id"]]
    operation = runtime.planning.get(ws.id, result.body["id"], HUMAN)
    assert operation["state"] == "COMPLETED", operation
    assert operation["result"]["verification_kind"] == "HUMAN_ATTESTATION"
    assert operation["result"]["run_id"] is None and not operation["result"]["inference"]
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    current = runtime.service.store.get("work_item", ws.id, work["id"])
    evidence, data = runtime.service.evidence(current)
    assert data["checks"] == [] and approve(runtime, ws, current, evidence).body["status"] == "DONE"


async def test_manual_repository_validation_is_real(execution):
    runtime, ws, _, _, repo = execution
    (repo / "greeting.py").write_text('def greet(name):\n    return "Hello, " + (name.strip() or "world") + "!"\n')
    work = item(execution, execution_kind="MANUAL")
    result = runtime.service.item_command(
        ws.id,
        work["id"],
        {
            "command_id": identity(),
            "expected_revision": work["revision"],
            "command": "manual_submit",
            "evidence": {"checklist": ["Implemented"], "references": ["greeting.py"]},
        },
        HUMAN,
    )
    await runtime.planning.tasks[result.body["id"]]
    current = runtime.service.store.get("work_item", ws.id, work["id"])
    evidence, data = runtime.service.evidence(current)
    assert evidence["kind"] == "MANUAL_VALIDATION" and data["checks"][0]["passed"]
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


async def test_stale_receipt_hash_cannot_approve(execution):
    runtime, ws, _, _, _ = execution
    current, evidence = await verified(execution)
    runtime.engine.store.db.execute("UPDATE wb_receipt_snapshots SET receipt_json='{}'")
    with pytest.raises(WorkbenchError, match="receipt changed"):
        approve(runtime, ws, current, evidence)


async def test_independent_review_accepts_exact_artifacts(execution):
    runtime, ws, _, _, _ = execution
    policy(runtime, ws, "AI_REVIEW")
    model = runtime.engine.settings.models[0]
    runtime.engine.settings.models.append(model.model_copy(update={"id": "independent-reviewer"}))
    attempt = queue(execution, item(execution, review_policy="AI_REVIEW"))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    current = runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt)[1].item_id)
    assert current.status == "DONE"
    review = runtime.engine.store.db.execute(
        "SELECT checks_json FROM wb_verifications WHERE kind='AI_REVIEW' AND status='ACCEPTED'"
    ).fetchone()
    import json

    worker = next(
        t["model"]
        for t in runtime.engine.store.traces(runtime.execution.contract(attempt)[1].request_id)
        if t["stage"] == "worker" and t.get("passed")
    )
    assert json.loads(review[0])["model_id"] != worker


async def test_inherited_project_review_cannot_weaken(execution):
    runtime, ws, project, _, _ = execution
    policy(runtime, ws, "AUTO_VERIFY")
    runtime.service.update(
        "project",
        ws.id,
        project["id"],
        {
            "command_id": identity(),
            "expected_revision": project["revision"],
            "review_policy": "HUMAN_APPROVAL_REQUIRED",
        },
        HUMAN,
    )
    attempt = queue(execution, item(execution, review_policy="AUTO_VERIFY"))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    current = runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt)[1].item_id)
    assert current.status == "REVIEW"
    evidence, _ = runtime.service.evidence(current)
    assert approve(runtime, ws, current, evidence).body["status"] == "DONE"


async def test_dependency_reopen_invalidates_completion(execution):
    runtime, ws, _, _, _ = execution
    blocker = item(execution, path="other.py", execution_kind="MANUAL")
    # No fabricated DONE: submit honest nonrepository evidence for blocker.
    runtime.service.update(
        "work_item",
        ws.id,
        blocker["id"],
        {
            "command_id": identity(),
            "expected_revision": blocker["revision"],
            "spec": {
                "objective": "Read prerequisites",
                "acceptance_criteria": ["Reviewed"],
                "definition_of_done": ["Attested"],
            },
        },
        HUMAN,
    )
    blocker = runtime.service.store.get("work_item", ws.id, blocker["id"])
    submitted = runtime.service.item_command(
        ws.id,
        blocker.id,
        {
            "command_id": identity(),
            "expected_revision": blocker.revision,
            "command": "manual_submit",
            "evidence": {"checklist": ["Reviewed"], "references": ["Notes"]},
        },
        HUMAN,
    )
    await runtime.planning.tasks[submitted.body["id"]]
    blocker = runtime.service.store.get("work_item", ws.id, blocker.id)
    evidence, _ = runtime.service.evidence(blocker)
    approve(runtime, ws, blocker, evidence)
    work = item(execution)
    runtime.service.dependencies(
        ws.id, work["id"], {"command_id": identity(), "expected_revision": work["revision"], "add": [blocker.id]}, HUMAN
    )
    current = runtime.service.store.get("work_item", ws.id, work["id"])
    attempt = queue(execution, current.model_dump(mode="json"))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    current = runtime.service.store.get("work_item", ws.id, current.id)
    evidence, _ = runtime.service.evidence(current)
    blocker = runtime.service.store.get("work_item", ws.id, blocker.id)
    runtime.service.item_command(
        ws.id,
        blocker.id,
        {
            "command_id": identity(),
            "expected_revision": blocker.revision,
            "command": "reopen",
            "reason": "Prerequisite changed",
        },
        HUMAN,
    )
    with pytest.raises(WorkbenchError):
        approve(runtime, ws, current, evidence)
