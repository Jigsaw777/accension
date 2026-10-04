"""Explicit recovery under the existing repository lock and hash preconditions."""
import json
from pathlib import Path
from .safety import repo_lock, Edits, digest, safe_path
from .privacy import repository_scope
from .engine import active_operation


def interrupted(engine):
    found = []
    for row in engine.store.db.execute("SELECT * FROM runs WHERE status='running'").fetchall():
        try:
            root = Path(engine.settings.repository(row["repo"]).path).resolve()
            with repo_lock(root):
                engine.store.db.execute("UPDATE runs SET status='interrupted' WHERE id=? AND status='running'", (row["id"],))
                data = json.loads(row["data"])
                engine.store.db.execute("UPDATE calls SET state='uncertain' WHERE request=? AND state='reserved'", (data["plan"]["request_id"],))
                engine.store.economics("pause", data["plan"]["request_id"], "interrupted")
                found.append(row["id"])
        except (ValueError, OSError):
            continue  # Active owner or unavailable repository; never guess ownership.
    return found


def list_runs(engine):
    interrupted(engine)
    return [dict(row) for row in engine.store.db.execute("SELECT id,repo,status FROM runs ORDER BY rowid DESC LIMIT 50")]


@active_operation
async def recover(engine, plan_id, action):
    row = engine.store.db.execute("SELECT * FROM runs WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown plan")
    repo = engine.settings.repository(row["repo"])
    root = Path(repo.path).resolve()
    with repo_lock(root), repository_scope(repo):
        row = engine.store.db.execute("SELECT * FROM runs WHERE id=?", (plan_id,)).fetchone()
        if row is None or row["repo"] != str(root):
            raise ValueError("Run changed while acquiring its repository lock")
        data = json.loads(row["data"])
        if row["status"] == "running":
            engine.store.db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (plan_id,))
        edits = Edits.restore(root, data["plan"]["request_id"])
        conflicts = [name for name in edits.written if digest(safe_path(root, name)) != edits.written[name]]
        if action == "inspect":
            return {"id": plan_id, "status": "interrupted" if row["status"] == "running" else row["status"], "goal": data["plan"]["goal"],
                    "completed_tasks": list(data.get("completed", {})), "router_files": list(edits.written), "changed_since_router_write": conflicts,
                    "actions": ["resume", "rollback", "discard"]}
        if row["status"] in {"discarded", "rolled_back"} or row["status"] == "complete" and action != "rollback":
            raise ValueError("This run is already finalized")
        def receipt(status):
            from .receipts import save
            from .schema import ExecutionPlan
            save(engine, ExecutionPlan.model_validate(data["plan"]), root, edits, data.get("starting", {}), status)
        if action == "discard":
            engine.store.db.execute("UPDATE runs SET status='discarded' WHERE id=?", (plan_id,))
            receipt("discarded")
            return {"status": "discarded", "files_unchanged": True, "backups_retained": True}
        if action == "rollback":
            conflicts = edits.rollback()
            engine.store.db.execute("UPDATE runs SET status=? WHERE id=?", ("rollback_conflicts" if conflicts else "rolled_back", plan_id))
            receipt("rollback_conflicts" if conflicts else "rolled_back")
            return {"status": "rollback_conflicts" if conflicts else "rolled_back", "conflicts": conflicts}
        if action == "resume":
            if engine.store.db.execute("SELECT 1 FROM savings_runs WHERE request=? AND status='final'", (data["plan"]["request_id"],)).fetchone():
                raise ValueError("This execution has a finalized receipt; compile a fresh plan to retry. Interrupted checkpoints can still resume.")
            from .context import ContextGraph
            import asyncio
            graph = ContextGraph(root, engine.store, repo)
            await asyncio.to_thread(graph.build)
            if graph.fingerprint != row["fingerprint"] or conflicts:
                raise ValueError("Repository differs from the last completed task checkpoint; inspect or roll back before planning again")
            engine.store.db.execute("UPDATE runs SET status='planned' WHERE id=?", (plan_id,))
            return await engine._execute(plan_id, root, resume=True)
        raise ValueError("Unknown recovery action")
