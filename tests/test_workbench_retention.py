"""Explicit retention preserves audit evidence and each leased snapshot boundary."""

import os
from dataclasses import replace

import pytest
from test_workbench_agents import HUMAN
from test_workbench_execution import execution as execution

from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.store import WorkbenchError, identity


async def test_retention_preserves_receipts_and_snapshot_versions(engine):
    runtime = WorkbenchRuntime(engine)
    ws = runtime.service.seed_defaults()
    project = runtime.service.create(
        "project", {"command_id": identity(), "workspace_id": ws.id, "name": "Old", "key": "RT"}, HUMAN
    ).body
    for revision in range(1, 5):
        runtime.service.update(
            "project",
            ws.id,
            project["id"],
            {"command_id": identity(), "expected_revision": revision, "name": str(revision)},
            HUMAN,
        )
    lease = runtime.journal.start_snapshot(ws.id, HUMAN, {"kinds": ["project"]})
    for revision in range(5, 8):
        runtime.service.update(
            "project",
            ws.id,
            project["id"],
            {"command_id": identity(), "expected_revision": revision, "name": str(revision)},
            HUMAN,
        )
    maximum = runtime.journal.sequence()
    before_audit = runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_events").fetchone()[0]
    preview = runtime.retention.preview(HUMAN, maximum)
    assert preview["boundary"] == lease["sequence"] and preview["versions"] == 4
    body = {
        "workspace_id": ws.id,
        "command_id": identity(),
        "before_sequence": maximum,
        "preview_hash": preview["preview_hash"],
    }
    result = runtime.retention.apply(body, HUMAN)
    assert result.body["versions_pruned"] == 4
    assert runtime.retention.apply(body, HUMAN).replayed
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_events").fetchone()[0] == before_audit + 1
    assert runtime.journal.start_snapshot(ws.id, HUMAN, {"kinds": ["project"]})["items"][0]["payload"]["name"] == "7"
    # Existing lease metadata is authoritative; preserved version is still present.
    assert (
        runtime.service.store.db.execute(
            "SELECT revision FROM wb_entity_versions WHERE entity_id=? AND sequence<=? ORDER BY sequence DESC LIMIT 1",
            (project["id"], lease["sequence"]),
        ).fetchone()[0]
        == 5
    )
    with pytest.raises(WorkbenchError) as error:
        runtime.journal.events(ws.id, HUMAN, after=0)
    assert error.value.status == 410
    await runtime.close()


async def test_retention_dry_run_scope_stale_preview_and_rotated_logs(engine):
    runtime = WorkbenchRuntime(engine)
    ws = runtime.service.seed_defaults()
    engine.settings.workbench_diagnostic_days = 1
    path = engine.settings.home / "logs" / "accension.jsonl.1"
    path.write_text("old diagnostic", encoding="utf-8")
    os.utime(path, (1, 1))
    preview = runtime.retention.preview(HUMAN, runtime.journal.sequence())
    assert path.exists() and preview["diagnostic_files"][0]["name"] == path.name
    with pytest.raises(WorkbenchError):
        runtime.retention.preview(replace(HUMAN, local=False), 0)
    body = {
        "workspace_id": ws.id,
        "command_id": identity(),
        "before_sequence": runtime.journal.sequence(),
        "preview_hash": preview["preview_hash"],
    }
    path.write_text("changed diagnostic", encoding="utf-8")
    os.utime(path, (1, 1))
    with pytest.raises(WorkbenchError, match="preview again"):
        runtime.retention.apply(body, HUMAN)
    preview = runtime.retention.preview(HUMAN, body["before_sequence"])
    body["preview_hash"] = preview["preview_hash"]
    runtime.retention.apply(body, HUMAN)
    assert not path.exists()
    assert (engine.settings.home / "logs" / "accension.jsonl").exists()
    await runtime.close()


async def test_purge_preserves_actual_execution_evidence(execution):
    from test_workbench_execution import item, queue

    runtime, ws, _, _, _ = execution
    attempt = queue(execution, item(execution))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    before = {
        table: [tuple(row) for row in runtime.service.store.db.execute("SELECT * FROM " + table)]
        for table in ("wb_receipt_snapshots", "wb_verifications", "wb_attempts", "wb_artifacts", "wb_approvals")
    }
    boundary = runtime.journal.sequence()
    preview = runtime.retention.preview(HUMAN, boundary)
    runtime.retention.apply(
        {
            "workspace_id": ws.id,
            "command_id": identity(),
            "before_sequence": boundary,
            "preview_hash": preview["preview_hash"],
        },
        HUMAN,
    )
    assert before == {
        table: [tuple(row) for row in runtime.service.store.db.execute("SELECT * FROM " + table)] for table in before
    }
    assert runtime.execution.read_attempt(ws.id, attempt, HUMAN, "receipt")["receipt"]
