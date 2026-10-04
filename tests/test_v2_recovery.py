import json
import pytest
from local_ai_router.schema import ExecutionPlan, TaskNode, FileChange
from local_ai_router.context import ContextGraph
from local_ai_router.safety import Edits, digest, repo_lock
from local_ai_router.recovery import recover, interrupted


def plan_fixture(engine,repo):
    (repo / "test_files.py").write_text('import unittest\nfrom pathlib import Path\nclass Files(unittest.TestCase):\n    def test_outputs(self):\n        self.assertEqual(Path("one.txt").read_text(), "one")\n        self.assertEqual(Path("two.txt").read_text(), "two")\n')
    plan=ExecutionPlan(goal="Two bounded changes",tasks=[TaskNode(id="one",title="One",objective="Write one",acceptance_criteria=["File one"],expected_artifacts=["one.txt"]),TaskNode(id="two",title="Two",objective="Write two",dependencies=["one"],acceptance_criteria=["File two"],expected_artifacts=["two.txt"])],success_criteria=["Both files"],global_validation=["tests"])
    graph=ContextGraph(repo,engine.store,engine.settings.repositories[0]);graph.build()
    engine.persist_plan(plan,repo,graph.fingerprint,"recovery-test")
    return plan,graph


async def test_resume_only_completed_checkpoint_and_no_replay(engine,repo,monkeypatch):
    plan,graph=plan_fixture(engine,repo)
    edits=Edits(repo,plan.request_id)
    edits.apply([FileChange(path="one.txt",content="one",original_sha256=None)],["one.txt"])
    graph.build()
    completed={"one":{"model":"mock-worker","escalations":0,"remaining_risks":[]}}
    data=json.loads(engine.store.db.execute("SELECT data FROM runs WHERE id=?",(plan.plan_id,)).fetchone()[0]);data["completed"]=completed
    engine.store.db.execute("UPDATE runs SET status='running', fingerprint=?, data=? WHERE id=?",(graph.fingerprint,json.dumps(data),plan.plan_id))
    assert interrupted(engine)==[plan.plan_id]
    calls=[]
    async def worker(task,plan,graph,repo_config,previous,restored,session):
        calls.append(task.id)
        assert "one" in previous and "one.txt" in restored.originals
        restored.apply([FileChange(path="two.txt",content="two",original_sha256=None)],["two.txt"])
        return {"model":"mock-worker","escalations":0,"remaining_risks":[]}
    monkeypatch.setattr(engine,"_worker",worker)
    result=await recover(engine,plan.plan_id,"resume")
    assert result["status"]=="complete" and calls==["two"]
    assert result["files_changed"]==["one.txt","two.txt"]


async def test_recovery_preserves_user_edits_and_refuses_stale_resume(engine,repo):
    (repo/"one.txt").write_text("original")
    plan,graph=plan_fixture(engine,repo)
    edits=Edits(repo,plan.request_id)
    edits.apply([FileChange(path="one.txt",content="router",original_sha256=digest(repo/"one.txt"))],["one.txt"])
    engine.store.db.execute("UPDATE runs SET status='interrupted' WHERE id=?",(plan.plan_id,))
    (repo/"one.txt").write_text("user edit")
    with pytest.raises(ValueError,match="differs"):
        await recover(engine,plan.plan_id,"resume")
    result=await recover(engine,plan.plan_id,"rollback")
    assert result["conflicts"]==["one.txt"] and (repo/"one.txt").read_text()=="user edit"


async def test_recovery_backup_integrity_and_active_lock(engine,repo):
    (repo/"one.txt").write_text("original")
    plan,_=plan_fixture(engine,repo)
    edits=Edits(repo,plan.request_id)
    edits.apply([FileChange(path="one.txt",content="router",original_sha256=digest(repo/"one.txt"))],["one.txt"])
    engine.store.db.execute("UPDATE runs SET status='running' WHERE id=?",(plan.plan_id,))
    with repo_lock(repo):
        assert interrupted(engine)==[]
        with pytest.raises(ValueError,match="owns"):
            await recover(engine,plan.plan_id,"rollback")
    next(edits.backup.glob("*.bak")).write_text("corrupted")
    with pytest.raises(ValueError,match="integrity"):
        await recover(engine,plan.plan_id,"rollback")
    assert (repo/"one.txt").read_text()=="router"
