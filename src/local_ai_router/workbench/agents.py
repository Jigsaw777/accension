"""Operator definitions and real daemon-owned built-in Engine instances."""

import asyncio
import json
import os

from .events import append_event
from .policy import authorize
from .schema import AgentCommand, AgentDefinition, AgentProjection
from .service import CommandResult, payload_hash
from .store import WorkbenchError, WorkbenchStore, identity, now


class Agents:
    def __init__(self, runtime):
        self.runtime, self.service = runtime, runtime.service
        self.store = self.service.store
        self.generation = identity()
        self.host_id = str(os.getpid()) + "-" + identity()
        self.instances = {}
        self.heartbeat_task = None

    def definition(self, agent_id):
        row = self.store.db.execute("SELECT * FROM wb_agent_definitions WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            raise WorkbenchError("NOT_FOUND", "Agent not found", 404)
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        return WorkbenchStore._decode(AgentDefinition, data)

    def configure(self, request, principal, agent_id=None):
        body = AgentCommand.model_validate(request)
        authorize(principal, "local", "agent.admin", local_only=True)
        for workspace_id in body.allowed_workspaces:
            authorize(principal, workspace_id, "agent.admin", local_only=True)
            self.store.get("workspace", workspace_id, workspace_id)
        for project_id in body.allowed_projects:
            row = self.store.db.execute("SELECT workspace_id FROM wb_projects WHERE id=?", (project_id,)).fetchone()
            if row is None or row[0] not in body.allowed_workspaces:
                raise WorkbenchError("INVALID_REFERENCE", "Agent project must belong to a granted workspace")
        for reference in body.repository_permissions:
            authorize(principal, "local", "repository.admin", local_only=True)
            self.service.repository(reference)
        if not set(body.tools) <= (self.service.settings.tool_registry.keys() | {"propose_file_changes"}):
            raise WorkbenchError("TOOL_UNAVAILABLE", "Agent tool grant must be registered", 422)
        for skill_id in body.skills:
            try:
                skill = self.service.skills.get(skill_id)
            except ValueError as exc:
                raise WorkbenchError("SKILL_UNTRUSTED", "Agent skill is unknown", 422) from exc
            if skill is None or not skill.enabled or not skill.trusted:
                raise WorkbenchError("SKILL_UNTRUSTED", "Agent skill must be enabled and trusted", 422)

        def apply():
            old = self.definition(agent_id) if agent_id else None
            if old:
                for ws in old.allowed_workspaces:
                    authorize(principal, ws, "agent.admin", local_only=True)
                if old.revision != body.expected_revision:
                    raise WorkbenchError("REVISION_CONFLICT", "Agent definition changed")
            elif body.expected_revision is not None:
                raise WorkbenchError("INVALID_INPUT", "Create agent without expected revision", 422)
            stamp = now()
            definition = AgentDefinition(
                **body.model_dump(exclude={"command_id", "expected_revision"}),
                id=agent_id or identity(),
                revision=old.revision + 1 if old else 1,
                created_at=old.created_at if old else stamp,
                updated_at=stamp,
                created_by=old.created_by if old else principal.id,
                updated_by=principal.id,
            )
            data = definition.model_dump()
            for key in (
                "capabilities",
                "tools",
                "skills",
                "repository_permissions",
                "allowed_workspaces",
                "allowed_projects",
                "supported_model_roles",
            ):
                data[key + "_json"] = json.dumps(data.pop(key))
            self.store.db.execute(
                f"INSERT INTO wb_agent_definitions({','.join(data)}) VALUES({','.join('?' for _ in data)}) ON CONFLICT(id) DO UPDATE SET "
                + ",".join(k + "=excluded." + k for k in data),
                tuple(data.values()),
            )
            for workspace_id in sorted(set(definition.allowed_workspaces) | set(old.allowed_workspaces if old else [])):
                projection = AgentProjection(**definition.model_dump(), workspace_id=workspace_id)
                append_event(
                    self.store,
                    "agent",
                    projection,
                    principal,
                    "agent_configured" if workspace_id in definition.allowed_workspaces else "agent_removed",
                    tombstone=workspace_id not in definition.allowed_workspaces,
                )
            return CommandResult(200 if old else 201, definition.model_dump(mode="json"))

        result = self.service.command(
            principal,
            "local",
            "agent.admin",
            body.command_id,
            "agent_configure",
            {"agent_id": agent_id, **body.model_dump(mode="json")},
            apply,
            local_only=True,
        )
        if self.runtime.started:
            self.sync_instances()
        return result

    def sync_instances(self):
        with self.store.router.transaction():
            ids = {row[0] for row in self.store.db.execute("SELECT id FROM wb_agent_definitions WHERE enabled=1")}
            for agent_id in ids - self.instances.keys():
                instance_id, stamp = identity(), now()
                self.store.db.execute(
                    "INSERT INTO wb_agent_instances VALUES(?,?,?,?,'AVAILABLE',?,?,?)",
                    (instance_id, agent_id, self.generation, self.host_id, stamp, stamp, stamp + 10_000),
                )
                self.instances[agent_id] = instance_id
            for agent_id in self.instances.keys() - ids:
                self.store.db.execute(
                    "UPDATE wb_agent_instances SET state='DISABLED',expires_at=? WHERE id=?",
                    (now(), self.instances.pop(agent_id)),
                )

    def heartbeat(self):
        if not self.runtime.started:
            return
        with self.store.router.transaction():
            stamp = now()
            for agent_id, instance_id in self.instances.items():
                definition = self.definition(agent_id)
                used = self.store.db.execute(
                    "SELECT COUNT(*) FROM wb_assignments WHERE instance_id=? AND status IN ('RESERVED','ACTIVE')",
                    (instance_id,),
                ).fetchone()[0]
                self.store.db.execute(
                    "UPDATE wb_agent_instances SET state=?,last_heartbeat=?,expires_at=? WHERE id=? AND owner_generation=?",
                    (
                        "BUSY" if used >= definition.concurrency_limit else "AVAILABLE",
                        stamp,
                        stamp + 10_000,
                        instance_id,
                        self.generation,
                    ),
                )

    async def heartbeat_loop(self):
        while self.runtime.started:
            self.heartbeat()
            await asyncio.sleep(1)

    async def start(self):
        self.sync_instances()
        self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())

    async def close(self):
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            await asyncio.gather(self.heartbeat_task, return_exceptions=True)
        with self.store.router.transaction():
            self.store.db.execute(
                "UPDATE wb_agent_instances SET state='OFFLINE',expires_at=? WHERE owner_generation=?",
                (now(), self.generation),
            )
        self.instances.clear()

    def list(self, workspace_id, principal):
        authorize(principal, workspace_id, "workspace.read")
        result = []
        with self.store.router.lock:
            for row in self.store.db.execute("SELECT id FROM wb_agent_definitions ORDER BY id LIMIT 200").fetchall():
                definition = self.definition(row[0])
                if workspace_id not in definition.allowed_workspaces:
                    continue
                instances = [
                    dict(r)
                    for r in self.store.db.execute(
                        "SELECT * FROM wb_agent_instances WHERE definition_id=? ORDER BY started_at DESC LIMIT 20",
                        (definition.id,),
                    )
                ]
                for instance in instances:
                    if instance["expires_at"] <= now():
                        instance["state"] = "OFFLINE" if definition.enabled else "DISABLED"
                live = next(
                    (i for i in instances if i["owner_generation"] == self.generation and i["expires_at"] > now()), None
                )
                state = "DISABLED" if not definition.enabled else live["state"] if live else "OFFLINE"
                result.append(
                    {
                        **definition.model_dump(mode="json"),
                        "state": state,
                        "instances": instances,
                        "provider_eligibility": "checked per work item",
                    }
                )
        return result

    def policy_hash(self, item):
        policy = self.service.item_policy(item)
        workspace = self.store.get("workspace", item.workspace_id, item.workspace_id)
        project = self.store.get("project", item.workspace_id, item.project_id)
        return payload_hash(
            {
                "item_contract_revision": item.contract_revision,
                "spec": item.spec.model_dump(mode="json"),
                "execution_kind": item.execution_kind,
                "workspace": workspace.model_dump(
                    mode="json", exclude={"revision", "updated_at", "updated_by", "name", "description"}
                ),
                "project": project.model_dump(
                    mode="json",
                    exclude={"revision", "updated_at", "updated_by", "name", "description", "default_priority"},
                ),
                "privacy": policy.privacy,
                "quality": policy.quality,
                "review": policy.review,
                "budget": policy.budget_microusd,
            }
        )

    def preview(self, workspace_id, item_id, principal):
        from ..contracts import contract_scope
        from ..routing import deterministic
        from .policy import remaining_budget

        authorize(principal, workspace_id, "workspace.read")
        with self.store.router.lock:
            item = self.store.get("work_item", workspace_id, item_id)
            project = self.store.get("project", workspace_id, item.project_id)
            policy = self.service.item_policy(item)
            budget = remaining_budget(self.store, item, policy.budget_microusd)
            eligibility, rejected = [], []
            base = []
            if item.execution_kind != "ENGINE" or item.status not in {"READY", "ASSIGNED", "WAITING_FOR_HUMAN"}:
                base.append("ITEM_NOT_READY")
            try:
                self.service.readiness(item)
            except WorkbenchError as exc:
                base.append(exc.code)
            if budget["remaining_microusd"] <= 0:
                base.append("BUDGET_EXHAUSTED")
            for row in self.list(workspace_id, principal):
                reasons = list(base)
                definition = self.definition(row["id"])
                if not definition.enabled:
                    reasons.append("AGENT_DISABLED")
                elif row["state"] not in {"AVAILABLE", "BUSY"}:
                    reasons.append("AGENT_OFFLINE")
                if definition.allowed_projects and item.project_id not in definition.allowed_projects:
                    reasons.append("PROJECT_DENIED")
                if project.repository_ref not in definition.repository_permissions:
                    reasons.append("REPOSITORY_DENIED")
                if not set(item.spec.required_capabilities) <= set(definition.capabilities):
                    reasons.append("CAPABILITY_UNAVAILABLE")
                if not set(item.spec.required_tools) <= set(definition.tools):
                    reasons.append("TOOL_UNAVAILABLE")
                if not set(item.spec.required_skills) <= set(definition.skills):
                    reasons.append("SKILL_UNTRUSTED")
                roles = {"executor", "repairer"} | ({"reviewer"} if policy.review == "AI_REVIEW" else set())
                if not roles <= set(definition.supported_model_roles):
                    reasons.append("ROLE_DENIED")
                instance = next(
                    (
                        i
                        for i in row["instances"]
                        if i["owner_generation"] == self.generation and i["expires_at"] > now()
                    ),
                    None,
                )
                used = self.store.db.execute(
                    "SELECT COUNT(*) FROM wb_assignments WHERE agent_definition_id=? AND status IN ('RESERVED','ACTIVE') AND item_id<>?",
                    (definition.id, item.id),
                ).fetchone()[0]
                if used >= definition.concurrency_limit:
                    reasons.append("NO_CAPACITY")
                models, model_decision = [], {}
                if not reasons:
                    from ..contracts import strictest

                    with contract_scope(
                        mode=strictest(policy.privacy, definition.privacy_policy), quality=policy.quality
                    ):
                        models, model_decision = self.runtime.engine.router.roles.select(
                            deterministic(item.spec.objective),
                            "executor",
                            item.spec.required_capabilities or ["code"],
                            allow_cloud=policy.privacy != "LOCAL_ONLY",
                            inputs=len(item.model_dump_json().encode()) + 2000,
                            budget=budget["remaining_microusd"] / 1_000_000,
                        )
                    if not models:
                        reasons.append("NO_ELIGIBLE_MODEL")
                candidate = {
                    "agent_id": definition.id,
                    "instance_id": instance["id"] if instance else None,
                    "candidate_model_id": next(
                        (m.id for m in models if m.id == item.preferred_model_id), models[0].id if models else None
                    ),
                    "load": used,
                    "role": definition.role,
                    "models": model_decision,
                    "reasons": list(dict.fromkeys(reasons)),
                }
                (rejected if reasons else eligibility).append(candidate)
            eligibility.sort(key=lambda row: (row["role"] != item.preferred_agent_role, row["load"], row["agent_id"]))
            return {
                "item_id": item.id,
                "item_revision": item.revision,
                "policy_revision_hash": self.policy_hash(item),
                "privacy": policy.privacy,
                "quality": policy.quality,
                "review": policy.review,
                "budget": budget,
                "eligible": eligibility,
                "rejected": rejected,
                "selected": eligibility[0]["agent_id"] if eligibility else None,
                "inference": False,
            }

    def assign(self, workspace_id, item_id, request, principal):
        from .schema import AssignmentCommand

        body = AssignmentCommand.model_validate(request)
        authorize(principal, workspace_id, "assignment.write")

        def apply():
            item = self.store.get("work_item", workspace_id, item_id)
            if item.revision != body.expected_revision or item.status != "READY":
                raise WorkbenchError("REVISION_CONFLICT", "Assignment requires current READY item")
            decision = self.preview(workspace_id, item_id, principal)
            if decision["policy_revision_hash"] != body.policy_revision_hash:
                raise WorkbenchError("POLICY_CHANGED", "Assignment policy changed after preview")
            selected = next((r for r in decision["eligible"] if r["agent_id"] == body.agent_id), None)
            if selected is None:
                raise WorkbenchError("AGENT_INELIGIBLE", "Selected agent is not currently eligible", 422)
            assignment_id = identity()
            self.store.db.execute(
                "INSERT INTO wb_assignments(id,workspace_id,item_id,item_contract_revision,agent_definition_id,instance_id,status,candidate_model_id,decision_json,created_at) VALUES(?,?,?,?,?,?,'RESERVED',?,?,?)",
                (
                    assignment_id,
                    workspace_id,
                    item.id,
                    item.contract_revision,
                    body.agent_id,
                    selected["instance_id"],
                    selected["candidate_model_id"],
                    json.dumps(decision),
                    now(),
                ),
            )
            changed = self.service.changed_item(
                item, {"status": "ASSIGNED", "assigned_agent_id": body.agent_id}, principal, "assigned"
            )
            return CommandResult(
                200, {"assignment_id": assignment_id, "item": changed.model_dump(mode="json"), "decision": decision}
            )

        return self.service.command(
            principal,
            workspace_id,
            "assignment.write",
            body.command_id,
            "assign",
            {"item_id": item_id, **body.model_dump(mode="json")},
            apply,
        )

    def execution_preflight(self, workspace_id, item_id, principal):
        item = self.service.get("work_item", workspace_id, item_id, principal)
        assignment = self.store.db.execute(
            "SELECT * FROM wb_assignments WHERE workspace_id=? AND item_id=? AND status='RESERVED'",
            (workspace_id, item_id),
        ).fetchone()
        if assignment is None:
            raise WorkbenchError("ASSIGNMENT_REQUIRED", "Assign a currently eligible worker before execution")
        definition = self.definition(assignment["agent_definition_id"])
        contract = {
            "item_id": item.id,
            "contract_revision": item.contract_revision,
            "spec": item.spec.model_dump(mode="json"),
            "assignment_id": assignment["id"],
            "agent_definition_revision": definition.revision,
            "policy_revision_hash": self.policy_hash(item),
        }
        return {
            "contract": contract,
            "contract_hash": payload_hash(contract),
            "policy_revision_hash": contract["policy_revision_hash"],
        }

    def approve_execution(self, workspace_id, item_id, request, principal):
        from .schema import ActorType, ExecutionApproval

        body = ExecutionApproval.model_validate(request)
        authorize(principal, workspace_id, "execution.approve")
        if principal.actor_type != ActorType.HUMAN:
            raise WorkbenchError("HUMAN_REQUIRED", "Execution approval requires a human action", 403)

        def apply():
            item = self.store.get("work_item", workspace_id, item_id)
            if (
                item.revision != body.expected_revision
                or item.contract_revision != body.contract_revision
                or item.status not in {"ASSIGNED", "WAITING_FOR_HUMAN"}
            ):
                raise WorkbenchError("REVISION_CONFLICT", "Execution approval source changed")
            preflight = self.execution_preflight(workspace_id, item_id, principal)
            if (
                body.contract_hash != preflight["contract_hash"]
                or body.policy_revision_hash != preflight["policy_revision_hash"]
            ):
                raise WorkbenchError("CONTRACT_CHANGED", "Execution contract/policy changed")
            grant_id = identity()
            self.store.db.execute(
                "INSERT INTO wb_execution_grants(id,workspace_id,item_id,contract_revision,contract_hash,principal_id,policy_revision_hash,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    grant_id,
                    workspace_id,
                    item_id,
                    item.contract_revision,
                    body.contract_hash,
                    principal.id,
                    body.policy_revision_hash,
                    now() + 900_000,
                ),
            )
            changed = self.service.changed_item(
                item, {"status": "ASSIGNED", "reason_code": None}, principal, "execution_approved"
            )
            return CommandResult(200, {"grant_id": grant_id, "item": changed.model_dump(mode="json")})

        return self.service.command(
            principal,
            workspace_id,
            "execution.approve",
            body.command_id,
            "approve_execution",
            {"item_id": item_id, **body.model_dump(mode="json")},
            apply,
        )
