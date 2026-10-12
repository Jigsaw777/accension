"""Explicit schema-bound planning, durable intent, and no automatic execution."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from ..contracts import strictest
from ..providers import ProviderError
from ..routing import deterministic
from ..schema import Request
from .events import append_event
from .policy import authorize
from .schema import CreatePRD, Operation, PlanningCommand, PlanningInput, PRDSpec, WorkItemSpec, budget_microusd
from .service import CommandResult, payload_hash
from .store import WorkbenchError, identity, now
from .templates import get as template_get


class Planning:
    def __init__(self, runtime):
        self.runtime, self.service, self.engine = runtime, runtime.service, runtime.engine
        self.store = self.service.store
        self.tasks = {}

    def context(self, body, principal):
        authorize(principal, body.workspace_id, "prd.write")
        workspace = self.store.get("workspace", body.workspace_id, body.workspace_id)
        project = self.store.get("project", body.workspace_id, body.project_id)
        if workspace.archived_at or project.archived_at:
            raise WorkbenchError("ARCHIVED", "Planning scope is archived")
        repo = self.service.repository(project.repository_ref) if project.repository_ref else None
        privacy = strictest(workspace.inference_policy, project.default_privacy, repo.privacy_mode if repo else None)
        if workspace.privacy_mode in {"DEVICE_ONLY", "PRIVATE_LAN"}:
            privacy = "LOCAL_ONLY"
        cap = min(budget_microusd(body.budget), int(self.engine.settings.budgets.default_request_budget * 1_000_000))
        if workspace.execution_policy.budget_ceiling is not None:
            cap = min(cap, budget_microusd(workspace.execution_policy.budget_ceiling))
        template = template_get(self.store.db, body.template_id, body.template_version)
        if template.kind != "prd":
            raise WorkbenchError("INVALID_TEMPLATE", "Planning requires a PRD template", 422)
        prd = None
        if body.kind == "decompose":
            if not repo or not body.prd_id:
                raise WorkbenchError(
                    "REPOSITORY_REQUIRED", "Decomposition requires an approved PRD and registered repository", 422
                )
            prd = self.store.get("prd", body.workspace_id, body.prd_id)
            if prd.project_id != project.id or prd.status != "APPROVED" or prd.revision != body.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Decomposition requires the current approved PRD revision")
            if not body.allowed_paths:
                raise WorkbenchError("SCOPE_REQUIRED", "Supply explicit allowed paths for decomposition", 422)
        elif not body.requirement.strip():
            raise WorkbenchError("INCOMPLETE", "Requirement text is required", 422)
        policy = payload_hash(
            {
                "workspace": workspace.model_dump(mode="json"),
                "project": project.model_dump(mode="json"),
                "privacy": privacy,
                "cap": cap,
                "template_hash": template.content_hash,
                "prd": prd.model_dump(mode="json") if prd else None,
            }
        )
        return repo, privacy, cap, template, prd, policy

    def quote(self, request, principal):
        body = PlanningInput.model_validate(request)
        repo, privacy, cap, _, _, policy = self.context(body, principal)
        candidates = self.engine.router.candidates(
            deterministic(body.requirement or "Decompose requirements"),
            "planner",
            ["code"],
            privacy != "LOCAL_ONLY",
            budget=cap / 1_000_000,
        )
        value = {
            "principal": principal.id,
            "input": payload_hash(body.model_dump(mode="json")),
            "policy": policy,
            "expires": now() + 300_000,
        }
        return {
            "quote": self.runtime.journal.token(value),
            "privacy": privacy,
            "budget": str(Decimal(cap) / 1_000_000),
            "eligible_models": [m.id for m in candidates],
            "repository_ref": self.store.get("project", body.workspace_id, body.project_id).repository_ref,
            "inference": False,
        }

    def get(self, workspace_id, operation_id, principal):
        authorize(principal, workspace_id, "workspace.read")
        with self.store.router.lock:
            row = self.store.db.execute(
                "SELECT * FROM wb_operations WHERE workspace_id=? AND id=?", (workspace_id, operation_id)
            ).fetchone()
            if row is None:
                raise WorkbenchError("NOT_FOUND", "Operation not found", 404)
            try:
                data = dict(row)
                record = json.loads(data.pop("result_json") or "{}")
                data["result"] = record.get("result")
                data["revision"] = {"PENDING": 1, "RUNNING": 2, "COMPLETED": 3, "FAILED": 3, "UNCERTAIN": 3}[
                    data["state"]
                ]
                result = Operation.model_validate(data).model_dump(mode="json")
                result["cost"] = self.engine.store.costs(record.get("request_id", operation_id))
                return result
            except (ValueError, TypeError, KeyError) as exc:
                raise WorkbenchError("CORRUPT_DATA", "Invalid planning operation", 500) from exc

    def event(self, operation_id, principal):
        row = self.store.db.execute("SELECT * FROM wb_operations WHERE id=?", (operation_id,)).fetchone()
        data = dict(row)
        record = json.loads(data.pop("result_json") or "{}")
        data["result"] = record.get("result")
        data["revision"] = {"PENDING": 1, "RUNNING": 2, "COMPLETED": 3, "FAILED": 3, "UNCERTAIN": 3}[data["state"]]
        append_event(
            self.store, "operation", Operation.model_validate(data), principal, "planning_" + data["state"].lower()
        )

    def enqueue(self, request, principal):
        body = PlanningCommand.model_validate(request)
        input_body = PlanningInput.model_validate(body.model_dump(exclude={"command_id", "quote"}))

        def apply():
            *_, policy = self.context(input_body, principal)
            quote = self.runtime.journal.decode(body.quote)
            if (
                quote.get("principal") != principal.id
                or quote.get("input") != payload_hash(input_body.model_dump(mode="json"))
                or quote.get("policy") != policy
                or quote.get("expires", 0) < now()
            ):
                raise WorkbenchError("STALE_QUOTE", "Planning quote changed or expired; quote again")
            result = self.service.operation_intent(
                body.workspace_id,
                body.kind,
                body.prd_id or body.project_id,
                body.expected_revision,
                payload_hash(input_body.model_dump(mode="json")),
            )
            record = {
                "input": input_body.model_dump(mode="json"),
                "policy": policy,
                "request_id": result.body["id"].replace("-", ""),
                "result": None,
            }
            self.store.db.execute(
                "UPDATE wb_operations SET result_json=? WHERE id=?", (json.dumps(record), result.body["id"])
            )
            self.event(result.body["id"], principal)
            return result

        result = self.service.command(
            principal,
            body.workspace_id,
            "prd.write",
            body.command_id,
            "planning_" + body.kind,
            body.model_dump(mode="json", exclude={"quote"}),
            apply,
        )
        if not result.replayed:
            task = asyncio.create_task(self.run(result.body["id"], principal))
            self.tasks[result.body["id"]] = task
            task.add_done_callback(lambda _: self.tasks.pop(result.body["id"], None))
        return result

    async def run(self, operation_id, principal):
        with self.store.router.transaction():
            row = self.store.db.execute(
                "SELECT * FROM wb_operations WHERE id=? AND state='PENDING'", (operation_id,)
            ).fetchone()
            if row is None:
                return
            record = json.loads(row["result_json"])
            body = PlanningInput.model_validate(record["input"])
            self.store.db.execute(
                "UPDATE wb_operations SET state='RUNNING',updated_at=? WHERE id=?", (now(), operation_id)
            )
            self.event(operation_id, principal)
        outcome, error = None, None
        state = "COMPLETED"
        try:
            repo, privacy, cap, template, prd, policy = self.context(body, principal)
            if record["policy"] != policy:
                raise WorkbenchError("POLICY_CHANGED", "Planning policy changed")
            if body.kind == "generate":
                spec = await self.engine.generate_planning_document(
                    body.requirement, body.constraints, privacy, cap / 1_000_000, PRDSpec, record["request_id"], repo
                )
                spec.constraints = list(dict.fromkeys(body.constraints + spec.constraints))
                spec.require_complete()
                template.document(spec.model_dump(mode="json"), complete=True)
                outcome = {"spec": spec.model_dump(mode="json")}
            else:
                outcome = await self.decompose(body, prd, repo, privacy, cap, record["request_id"])
            # Revoke/tighten during inference cannot commit under the previous quote.
            if self.context(body, principal)[-1] != policy:
                raise WorkbenchError("POLICY_CHANGED", "Planning policy changed during inference")
            with self.store.router.transaction():
                if self.context(body, principal)[-1] != policy:
                    raise WorkbenchError("POLICY_CHANGED", "Planning policy changed before result commit")
                if body.kind == "generate":
                    request = CreatePRD(
                        command_id=identity(),
                        workspace_id=body.workspace_id,
                        project_id=body.project_id,
                        title=body.title,
                        template_id=body.template_id,
                        template_version=body.template_version,
                        spec=outcome["spec"],
                    )
                    fields = self.service.prd_template(request.model_dump())
                    from .schema import PRD

                    stamp = now()
                    draft = PRD(
                        **request.model_dump(exclude={"command_id"}),
                        id=identity(),
                        revision=1,
                        status="DRAFT",
                        template_hash=fields["template_hash"],
                        created_at=stamp,
                        updated_at=stamp,
                        created_by=principal.id,
                        updated_by=principal.id,
                    )
                    self.service.persist("prd", draft, principal, "generated")
                    outcome = {"prd_id": draft.id}
                self.finish(operation_id, record, state, outcome, error, principal)
        except asyncio.CancelledError:
            state, error = "UNCERTAIN", "PLANNING_INTERRUPTED"
            with self.store.router.transaction():
                self.finish(operation_id, record, state, None, error, principal)
            raise
        except (WorkbenchError, ProviderError, ValueError) as exc:
            state = "UNCERTAIN" if self.engine.store.costs(record["request_id"])["uncertain_calls"] else "FAILED"
            error = exc.code if isinstance(exc, WorkbenchError) else "PLANNING_FAILED"
            with self.store.router.transaction():
                self.finish(operation_id, record, state, None, error, principal)
        except Exception:
            with self.store.router.transaction():
                self.finish(operation_id, record, "UNCERTAIN", None, "PLANNING_INTERRUPTED", principal)
            raise
        self.service.wake.notify()

    def finish(self, operation_id, record, state, outcome, error, principal):
        record["result"] = outcome
        self.store.db.execute(
            "UPDATE wb_operations SET state=?,result_json=?,error_code=?,updated_at=? WHERE id=? AND state='RUNNING'",
            (state, json.dumps(record), error, now(), operation_id),
        )
        self.event(operation_id, principal)

    async def decompose(self, body, prd, repo, privacy, cap, request_id):
        requirements = prd.spec.functional_requirements + prd.spec.nonfunctional_requirements
        groups = body.requirement_groups or [
            [r.id for r in requirements[i : i + 32]] for i in range(0, len(requirements), 32)
        ]
        flat = [rid for group in groups for rid in group]
        if len(flat) != len(set(flat)) or set(flat) != {r.id for r in requirements}:
            raise WorkbenchError(
                "INVALID_COVERAGE", "Requirement groups must cover every requirement exactly once", 422
            )
        batches, items, edges = [], [], []
        for index, group in enumerate(groups):
            goal = json.dumps(
                {
                    "problem": prd.spec.problem_statement,
                    "requirements": [r.model_dump() for r in requirements if r.id in group],
                    "acceptance": [r.model_dump() for r in prd.spec.acceptance_criteria],
                    "allowed_paths": body.allowed_paths,
                }
            )
            plan = await self.engine.plan(
                Request(
                    task=goal,
                    repo_path=repo.path,
                    constraints=list(dict.fromkeys(body.constraints + prd.spec.constraints)),
                    privacy=privacy,
                    budget=cap / 1_000_000,
                    session_id="workbench-planning",
                    execution_mode="plan-only",
                ),
                request_id=request_id,
            )
            batches.append({"index": index, "requirement_ids": group, "plan": plan.model_dump(mode="json")})
            for node in plan.tasks:
                checks = list(dict.fromkeys(node.validation_commands + plan.global_validation))
                spec = WorkItemSpec(
                    objective=node.objective,
                    description=node.detailed_description,
                    rationale=node.rationale,
                    acceptance_criteria=node.acceptance_criteria,
                    definition_of_done=node.verifier_requirements or node.acceptance_criteria,
                    relevant_files=node.relevant_files,
                    allowed_paths=body.allowed_paths,
                    expected_artifacts=node.expected_artifacts,
                    required_capabilities=node.minimum_capabilities,
                    required_tools=node.tools_required,
                    required_skills=node.skills_required,
                    validation_checks=checks,
                    verifier_requirements=node.verifier_requirements,
                    constraints=plan.constraints,
                    context_notes=plan.architecture_summary,
                    requirement_ids=group,
                )
                spec.require_ready("ENGINE", "registered")
                items.append(
                    {
                        "proposal_id": f"b{index}_{node.id}",
                        "source_plan_id": plan.plan_id,
                        "source_node_id": node.id,
                        "source_hash": payload_hash(node.model_dump(mode="json")),
                        "title": node.title,
                        "spec": spec.model_dump(mode="json"),
                        "budget_limit": str(Decimal(str(min(node.max_cost, cap / 1_000_000)))),
                        "engine_max_attempts": node.max_attempts,
                        "quality_target": node.quality_slo,
                        "privacy_policy": strictest(
                            privacy, node.privacy_class, "LOCAL_ONLY" if not node.cloud_eligible else None
                        ),
                        "risk": node.risk,
                        "complexity": node.complexity,
                    }
                )
                edges.extend((f"b{index}_{blocker}", f"b{index}_{node.id}") for blocker in node.dependencies)
        return {
            "prd_id": prd.id,
            "prd_revision": prd.revision,
            "prd_hash": payload_hash(prd.model_dump(mode="json")),
            "project_id": body.project_id,
            "batches": batches,
            "items": items,
            "edges": edges,
            "coverage": flat,
        }

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def preview(self, request, principal, *, reconcile=False):
        from ..schema import ExecutionPlan
        from .schema import ImportPreview, PlanImport, validate_workbench_graph

        body = ImportPreview.model_validate(request)
        authorize(principal, body.workspace_id, "work.write")

        def apply():
            prd = self.store.get("prd", body.workspace_id, body.prd_id)
            project = self.store.get("project", body.workspace_id, body.project_id)
            if prd.project_id != project.id or prd.status != "APPROVED" or prd.revision != body.prd_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Import requires current approved PRD")
            if body.operation_id:
                operation = self.get(body.workspace_id, body.operation_id, principal)
                if operation["state"] != "COMPLETED" or operation["kind"] != "decompose":
                    raise WorkbenchError("INVALID_OPERATION", "Decomposition is not complete")
                proposal = operation["result"]
                if proposal["prd_id"] != prd.id or proposal["prd_revision"] != prd.revision:
                    raise WorkbenchError("REVISION_CONFLICT", "Operation source changed")
            else:
                if not project.repository_ref or not body.plans or len(body.plans) != len(body.requirement_groups):
                    raise WorkbenchError(
                        "INCOMPLETE", "Supply registered repository, plans and requirement groups", 422
                    )
                repo = self.service.repository(project.repository_ref)
                workspace = self.store.get("workspace", body.workspace_id, body.workspace_id)
                privacy = strictest(
                    workspace.inference_policy,
                    project.default_privacy,
                    repo.privacy_mode,
                    "LOCAL_ONLY" if workspace.privacy_mode != "HYBRID" else None,
                )
                cap = self.engine.settings.budgets.default_request_budget
                items, edges, batches = [], [], []
                for index, (raw, group) in enumerate(zip(body.plans, body.requirement_groups, strict=True)):
                    plan = ExecutionPlan.model_validate(raw)
                    self.engine.validate_plan(plan, Path(repo.path), repo)
                    batches.append({"index": index, "requirement_ids": group, "plan": plan.model_dump(mode="json")})
                    for node in plan.tasks:
                        spec = WorkItemSpec(
                            objective=node.objective,
                            description=node.detailed_description,
                            rationale=node.rationale,
                            acceptance_criteria=node.acceptance_criteria,
                            definition_of_done=node.verifier_requirements or node.acceptance_criteria,
                            relevant_files=node.relevant_files,
                            allowed_paths=body.allowed_paths,
                            expected_artifacts=node.expected_artifacts,
                            required_capabilities=node.minimum_capabilities,
                            required_tools=node.tools_required,
                            required_skills=node.skills_required,
                            validation_checks=list(dict.fromkeys(node.validation_commands + plan.global_validation)),
                            verifier_requirements=node.verifier_requirements,
                            constraints=list(dict.fromkeys(prd.spec.constraints + plan.constraints)),
                            context_notes=plan.architecture_summary,
                            requirement_ids=group,
                        )
                        spec.require_ready("ENGINE", project.repository_ref)
                        items.append(
                            {
                                "proposal_id": f"b{index}_{node.id}",
                                "source_plan_id": plan.plan_id,
                                "source_node_id": node.id,
                                "source_hash": payload_hash(node.model_dump(mode="json")),
                                "title": node.title,
                                "spec": spec.model_dump(mode="json"),
                                "budget_limit": str(min(node.max_cost, plan.allowed_budget, cap)),
                                "engine_max_attempts": node.max_attempts,
                                "quality_target": max(node.quality_slo or 0, plan.quality_contract or 0) or None,
                                "privacy_policy": strictest(
                                    privacy,
                                    node.privacy_class,
                                    plan.privacy_contract,
                                    "LOCAL_ONLY" if not node.cloud_eligible else None,
                                ),
                                "risk": node.risk,
                                "complexity": node.complexity,
                            }
                        )
                        edges.extend((f"b{index}_{blocker}", f"b{index}_{node.id}") for blocker in node.dependencies)
                proposal = {
                    "prd_id": prd.id,
                    "prd_revision": prd.revision,
                    "prd_hash": payload_hash(prd.model_dump(mode="json")),
                    "project_id": project.id,
                    "items": items,
                    "edges": edges,
                    "batches": batches,
                    "coverage": [rid for group in body.requirement_groups for rid in group],
                }
            proposal = json.loads(json.dumps(proposal))
            if not proposal["items"] or len(proposal["items"]) > 2000:
                raise WorkbenchError("INVALID_GRAPH_SIZE", "A staged import must contain 1 to 2000 items", 422)
            proposal["edges"].extend(body.cross_batch_edges)
            ids = [item["proposal_id"] for item in proposal["items"]]
            validate_workbench_graph(ids, [tuple(edge) for edge in proposal["edges"]])
            required = {r.id for r in prd.spec.functional_requirements + prd.spec.nonfunctional_requirements}
            covered = {rid for item in proposal["items"] for rid in item["spec"]["requirement_ids"]}
            if covered != required:
                raise WorkbenchError(
                    "INVALID_COVERAGE", "Proposal must cover every PRD requirement and no unknown IDs", 422
                )
            proposal["comparison"] = self.compare(body.workspace_id, prd.id, proposal["items"]) if reconcile else {}
            preview = PlanImport(
                id=identity(),
                workspace_id=body.workspace_id,
                project_id=project.id,
                prd_id=prd.id,
                prd_revision=prd.revision,
                source_hash=payload_hash(proposal),
                operation_id=body.operation_id,
                status="PREVIEW",
                proposal=proposal,
                revision=1,
                created_at=now(),
            )
            self.service.persist(
                "plan_import", preview, principal, "reconcile_preview" if reconcile else "import_preview"
            )
            return CommandResult(201, preview.model_dump(mode="json"))

        return self.service.command(
            principal,
            body.workspace_id,
            "work.write",
            body.command_id,
            "reconcile_preview" if reconcile else "import_preview",
            body.model_dump(mode="json"),
            apply,
        )

    def compare(self, workspace_id, prd_id, proposals):
        existing = [
            self.store.get("work_item", workspace_id, row[0])
            for row in self.store.db.execute(
                "SELECT id FROM wb_work_items WHERE workspace_id=? AND prd_id=? AND archived_at IS NULL",
                (workspace_id, prd_id),
            ).fetchall()
        ]
        by_source = {}
        for item in existing:
            provenance = item.spec.source_provenance
            if provenance.kind == "plan":
                key = (tuple(sorted(item.spec.requirement_ids)), provenance.source_node_id)
                if key in by_source:
                    raise WorkbenchError("AMBIGUOUS_PROVENANCE", "Multiple current items share source provenance")
                by_source[key] = item
        comparison, matched = {}, set()
        for proposal in proposals:
            key = (tuple(sorted(proposal["spec"]["requirement_ids"])), proposal["source_node_id"])
            item = by_source.get(key)
            if item:
                matched.add(item.id)
                old = item.spec.model_dump(mode="json", exclude={"source_provenance"})
                new = {k: v for k, v in proposal["spec"].items() if k != "source_provenance"}
                comparison[proposal["proposal_id"]] = {
                    "group": "unchanged" if old == new and item.title == proposal["title"] else "changed",
                    "item_id": item.id,
                    "revision": item.revision,
                }
            else:
                comparison[proposal["proposal_id"]] = {"group": "new"}
        for item in existing:
            if item.id not in matched:
                comparison["obsolete_" + item.id.replace("-", "")] = {
                    "group": "obsolete",
                    "item_id": item.id,
                    "revision": item.revision,
                }
        return comparison

    def commit(self, workspace_id, import_id, request, principal, *, reconcile=False):
        from .schema import CreateWorkItem, ImportCommit, WorkItem, validate_workbench_graph

        body = ImportCommit.model_validate(request)

        def apply():
            preview = self.store.get("plan_import", workspace_id, import_id)
            if (
                preview.revision != body.expected_revision
                or preview.source_hash != body.source_hash
                or preview.status != "PREVIEW"
            ):
                raise WorkbenchError("REVISION_CONFLICT", "Preview changed or was committed")
            prd = self.store.get("prd", workspace_id, preview.prd_id)
            if (
                prd.status != "APPROVED"
                or prd.revision != preview.prd_revision
                or payload_hash(prd.model_dump(mode="json")) != preview.proposal["prd_hash"]
            ):
                raise WorkbenchError("REVISION_CONFLICT", "PRD changed after preview")
            proposals = preview.proposal["items"]
            comparison = preview.proposal.get("comparison", {})
            if reconcile != bool(comparison):
                raise WorkbenchError("INVALID_INPUT", "Use the matching import or reconciliation command", 422)
            keys = set(comparison) if reconcile else {p["proposal_id"] for p in proposals}
            if set(body.actions) != keys:
                raise WorkbenchError("INCOMPLETE_SELECTION", "Explicitly choose one action for every proposal", 422)
            mapping, changed_ids = {}, set()
            for match in comparison.values():
                if "item_id" in match:
                    item = self.store.get("work_item", workspace_id, match["item_id"])
                    if body.item_revisions.get(item.id) != item.revision or item.revision != match["revision"]:
                        raise WorkbenchError("REVISION_CONFLICT", "Compared item changed")
            for proposal in proposals:
                key = proposal["proposal_id"]
                action = body.actions[key]
                match = comparison.get(key, {"group": "new"})
                old = self.store.get("work_item", workspace_id, match["item_id"]) if "item_id" in match else None
                if action == "keep" and old:
                    mapping[key] = old.id
                    continue
                if (
                    action not in {"create", "successor", "update"}
                    or (action == "create" and old)
                    or (action in {"successor", "update"} and not old)
                ):
                    raise WorkbenchError("INVALID_SELECTION", "Action does not match proposal", 422)
                fields = {
                    k: v
                    for k, v in proposal.items()
                    if k not in {"proposal_id", "source_plan_id", "source_node_id", "source_hash"}
                }
                fields["spec"] = {
                    **fields["spec"],
                    "source_provenance": {
                        "kind": "plan",
                        "import_id": import_id,
                        "source_plan_id": proposal["source_plan_id"],
                        "source_node_id": proposal["source_node_id"],
                        "source_hash": proposal["source_hash"],
                    },
                }
                if action == "update":
                    if old.active_attempt_id or old.status in {"RUNNING", "VERIFYING", "DONE"}:
                        raise WorkbenchError(
                            "ACTIVE_OR_COMPLETED", "Preserve active/completed history; create an explicit successor"
                        )
                    self.service.invalidate(old)
                    self.store.db.execute(
                        "DELETE FROM wb_dependencies WHERE workspace_id=? AND dependent_id=?", (workspace_id, old.id)
                    )
                    item = WorkItem.model_validate(
                        {
                            **old.model_dump(),
                            **fields,
                            "revision": old.revision + 1,
                            "contract_revision": old.contract_revision + 1,
                            "status": "BACKLOG",
                            "assigned_agent_id": None,
                            "updated_at": now(),
                            "updated_by": principal.id,
                        }
                    )
                    self.service.persist("work_item", item, principal, "reconciled", previous=old)
                else:
                    item = self.store.allocate_item(
                        CreateWorkItem(
                            command_id=identity(),
                            workspace_id=workspace_id,
                            project_id=preview.project_id,
                            prd_id=prd.id,
                            execution_kind="ENGINE",
                            parent_item_id=old.id if old else None,
                            **fields,
                        ),
                        principal.id,
                    )
                    append_event(self.store, "work_item", item, principal, "successor" if old else "imported")
                mapping[key] = item.id
                changed_ids.add(item.id)
                self.store.db.execute(
                    "INSERT INTO wb_plan_bindings VALUES(?,?,?,?,?)",
                    (import_id, key, item.id, proposal["source_plan_id"], proposal["source_hash"]),
                )
            for key, match in comparison.items():
                if match["group"] == "obsolete":
                    action = body.actions[key]
                    old = self.store.get("work_item", workspace_id, match["item_id"])
                    if action == "archive":
                        if old.active_attempt_id or old.status in {"RUNNING", "VERIFYING", "DONE"}:
                            raise WorkbenchError(
                                "ACTIVE_OR_COMPLETED", "Cannot archive active/completed reconciliation history"
                            )
                        archived = WorkItem.model_validate(
                            {
                                **old.model_dump(),
                                "revision": old.revision + 1,
                                "archived_at": now(),
                                "updated_at": now(),
                                "updated_by": principal.id,
                            }
                        )
                        self.service.persist(
                            "work_item", archived, principal, "reconcile_archive", previous=old, tombstone=True
                        )
                    elif action != "keep":
                        raise WorkbenchError("INVALID_SELECTION", "Obsolete items require keep or archive", 422)
            edges = [(mapping[a], mapping[b]) for a, b in preview.proposal["edges"]]
            for blocker, dependent in edges:
                if dependent not in changed_ids:
                    existing = self.store.db.execute(
                        "SELECT 1 FROM wb_dependencies WHERE workspace_id=? AND blocker_id=? AND dependent_id=?",
                        (workspace_id, blocker, dependent),
                    ).fetchone()
                    if not existing:
                        raise WorkbenchError(
                            "INVALID_SELECTION", "Dependency changes require updating the dependent item"
                        )
                self.store.db.execute(
                    "INSERT OR IGNORE INTO wb_dependencies(workspace_id,blocker_id,dependent_id,created_at,created_by) VALUES(?,?,?,?,?)",
                    (workspace_id, blocker, dependent, now(), principal.id),
                )
            all_ids = [
                row[0]
                for row in self.store.db.execute("SELECT id FROM wb_work_items WHERE workspace_id=?", (workspace_id,))
            ]
            all_edges = [
                tuple(row)
                for row in self.store.db.execute(
                    "SELECT blocker_id,dependent_id FROM wb_dependencies WHERE workspace_id=?", (workspace_id,)
                )
            ]
            validate_workbench_graph(all_ids, all_edges)
            final = type(preview).model_validate(
                {**preview.model_dump(), "status": "COMMITTED", "revision": preview.revision + 1}
            )
            self.service.persist(
                "plan_import",
                final,
                principal,
                "reconcile_committed" if reconcile else "import_committed",
                previous=preview,
            )
            return CommandResult(200, {"id": import_id, "status": "COMMITTED", "items": mapping})

        return self.service.command(
            principal,
            workspace_id,
            "work.write",
            body.command_id,
            "reconcile_commit" if reconcile else "import_commit",
            {"import_id": import_id, **body.model_dump(mode="json")},
            apply,
        )

    def reconcile_startup(self):
        from .policy import CAPABILITIES, Principal
        from .schema import ActorType

        principal = Principal("system", ActorType.SYSTEM, CAPABILITIES, frozenset({"*"}), True)
        with self.store.router.transaction():
            for row in self.store.db.execute(
                "SELECT id,state,result_json,kind,workspace_id,subject_id FROM wb_operations WHERE state IN ('PENDING','RUNNING')"
            ).fetchall():
                self.store.db.execute(
                    "UPDATE wb_operations SET state='UNCERTAIN',error_code='PLANNING_INTERRUPTED',updated_at=? WHERE id=?",
                    (now(), row["id"]),
                )
                self.event(row["id"], principal)
                if row["kind"] == "manual":
                    item = self.store.get("work_item", row["workspace_id"], row["subject_id"])
                    if item.status == "VERIFYING":
                        self.service.changed_item(
                            item,
                            {"status": "FAILED", "reason_code": "MANUAL_VERIFICATION_INTERRUPTED"},
                            principal,
                            "manual_interrupted",
                        )

    def team_lead(self, request, principal):
        from .schema import TeamLeadCommand

        body = TeamLeadCommand.model_validate(request)

        def apply():
            item = self.service.get("work_item", body.workspace_id, body.item_id, principal)
            if item.revision != body.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Team Lead source item changed")
            decision = self.runtime.agents.preview(body.workspace_id, body.item_id, principal)
            result = self.service.operation_intent(
                body.workspace_id, "team_lead", item.id, item.revision, payload_hash(body.model_dump(mode="json"))
            )
            record = {
                "request_id": result.body["id"].replace("-", ""),
                "input": body.model_dump(mode="json"),
                "policy": self.runtime.agents.policy_hash(item),
                "decision": decision,
                "result": None,
            }
            self.store.db.execute(
                "UPDATE wb_operations SET result_json=? WHERE id=?", (json.dumps(record), result.body["id"])
            )
            self.event(result.body["id"], principal)
            return result

        result = self.service.command(
            principal,
            body.workspace_id,
            "assignment.write",
            body.command_id,
            "team_lead",
            body.model_dump(mode="json"),
            apply,
        )
        if not result.replayed:
            task = asyncio.create_task(self.run_advice(result.body["id"], principal))
            self.tasks[result.body["id"]] = task
            task.add_done_callback(lambda _: self.tasks.pop(result.body["id"], None))
        return result

    async def run_advice(self, operation_id, principal):
        from .schema import TeamLeadAdvice, TeamLeadCommand

        with self.store.router.transaction():
            row = self.store.db.execute(
                "SELECT * FROM wb_operations WHERE id=? AND state='PENDING'", (operation_id,)
            ).fetchone()
            if row is None:
                return
            record = json.loads(row["result_json"])
            body = TeamLeadCommand.model_validate(record["input"])
            self.store.db.execute(
                "UPDATE wb_operations SET state='RUNNING',updated_at=? WHERE id=?", (now(), operation_id)
            )
            self.event(operation_id, principal)
        try:
            item = self.service.get("work_item", body.workspace_id, body.item_id, principal)
            policy = self.service.item_policy(item)
            project = self.store.get("project", body.workspace_id, item.project_id)
            repo = self.service.repository(project.repository_ref) if project.repository_ref else None
            advice = await self.engine.generate_planning_document(
                json.dumps(
                    {"work_item": item.model_dump(mode="json"), "eligible_agents": record["decision"]["eligible"]}
                ),
                item.spec.constraints,
                policy.privacy,
                min(float(body.budget), self.engine.settings.budgets.default_request_budget),
                TeamLeadAdvice,
                record["request_id"],
                repo,
            )
            with self.store.router.transaction():
                current = self.store.get("work_item", body.workspace_id, body.item_id)
                authorize(principal, body.workspace_id, "assignment.write")
                if (
                    current.revision != body.expected_revision
                    or self.runtime.agents.policy_hash(current) != record["policy"]
                ):
                    raise WorkbenchError("POLICY_CHANGED", "Advice source changed")
                self.finish(
                    operation_id,
                    record,
                    "COMPLETED",
                    {"proposal": advice.model_dump(mode="json"), "authority": False},
                    None,
                    principal,
                )
        except asyncio.CancelledError:
            with self.store.router.transaction():
                self.finish(operation_id, record, "UNCERTAIN", None, "PLANNING_INTERRUPTED", principal)
            raise
        except (WorkbenchError, ProviderError, ValueError):
            with self.store.router.transaction():
                self.finish(operation_id, record, "FAILED", None, "PLANNING_FAILED", principal)
