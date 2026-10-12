"""Fresh approved item attempts compiled through the existing Engine safety boundary."""

import asyncio
import hashlib
import json
from pathlib import Path

from ..context import ContextGraph
from ..contracts import ExecutionAborted, contract_scope, execution_scope, strictest
from ..privacy import repository_scope
from ..safety import digest, redact, repo_lock, safe_path
from ..schema import ExecutionPlan, TaskNode
from .policy import remaining_budget
from .schema import BasicCommand, WorkExecutionContract
from .service import CommandResult, payload_hash
from .store import WorkbenchError, identity, now


class EngineAgentAdapter:
    def __init__(self, runtime):
        self.runtime, self.service, self.engine = runtime, runtime.service, runtime.engine
        self.store = self.service.store

    def request(self, workspace_id, item_id, request, principal, *, _delivery_batch=None):
        body = BasicCommand.model_validate(request)
        if body.command != "request_execution":
            raise WorkbenchError("INVALID_INPUT", "Expected request_execution", 422)

        def apply():
            item = self.store.get("work_item", workspace_id, item_id)
            if item.revision != body.expected_revision or item.status != "ASSIGNED" or item.active_attempt_id:
                raise WorkbenchError("REVISION_CONFLICT", "Execution requires current assigned item")
            self.service.readiness(item)
            decision = self.runtime.agents.preview(workspace_id, item_id, principal)
            selected = next((a for a in decision["eligible"] if a["agent_id"] == item.assigned_agent_id), None)
            if selected is None:
                raise WorkbenchError("AGENT_INELIGIBLE", "Assigned worker is no longer eligible", 422)
            preflight = self.runtime.agents.execution_preflight(workspace_id, item_id, principal)
            workspace = self.store.get("workspace", workspace_id, workspace_id)
            delivery_grant = None
            if _delivery_batch is not None:
                if not self.store.db.in_transaction or principal.actor_type != "SYSTEM":
                    raise WorkbenchError("INVALID_GRANT", "Delivery intent requires the coordinator transaction", 403)
                _, _, delivery_grant, approved_agents = self.runtime.delivery.authorization(_delivery_batch, item_id)
                definition = self.runtime.agents.definition(item.assigned_agent_id)
                if approved_agents.get(definition.id) != definition.revision:
                    raise WorkbenchError("AGENT_INELIGIBLE", "Delivery agent revision changed", 422)
            grant = self.store.db.execute(
                "SELECT * FROM wb_execution_grants WHERE workspace_id=? AND item_id=? AND revoked_at IS NULL AND consumed_attempt_id IS NULL AND expires_at>? AND contract_revision=? AND contract_hash=? AND policy_revision_hash=? ORDER BY expires_at DESC LIMIT 1",
                (
                    workspace_id,
                    item_id,
                    now(),
                    item.contract_revision,
                    preflight["contract_hash"],
                    preflight["policy_revision_hash"],
                ),
            ).fetchone()
            if workspace.execution_policy.require_execution_approval and grant is None and delivery_grant is None:
                waiting = self.service.changed_item(
                    item,
                    {"status": "WAITING_FOR_HUMAN", "reason_code": "EXECUTION_APPROVAL_REQUIRED"},
                    principal,
                    "execution_waiting",
                )
                return CommandResult(202, {"item": waiting.model_dump(mode="json"), "approval_required": True})
            ordinal = self.store.db.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM wb_attempts WHERE item_id=?", (item_id,)
            ).fetchone()[0]
            if ordinal > item.max_attempts:
                raise WorkbenchError("ATTEMPT_LIMIT", "Work item dispatch attempt limit reached", 422)
            assignment = self.store.db.execute(
                "SELECT * FROM wb_assignments WHERE workspace_id=? AND item_id=? AND status='RESERVED'",
                (workspace_id, item_id),
            ).fetchone()
            definition = self.runtime.agents.definition(assignment["agent_definition_id"])
            project = self.store.get("project", workspace_id, item.project_id)
            policy = self.service.item_policy(item)
            remaining = remaining_budget(self.store, item, policy.budget_microusd)["remaining_microusd"]
            if not remaining:
                raise WorkbenchError("BUDGET_EXHAUSTED", "Lifetime work item allowance exhausted", 422)
            attempt_id, plan_id, request_id = identity(), identity().replace("-", ""), identity().replace("-", "")
            skills = self.engine.skills.freeze(item.spec.required_skills)
            dependencies = []
            for row in self.store.db.execute(
                "SELECT blocker_id FROM wb_dependencies WHERE workspace_id=? AND dependent_id=?",
                (workspace_id, item.id),
            ).fetchall():
                dependency = self.store.get("work_item", workspace_id, row[0])
                dependencies.append(
                    {
                        "item_id": dependency.id,
                        "contract_revision": dependency.contract_revision,
                        "summary": dependency.spec.objective[:2000],
                        "privacy": self.service.item_policy(dependency).privacy,
                    }
                )
            contract = WorkExecutionContract(
                workspace_id=workspace_id,
                project_id=item.project_id,
                item_id=item.id,
                attempt_id=attempt_id,
                plan_id=plan_id,
                request_id=request_id,
                item_contract_revision=item.contract_revision,
                assignment_id=assignment["id"],
                agent_definition_id=definition.id,
                agent_definition_revision=definition.revision,
                agent_name=definition.name,
                policy_revision_hash=preflight["policy_revision_hash"],
                repository_ref=project.repository_ref,
                title=item.title,
                spec=item.spec.model_copy(deep=True),
                privacy=strictest(policy.privacy, definition.privacy_policy),
                budget_microusd=remaining,
                engine_max_attempts=item.engine_max_attempts,
                quality=policy.quality,
                review_policy=policy.review,
                model_roles=definition.supported_model_roles,
                model_bindings=definition.model_bindings.model_copy(deep=True),
                tools=definition.tools,
                skill_hashes={s.id: s.content_hash for s in skills.skills},
                grant_id=grant["id"] if grant else None,
                risk=item.risk,
                complexity=item.complexity,
                dependency_evidence=dependencies,
                batch_id=_delivery_batch,
            )
            self.store.db.execute(
                "INSERT INTO wb_attempts(id,workspace_id,item_id,ordinal,assignment_id,contract_revision,contract_hash,contract_json,state,request_id,fence,queued_at) VALUES(?,?,?,?,?,?,?,?,'QUEUED',?,1,?)",
                (
                    attempt_id,
                    workspace_id,
                    item.id,
                    ordinal,
                    assignment["id"],
                    item.contract_revision,
                    payload_hash(contract.model_dump(mode="json")),
                    contract.model_dump_json(),
                    request_id,
                    now(),
                ),
            )
            self.store.db.execute(
                "INSERT INTO wb_budget_reservations VALUES(?,?,?,'RESERVED',?,NULL)",
                (attempt_id, item.id, remaining, now()),
            )
            if _delivery_batch is not None:
                batch = self.store.db.execute(
                    "SELECT max_cost_microusd FROM wb_delivery_batches WHERE id=?", (_delivery_batch,)
                ).fetchone()
                charged = self.store.db.execute(
                    "SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_microusd ELSE reserved_microusd END),0) FROM wb_delivery_reservations WHERE batch_id=?",
                    (_delivery_batch,),
                ).fetchone()[0]
                if charged + remaining > batch[0]:
                    raise WorkbenchError("BATCH_BUDGET_EXHAUSTED", "Delivery reservation exceeds approved cap", 422)
                self.store.db.execute("UPDATE wb_attempts SET batch_id=? WHERE id=?", (_delivery_batch, attempt_id))
                self.store.db.execute(
                    "INSERT INTO wb_delivery_reservations VALUES(?,?,?,NULL,'reserved')",
                    (attempt_id, _delivery_batch, remaining),
                )
                self.store.db.execute(
                    "UPDATE wb_delivery_members SET member_state='queued',last_attempt_id=? WHERE batch_id=? AND item_id=?",
                    (attempt_id, _delivery_batch, item_id),
                )
            if grant:
                self.store.db.execute(
                    "UPDATE wb_execution_grants SET consumed_attempt_id=? WHERE id=? AND consumed_attempt_id IS NULL",
                    (attempt_id, grant["id"]),
                )
            changed = self.service.changed_item(item, {"active_attempt_id": attempt_id}, principal, "execution_queued")
            return CommandResult(202, {"attempt_id": attempt_id, "item": changed.model_dump(mode="json")})

        if _delivery_batch is not None:
            return apply()
        return self.service.command(
            principal,
            workspace_id,
            "execution.request",
            body.command_id,
            "request_execution",
            {"item_id": item_id, **body.model_dump(mode="json")},
            apply,
        )

    def contract(self, attempt_id):
        row = self.store.db.execute("SELECT * FROM wb_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise WorkbenchError("NOT_FOUND", "Attempt not found", 404)
        try:
            contract = WorkExecutionContract.model_validate_json(row["contract_json"])
            if (
                payload_hash(json.loads(row["contract_json"])) != row["contract_hash"]
                or contract.attempt_id != row["id"]
            ):
                raise ValueError()
            return row, contract
        except ValueError as exc:
            raise WorkbenchError("CORRUPT_DATA", "Attempt contract integrity failed", 500) from exc

    def read_attempt(self, workspace_id, attempt_id, principal, section=None):
        from .policy import authorize

        authorize(
            principal,
            workspace_id,
            "artifact.read" if section in {"receipt", "verification", "artifacts"} else "workspace.read",
        )
        with self.store.router.lock:
            row, contract = self.contract(attempt_id)
            if contract.workspace_id != workspace_id:
                raise WorkbenchError("NOT_FOUND", "Attempt not found", 404)
            if section == "receipt":
                snapshot = self.store.db.execute(
                    "SELECT receipt_hash,receipt_json,source_run_id FROM wb_receipt_snapshots WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                if snapshot is None:
                    raise WorkbenchError("RECEIPT_UNAVAILABLE", "Receipt not finalized", 404)
                data = json.loads(snapshot["receipt_json"])
                if payload_hash(data) != snapshot["receipt_hash"] or data.get("run_id") != contract.plan_id:
                    raise WorkbenchError("CORRUPT_DATA", "Receipt snapshot integrity failed", 500)
                return {
                    "attempt_id": attempt_id,
                    "source_run_id": snapshot["source_run_id"],
                    "receipt_hash": snapshot["receipt_hash"],
                    "receipt": redact(data),
                }
            if section == "verification":
                rows = self.store.db.execute(
                    "SELECT * FROM wb_verifications WHERE workspace_id=? AND attempt_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100",
                    (workspace_id, attempt_id),
                ).fetchall()
                return {"items": [{**dict(r), "checks_json": redact(json.loads(r["checks_json"]))} for r in rows]}
            if section == "artifacts":
                return {
                    "items": [
                        dict(r)
                        for r in self.store.db.execute(
                            "SELECT * FROM wb_artifacts WHERE workspace_id=? AND attempt_id=? ORDER BY id LIMIT 100",
                            (workspace_id, attempt_id),
                        )
                    ]
                }
            result = dict(row)
            result.pop("contract_json")
            result["contract"] = redact(contract.model_dump(mode="json"))
            result["cost"] = self.engine.store.costs(contract.request_id)
            result["models"] = [
                r[0]
                for r in self.store.db.execute(
                    "SELECT DISTINCT model FROM calls WHERE request=? ORDER BY model LIMIT 100", (contract.request_id,)
                )
            ]
            return result

    def attempts(self, workspace_id, item_id, principal, limit=50, before=None):
        self.service.get("work_item", workspace_id, item_id, principal)
        if not 1 <= limit <= 100 or before is not None and before < 1:
            raise WorkbenchError("INVALID_PAGE", "Invalid attempt page", 422)
        rows = self.store.db.execute(
            "SELECT id,ordinal,state,run_id,request_id,started_at,finished_at,error_code,cancel_requested_at FROM wb_attempts WHERE workspace_id=? AND item_id=? AND (? IS NULL OR ordinal<?) ORDER BY ordinal DESC LIMIT ?",
            (workspace_id, item_id, before, before, limit),
        ).fetchall()
        return {"items": [dict(r) for r in rows], "next_cursor": rows[-1]["ordinal"] if len(rows) == limit else None}

    def artifact_content(self, workspace_id, artifact_id, principal):
        from .policy import authorize

        authorize(principal, workspace_id, "artifact.read")
        row = self.store.db.execute(
            "SELECT * FROM wb_artifacts WHERE workspace_id=? AND id=?", (workspace_id, artifact_id)
        ).fetchone()
        if row is None or row["kind"] != "FILE":
            raise WorkbenchError("NOT_FOUND", "Artifact not found", 404)
        _, contract = self.contract(row["attempt_id"])
        path = row["relative_path_or_external_reference"]
        if path not in contract.spec.expected_artifacts:
            raise WorkbenchError("ARTIFACT_SCOPE", "Artifact outside approved scope", 403)
        root = Path(self.service.repository(contract.repository_ref).path)
        resolved = safe_path(root, path)
        if row["size_bytes"] > 256000 or resolved.stat().st_size > 256000:
            raise WorkbenchError("ARTIFACT_TOO_LARGE", "Artifact exceeds content limit", 413)
        raw = resolved.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["content_hash"]:
            raise WorkbenchError("ARTIFACT_CHANGED", "Artifact changed since verification")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkbenchError("ARTIFACT_MEDIA", "Only UTF-8 text artifacts can be displayed", 422) from exc
        return {
            "id": artifact_id,
            "media_type": "text/plain",
            "content": redact(content),
            "content_hash": row["content_hash"],
        }

    def guard(self, attempt_id, generation, fence):
        with self.store.router.lock:
            row, contract = self.contract(attempt_id)
            if (
                row["owner_generation"] != generation
                or row["fence"] != fence
                or row["state"] not in {"CLAIMED", "RUNNING", "VERIFYING"}
                or row["cancel_requested_at"]
                or (row["lease_until"] is not None and row["lease_until"] <= now())
            ):
                raise ExecutionAborted("Attempt cancelled, expired or fenced")
            item = self.store.get("work_item", contract.workspace_id, contract.item_id)
            definition = self.runtime.agents.definition(contract.agent_definition_id)
            if (
                item.active_attempt_id != attempt_id
                or item.contract_revision != contract.item_contract_revision
                or not definition.enabled
                or definition.revision != contract.agent_definition_revision
                or self.runtime.agents.policy_hash(item) != contract.policy_revision_hash
            ):
                raise ExecutionAborted("Execution policy or contract changed")
            if contract.grant_id:
                grant = self.store.db.execute(
                    "SELECT revoked_at,consumed_attempt_id FROM wb_execution_grants WHERE id=?", (contract.grant_id,)
                ).fetchone()
                if grant is None or grant[0] is not None or grant[1] != attempt_id:
                    raise ExecutionAborted("Execution grant revoked")
            if contract.batch_id:
                try:
                    self.runtime.delivery.authorization(contract.batch_id, contract.item_id, active_attempt=True)
                except WorkbenchError as exc:
                    raise ExecutionAborted("Delivery authorization changed") from exc

    async def bind(self, attempt_id, generation, fence):
        row, contract = self.contract(attempt_id)
        self.guard(attempt_id, generation, fence)
        repo = self.service.repository(contract.repository_ref)
        root = Path(repo.path).resolve(strict=True)
        with repository_scope(repo), repo_lock(root), contract_scope(mode=contract.privacy, quality=contract.quality):
            graph = ContextGraph(root, self.engine.store, repo)
            import asyncio

            await asyncio.to_thread(graph.build)
            self.guard(attempt_id, generation, fence)
            spec = contract.spec
            skills = self.engine.skills.freeze(spec.required_skills)
            if {s.id: s.content_hash for s in skills.skills} != contract.skill_hashes:
                raise ExecutionAborted("Skill content changed")
            node = TaskNode(
                id="work",
                jira_key=self.store.get("work_item", contract.workspace_id, contract.item_id).display_key,
                title=contract.title,
                objective=spec.objective,
                detailed_description=spec.description,
                rationale=spec.rationale,
                relevant_files=spec.relevant_files,
                expected_artifacts=spec.expected_artifacts,
                acceptance_criteria=spec.acceptance_criteria,
                verifier_requirements=spec.verifier_requirements + spec.definition_of_done,
                skills_required=spec.required_skills,
                tools_required=spec.required_tools,
                minimum_capabilities=spec.required_capabilities or ["code"],
                validation_commands=spec.validation_checks,
                max_cost=contract.budget_microusd / 1_000_000,
                max_attempts=contract.engine_max_attempts,
                quality_slo=contract.quality,
                privacy_class=contract.privacy,
                risk=contract.risk,
                complexity=contract.complexity,
            )
            plan = ExecutionPlan(
                plan_id=contract.plan_id,
                request_id=contract.request_id,
                goal=spec.objective,
                success_criteria=spec.acceptance_criteria + spec.definition_of_done,
                constraints=spec.constraints + [json.dumps({"dependency_evidence": contract.dependency_evidence})],
                allowed_budget=contract.budget_microusd / 1_000_000,
                tasks=[node],
                global_validation=spec.validation_checks,
                privacy_contract=contract.privacy,
                quality_contract=contract.quality,
                skill_contract=skills,
            )
            self.engine.validate_plan(plan, root, repo)
            self.engine.persist_plan(plan, root, graph.fingerprint, "workbench", graph.portable_fingerprint)
            with self.store.router.transaction():
                self.guard(attempt_id, generation, fence)
                self.store.db.execute(
                    "UPDATE wb_attempts SET run_id=? WHERE id=? AND owner_generation=? AND fence=? AND run_id IS NULL",
                    (plan.plan_id, attempt_id, generation, fence),
                )
            return plan

    async def execute(self, attempt_id, generation, fence):
        row, contract = self.contract(attempt_id)
        with execution_scope(
            lambda: self.guard(attempt_id, generation, fence),
            contract.model_roles,
            tools=contract.tools,
            required_review=contract.review_policy == "AI_REVIEW",
            model_bindings=contract.model_bindings.model_dump(mode="json"),
            attempt_context={
                "attempt_id": attempt_id,
                "contract_hash": row["contract_hash"],
                "expires_at": row["lease_until"] or now() + 30000,
                "fence": fence,
            },
        ):
            plan = await self.bind(attempt_id, generation, fence)
            self.guard(attempt_id, generation, fence)
            return await self.engine.execute_plan(
                plan.plan_id,
                self.service.repository(contract.repository_ref).path,
                observer=lambda signal: self.observe(attempt_id, generation, fence, signal),
            )

    def observe(self, attempt_id, generation, fence, signal):
        from .policy import CAPABILITIES, Principal
        from .schema import ActorType

        principal = Principal("engine", ActorType.SYSTEM, CAPABILITIES, frozenset({"*"}), True)
        with self.store.router.transaction():
            if signal.kind == "receipt_finalized" and signal.status != "complete":
                row, _ = self.contract(attempt_id)
                if row["owner_generation"] != generation or row["fence"] != fence:
                    raise ExecutionAborted("Receipt owner fenced")
            else:
                self.guard(attempt_id, generation, fence)
            row, contract = self.contract(attempt_id)
            item = self.store.get("work_item", contract.workspace_id, contract.item_id)
            from .activity import append_activity

            inserted = append_activity(
                self.store,
                source_key=f"signal:{attempt_id}:{signal.event_id}",
                workspace_id=contract.workspace_id,
                project_id=contract.project_id,
                item_id=contract.item_id,
                agent_definition_id=contract.agent_definition_id,
                attempt_id=attempt_id,
                occurred_at=now(),
                actor_type=ActorType.SYSTEM,
                actor_id="engine",
                event_type="engine_" + signal.kind,
                metadata={
                    "stage": signal.kind,
                    "model_id": signal.model_id,
                    "agent_name": self.store.db.execute(
                        "SELECT name FROM wb_agent_definitions WHERE id=?", (contract.agent_definition_id,)
                    ).fetchone()[0],
                    "agent_revision": contract.agent_definition_revision,
                },
            )
            if not inserted:
                return
            state = {"execution_start": "RUNNING", "worker_start": "RUNNING", "validation_start": "VERIFYING"}.get(
                signal.kind
            )
            if state:
                self.store.db.execute(
                    "UPDATE wb_attempts SET state=?,started_at=COALESCE(started_at,?) WHERE id=? AND owner_generation=? AND fence=?",
                    (state, now(), attempt_id, generation, fence),
                )
                if item.status != state:
                    self.service.changed_item(item, {"status": state}, principal, "engine_" + signal.kind)
            if signal.kind == "validation_end":
                checks = [{**check, "output": str(redact(check.get("output", "")))[-6000:]} for check in signal.checks]
                evidence = {"checks": checks, "task_id": signal.task_id, "model_id": signal.model_id}
                self.store.db.execute(
                    "INSERT INTO wb_verifications(id,workspace_id,item_id,attempt_id,kind,status,checks_json,artifacts_hash,evidence_hash,source_run_id,created_at,actor) VALUES(?,?,?,?,'ENGINE_VALIDATION',?,?,?,?,?,?,?)",
                    (
                        identity(),
                        contract.workspace_id,
                        item.id,
                        attempt_id,
                        "PASSED" if checks and all(c["passed"] for c in checks) else "FAILED",
                        json.dumps(evidence),
                        payload_hash([]),
                        payload_hash(evidence),
                        row["run_id"],
                        now(),
                        principal.id,
                    ),
                )
            if signal.kind == "receipt_finalized":
                self.capture_receipt(row, contract, item, principal, signal.status)
            elif signal.kind == "review_end":
                evidence = {
                    "model_id": signal.model_id,
                    "artifacts": list(signal.checks),
                    "contract_revision": contract.item_contract_revision,
                }
                self.store.db.execute(
                    "INSERT INTO wb_verifications(id,workspace_id,item_id,attempt_id,kind,status,checks_json,artifacts_hash,evidence_hash,source_run_id,created_at,actor) VALUES(?,?,?,?,'AI_REVIEW',?,?,?,?,?,?,?)",
                    (
                        identity(),
                        contract.workspace_id,
                        item.id,
                        attempt_id,
                        "ACCEPTED"
                        if signal.status == "accepted"
                        else "REJECTED"
                        if signal.status == "rejected"
                        else "MISSING",
                        json.dumps(evidence),
                        payload_hash(evidence["artifacts"]),
                        payload_hash(evidence),
                        contract.plan_id,
                        now(),
                        principal.id,
                    ),
                )
        self.service.wake.notify()

    def capture_receipt(self, row, contract, item, principal, status):
        from ..receipts import get

        try:
            receipt = get(self.engine, contract.plan_id)
        except (ValueError, TypeError) as exc:
            raise WorkbenchError("RECEIPT_UNAVAILABLE", "Execution receipt was not finalized", 500) from exc
        if (
            receipt.get("run_id") != contract.plan_id
            or receipt.get("request_id") != contract.request_id
            or receipt.get("status") != status
        ):
            raise WorkbenchError("RECEIPT_INVALID", "Receipt identity or outcome mismatch", 500)
        receipt = redact(receipt)
        self.store.db.execute(
            "INSERT INTO wb_receipt_snapshots VALUES(?,?,?,?,?)",
            (contract.attempt_id, contract.plan_id, payload_hash(receipt), json.dumps(receipt), now()),
        )
        artifacts = []
        root = Path(self.service.repository(contract.repository_ref).path)
        for artifact in receipt.get("files_changed", []):
            path = artifact["path"]
            if path not in contract.spec.expected_artifacts:
                raise WorkbenchError("ARTIFACT_SCOPE", "Receipt artifact exceeds approved scope", 500)
            resolved = safe_path(root, path)
            actual = digest(resolved) if resolved.exists() else None
            if status == "complete" and actual != artifact["after"]:
                raise WorkbenchError("ARTIFACT_CHANGED", "Changed artifact no longer matches receipt")
            if actual:
                artifacts.append({"path": path, "hash": actual})
                self.store.db.execute(
                    "INSERT INTO wb_artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity(),
                        contract.workspace_id,
                        item.id,
                        contract.attempt_id,
                        "FILE",
                        path,
                        actual,
                        "text/plain",
                        resolved.stat().st_size,
                        now(),
                    ),
                )
        costs = self.engine.store.costs(contract.request_id)
        self.store.db.execute(
            "UPDATE wb_budget_reservations SET state=?,settled_at=? WHERE attempt_id=?",
            ("UNCERTAIN" if costs["uncertain_calls"] else "SETTLED", now(), contract.attempt_id),
        )
        if status == "complete":
            final_validation = self.store.db.execute(
                "SELECT checks_json FROM wb_verifications WHERE attempt_id=? AND kind='ENGINE_VALIDATION' ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (contract.attempt_id,),
            ).fetchone()
            final_evidence = json.loads(final_validation[0]) if final_validation else {}
            checks = final_evidence.get("checks", []) if final_evidence.get("task_id") is None else []
            if not checks or not all(c.get("passed") for c in checks) or not artifacts:
                raise WorkbenchError("VERIFICATION_MISSING", "Receipt lacks independent validation or artifacts", 500)
            evidence = {
                "receipt_hash": payload_hash(receipt),
                "artifacts": artifacts,
                "contract_hash": row["contract_hash"],
                "checks": checks,
            }
            self.store.db.execute(
                "INSERT INTO wb_verifications(id,workspace_id,item_id,attempt_id,kind,status,checks_json,artifacts_hash,evidence_hash,source_run_id,created_at,actor) VALUES(?,?,?,?,'ENGINE_VALIDATION','VERIFIED',?,?,?,?,?,?)",
                (
                    identity(),
                    contract.workspace_id,
                    item.id,
                    contract.attempt_id,
                    json.dumps(evidence),
                    payload_hash(artifacts),
                    payload_hash(evidence),
                    contract.plan_id,
                    now(),
                    principal.id,
                ),
            )
            self.store.db.execute(
                "UPDATE wb_attempts SET state='SUCCEEDED',finished_at=? WHERE id=?", (now(), contract.attempt_id)
            )
            self.store.db.execute(
                "UPDATE wb_assignments SET status='RELEASED',ended_at=? WHERE id=?", (now(), contract.assignment_id)
            )
            self.service.changed_item(
                item, {"status": "REVIEW", "active_attempt_id": None}, principal, "verification_ready"
            )
            self.service.review_gate(self.store.get("work_item", contract.workspace_id, item.id), principal)
            from .metrics import reconcile_attempt

            reconcile_attempt(self.runtime, contract.attempt_id)

    def terminal_failure(self, attempt_id, generation, fence):
        from ..receipts import get
        from ..safety import Edits
        from .scheduler import SYSTEM

        with self.store.router.transaction():
            row, contract = self.contract(attempt_id)
            if row["owner_generation"] != generation or row["fence"] != fence:
                return False
            run = self.store.db.execute("SELECT status FROM runs WHERE id=?", (contract.plan_id,)).fetchone()
            if run is None or run[0] not in {"failed", "rolled_back", "discarded"}:
                return False
            try:
                receipt = get(self.engine, contract.plan_id)
                if receipt.get("status") != run[0]:
                    return False
                root = Path(self.service.repository(contract.repository_ref).path)
                edits = Edits.restore(root, contract.request_id)
                conflicts = [
                    name
                    for name, original in edits.originals.items()
                    if digest(safe_path(root, name))
                    != (
                        edits.written[name]
                        if run[0] == "discarded"
                        else hashlib.sha256(original).hexdigest()
                        if original is not None
                        else None
                    )
                ]
                if conflicts or receipt.get("rollback_conflicts"):
                    return False
            except (ValueError, OSError):
                return False
            status = "CANCELLED" if row["cancel_requested_at"] else "FAILED"
            item = self.store.get("work_item", contract.workspace_id, contract.item_id)
            if not self.store.db.execute(
                "SELECT 1 FROM wb_receipt_snapshots WHERE attempt_id=?", (attempt_id,)
            ).fetchone():
                self.capture_receipt(row, contract, item, SYSTEM, run[0])
            self.store.db.execute(
                "UPDATE wb_attempts SET state=?,finished_at=? WHERE id=?", (status, now(), attempt_id)
            )
            self.store.db.execute(
                "UPDATE wb_assignments SET status='RELEASED',ended_at=? WHERE id=?", (now(), contract.assignment_id)
            )
            costs = self.engine.store.costs(contract.request_id)
            self.store.db.execute(
                "UPDATE wb_budget_reservations SET state=?,settled_at=? WHERE attempt_id=?",
                ("UNCERTAIN" if costs["uncertain_calls"] else "SETTLED", now(), attempt_id),
            )
            self.service.changed_item(
                item, {"status": status, "active_attempt_id": None, "reason_code": None}, SYSTEM, "cleanup_acknowledged"
            )
            from .metrics import reconcile_attempt

            reconcile_attempt(self.runtime, attempt_id)
            return True

    def reconcile_terminal(self, attempt_id):
        row = self.store.db.execute("SELECT error_code FROM wb_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row and row[0] == "ConnectorUncertain":
            return False
        from .scheduler import SYSTEM

        row, contract = self.contract(attempt_id)
        repo = self.service.repository(contract.repository_ref)
        with repo_lock(Path(repo.path)):
            run = self.store.db.execute("SELECT status FROM runs WHERE id=?", (contract.plan_id,)).fetchone()
            if run is None:
                return False
            with self.store.router.transaction():
                row, contract = self.contract(attempt_id)
                if row["state"] != "RECONCILING":
                    return False
                if row["run_id"] is None:
                    self.store.db.execute("UPDATE wb_attempts SET run_id=? WHERE id=?", (contract.plan_id, attempt_id))
                item = self.store.get("work_item", contract.workspace_id, contract.item_id)
                if run[0] == "complete":
                    if (
                        item.contract_revision != contract.item_contract_revision
                        or self.runtime.agents.policy_hash(item) != contract.policy_revision_hash
                    ):
                        return False
                    if self.store.db.execute(
                        "SELECT 1 FROM wb_receipt_snapshots WHERE attempt_id=?", (attempt_id,)
                    ).fetchone():
                        return False
                    self.capture_receipt(row, contract, item, SYSTEM, "complete")
                    return True
            return self.terminal_failure(attempt_id, row["owner_generation"], row["fence"])

    def recovery_request(self, workspace_id, attempt_id, request, principal):
        from typing import Literal

        from .policy import authorize
        from .schema import Mutation

        class Recovery(Mutation):
            action: Literal["inspect", "resume", "rollback", "discard"]

        body = Recovery.model_validate(request)
        authorize(principal, workspace_id, "repository.admin", local_only=True)

        def apply():
            row, contract = self.contract(attempt_id)
            item = self.store.get("work_item", workspace_id, contract.item_id)
            if contract.workspace_id != workspace_id:
                raise WorkbenchError("NOT_FOUND", "Attempt not found", 404)
            if row["state"] != "RECONCILING" or item.revision != body.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Refresh the uncertain attempt before recovery")
            if not self.runtime.scheduler.leader():
                raise WorkbenchError("DISPATCHER_REQUIRED", "Recovery requires the owning daemon")
            if self.store.db.execute(
                "SELECT 1 FROM wb_operations WHERE kind='recovery' AND subject_id=? AND state IN ('PENDING','RUNNING')",
                (attempt_id,),
            ).fetchone():
                raise WorkbenchError("RECOVERY_BUSY", "Another recovery operation is pending")
            result = self.service.operation_intent(
                workspace_id, "recovery", attempt_id, item.revision, payload_hash(body.model_dump(mode="json"))
            )
            self.store.db.execute(
                "UPDATE wb_operations SET result_json=? WHERE id=?",
                (
                    json.dumps({"action": body.action, "attempt_id": attempt_id, "request_id": contract.request_id}),
                    result.body["id"],
                ),
            )
            self.runtime.planning.event(result.body["id"], principal)
            return result

        result = self.service.command(
            principal,
            workspace_id,
            "repository.admin",
            body.command_id,
            "recovery",
            {"attempt_id": attempt_id, **body.model_dump(mode="json")},
            apply,
            local_only=True,
        )
        if not result.replayed:
            task = asyncio.create_task(self.recovery_run(result.body["id"], principal))
            self.runtime.planning.tasks[result.body["id"]] = task
            task.add_done_callback(lambda _: self.runtime.planning.tasks.pop(result.body["id"], None))
        return result

    def manual_request(self, workspace_id, item_id, request, principal):
        from .policy import authorize
        from .schema import ManualSubmit

        body = ManualSubmit.model_validate(request)
        authorize(principal, workspace_id, "work.write")
        if principal.actor_type != "HUMAN":
            raise WorkbenchError("HUMAN_REQUIRED", "Manual submission requires human attestation", 403)

        def apply():
            item = self.store.get("work_item", workspace_id, item_id)
            if (
                item.revision != body.expected_revision
                or item.execution_kind != "MANUAL"
                or item.active_attempt_id
                or item.status not in {"BACKLOG", "READY", "ASSIGNED"}
            ):
                raise WorkbenchError("REVISION_CONFLICT", "Manual evidence requires current unexecuted manual item")
            self.service.readiness(item)
            if item.spec.expected_artifacts or item.spec.validation_checks:
                authorize(principal, workspace_id, "execution.request")
            result = self.service.operation_intent(
                workspace_id, "manual", item_id, item.revision, payload_hash(body.model_dump(mode="json"))
            )
            self.store.db.execute(
                "UPDATE wb_operations SET result_json=? WHERE id=?",
                (
                    json.dumps(
                        {
                            "item_id": item_id,
                            "contract_revision": item.contract_revision,
                            "policy_hash": self.runtime.agents.policy_hash(item),
                            "submission": body.evidence.model_dump(mode="json"),
                        }
                    ),
                    result.body["id"],
                ),
            )
            self.runtime.planning.event(result.body["id"], principal)
            self.service.changed_item(item, {"status": "VERIFYING"}, principal, "manual_verification_started")
            return result

        result = self.service.command(
            principal,
            workspace_id,
            "work.write",
            body.command_id,
            "manual_submit",
            {"item_id": item_id, **body.model_dump(mode="json")},
            apply,
        )
        if not result.replayed:
            task = asyncio.create_task(self.manual_run(result.body["id"], principal))
            self.runtime.planning.tasks[result.body["id"]] = task
            task.add_done_callback(lambda _: self.runtime.planning.tasks.pop(result.body["id"], None))
        return result

    async def manual_run(self, operation_id, principal):
        from contextlib import nullcontext

        from ..safety import validate

        operation = self.store.db.execute(
            "SELECT * FROM wb_operations WHERE id=? AND state='PENDING'", (operation_id,)
        ).fetchone()
        if operation is None:
            return
        record = json.loads(operation["result_json"])
        with self.store.router.transaction():
            self.store.db.execute(
                "UPDATE wb_operations SET state='RUNNING',updated_at=? WHERE id=?", (now(), operation_id)
            )
            self.runtime.planning.event(operation_id, principal)
        state, error = "COMPLETED", None
        try:
            item = self.store.get("work_item", operation["workspace_id"], record["item_id"])
            project = self.store.get("project", item.workspace_id, item.project_id)
            changing = bool(item.spec.expected_artifacts or item.spec.validation_checks)
            repo = self.service.repository(project.repository_ref) if changing else None
            if changing and (not item.spec.expected_artifacts or not item.spec.validation_checks):
                raise WorkbenchError(
                    "VERIFICATION_REQUIRED", "Repository manual work requires artifacts and registered validation"
                )
            if repo and (
                not set(item.spec.expected_artifacts) <= set(item.spec.allowed_paths)
                or not set(item.spec.validation_checks) <= repo.validation.keys()
            ):
                raise WorkbenchError(
                    "SCOPE_REQUIRED", "Manual artifacts and checks must be explicitly allowed and registered"
                )
            with repo_lock(Path(repo.path)) if repo else nullcontext():
                artifacts = (
                    [{"path": p, "hash": digest(safe_path(Path(repo.path), p))} for p in item.spec.expected_artifacts]
                    if repo
                    else []
                )
                if any(a["hash"] is None for a in artifacts):
                    raise WorkbenchError("ARTIFACT_MISSING", "Manual artifact does not exist")
                checks = (
                    await validate(
                        Path(repo.path),
                        repo.validation,
                        item.spec.validation_checks,
                        self.engine.settings.routing.timeouts["tests"],
                    )
                    if repo
                    else []
                )
                if repo and any(digest(safe_path(Path(repo.path), a["path"])) != a["hash"] for a in artifacts):
                    raise WorkbenchError("ARTIFACT_CHANGED", "Manual artifact changed during verification")
                data = {
                    "contract_revision": record["contract_revision"],
                    "policy_hash": record["policy_hash"],
                    "artifacts": artifacts,
                    "checks": checks,
                    "human_attestation": record["submission"],
                    "actor": principal.id,
                }
                passed = not repo or bool(checks) and all(c["passed"] for c in checks)
                with self.store.router.transaction():
                    current = self.store.get("work_item", item.workspace_id, item.id)
                    if (
                        current.contract_revision != record["contract_revision"]
                        or current.status != "VERIFYING"
                        or self.runtime.agents.policy_hash(current) != record["policy_hash"]
                    ):
                        raise WorkbenchError("CONTRACT_CHANGED", "Manual verification source changed")
                    kind = "MANUAL_VALIDATION" if repo else "HUMAN_ATTESTATION"
                    status = "VERIFIED" if repo and passed else "ATTESTED" if passed else "FAILED"
                    self.store.db.execute(
                        "INSERT INTO wb_verifications(id,workspace_id,item_id,kind,status,checks_json,artifacts_hash,evidence_hash,created_at,actor) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            identity(),
                            item.workspace_id,
                            item.id,
                            kind,
                            status,
                            json.dumps(data),
                            payload_hash(artifacts),
                            payload_hash(data),
                            now(),
                            principal.id,
                        ),
                    )
                    result = self.service.changed_item(
                        current,
                        {
                            "status": "REVIEW" if passed else "FAILED",
                            "reason_code": None if passed else "VALIDATION_FAILED",
                        },
                        principal,
                        "manual_verification_finished",
                    )
                    if passed:
                        self.service.review_gate(result, principal)
                    record["result"] = {
                        "verification_kind": kind,
                        "status": status,
                        "evidence_hash": payload_hash(data),
                        "inference": False,
                        "run_id": None,
                    }
        except BaseException as exc:
            state, error = "UNCERTAIN", type(exc).__name__
            with self.store.router.transaction():
                item = self.store.get("work_item", operation["workspace_id"], record["item_id"])
                if item.status == "VERIFYING":
                    self.service.changed_item(
                        item,
                        {"status": "FAILED", "reason_code": "MANUAL_VERIFICATION_FAILED"},
                        principal,
                        "manual_verification_failed",
                    )
        with self.store.router.transaction():
            self.store.db.execute(
                "UPDATE wb_operations SET state=?,error_code=?,result_json=?,updated_at=? WHERE id=?",
                (state, error, json.dumps(record), now(), operation_id),
            )
            self.runtime.planning.event(operation_id, principal)

    async def recovery_run(self, operation_id, principal):
        with self.store.router.transaction():
            operation = self.store.db.execute(
                "SELECT * FROM wb_operations WHERE id=? AND state='PENDING'", (operation_id,)
            ).fetchone()
            if operation is None:
                return
            record = json.loads(operation["result_json"])
            self.store.db.execute(
                "UPDATE wb_operations SET state='RUNNING',updated_at=? WHERE id=?", (now(), operation_id)
            )
            self.runtime.planning.event(operation_id, principal)
        state, error = "COMPLETED", None
        try:
            record["result"] = await self.recover(
                operation["workspace_id"], record["attempt_id"], record["action"], principal
            )
        except BaseException as exc:
            state, error = "UNCERTAIN", type(exc).__name__
        finally:
            self.runtime.scheduler.workers.pop(record["attempt_id"], None)
        with self.store.router.transaction():
            self.store.db.execute(
                "UPDATE wb_operations SET state=?,error_code=?,result_json=?,updated_at=? WHERE id=?",
                (state, error, json.dumps(record), now(), operation_id),
            )
            self.runtime.planning.event(operation_id, principal)

    async def recover(self, workspace_id, attempt_id, action, principal):
        from ..contracts import observer_scope
        from ..recovery import recover
        from .policy import authorize

        authorize(principal, workspace_id, "repository.admin", local_only=True)
        row, contract = self.contract(attempt_id)
        if contract.workspace_id != workspace_id:
            raise WorkbenchError("NOT_FOUND", "Attempt not found", 404)
        if row["state"] != "RECONCILING":
            raise WorkbenchError("RECOVERY_UNAVAILABLE", "Recovery requires a fenced uncertain attempt")
        if row["run_id"] is None:
            from ..safety import Edits

            with repo_lock(Path(self.service.repository(contract.repository_ref).path)):
                run = self.store.db.execute("SELECT id FROM runs WHERE id=?", (contract.plan_id,)).fetchone()
                if run:
                    self.store.db.execute("UPDATE wb_attempts SET run_id=? WHERE id=?", (contract.plan_id, attempt_id))
                elif action == "inspect":
                    return {
                        "attempt_id": attempt_id,
                        "run_id": None,
                        "status": "RECONCILING",
                        "router_files": list(
                            Edits.restore(
                                Path(self.service.repository(contract.repository_ref).path), contract.request_id
                            ).written
                        ),
                        "actions": ["discard"],
                    }
                elif (
                    action == "discard"
                    and not Edits.restore(
                        Path(self.service.repository(contract.repository_ref).path), contract.request_id
                    ).written
                ):
                    with self.store.router.transaction():
                        self.store.db.execute(
                            "UPDATE wb_attempts SET state='FAILED',finished_at=? WHERE id=?", (now(), attempt_id)
                        )
                        self.store.db.execute(
                            "UPDATE wb_assignments SET status='RELEASED',ended_at=? WHERE id=?",
                            (now(), contract.assignment_id),
                        )
                        self.store.db.execute(
                            "UPDATE wb_budget_reservations SET state='SETTLED',settled_at=? WHERE attempt_id=?",
                            (now(), attempt_id),
                        )
                        from .scheduler import SYSTEM

                        self.service.changed_item(
                            self.store.get("work_item", workspace_id, contract.item_id),
                            {"status": "FAILED", "active_attempt_id": None, "reason_code": None},
                            SYSTEM,
                            "unstarted_intent_discarded",
                        )
                    return {"status": "discarded", "run_id": None, "files_unchanged": True}
                else:
                    raise WorkbenchError(
                        "RUN_MISSING", "No persisted run; explicit discard can release a confirmed unstarted intent"
                    )
        if action == "inspect":
            return await recover(self.engine, contract.plan_id, action)
        if action not in {"resume", "rollback", "discard"}:
            raise WorkbenchError("INVALID_INPUT", "Unknown recovery action", 422)
        generation, fence = self.runtime.agents.generation, row["fence"] + 1
        if action == "resume":
            with self.store.router.transaction():
                item = self.store.get("work_item", workspace_id, contract.item_id)
                if self.runtime.agents.policy_hash(item) != contract.policy_revision_hash:
                    raise WorkbenchError("POLICY_CHANGED", "Recovery cannot resume under changed policy")
                self.store.db.execute(
                    "UPDATE wb_attempts SET owner_generation=?,fence=?,state='CLAIMED',lease_until=?,cancel_requested_at=NULL WHERE id=?",
                    (generation, fence, now() + 30000, attempt_id),
                )
                self.runtime.scheduler.workers[attempt_id] = (asyncio.current_task(), fence)
            try:
                with (
                    execution_scope(
                        lambda: self.guard(attempt_id, generation, fence),
                        contract.model_roles,
                        tools=contract.tools,
                        required_review=contract.review_policy == "AI_REVIEW",
                        model_bindings=contract.model_bindings.model_dump(mode="json"),
                    ),
                    observer_scope(lambda signal: self.observe(attempt_id, generation, fence, signal)),
                ):
                    return await recover(self.engine, contract.plan_id, "resume")
            except BaseException:
                with self.store.router.transaction():
                    self.store.db.execute(
                        "UPDATE wb_attempts SET state='RECONCILING',fence=fence+1 WHERE id=?", (attempt_id,)
                    )
                raise
        result = await recover(self.engine, contract.plan_id, action)
        if result["status"] in {"rolled_back", "discarded"}:
            self.terminal_failure(attempt_id, row["owner_generation"], row["fence"])
        return result
