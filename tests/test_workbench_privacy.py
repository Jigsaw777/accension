"""Item state commands, transaction-safe DAG edits, and strictest effective policy."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from pydantic import ValidationError

from local_ai_router.store import Store
from local_ai_router.workbench.policy import CAPABILITIES, Principal
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.service import WorkbenchService
from local_ai_router.workbench.store import WorkbenchError, identity


@pytest.fixture
def context(settings):
    router = Store(settings)
    service = WorkbenchService(router)
    human = Principal("human", ActorType.HUMAN, CAPABILITIES, frozenset({"*"}), True)
    ws = service.create("workspace", {"command_id": identity(), "name": "Test"}, human).body
    project = service.create(
        "project", {"command_id": identity(), "workspace_id": ws["id"], "key": "WB", "name": "Test"}, human
    ).body
    yield service, human, ws, project
    router.close()


def item(context, **fields):
    service, human, ws, project = context
    return service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws["id"],
            "project_id": project["id"],
            "title": "Task",
            "execution_kind": "MANUAL",
            "spec": {"objective": "Do task", "acceptance_criteria": ["Accepted"], "definition_of_done": ["Evidence"]},
            **fields,
        },
        human,
    ).body


def command(context, work, name, **fields):
    service, human, ws, _ = context
    return service.item_command(
        ws["id"],
        work["id"],
        {"command_id": identity(), "expected_revision": work["revision"], "command": name, **fields},
        human,
    ).body


def test_allowed_and_forbidden_transition_matrix(context):
    service, human, ws, _ = context
    work = item(context)
    assert service.allowed_actions(service.get("work_item", ws["id"], work["id"], human), human) == [
        "ready",
        "block",
        "cancel",
    ]
    with pytest.raises(WorkbenchError, match="unavailable"):
        command(context, work, "unblock")
    work = command(context, work, "ready")
    assert work["status"] == "READY"
    with pytest.raises(WorkbenchError, match="unavailable"):
        command(context, work, "ready")
    work = command(context, work, "block", reason_code="MANUAL_BLOCK", reason="Waiting")
    assert work["resume_state"] == "READY"
    work = command(context, work, "unblock")
    work = command(context, work, "cancel")
    assert work["status"] == "CANCELLED"
    work = command(context, work, "reopen", reason="Need again")
    assert work["status"] == "BACKLOG" and work["contract_revision"] == 2
    for action in ("request_execution", "request_review", "manual_submit"):
        with pytest.raises((WorkbenchError, ValidationError)):
            command(context, work, action)
    read_only = replace(human, capabilities=frozenset({"workspace.read"}))
    assert service.allowed_actions(service.get("work_item", ws["id"], work["id"], human), read_only) == []


def test_policy_cannot_weaken_parent(context, settings):
    service, human, ws, project = context
    ws = service.update(
        "workspace",
        ws["id"],
        ws["id"],
        {
            "command_id": identity(),
            "expected_revision": 1,
            "inference_policy": "LOCAL_ONLY",
            "execution_policy": {"budget_ceiling": "0.03"},
        },
        human,
    ).body
    project = service.update(
        "project",
        ws["id"],
        project["id"],
        {"command_id": identity(), "expected_revision": 1, "default_privacy": "CLOUD_ALLOWED"},
        human,
    ).body
    work = item(
        context, privacy_policy="CLOUD_ALLOWED", review_policy="AUTO_VERIFY", budget_limit="1", quality_target=0.99
    )
    effective = service.item_policy(service.get("work_item", ws["id"], work["id"], human))
    assert effective.privacy == "LOCAL_ONLY"
    assert effective.review == "HUMAN_APPROVAL_REQUIRED"
    assert effective.budget_microusd == 30_000
    assert effective.quality == 0.99


def test_concurrent_opposite_edges_rejected(context, settings):
    service, human, ws, _ = context
    left, right = item(context), item(context)
    router = Store(settings)
    second = WorkbenchService(router)
    barrier = Barrier(2)

    def edit(index):
        barrier.wait(timeout=10)
        target, blocker = (left, right) if index == 0 else (right, left)
        try:
            return (
                (service, second)[index]
                .dependencies(
                    ws["id"],
                    target["id"],
                    {"command_id": identity(), "expected_revision": 1, "add": [blocker["id"]]},
                    human,
                )
                .status
            )
        except WorkbenchError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(edit, range(2)))
        assert sorted(map(str, results)) == ["200", "INVALID_GRAPH"]
        assert service.store.db.execute("SELECT COUNT(*) FROM wb_dependencies").fetchone()[0] == 1
    finally:
        router.close()


def test_reopened_dependency_invalidates_queued_descendant(context):
    service, human, ws, _ = context
    blocker, descendant = item(context), item(context)
    descendant = service.dependencies(
        ws["id"], descendant["id"], {"command_id": identity(), "expected_revision": 1, "add": [blocker["id"]]}, human
    ).body
    # Internal verified completion stands in for WB-19's later evidence command.
    with service.store.router.transaction():
        entity = service.store.get("work_item", ws["id"], blocker["id"])
        blocker = service.changed_item(entity, {"status": "DONE"}, human, "test_verified").model_dump(mode="json")
    descendant = command(context, descendant, "ready")
    assert descendant["status"] == "READY"
    command(context, blocker, "reopen", reason="Changed input")
    changed = service.get("work_item", ws["id"], descendant["id"], human)
    assert changed.status == "BLOCKED" and changed.reason_code == "DEPENDENCY_NOT_DONE"
    still_blocked = command(context, changed.model_dump(mode="json"), "unblock")
    assert still_blocked["status"] == "BLOCKED"


def test_public_status_and_actor_injection_denied(context):
    service, human, ws, project = context
    for field, value in (("status", "DONE"), ("actor_type", "HUMAN"), ("active_attempt_id", identity())):
        with pytest.raises(ValidationError):
            item(context, **{field: value})
    work = item(context)
    for field, value in (("status", "RUNNING"), ("created_by", "system")):
        with pytest.raises(ValidationError):
            service.update(
                "work_item",
                ws["id"],
                work["id"],
                {"command_id": identity(), "expected_revision": 1, field: value},
                human,
            )


def test_engine_readiness_registered_checks_and_complete_contract(context, repo):
    service, human, ws, project = context
    project = service.update(
        "project",
        ws["id"],
        project["id"],
        {"command_id": identity(), "expected_revision": 1, "repository_ref": next(iter(service.repositories()))},
        human,
    ).body
    work = item(context, execution_kind="ENGINE")
    with pytest.raises(WorkbenchError, match="ENGINE READY"):
        command(context, work, "ready")
    spec = {
        "objective": "Implement",
        "acceptance_criteria": ["Pass"],
        "definition_of_done": ["Tests"],
        "allowed_paths": ["app.py"],
        "expected_artifacts": ["app.py"],
        "validation_checks": ["unregistered"],
    }
    work = service.update(
        "work_item", ws["id"], work["id"], {"command_id": identity(), "expected_revision": 1, "spec": spec}, human
    ).body
    with pytest.raises(WorkbenchError, match="registered"):
        command(context, work, "ready")
    spec["validation_checks"] = ["tests"]
    work = service.update(
        "work_item", ws["id"], work["id"], {"command_id": identity(), "expected_revision": 2, "spec": spec}, human
    ).body
    assert command(context, work, "ready")["status"] == "READY"


def test_contract_edits_frozen_cosmetics_and_comments_allowed(context):
    service, human, ws, _ = context
    work = item(context)
    with service.store.router.transaction():
        entity = service.store.get("work_item", ws["id"], work["id"])
        work = service.changed_item(
            entity, {"status": "RUNNING", "active_attempt_id": identity()}, human, "test_started"
        ).model_dump(mode="json")
    with pytest.raises(WorkbenchError, match="frozen"):
        service.update(
            "work_item",
            ws["id"],
            work["id"],
            {"command_id": identity(), "expected_revision": 2, "budget_limit": "2"},
            human,
        )
    changed = service.update(
        "work_item", ws["id"], work["id"], {"command_id": identity(), "expected_revision": 2, "title": "Display"}, human
    ).body
    assert changed["status"] == "RUNNING" and changed["contract_revision"] == 1
    comment = service.create(
        "comment",
        {"command_id": identity(), "workspace_id": ws["id"], "item_id": work["id"], "body": "Observation"},
        human,
    ).body
    assert comment["revision"] == 1
    with pytest.raises(WorkbenchError, match="frozen"):
        service.dependencies(
            ws["id"], work["id"], {"command_id": identity(), "expected_revision": 3, "add": [identity()]}, human
        )


def test_policy_change_invalidates_ready_and_archive_retains_history(context):
    service, human, ws, project = context
    work = command(context, item(context), "ready")
    service.update(
        "workspace",
        ws["id"],
        ws["id"],
        {"command_id": identity(), "expected_revision": 1, "execution_policy": {"budget_ceiling": "0.01"}},
        human,
    )
    changed = service.get("work_item", ws["id"], work["id"], human)
    assert changed.status == "BACKLOG" and changed.reason_code == "PRIVACY_POLICY"
    archived = service.archive(
        "project", ws["id"], project["id"], {"command_id": identity(), "expected_revision": 1}, human
    ).body
    assert archived["archived_at"] is not None
    assert service.list("project", ws["id"], human) == []
    assert service.get("work_item", ws["id"], work["id"], human).id == work["id"]
    with pytest.raises(WorkbenchError, match="archived"):
        item(context)


def test_execution_kind_replay_requires_human_and_contract_edit_resets_ready(context):
    service, human, ws, _ = context
    work = item(context)
    req = {"command_id": identity(), "expected_revision": 1, "execution_kind": "ENGINE"}
    changed = service.update("work_item", ws["id"], work["id"], req, human).body
    with pytest.raises(WorkbenchError, match="human"):
        service.update("work_item", ws["id"], work["id"], req, replace(human, actor_type=ActorType.MACHINE))
    assert changed["contract_revision"] == 2
    manual = command(context, item(context), "ready")
    edited = service.update(
        "work_item",
        ws["id"],
        manual["id"],
        {"command_id": identity(), "expected_revision": 2, "budget_limit": "0.05"},
        human,
    ).body
    assert edited["status"] == "BACKLOG" and edited["contract_revision"] == 2


def test_cross_project_dependencies_and_transitive_privacy(context):
    service, human, ws, project = context
    service.update(
        "workspace",
        ws["id"],
        ws["id"],
        {
            "command_id": identity(),
            "expected_revision": 1,
            "privacy_mode": "HYBRID",
            "inference_policy": "CLOUD_ALLOWED",
            "review_policy": "AUTO_VERIFY",
        },
        human,
    )
    service.update(
        "project",
        ws["id"],
        project["id"],
        {"command_id": identity(), "expected_revision": 1, "default_privacy": "CLOUD_ALLOWED"},
        human,
    )
    second = service.create(
        "project",
        {
            "command_id": identity(),
            "workspace_id": ws["id"],
            "name": "Private source",
            "key": "PR",
            "default_privacy": "LOCAL_ONLY",
        },
        human,
    ).body
    source = service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws["id"],
            "project_id": second["id"],
            "title": "Source",
            "execution_kind": "MANUAL",
            "privacy_policy": "CLOUD_ALLOWED",
        },
        human,
    ).body
    middle, target = item(context, privacy_policy="CLOUD_ALLOWED"), item(context, privacy_policy="CLOUD_ALLOWED")
    service.dependencies(
        ws["id"], middle["id"], {"command_id": identity(), "expected_revision": 1, "add": [source["id"]]}, human
    )
    service.dependencies(
        ws["id"], target["id"], {"command_id": identity(), "expected_revision": 1, "add": [middle["id"]]}, human
    )
    policy = service.item_policy(service.get("work_item", ws["id"], target["id"], human))
    assert policy.privacy == "LOCAL_ONLY"
