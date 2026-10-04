"""Version-one portable execution IR; binding is always local and explicit."""
from __future__ import annotations
import difflib
import json
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from .schema import Strict, TaskNode, ExecutionPlan, EgressBudget, uid
from .safety import SECRET, repo_lock
from .context import ContextGraph
from .contracts import strictest, contract_scope


class AXIR(Strict):
    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    goal: str = Field(min_length=1, max_length=100000)
    repository_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    constraints: list[str] = []
    assumptions: list[str] = []
    privacy_contract: Literal["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"] = "LOCAL_ONLY"
    egress_budget: EgressBudget = Field(default_factory=EgressBudget)
    cost_budget: float = Field(ge=0)
    quality_contract: float | None = Field(default=None, ge=0, le=1)
    task_graph: list[TaskNode] = Field(min_length=1, max_length=64)
    capability_requirements: list[str] = []
    artifacts: list[str] = []
    validation_contract: list[str] = []
    fallback_policy: Literal["eligible-current-models"] = "eligible-current-models"
    completion_contract: list[str] = Field(min_length=1)
    provenance: dict[str, str] = {}

    @model_validator(mode="after")
    def validate_graph(self):
        self.to_plan()
        if SECRET.search(self.model_dump_json()):
            raise ValueError("Secret-like content in AXIR")
        return self

    def to_plan(self):
        tasks = [t.model_copy(deep=True) for t in self.task_graph]
        for task in tasks:
            task.minimum_capabilities = list(dict.fromkeys(self.capability_requirements + task.minimum_capabilities))
        return ExecutionPlan(goal=self.goal, constraints=self.constraints, assumptions=self.assumptions,
            success_criteria=self.completion_contract, allowed_budget=self.cost_budget,
            tasks=tasks, artifacts=self.artifacts,
            global_validation=self.validation_contract, privacy_contract=self.privacy_contract,
            egress_budget=self.egress_budget, quality_contract=self.quality_contract,
            planner_model="imported-axir")


def read(path):
    path = Path(path)
    if path.stat().st_size > 4_000_000:
        raise ValueError("AXIR exceeds the 4 MB safety limit")
    return AXIR.model_validate_json(path.read_text(encoding="utf-8"))


def export(engine, plan_id, output=None):
    row = engine.store.db.execute("SELECT * FROM runs WHERE id=?", (plan_id,)).fetchone()
    if not row:
        raise ValueError("Unknown plan")
    data = json.loads(row["data"])
    plan = ExecutionPlan.model_validate(data["plan"])
    fingerprint = data.get("portable_fingerprint")
    if not fingerprint:
        raise ValueError("Legacy plan has no portable fingerprint; compile a new plan")
    from . import __version__
    result = AXIR(plan_id=plan.plan_id, goal=plan.goal, repository_fingerprint=fingerprint,
        constraints=plan.constraints, assumptions=plan.assumptions, privacy_contract=plan.privacy_contract,
        egress_budget=plan.egress_budget, cost_budget=plan.allowed_budget, quality_contract=plan.quality_contract,
        task_graph=plan.tasks, capability_requirements=sorted({c for t in plan.tasks for c in t.minimum_capabilities}),
        artifacts=plan.artifacts, validation_contract=plan.global_validation, completion_contract=plan.success_criteria,
        provenance={"compiler": "accension", "version": __version__})
    # Replace the explicitly known binding path even when prose mentions it.
    serialized = result.model_dump_json(indent=2)
    for root in (row["repo"], row["repo"].replace("\\", "/")):
        serialized = serialized.replace(json.dumps(root)[1:-1], "[repository]")
    result = AXIR.model_validate_json(serialized)
    if output:
        Path(output).write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return result.model_dump(mode="json")


def bind(engine, ir, repo_path):
    repo = engine.settings.repository(repo_path)
    root = Path(repo.path).resolve()
    with repo_lock(root):
        graph = ContextGraph(root, engine.store, repo)
        graph.build()
        if graph.portable_fingerprint != ir.repository_fingerprint:
            raise ValueError("AXIR repository content is stale; compile against the current files")
        plan = ir.to_plan()  # Fresh IDs: imported provenance cannot overwrite local journals.
        plan.allowed_budget = min(plan.allowed_budget, engine.settings.budgets.default_request_budget)
        plan.privacy_contract = strictest(plan.privacy_contract, repo.privacy_mode)
        engine.validate_plan(plan, root, repo)
        engine.persist_plan(plan, root, graph.fingerprint, "axir", graph.portable_fingerprint)
        engine.store.metadata("axir-source:" + plan.plan_id, {"source_plan": ir.plan_id, "schema_version": ir.schema_version})
    return plan


def reroute(engine, ir):
    from .routing import deterministic
    rows = []
    for task in ir.task_graph:
        with contract_scope(mode=strictest(ir.privacy_contract, task.privacy_class if task.cloud_eligible else "LOCAL_ONLY"),
                            quality=max(ir.quality_contract or 0, task.quality_slo or 0)):
            decision = deterministic(task.objective)
            _, explanation = engine.router.roles.select(decision, "executor", task.minimum_capabilities,
                budget=min(ir.cost_budget, task.max_cost), inputs=task.estimated_input_tokens, outputs=task.estimated_output_tokens)
            rows.append({"task": task.id, **explanation})
    return {"source_plan": ir.plan_id, "bindings": rows, "inference_calls": 0,
            "portable_plan_unchanged": True, "note": "Current registry simulation; execution also applies the registered repository policy."}


def diff(old, new):
    left, right = {t.id: t.model_dump() for t in old.task_graph}, {t.id: t.model_dump() for t in new.task_graph}
    changes = {key: {"before": old.model_dump()[key], "after": new.model_dump()[key]} for key in
               ("quality_contract", "privacy_contract", "egress_budget", "cost_budget", "validation_contract", "completion_contract")
               if old.model_dump()[key] != new.model_dump()[key]}
    return {"tasks_added": sorted(right.keys()-left.keys()), "tasks_removed": sorted(left.keys()-right.keys()),
            "tasks_changed": {key: [field for field in left[key] if left[key][field] != right[key][field]]
                              for key in left.keys() & right.keys() if left[key] != right[key]}, "contracts_changed": changes}
