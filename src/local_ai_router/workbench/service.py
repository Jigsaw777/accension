"""Workbench domain services; repository registration remains owned by Settings."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .events import EventWake, append_event
from .policy import CAPABILITIES, Principal, authorize, effective
from .schema import (
    ActorType,
    CreateComment,
    CreatePRD,
    CreateProject,
    CreateWorkItem,
    CreateWorkspace,
    DependencyEdit,
    PRDCommand,
    UpdateComment,
    UpdatePRD,
    UpdateProject,
    UpdateWorkItem,
    UpdateWorkspace,
    Workspace,
    command_adapter,
    validate_workbench_graph,
)
from .store import ENTITIES, WorkbenchError, WorkbenchStore, identity, now

CREATES = {
    "workspace": CreateWorkspace,
    "project": CreateProject,
    "prd": CreatePRD,
    "work_item": CreateWorkItem,
    "comment": CreateComment,
}
UPDATES = {
    "workspace": UpdateWorkspace,
    "project": UpdateProject,
    "prd": UpdatePRD,
    "work_item": UpdateWorkItem,
    "comment": UpdateComment,
}
WRITE = {
    "workspace": "policy.admin",
    "project": "project.write",
    "prd": "prd.write",
    "work_item": "work.write",
    "comment": "comment.write",
}
COSMETIC_ITEM_FIELDS = {"title", "priority", "type"}


@dataclass(frozen=True)
class CommandResult:
    status: int
    body: dict
    replayed: bool = False


def payload_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class WorkbenchService:
    def remote_fields(self, kind, request, principal):
        if not isinstance(principal, Principal):
            raise WorkbenchError("UNAUTHENTICATED", "Authenticated principal required", 401)
        if principal.local:
            return
        protected = {
            "repository_ref",
            "default_privacy",
            "default_budget",
            "privacy_policy",
            "budget_limit",
            "max_attempts",
            "engine_max_attempts",
            "quality_target",
            "review_policy",
            "execution_policy",
            "inference_policy",
            "privacy_mode",
        }
        if kind == "workspace" or request.model_fields_set & protected:
            raise WorkbenchError(
                "LOCAL_POLICY_REQUIRED", "Policy and repository changes require local administration", 403
            )

    def __init__(self, store):
        self.store = WorkbenchStore(store)
        self.settings = store.settings
        self.wake = EventWake()
        self.last_notification_error = None
        self.skills = None
        self.agents = None
        self.execution = None

    def command(self, principal, workspace_id, capability, command_id, kind, payload, apply, *, local_only=False):
        """Authorize, replay or apply atomically. External work follows the returned intent."""
        authorize(principal, workspace_id, capability, local_only=local_only)
        digest = payload_hash({"kind": kind, "workspace_id": workspace_id, "payload": payload})
        try:
            with self.store.router.transaction():
                authorize(principal, workspace_id, capability, local_only=local_only)
                previous = self.store.db.execute(
                    "SELECT payload_hash,state,response_status,response_json FROM wb_commands WHERE workspace_id=? AND principal_id=? AND command_id=?",
                    (workspace_id, principal.id, command_id),
                ).fetchone()
                if previous:
                    if previous[0] != digest:
                        raise WorkbenchError("IDEMPOTENCY_CONFLICT", "Command id was already used for different input")
                    if previous[1] != "COMPLETED":
                        raise WorkbenchError("COMMAND_UNCERTAIN", "Command requires reconciliation")
                    try:
                        return CommandResult(previous[2], json.loads(previous[3]), True)
                    except (ValueError, TypeError) as exc:
                        raise WorkbenchError("CORRUPT_DATA", "Invalid stored command result", 500) from exc
                self.store.db.execute(
                    "INSERT INTO wb_commands(workspace_id,principal_id,command_id,kind,payload_hash,state,created_at) VALUES(?,?,?,?,?,'PENDING',?)",
                    (workspace_id, principal.id, command_id, kind, digest, now()),
                )
                result = apply()
                self.store.db.execute(
                    "UPDATE wb_commands SET state='COMPLETED',response_status=?,response_json=?,entity_id=? WHERE workspace_id=? AND principal_id=? AND command_id=?",
                    (
                        result.status,
                        json.dumps(result.body, sort_keys=True, separators=(",", ":"), allow_nan=False),
                        result.body.get("id"),
                        workspace_id,
                        principal.id,
                        command_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkbenchError(
                "CONSTRAINT_CONFLICT", "Mutation conflicts with an existing identity or reference"
            ) from exc
        try:
            self.wake.notify()
            self.last_notification_error = None
        except Exception as exc:
            self.last_notification_error = type(exc).__name__
        return result

    def persist(self, kind, entity, principal, event_type, *, previous=None, tombstone=False):
        if previous is None:
            self.store.insert(kind, entity)
        else:
            self.store.replace(kind, entity, previous.revision)
        workspace_id = entity.id if kind == "workspace" else entity.workspace_id
        entity = self.store.get(kind, workspace_id, entity.id)
        append_event(self.store, kind, entity, principal, event_type, tombstone=tombstone)
        return entity

    def get(self, kind, workspace_id, entity_id, principal):
        authorize(principal, workspace_id, "workspace.read")
        return self.store.get(kind, workspace_id, entity_id)

    def list(self, kind, workspace_id, principal, **filters):
        authorize(principal, workspace_id, "workspace.read")
        return self.store.list(kind, workspace_id, **filters)

    def create(self, kind, request, principal):
        request = CREATES[kind].model_validate(request)
        self.remote_fields(kind, request, principal)
        workspace_id = "local" if kind == "workspace" else request.workspace_id
        if kind == "project" and request.repository_ref:
            authorize(principal, workspace_id, "repository.admin", local_only=True)

        def apply():
            if kind == "project" and request.repository_ref:
                authorize(principal, workspace_id, "repository.admin", local_only=True)
                self.repository(request.repository_ref)
            stamp = now()
            if kind == "work_item":
                entity = self.store.allocate_item(request, principal.id)
                append_event(self.store, kind, entity, principal, "created")
            else:
                model = ENTITIES[kind][1]
                fields = request.model_dump(exclude={"command_id"})
                if kind == "prd":
                    fields["status"] = "DRAFT"
                    fields.update(self.prd_template(fields))
                entity = model.model_validate(
                    {
                        **fields,
                        "id": identity(),
                        "revision": 1,
                        "created_at": stamp,
                        "updated_at": stamp,
                        "created_by": principal.id,
                        "updated_by": principal.id,
                    }
                )
                entity = self.persist(kind, entity, principal, "created")
            return CommandResult(201, entity.model_dump(mode="json"))

        return self.command(
            principal,
            workspace_id,
            WRITE[kind],
            request.command_id,
            "create_" + kind,
            request.model_dump(mode="json"),
            apply,
            local_only=kind == "workspace",
        )

    def update(self, kind, workspace_id, entity_id, request, principal):
        request = UPDATES[kind].model_validate(request)
        self.remote_fields(kind, request, principal)
        if kind == "project" and "repository_ref" in request.model_fields_set:
            authorize(principal, workspace_id, "repository.admin", local_only=True)
        if kind == "work_item" and "execution_kind" in request.model_fields_set:
            if not isinstance(principal, Principal) or principal.actor_type != ActorType.HUMAN:
                raise WorkbenchError("HUMAN_REQUIRED", "Execution kind edits require an observed human action", 403)

        def apply():
            previous = self.store.get(kind, workspace_id, entity_id)
            if previous.revision != request.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Entity changed; refresh before editing")
            fields = request.model_dump(exclude_unset=True, exclude={"command_id", "expected_revision"})
            if not fields:
                raise WorkbenchError("EMPTY_UPDATE", "Update requires at least one field", 422)
            if kind == "workspace":
                authorize(principal, workspace_id, "policy.admin", local_only=True)
            if kind == "project" and "repository_ref" in fields:
                authorize(principal, workspace_id, "repository.admin", local_only=True)
                if fields["repository_ref"]:
                    self.repository(fields["repository_ref"])
            if kind == "work_item" and set(fields) - COSMETIC_ITEM_FIELDS:
                if previous.active_attempt_id or previous.status in {"RUNNING", "VERIFYING"}:
                    raise WorkbenchError("ACTIVE_CONTRACT", "Executed contract is frozen while an attempt is active")
                fields["contract_revision"] = previous.contract_revision + 1
                if "execution_kind" in fields and fields["execution_kind"] != previous.execution_kind:
                    if previous.status != "BACKLOG" or principal.actor_type != ActorType.HUMAN:
                        raise WorkbenchError(
                            "HUMAN_REQUIRED", "Execution kind changes require human authorization in BACKLOG", 403
                        )
                fields["status"] = "BACKLOG"
                self.invalidate(previous)
            if kind == "prd" and previous.status == "APPROVED":
                fields["status"] = "DRAFT"
            if kind == "prd":
                fields.update(self.prd_template({**previous.model_dump(), **fields}))
            entity = ENTITIES[kind][1].model_validate(
                {
                    **previous.model_dump(),
                    **fields,
                    "revision": previous.revision + 1,
                    "updated_at": now(),
                    "updated_by": principal.id,
                }
            )
            entity = self.persist(kind, entity, principal, "updated", previous=previous)
            policy_fields = {
                "privacy_mode",
                "inference_policy",
                "execution_policy",
                "review_policy",
                "repository_ref",
                "default_privacy",
            }
            if kind in {"workspace", "project"} and set(fields) & policy_fields:
                self.invalidate_policy_items(kind, entity, principal)
            return CommandResult(200, entity.model_dump(mode="json"))

        payload = {"entity_id": entity_id, **request.model_dump(mode="json", exclude_unset=True)}
        return self.command(
            principal,
            workspace_id,
            WRITE[kind],
            request.command_id,
            "update_" + kind,
            payload,
            apply,
            local_only=kind == "workspace",
        )

    def revisions(self, kind, workspace_id, entity_id, principal, *, limit=50, before=None):
        self.get(kind, workspace_id, entity_id, principal)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise WorkbenchError("INVALID_PAGE", "Page size must be between 1 and 200", 422)
        clauses, params = ["workspace_id=?", "entity_type=?", "entity_id=?"], [workspace_id, kind, entity_id]
        if before is not None:
            if type(before) is not int or before < 1:
                raise WorkbenchError("INVALID_PAGE", "Revision cursor must be positive", 422)
            clauses.append("revision<?")
            params.append(before)
        with self.store.router.lock:
            rows = self.store.db.execute(
                f"SELECT snapshot_json FROM wb_entity_versions WHERE {' AND '.join(clauses)} ORDER BY revision DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            try:
                return [ENTITIES[kind][1].model_validate_json(row[0]) for row in rows]
            except ValueError as exc:
                raise WorkbenchError("CORRUPT_DATA", "Invalid entity revision", 500) from exc

    def operation_intent(self, workspace_id, kind, subject_id, source_revision, request_hash):
        if not self.store.db.in_transaction:
            raise RuntimeError("Operation intent requires an owning transaction")
        operation_id, stamp = identity(), now()
        self.store.db.execute(
            "INSERT INTO wb_operations(id,workspace_id,kind,subject_id,state,source_revision,request_hash,created_at,updated_at) VALUES(?,?,?,?,'PENDING',?,?,?,?)",
            (operation_id, workspace_id, kind, subject_id, source_revision, request_hash, stamp, stamp),
        )
        return CommandResult(202, {"id": operation_id, "state": "PENDING", "source_revision": source_revision})

    def item_policy(self, item):
        workspace = self.store.get("workspace", item.workspace_id, item.workspace_id)
        project = self.store.get("project", item.workspace_id, item.project_id)
        repository = self.repository(project.repository_ref) if project.repository_ref else None
        ids = self.store.db.execute(
            "WITH RECURSIVE sources(id) AS (SELECT blocker_id FROM wb_dependencies WHERE workspace_id=? AND dependent_id=? UNION SELECT d.blocker_id FROM wb_dependencies d JOIN sources s ON d.dependent_id=s.id WHERE d.workspace_id=?) SELECT id FROM sources",
            (item.workspace_id, item.id, item.workspace_id),
        ).fetchall()
        sources = []
        for row in ids:
            source = self.store.get("work_item", item.workspace_id, row[0])
            source_project = self.store.get("project", item.workspace_id, source.project_id)
            source_repository = (
                self.repository(source_project.repository_ref) if source_project.repository_ref else None
            )
            policy = effective(workspace, source_project, source, self.settings, source_repository)
            sources.append(
                source.model_copy(
                    update={
                        "privacy_policy": policy.privacy,
                        "review_policy": policy.review,
                        "quality_target": policy.quality,
                    }
                )
            )
        return effective(workspace, project, item, self.settings, repository, sources)

    def invalidate_policy_items(self, kind, entity, principal):
        workspace_id = entity.id if kind == "workspace" else entity.workspace_id
        query = "SELECT id FROM wb_work_items WHERE workspace_id=?"
        params = [workspace_id]
        if kind == "project":
            query += " AND project_id=?"
            params.append(entity.id)
        for row in self.store.db.execute(query, params).fetchall():
            item = self.store.get("work_item", workspace_id, row[0])
            self.invalidate(item)
            fields = {}
            if item.active_attempt_id:
                self.store.db.execute(
                    "UPDATE wb_attempts SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE workspace_id=? AND item_id=? AND id=?",
                    (now(), workspace_id, item.id, item.active_attempt_id),
                )
                fields.update(status="BLOCKED", reason_code="RECOVERY_REQUIRED", resume_state="READY")
            elif item.status in {"READY", "ASSIGNED", "WAITING_FOR_HUMAN"}:
                fields.update(status="BACKLOG", reason_code="PRIVACY_POLICY", assigned_agent_id=None, resume_state=None)
            if fields:
                self.changed_item(item, fields, principal, "policy_invalidated")

    def readiness(self, item):
        project = self.store.get("project", item.workspace_id, item.project_id)
        try:
            item.spec.require_ready(item.execution_kind, project.repository_ref)
        except ValueError as exc:
            raise WorkbenchError("INCOMPLETE_CONTRACT", str(exc), 422) from exc
        if item.execution_kind == "ENGINE":
            repo = self.repository(project.repository_ref)
            if not set(item.spec.validation_checks) <= repo.validation.keys():
                raise WorkbenchError("VALIDATION_UNREGISTERED", "Every validation check must be registered", 422)
            if not set(item.spec.required_tools) <= (self.settings.tool_registry.keys() | {"propose_file_changes"}):
                raise WorkbenchError("TOOL_UNAVAILABLE", "Required tool is not registered", 422)
            for skill_id in item.spec.required_skills:
                if self.skills is None:
                    raise WorkbenchError("SKILL_UNTRUSTED", "Skill registry is unavailable", 422)
                try:
                    skill = self.skills.get(skill_id)
                except ValueError as exc:
                    raise WorkbenchError("SKILL_UNTRUSTED", "Required skill is unknown", 422) from exc
                if skill is None or not skill.enabled or not skill.trusted:
                    raise WorkbenchError("SKILL_UNTRUSTED", "Required skill is missing, disabled or untrusted", 422)
        blockers = self.store.db.execute(
            "SELECT i.status FROM wb_dependencies d JOIN wb_work_items i ON i.id=d.blocker_id AND i.workspace_id=d.workspace_id WHERE d.workspace_id=? AND d.dependent_id=?",
            (item.workspace_id, item.id),
        ).fetchall()
        if any(row[0] != "DONE" for row in blockers):
            return "BLOCKED", "DEPENDENCY_NOT_DONE"
        if item.execution_kind == "ENGINE" and self.item_policy(item).budget_microusd == 0:
            return "BLOCKED", "BUDGET_EXHAUSTED"
        return "READY", None

    def invalidate(self, item):
        self.store.db.execute(
            "UPDATE wb_execution_grants SET revoked_at=? WHERE workspace_id=? AND item_id=? AND revoked_at IS NULL",
            (now(), item.workspace_id, item.id),
        )
        if not item.active_attempt_id:
            self.store.db.execute(
                "UPDATE wb_assignments SET status='RELEASED',ended_at=? WHERE workspace_id=? AND item_id=? AND status IN ('PROPOSED','RESERVED','ACTIVE')",
                (now(), item.workspace_id, item.id),
            )

    def changed_item(self, previous, fields, principal, event_type):
        entity = type(previous).model_validate(
            {
                **previous.model_dump(),
                **fields,
                "revision": previous.revision + 1,
                "updated_at": now(),
                "updated_by": principal.id,
            }
        )
        return self.persist("work_item", entity, principal, event_type, previous=previous)

    def invalidate_descendants(self, item, principal):
        rows = self.store.db.execute(
            "WITH RECURSIVE descendants(id) AS (SELECT dependent_id FROM wb_dependencies WHERE workspace_id=? AND blocker_id=? UNION SELECT d.dependent_id FROM wb_dependencies d JOIN descendants p ON d.blocker_id=p.id WHERE d.workspace_id=?) SELECT id FROM descendants",
            (item.workspace_id, item.id, item.workspace_id),
        ).fetchall()
        for row in rows:
            descendant = self.store.get("work_item", item.workspace_id, row[0])
            self.invalidate(descendant)
            fields = {"reason_code": "DEPENDENCY_NOT_DONE"}
            if descendant.active_attempt_id:
                self.store.db.execute(
                    "UPDATE wb_attempts SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE workspace_id=? AND item_id=? AND id=?",
                    (now(), item.workspace_id, descendant.id, descendant.active_attempt_id),
                )
                fields.update(status="BLOCKED", reason_code="RECOVERY_REQUIRED", resume_state="READY")
            elif descendant.status in {"READY", "ASSIGNED", "WAITING_FOR_HUMAN"}:
                fields.update(status="BLOCKED", resume_state="READY", assigned_agent_id=None)
            self.changed_item(descendant, fields, principal, "dependency_invalidated")

    def item_command(self, workspace_id, entity_id, request, principal):
        request = command_adapter.validate_python(request)
        if request.command in {"request_review", "approve_completion", "reject_completion"}:
            return self.completion_command(workspace_id, entity_id, request, principal)
        if request.command == "manual_submit" and self.execution:
            return self.execution.manual_request(workspace_id, entity_id, request.model_dump(mode="json"), principal)
        if request.command == "request_execution" and self.execution:
            return self.execution.request(workspace_id, entity_id, request.model_dump(mode="json"), principal)
        if request.command == "approve_execution" and self.agents:
            return self.agents.approve_execution(workspace_id, entity_id, request.model_dump(mode="json"), principal)
        permitted = {"ready", "block", "unblock", "unassign", "cancel", "reopen", "retry"}
        if request.command not in permitted:
            raise WorkbenchError("COMMAND_UNAVAILABLE", "Execution/evidence command is not available yet", 422)

        def apply():
            item = self.store.get("work_item", workspace_id, entity_id)
            if item.revision != request.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Item changed; refresh before applying command")
            transitions = {
                "ready": {"BACKLOG"},
                "block": {"BACKLOG", "READY", "ASSIGNED"},
                "unblock": {"BLOCKED"},
                "unassign": {"ASSIGNED"},
                "cancel": {
                    "BACKLOG",
                    "READY",
                    "ASSIGNED",
                    "BLOCKED",
                    "WAITING_FOR_HUMAN",
                    "REVIEW",
                    "FAILED",
                    "RUNNING",
                    "VERIFYING",
                },
                "reopen": {"DONE", "CANCELLED"},
                "retry": {"FAILED", "BLOCKED"},
            }
            if item.status not in transitions[request.command]:
                raise WorkbenchError("INVALID_STATE", "Command is unavailable in current item state")
            if item.active_attempt_id:
                if request.command == "cancel":
                    self.store.db.execute(
                        "UPDATE wb_attempts SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE id=? AND workspace_id=?",
                        (now(), item.active_attempt_id, workspace_id),
                    )
                    result = self.changed_item(item, {}, principal, "cancel_requested")
                    return CommandResult(202, result.model_dump(mode="json"))
                raise WorkbenchError("RECOVERY_REQUIRED", "Active attempt must stop and reconcile before this command")
            fields = {"reason_code": None, "resume_state": None, "assigned_agent_id": None}
            if request.command in {"ready", "unblock", "unassign", "retry"}:
                if request.command == "retry":
                    attempts = self.store.db.execute(
                        "SELECT COUNT(*) FROM wb_attempts WHERE workspace_id=? AND item_id=?", (workspace_id, item.id)
                    ).fetchone()[0]
                    if attempts >= item.max_attempts:
                        raise WorkbenchError("ATTEMPT_LIMIT", "Work item dispatch attempt limit reached")
                fields["status"], fields["reason_code"] = self.readiness(item)
                if fields["status"] == "BLOCKED":
                    fields["resume_state"] = "READY"
            elif request.command == "block":
                fields.update(status="BLOCKED", reason_code=request.reason_code, resume_state="READY")
            elif request.command == "cancel":
                fields["status"] = "CANCELLED"
            elif request.command == "reopen":
                fields.update(status="BACKLOG", contract_revision=item.contract_revision + 1)
            self.invalidate(item)
            result = self.changed_item(item, fields, principal, request.command)
            if request.command == "reopen":
                self.invalidate_descendants(result, principal)
            return CommandResult(200, result.model_dump(mode="json"))

        return self.command(
            principal,
            workspace_id,
            "work.write",
            request.command_id,
            "item_" + request.command,
            {"entity_id": entity_id, **request.model_dump(mode="json")},
            apply,
        )

    def evidence(self, item):
        from pathlib import Path

        from ..safety import digest, safe_path

        if item.active_attempt_id or item.status not in {"REVIEW", "WAITING_FOR_HUMAN", "DONE"}:
            raise WorkbenchError("VERIFICATION_REQUIRED", "Current verified evidence is required")
        ready, reason = self.readiness(item)
        if ready != "READY":
            raise WorkbenchError(
                reason or "VERIFICATION_REQUIRED", "Dependencies or contract no longer permit completion"
            )
        row = self.store.db.execute(
            "SELECT * FROM wb_verifications WHERE workspace_id=? AND item_id=? AND status IN ('VERIFIED','ATTESTED') ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (item.workspace_id, item.id),
        ).fetchone()
        if row is None:
            raise WorkbenchError("VERIFICATION_REQUIRED", "No successful verification exists")
        data = json.loads(row["checks_json"])
        if payload_hash(data) != row["evidence_hash"]:
            raise WorkbenchError("EVIDENCE_CHANGED", "Verification integrity failed")
        if row["attempt_id"]:
            attempt, contract = self.execution.contract(row["attempt_id"])
            snapshot = self.store.db.execute(
                "SELECT * FROM wb_receipt_snapshots WHERE attempt_id=?", (attempt["id"],)
            ).fetchone()
            if (
                attempt["state"] != "SUCCEEDED"
                or item.contract_revision != contract.item_contract_revision
                or self.agents.policy_hash(item) != contract.policy_revision_hash
                or snapshot is None
                or data.get("receipt_hash") != snapshot["receipt_hash"]
                or payload_hash(json.loads(snapshot["receipt_json"])) != snapshot["receipt_hash"]
            ):
                raise WorkbenchError("EVIDENCE_CHANGED", "Execution contract or receipt changed")
        elif data.get("contract_revision") != item.contract_revision or data.get(
            "policy_hash"
        ) != self.agents.policy_hash(item):
            raise WorkbenchError("EVIDENCE_CHANGED", "Manual contract or policy changed")
        artifacts = data.get("artifacts", [])
        if artifacts:
            project = self.store.get("project", item.workspace_id, item.project_id)
            root = Path(self.repository(project.repository_ref).path)
            for artifact in artifacts:
                if (
                    artifact["path"] not in item.spec.expected_artifacts
                    or digest(safe_path(root, artifact["path"])) != artifact["hash"]
                ):
                    raise WorkbenchError("ARTIFACT_CHANGED", "Verified artifact changed; verify again")
        return row, data

    def review_gate(self, item, principal):
        row, data = self.evidence(item)
        policy = self.item_policy(item)
        status, reason = "REVIEW", None
        if policy.review == "AUTO_VERIFY":
            status = "DONE"
        elif policy.review == "AI_REVIEW":
            review = self.store.db.execute(
                "SELECT * FROM wb_verifications WHERE item_id=? AND attempt_id=? AND kind='AI_REVIEW' AND status='ACCEPTED' ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (item.id, row["attempt_id"]),
            ).fetchone()
            accepted = json.loads(review["checks_json"]) if review else {}
            if (
                accepted.get("model_id")
                and payload_hash(accepted) == review["evidence_hash"]
                and payload_hash(sorted(accepted.get("artifacts", []), key=lambda a: a["path"]))
                == payload_hash(sorted(data.get("artifacts", []), key=lambda a: a["path"]))
            ):
                status = "DONE"
            else:
                status, reason = "WAITING_FOR_HUMAN", "NO_ELIGIBLE_REVIEWER"
        if item.status != status or item.reason_code != reason:
            return self.changed_item(item, {"status": status, "reason_code": reason}, principal, "review_gate_applied")
        return item

    def completion_command(self, workspace_id, item_id, request, principal):
        human = request.command != "request_review"
        capability = "review.approve" if human else "work.write"
        authorize(principal, workspace_id, capability)
        if human and principal.actor_type != "HUMAN":
            raise WorkbenchError("HUMAN_REQUIRED", "Completion decision requires a human", 403)

        def apply():
            item = self.store.get("work_item", workspace_id, item_id)
            if item.revision != request.expected_revision or item.status not in {"REVIEW", "WAITING_FOR_HUMAN"}:
                raise WorkbenchError("REVISION_CONFLICT", "Completion source changed")
            row, data = self.evidence(item)
            if not human:
                return CommandResult(200, self.review_gate(item, principal).model_dump(mode="json"))
            if request.contract_revision != item.contract_revision or request.evidence_hash != row["evidence_hash"]:
                raise WorkbenchError("EVIDENCE_CHANGED", "Approval must name the exact current evidence")
            decision = "APPROVE" if request.command == "approve_completion" else "REJECT"
            self.store.db.execute(
                "INSERT INTO wb_approvals(id,workspace_id,item_id,attempt_id,subject_kind,contract_revision,evidence_hash,decision,actor_type,actor_id,reason,created_at) VALUES(?,?,?,?,'COMPLETION',?,?,?,?,?,?,?)",
                (
                    identity(),
                    workspace_id,
                    item.id,
                    row["attempt_id"],
                    item.contract_revision,
                    row["evidence_hash"],
                    decision,
                    principal.actor_type,
                    principal.id,
                    request.reason,
                    now(),
                ),
            )
            result = self.changed_item(
                item,
                {
                    "status": "DONE" if decision == "APPROVE" else "FAILED",
                    "reason_code": None if decision == "APPROVE" else "REVIEW_REJECTED",
                },
                principal,
                "completion_" + decision.lower(),
            )
            return CommandResult(200, result.model_dump(mode="json"))

        return self.command(
            principal,
            workspace_id,
            capability,
            request.command_id,
            request.command,
            {"item_id": item_id, **request.model_dump(mode="json")},
            apply,
        )

    def dependencies(self, workspace_id, entity_id, request, principal):
        request = DependencyEdit.model_validate(request)

        def apply():
            item = self.store.get("work_item", workspace_id, entity_id)
            if item.revision != request.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Item changed; refresh dependencies")
            if item.active_attempt_id or item.status in {"RUNNING", "VERIFYING"}:
                raise WorkbenchError("ACTIVE_CONTRACT", "Dependencies are frozen during active execution")
            for blocker_id in request.add + request.remove:
                self.store.get("work_item", workspace_id, blocker_id)
            for blocker_id in request.remove:
                if not self.store.db.execute(
                    "DELETE FROM wb_dependencies WHERE workspace_id=? AND blocker_id=? AND dependent_id=?",
                    (workspace_id, blocker_id, item.id),
                ).rowcount:
                    raise WorkbenchError("UNKNOWN_EDGE", "Dependency does not exist")
            for blocker_id in request.add:
                self.store.db.execute(
                    "INSERT INTO wb_dependencies VALUES(?,?,?,'BLOCKS',?,?)",
                    (workspace_id, blocker_id, item.id, now(), principal.id),
                )
            ids = [
                row[0]
                for row in self.store.db.execute("SELECT id FROM wb_work_items WHERE workspace_id=?", (workspace_id,))
            ]
            edges = [
                tuple(row)
                for row in self.store.db.execute(
                    "SELECT blocker_id,dependent_id FROM wb_dependencies WHERE workspace_id=?", (workspace_id,)
                )
            ]
            try:
                validate_workbench_graph(ids, edges)
            except ValueError as exc:
                raise WorkbenchError("INVALID_GRAPH", str(exc), 422) from exc
            fields = {"contract_revision": item.contract_revision + 1, "assigned_agent_id": None}
            if item.status in {"READY", "ASSIGNED", "BLOCKED", "WAITING_FOR_HUMAN"}:
                fields["status"], fields["reason_code"] = self.readiness(item)
                fields["resume_state"] = "READY" if fields["status"] == "BLOCKED" else None
            self.invalidate(item)
            result = self.changed_item(item, fields, principal, "dependencies_changed")
            return CommandResult(200, result.model_dump(mode="json"))

        return self.command(
            principal,
            workspace_id,
            "work.write",
            request.command_id,
            "dependencies",
            {"entity_id": entity_id, **request.model_dump(mode="json")},
            apply,
        )

    def archive(self, kind, workspace_id, entity_id, request, principal):
        from .schema import Mutation

        request = Mutation.model_validate(request)
        if kind not in {"workspace", "project", "work_item", "comment"}:
            raise WorkbenchError("COMMAND_UNAVAILABLE", "Archive is unavailable for this entity", 422)

        def apply():
            previous = self.store.get(kind, workspace_id, entity_id)
            if previous.revision != request.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Entity changed; refresh before archive")
            if kind == "work_item" and previous.active_attempt_id:
                raise WorkbenchError("ACTIVE_CONTRACT", "Active item cannot be archived")
            if kind == "work_item":
                self.invalidate(previous)
            field = "deleted_at" if kind == "comment" else "archived_at"
            if getattr(previous, field) is not None:
                raise WorkbenchError("ARCHIVED", "Entity is already archived")
            entity = type(previous).model_validate(
                {
                    **previous.model_dump(),
                    field: now(),
                    "revision": previous.revision + 1,
                    "updated_at": now(),
                    "updated_by": principal.id,
                }
            )
            entity = self.persist(kind, entity, principal, "archived", previous=previous, tombstone=True)
            if kind in {"workspace", "project"}:
                self.invalidate_policy_items(kind, entity, principal)
            return CommandResult(200, entity.model_dump(mode="json"))

        return self.command(
            principal,
            workspace_id,
            WRITE[kind],
            request.command_id,
            "archive_" + kind,
            {"entity_id": entity_id, **request.model_dump(mode="json")},
            apply,
            local_only=kind == "workspace",
        )

    def allowed_actions(self, item, principal):
        authorize(principal, item.workspace_id, "workspace.read")
        if item.archived_at is not None:
            return []
        if item.active_attempt_id:
            return ["cancel"] if "work.write" in principal.capabilities else []
        actions = {
            "BACKLOG": ["ready", "block", "cancel"]
            + (
                ["manual_submit"]
                if self.execution and item.execution_kind == "MANUAL" and principal.actor_type == "HUMAN"
                else []
            ),
            "READY": ["block", "cancel"],
            "ASSIGNED": ["unassign", "block", "cancel"],
            "BLOCKED": ["unblock", "retry", "cancel"],
            "FAILED": ["retry", "cancel"],
            "DONE": ["reopen"],
            "CANCELLED": ["reopen"],
            "REVIEW": ["request_review", "cancel"],
            "WAITING_FOR_HUMAN": ["cancel"],
        }
        result = actions.get(item.status, []) if "work.write" in principal.capabilities else []
        if (
            self.execution
            and "work.write" in principal.capabilities
            and item.execution_kind == "MANUAL"
            and item.status in {"READY", "ASSIGNED"}
            and principal.actor_type == "HUMAN"
        ):
            result += ["manual_submit"]
        if (
            item.status in {"REVIEW", "WAITING_FOR_HUMAN"}
            and principal.actor_type == "HUMAN"
            and "review.approve" in principal.capabilities
        ):
            try:
                self.evidence(item)
                result += ["approve_completion", "reject_completion"]
            except WorkbenchError:
                pass
        if (
            item.execution_kind == "ENGINE"
            and item.status in {"ASSIGNED", "WAITING_FOR_HUMAN"}
            and not item.active_attempt_id
        ):
            if "execution.approve" in principal.capabilities and principal.actor_type == ActorType.HUMAN:
                result += ["approve_execution"]
            if "execution.request" in principal.capabilities:
                result += ["request_execution"]
        return result

    def prd_template(self, fields):
        from .templates import get, seed

        seed(self.store.db)
        template_id = fields.get("template_id") or "prd-default"
        version = fields.get("template_version") or 1
        template = get(self.store.db, template_id, version)
        if template.kind != "prd":
            raise WorkbenchError("INVALID_TEMPLATE", "PRD requires a PRD template", 422)
        spec = fields.get("spec", {})
        if hasattr(spec, "model_dump"):
            spec = spec.model_dump(mode="json")
        template.document(spec)
        return {
            "template_id": template.id,
            "template_version": template.version,
            "template_hash": template.content_hash,
        }

    def prd_command(self, workspace_id, entity_id, request, principal):
        from .templates import get

        request = PRDCommand.model_validate(request)
        capability = "review.approve" if request.command == "approve" else "prd.write"
        if request.command == "approve" and (
            not isinstance(principal, Principal) or principal.actor_type != ActorType.HUMAN
        ):
            raise WorkbenchError("HUMAN_REQUIRED", "PRD approval requires an observed human action", 403)

        def apply():
            previous = self.store.get("prd", workspace_id, entity_id)
            if previous.revision != request.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "PRD changed; refresh before applying command")
            transitions = {
                "submit_review": ({"DRAFT"}, "IN_REVIEW"),
                "approve": ({"IN_REVIEW"}, "APPROVED"),
                "archive": ({"DRAFT", "IN_REVIEW", "APPROVED"}, "ARCHIVED"),
            }
            source, target = transitions[request.command]
            if previous.status not in source:
                raise WorkbenchError("INVALID_STATE", "PRD command is unavailable in its current state")
            if target in {"IN_REVIEW", "APPROVED"}:
                previous.spec.require_complete()
                template = get(self.store.db, previous.template_id, previous.template_version)
                if template.content_hash != previous.template_hash:
                    raise WorkbenchError("CORRUPT_DATA", "PRD template hash mismatch", 500)
                template.document(previous.spec.model_dump(mode="json"), complete=True)
            entity = type(previous).model_validate(
                {
                    **previous.model_dump(),
                    "status": target,
                    "revision": previous.revision + 1,
                    "updated_at": now(),
                    "updated_by": principal.id,
                }
            )
            result = self.persist("prd", entity, principal, request.command, previous=previous)
            return CommandResult(200, result.model_dump(mode="json"))

        return self.command(
            principal,
            workspace_id,
            capability,
            request.command_id,
            "prd_" + request.command,
            {"entity_id": entity_id, **request.model_dump(mode="json")},
            apply,
        )

    def import_template(self, raw, principal):
        from .templates import load_template, register

        authorize(principal, "local", "policy.admin", local_only=True)
        template = load_template(raw)
        with self.store.router.transaction():
            register(self.store.db, template, "local-import")
        return template

    def repositories(self):
        return {
            hashlib.sha256(str(Path(repo.path).resolve()).encode()).hexdigest(): repo
            for repo in self.settings.repositories
        }

    def repository(self, reference: str):
        repo = self.repositories().get(reference)
        if repo is None:
            raise WorkbenchError("REPOSITORY_UNREGISTERED", "Repository reference is not registered", 422)
        return self.settings.repository(repo.path)

    def seed_defaults(self, seed_templates=None):
        """Idempotent local seed, without projects, agents or network opt-in."""
        if seed_templates is None:
            from .templates import seed

            seed_templates = seed
        workspace_id = "00000000-0000-4000-8000-000000000001"
        with self.store.router.transaction():
            if not self.store.db.execute("SELECT 1 FROM wb_workspaces WHERE id=?", (workspace_id,)).fetchone():
                stamp = now()
                self.persist(
                    "workspace",
                    Workspace(
                        id=workspace_id,
                        name="Personal",
                        revision=1,
                        created_at=stamp,
                        updated_at=stamp,
                        created_by="system",
                        updated_by="system",
                    ),
                    Principal("system", ActorType.SYSTEM, CAPABILITIES, frozenset({"*"}), True),
                    "seeded",
                )
            if seed_templates:
                seed_templates(self.store.db)
        return self.store.get("workspace", workspace_id, workspace_id)
