"""Atomic complete graph imports, explicit reconciliation, independent persistent IDs."""

import pytest
from test_workbench_planning import HUMAN, SPEC

from local_ai_router.schema import ExecutionPlan, TaskNode
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.schema import WorkItem
from local_ai_router.workbench.store import WorkbenchError, identity, now


@pytest.fixture
async def graph(engine, repo):
    runtime = WorkbenchRuntime(engine)
    service = runtime.service
    ws = service.seed_defaults()
    ref = next(iter(service.repositories()))
    project = service.create(
        "project",
        {"command_id": identity(), "workspace_id": ws.id, "name": "Graph", "key": "GR", "repository_ref": ref},
        HUMAN,
    ).body
    prd = service.create(
        "prd",
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "project_id": project["id"],
            "title": "Graph",
            "spec": {**SPEC, "constraints": ["Retain caller constraint"]},
        },
        HUMAN,
    ).body
    for revision, command in [(1, "submit_review"), (2, "approve")]:
        prd = service.prd_command(
            ws.id, prd["id"], {"command_id": identity(), "expected_revision": revision, "command": command}, HUMAN
        ).body
    yield runtime.planning, ws, project, prd
    await runtime.close()


def plan(count=2, offset=0):
    return ExecutionPlan(
        goal="Greeting",
        success_criteria=["Greeting verified"],
        global_validation=["tests"],
        constraints=["Additional plan constraint"],
        tasks=[
            TaskNode(
                id=f"n{i}",
                title=f"Item {i}",
                objective=f"Implement greeting {i}",
                acceptance_criteria=["Named and blank inputs"],
                verifier_requirements=["Independent checks pass"],
                expected_artifacts=[f"file{i}.py"],
                validation_commands=["tests"],
                dependencies=[f"n{i - 1}"] if i > offset else [],
                tools_required=["propose_file_changes"],
                privacy_class="CLOUD_ALLOWED",
                max_attempts=2,
            )
            for i in range(offset, offset + count)
        ],
    ).model_dump(mode="json")


def preview(graph, plans=None, *, reconcile=False, edges=()):
    planner, ws, project, prd = graph
    plans = plans or [plan()]
    files = [path for p in plans for task in p["tasks"] for path in task["expected_artifacts"]]
    return planner.preview(
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "project_id": project["id"],
            "prd_id": prd["id"],
            "prd_revision": prd["revision"],
            "plans": plans,
            "allowed_paths": files,
            "requirement_groups": [["R1"] for _ in plans],
            "cross_batch_edges": list(edges),
        },
        HUMAN,
        reconcile=reconcile,
    ).body


def commit(graph, proposed, **extra):
    planner, ws, _, _ = graph
    body = {
        "command_id": identity(),
        "expected_revision": proposed["revision"],
        "source_hash": proposed["source_hash"],
        "actions": {p["proposal_id"]: "create" for p in proposed["proposal"]["items"]},
        **extra,
    }
    return planner.commit(ws.id, proposed["id"], body, HUMAN), body


async def test_import_retry_no_duplicate_items(graph):
    planner, ws, _, _ = graph
    proposed = preview(graph)
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_work_items").fetchone()[0] == 0
    first, body = commit(graph, proposed)
    events = planner.store.db.execute("SELECT COUNT(*) FROM wb_events").fetchone()[0]
    second = planner.commit(ws.id, proposed["id"], body, HUMAN)
    assert second.replayed and second.body == first.body
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_work_items").fetchone()[0] == 2
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_events").fetchone()[0] == events
    with pytest.raises(WorkbenchError):
        planner.commit(ws.id, proposed["id"], {**body, "command_id": identity()}, HUMAN)


async def test_field_mapping_and_requirement_coverage(graph):
    planner, ws, _, prd = graph
    proposed = preview(graph)
    result, _ = commit(graph, proposed)
    item = planner.store.get("work_item", ws.id, result.body["items"]["b0_n0"])
    assert item.id != "n0" and item.display_key == "GR-1" and item.status == "BACKLOG"
    assert item.prd_id == prd["id"] and item.engine_max_attempts == 2
    assert item.spec.requirement_ids == ["R1"] and item.spec.required_tools == ["propose_file_changes"]
    assert item.spec.definition_of_done == ["Independent checks pass"]
    assert item.spec.constraints == ["Retain caller constraint", "Additional plan constraint"]
    assert item.privacy_policy == "LOCAL_ONLY" and item.spec.source_provenance.import_id == proposed["id"]
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_dependencies").fetchone()[0] == 1
    bad = dict(proposed["proposal"]["batches"][0]["plan"])
    with pytest.raises(WorkbenchError, match="cover"):
        planner.preview(
            {
                "command_id": identity(),
                "workspace_id": ws.id,
                "project_id": item.project_id,
                "prd_id": prd["id"],
                "prd_revision": 3,
                "plans": [bad],
                "allowed_paths": item.spec.allowed_paths,
                "requirement_groups": [["UNKNOWN"]],
            },
            HUMAN,
        )


async def test_multi_batch_graph_over_64(graph):
    planner, ws, _, _ = graph
    plans = [plan(64), plan(1, 64)]
    proposed = preview(graph, plans, edges=[("b0_n63", "b1_n64")])
    result, _ = commit(graph, proposed)
    assert len(result.body["items"]) == 65
    assert (
        planner.store.db.execute("SELECT COUNT(*) FROM wb_dependencies WHERE workspace_id=?", (ws.id,)).fetchone()[0]
        == 64
    )
    assert all(len(batch["plan"]["tasks"]) <= 64 for batch in proposed["proposal"]["batches"])
    with pytest.raises(ValueError):
        ExecutionPlan.model_validate({**plans[0], "tasks": plans[0]["tasks"] + plans[1]["tasks"]})
    with pytest.raises(ValueError, match="cycle"):
        preview(graph, plans, edges=[("b0_n63", "b1_n64"), ("b1_n64", "b0_n0")])


async def test_import_failure_rolls_back_counter_items_edges_events(graph):
    planner, ws, project, _ = graph
    proposed = preview(graph)
    count = planner.store.db.execute("SELECT COUNT(*) FROM wb_events").fetchone()[0]
    planner.store.db.execute(
        "CREATE TRIGGER reject_import BEFORE INSERT ON wb_dependencies BEGIN SELECT RAISE(ABORT,'injected failure'); END"
    )
    with pytest.raises(WorkbenchError):
        commit(graph, proposed)
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_work_items").fetchone()[0] == 0
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_events").fetchone()[0] == count
    assert (
        planner.store.db.execute(
            "SELECT next_number FROM wb_project_counters WHERE project_id=?", (project["id"],)
        ).fetchone()[0]
        == 1
    )
    assert planner.store.get("plan_import", ws.id, proposed["id"]).status == "PREVIEW"


async def test_reconcile_stale_and_active_conflict(graph):
    planner, ws, _, _ = graph
    first = preview(graph)
    result, _ = commit(graph, first)
    changed = plan()
    changed["tasks"][0]["objective"] = "New objective"
    proposed = preview(graph, [changed], reconcile=True)
    comparison = proposed["proposal"]["comparison"]
    assert comparison["b0_n0"]["group"] == "changed" and comparison["b0_n1"]["group"] == "unchanged"
    actions = {key: "update" if row["group"] == "changed" else "keep" for key, row in comparison.items()}
    revisions = {row["item_id"]: row["revision"] for row in comparison.values()}
    body = {
        "command_id": identity(),
        "expected_revision": 1,
        "source_hash": proposed["source_hash"],
        "actions": actions,
        "item_revisions": revisions,
    }
    item_id = result.body["items"]["b0_n0"]
    planner.service.update(
        "work_item", ws.id, item_id, {"command_id": identity(), "expected_revision": 1, "title": "Other writer"}, HUMAN
    )
    with pytest.raises(WorkbenchError, match="changed"):
        planner.commit(ws.id, proposed["id"], body, HUMAN, reconcile=True)
    proposed = preview(graph, [changed], reconcile=True)
    comparison = proposed["proposal"]["comparison"]
    old = planner.store.get("work_item", ws.id, item_id)
    with planner.store.router.transaction():
        active = WorkItem.model_validate(
            {**old.model_dump(), "revision": old.revision + 1, "status": "RUNNING", "updated_at": now()}
        )
        planner.service.persist("work_item", active, HUMAN, "test_running", previous=old)
    proposed = preview(graph, [changed], reconcile=True)
    body = {
        **body,
        "command_id": identity(),
        "source_hash": proposed["source_hash"],
        "item_revisions": {row["item_id"]: row["revision"] for row in proposed["proposal"]["comparison"].values()},
    }
    with pytest.raises(WorkbenchError, match="active/completed"):
        planner.commit(ws.id, proposed["id"], body, HUMAN, reconcile=True)


async def test_reconcile_update_preserves_key_and_replaces_edges(graph):
    planner, ws, _, _ = graph
    first = preview(graph)
    result, _ = commit(graph, first)
    changed = plan()
    changed["tasks"][1]["objective"] = "Replace dependency"
    changed["tasks"][1]["dependencies"] = []
    proposed = preview(graph, [changed], reconcile=True)
    comparison = proposed["proposal"]["comparison"]
    body = {
        "command_id": identity(),
        "expected_revision": 1,
        "source_hash": proposed["source_hash"],
        "actions": {key: "update" if row["group"] == "changed" else "keep" for key, row in comparison.items()},
        "item_revisions": {row["item_id"]: row["revision"] for row in comparison.values()},
    }
    changed_result = planner.commit(ws.id, proposed["id"], body, HUMAN, reconcile=True)
    assert changed_result.body["items"] == result.body["items"]
    item = planner.store.get("work_item", ws.id, result.body["items"]["b0_n1"])
    assert item.display_key == "GR-2" and item.contract_revision == 2 and item.status == "BACKLOG"
    assert item.spec.objective == "Replace dependency"
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_dependencies").fetchone()[0] == 0


async def test_stale_prd_commit_and_explicit_selection(graph):
    planner, ws, _, prd = graph
    proposed = preview(graph)
    with pytest.raises(WorkbenchError, match="choose"):
        commit(graph, proposed, actions={})
    planner.service.update(
        "prd", ws.id, prd["id"], {"command_id": identity(), "expected_revision": 3, "title": "Changed PRD"}, HUMAN
    )
    with pytest.raises(WorkbenchError, match="PRD changed"):
        commit(graph, proposed)
    assert planner.store.db.execute("SELECT COUNT(*) FROM wb_work_items").fetchone()[0] == 0


async def test_completed_item_history_preserved(graph):
    planner, ws, _, _ = graph
    first = preview(graph)
    result, _ = commit(graph, first)
    item_id = result.body["items"]["b0_n0"]
    old = planner.store.get("work_item", ws.id, item_id)
    with planner.store.router.transaction():
        done = WorkItem.model_validate({**old.model_dump(), "revision": 2, "status": "DONE", "updated_at": now()})
        planner.service.persist("work_item", done, HUMAN, "test_completed", previous=old)
    proposed = preview(graph, [plan(1)], reconcile=True)
    comparison = proposed["proposal"]["comparison"]
    body = {
        "command_id": identity(),
        "expected_revision": 1,
        "source_hash": proposed["source_hash"],
        "actions": {key: "keep" for key in comparison},
        "item_revisions": {row["item_id"]: row["revision"] for row in comparison.values()},
    }
    kept = planner.commit(ws.id, proposed["id"], body, HUMAN, reconcile=True)
    assert kept.body["items"]["b0_n0"] == item_id
    assert planner.store.get("work_item", ws.id, item_id).status == "DONE"
    assert (
        planner.store.db.execute("SELECT COUNT(*) FROM wb_entity_versions WHERE entity_id=?", (item_id,)).fetchone()[0]
        == 2
    )
