"""Workbench authority boundaries, exact money, and independent graph identity."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from local_ai_router.dag import normalize_dag
from local_ai_router.schema import ExecutionPlan, TaskNode
from local_ai_router.workbench import schema as wb

EXAMPLES = Path(__file__).parent / "fixtures/workbench/examples"
COMMAND_ID = str(uuid4())
WORKSPACE_ID = str(uuid4())
PROJECT_ID = str(uuid4())


def draft(**changes):
    return {
        "command_id": COMMAND_ID,
        "workspace_id": WORKSPACE_ID,
        "project_id": PROJECT_ID,
        "title": "A work item",
        "execution_kind": "MANUAL",
        **changes,
    }


def audit(**changes):
    return {
        "id": str(uuid4()),
        "revision": 1,
        "created_at": 1700000000123,
        "updated_at": 1700000000123,
        "created_by": "host",
        "updated_by": "host",
        **changes,
    }


def test_example_contracts_roundtrip_and_draft_ready_boundary():
    item = wb.CreateWorkItem.model_validate_json((EXAMPLES / "work-item.json").read_text())
    assert wb.CreateWorkItem.model_validate_json(item.model_dump_json()) == item
    item.spec.require_ready(item.execution_kind, "registered-root")
    for missing in (
        "objective",
        "acceptance_criteria",
        "definition_of_done",
        "allowed_paths",
        "expected_artifacts",
        "validation_checks",
    ):
        values = item.spec.model_dump()
        values[missing] = "" if missing == "objective" else []
        if missing == "allowed_paths":
            values["expected_artifacts"] = []
        incomplete = wb.WorkItemSpec(**values)
        with pytest.raises(ValueError, match="READY"):
            incomplete.require_ready(wb.ExecutionKind.ENGINE, "registered-root")
    with pytest.raises(ValueError, match="registered repository"):
        item.spec.require_ready(wb.ExecutionKind.ENGINE, None)
    empty = wb.CreateWorkItem(**draft())
    with pytest.raises(ValueError, match="READY"):
        empty.spec.require_ready(wb.ExecutionKind.MANUAL, None)
    complete_manual = wb.WorkItemSpec(
        objective="Review", acceptance_criteria=["Finding recorded"], definition_of_done=["Human reviewed"]
    )
    complete_manual.require_ready(wb.ExecutionKind.MANUAL, None)
    with pytest.raises(ValidationError):
        wb.CreateWorkItem(**{key: value for key, value in draft().items() if key != "execution_kind"})
    prd = wb.CreatePRD.model_validate_json((EXAMPLES / "prd.json").read_text())
    prd.spec.require_complete()
    assert wb.CreatePRD.model_validate_json(prd.model_dump_json()) == prd
    with pytest.raises(ValueError, match="PRD requires"):
        wb.PRDSpec().require_complete()


def test_workbench_graph_over_64_nodes():
    ids = [str(uuid4()) for _ in range(100)]
    result = wb.validate_workbench_graph(ids, list(zip(ids, ids[1:], strict=False)))
    assert result[ids[-1]] == [ids[-2]]
    assert result[ids[0]] == []


@pytest.mark.parametrize(
    ("ids", "edges", "error"),
    [
        (["a", "a"], [], "Duplicate task ids"),
        (["a"], [("a", "a")], "cycle"),
        (["a"], [("a", "b")], "Unknown dependency edge"),
        (["a", "b"], [("a", "b"), ("a", "b")], "Duplicate dependency edge"),
        (["a", "b"], [("a", "b"), ("b", "a")], "cycle"),
    ],
)
def test_self_unknown_duplicate_cycle_edges(ids, edges, error):
    with pytest.raises(ValueError, match=error):
        wb.validate_workbench_graph(ids, edges)


def test_unknown_dependency_lists_rejected():
    for dependencies in ({"missing": []}, {"a": ["missing"]}):
        with pytest.raises(ValueError, match="Unknown dependency"):
            normalize_dag(["a"], dependencies=dependencies)


def test_parent_cycle_and_depth_32():
    parents = {str(number): str(number - 1) if number else None for number in range(33)}
    wb.validate_parents(parents)
    with pytest.raises(ValueError, match="depth exceeds 32"):
        wb.validate_parents({**parents, "33": "32"})
    for values in ({"a": "a"}, {"a": "b", "b": "a"}):
        with pytest.raises(ValueError, match="cycle"):
            wb.validate_parents(values)
    with pytest.raises(ValueError, match="Unknown parent"):
        wb.validate_parents({"a": "missing"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quality_target", float("nan")),
        ("quality_target", float("inf")),
        ("quality_target", -0.1),
        ("quality_target", 1.01),
        ("quality_target", True),
        ("budget_limit", "NaN"),
        ("budget_limit", "Infinity"),
        ("budget_limit", "-1"),
        ("budget_limit", 0.1),
        ("budget_limit", "1e3"),
        ("budget_limit", "01.0"),
        ("budget_limit", "9223372036854.775808"),
        ("priority", -1),
        ("priority", 5),
        ("priority", True),
        ("risk", 101),
        ("complexity", -1),
        ("max_attempts", 0),
        ("engine_max_attempts", 6),
        ("title", "x" * 201),
        ("title", "  "),
        ("workspace_id", "not-a-uuid"),
        ("project_id", str(uuid4()).upper()),
        ("status", "DONE"),
        ("actor_type", "HUMAN"),
        ("assigned_agent_id", str(uuid4())),
        ("contract_revision", 1),
        ("run_id", "model-provided"),
        ("evidence_hash", "a" * 64),
    ],
)
def test_finite_numeric_bounds_and_unknown_fields(field, value):
    with pytest.raises(ValidationError):
        wb.CreateWorkItem(**draft(**{field: value}))


def test_exact_money_without_spending_inflation():
    for value, expected in (
        ("0", 0),
        ("0.0000009", 0),
        ("0.0000019", 1),
        ("0.10", 100000),
        ("9223372036854.775807", 2**63 - 1),
    ):
        assert wb.budget_microusd(value) == expected
    assert wb.budget_microusd("0.99999999999999999999999999999999999") == 999999


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "C:\\root",
        "a//b",
        "a/./b",
        "file:stream",
        "*.py",
        "[ab].py",
        "NUL.txt",
        "nested/COM1",
        ".git/config",
        ".env",
        "credentials",
        "file.",
        "bad\x00file",
    ],
)
def test_exact_path_syntax_and_artifact_scope(path):
    with pytest.raises(ValidationError):
        wb.WorkItemSpec(allowed_paths=[path])
    with pytest.raises(ValidationError, match="within allowed_paths"):
        wb.WorkItemSpec(allowed_paths=["safe.txt"], expected_artifacts=["other.txt"])


def test_collections_provenance_and_prd_ids_are_bounded():
    assert wb.WorkItemSpec(allowed_paths=["src\\one.py"], expected_artifacts=["src/one.py"]).allowed_paths == [
        "src/one.py"
    ]
    for changes in (
        {"constraints": ["a"] * 101},
        {"acceptance_criteria": ["x" * 2001]},
        {"context_notes": "x" * 100001},
        {"validation_checks": ["python -c evil"]},
        {"status": "DONE"},
    ):
        with pytest.raises(ValidationError):
            wb.WorkItemSpec(**changes)
    provenance = {
        "kind": "plan",
        "import_id": str(uuid4()),
        "source_plan_id": "plan",
        "source_node_id": "node",
        "source_hash": "a" * 64,
    }
    assert wb.WorkItemSpec(source_provenance=provenance).source_provenance.source_plan_id == "plan"
    for data in ({"kind": "manual", "actor": "human"}, {"kind": "plan", "import_id": "plan"}):
        with pytest.raises(ValidationError):
            wb.WorkItemSpec(source_provenance=data)
    with pytest.raises(ValidationError, match="Duplicate PRD"):
        wb.PRDSpec(functional_requirements=[{"id": "FR-1", "text": "First"}, {"id": "FR-1", "text": "Second"}])


def test_update_field_authority_and_null_semantics():
    values = {"command_id": COMMAND_ID, "expected_revision": 1}
    for model in (wb.UpdateWorkspace, wb.UpdateProject, wb.UpdatePRD, wb.UpdateWorkItem):
        for field in ("status", "actor_id", "revision", "created_at"):
            with pytest.raises(ValidationError):
                model(**values, **{field: "injected"})
    for field in ("title", "budget_limit", "execution_kind", "spec"):
        with pytest.raises(ValidationError, match="cannot be null"):
            wb.UpdateWorkItem(**values, **{field: None})
    patch = wb.UpdateWorkItem(**values, parent_item_id=None)
    assert patch.model_fields_set == {"command_id", "expected_revision", "parent_item_id"}
    assert wb.UpdateProject(**values, key="WB").key == "WB"
    with pytest.raises(ValidationError):
        wb.CreateProject(command_id=COMMAND_ID, workspace_id=WORKSPACE_ID, key="a", name="Invalid key")


def test_command_discriminator_and_approval_evidence():
    ready = json.loads((EXAMPLES / "command.json").read_text())
    assert wb.command_adapter.validate_python(ready).command == "ready"
    for command in ("RUNNING", "engine_started", "approve_as_human"):
        with pytest.raises(ValidationError):
            wb.command_adapter.validate_python({**ready, "command": command})
    with pytest.raises(ValidationError):
        wb.command_adapter.validate_python({**ready, "actor_type": "HUMAN"})
    decision = {
        **ready,
        "command": "approve_completion",
        "evidence_hash": "a" * 64,
        "contract_revision": 1,
        "reason": "Reviewed current evidence",
    }
    assert wb.command_adapter.validate_python(decision).evidence_hash == "a" * 64
    with pytest.raises(ValidationError):
        wb.command_adapter.validate_python({key: value for key, value in decision.items() if key != "evidence_hash"})
    with pytest.raises(ValidationError):
        wb.command_adapter.validate_python(
            {**ready, "command": "manual_submit", "evidence": {"checklist": [], "references": []}}
        )
    with pytest.raises(ValidationError):
        wb.command_adapter.validate_python({**ready, "command": "block", "reason_code": "anything", "reason": "Why"})


def test_domain_event_identity_and_timestamp_roundtrip():
    workspace = wb.Workspace(name="Workspace", **audit(id=WORKSPACE_ID))
    assert wb.Workspace.model_validate_json(workspace.model_dump_json()) == workspace
    event = wb.WorkbenchEvent(
        sequence=1,
        event_id=str(uuid4()),
        workspace_id=workspace.id,
        entity_type="workspace",
        entity_id=workspace.id,
        entity_revision=1,
        event_type="created",
        actor_type="HUMAN",
        actor_id="host",
        timestamp=1700000000123,
        payload=workspace,
    )
    assert wb.WorkbenchEvent.model_validate_json(event.model_dump_json()) == event
    assert json.loads(event.model_dump_json())["timestamp"].endswith(".123Z")
    for changes in ({"entity_id": str(uuid4())}, {"workspace_id": str(uuid4())}, {"entity_revision": 2}):
        with pytest.raises(ValidationError):
            wb.WorkbenchEvent(**{**event.model_dump(), **changes})
    project = wb.Project(workspace_id=WORKSPACE_ID, key="WB", name="Project", **audit())
    assert wb.Project.model_validate_json(project.model_dump_json()) == project
    item_values = wb.CreateWorkItem(**draft()).model_dump(exclude={"command_id"})
    item = wb.WorkItem(**item_values, **audit(), number=1, display_key="WB-1", status="BACKLOG", contract_revision=1)
    assert wb.WorkItem.model_validate_json(item.model_dump_json()) == item
    with pytest.raises(ValidationError, match="Display key number"):
        wb.WorkItem(**{**item.model_dump(), "display_key": "WB-2"})
    prd_values = wb.CreatePRD.model_validate_json((EXAMPLES / "prd.json").read_text()).model_dump(
        exclude={"command_id"}
    )
    prd = wb.PRD(**prd_values, **audit(), status="APPROVED")
    assert wb.PRD.model_validate_json(prd.model_dump_json()) == prd
    with pytest.raises(ValidationError, match="PRD requires"):
        wb.PRD(**{**prd.model_dump(), "spec": {}, "status": "IN_REVIEW"})
    for timestamp in (-1, "2026-10-12T00:00:00+05:30", True):
        with pytest.raises(ValidationError):
            TypeAdapter(wb.Timestamp).validate_python(timestamp)
    with pytest.raises(ValidationError):
        wb.CreateComment(command_id=COMMAND_ID, workspace_id=WORKSPACE_ID, item_id=item.id, body="x" * 10001)


def test_executionplan_compatibility_and_64_node_limit():
    def node(name, dependencies=()):
        return TaskNode(
            id=name,
            title=name,
            objective=name,
            acceptance_criteria=["Pass"],
            expected_artifacts=["file.py"],
            dependencies=list(dependencies),
        )

    plan = ExecutionPlan(
        goal="Compatibility",
        success_criteria=["Pass"],
        tasks=[node("b", ["a", "a"]), node("a")],
        dependency_edges=[("a", "b"), ("a", "b")],
    )
    assert plan.tasks[0].dependencies == ["a"]
    assert plan.dependency_edges == [("a", "b"), ("a", "b")]
    assert ExecutionPlan.model_validate_json(plan.model_dump_json()) == plan
    values = {"goal": "Bounded", "success_criteria": ["Pass"]}
    assert len(ExecutionPlan(**values, tasks=[node(str(i)) for i in range(64)]).tasks) == 64
    with pytest.raises(ValidationError):
        ExecutionPlan(**values, tasks=[node(str(i)) for i in range(65)])
