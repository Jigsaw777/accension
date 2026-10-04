"""Counterfactual routing from recorded tasks and current evidence. Never calls a model."""

import json
from decimal import Decimal

from .contracts import contract_scope
from .policy import preset
from .privacy import fully_local, is_local, repository_scope
from .roles import RoleResolver
from .routing import deterministic
from .savings import cost, price


def compare(engine, run_id):
    row = engine.store.db.execute(
        "SELECT * FROM runs WHERE id=? OR json_extract(data,'$.plan.request_id')=?", (run_id, run_id)
    ).fetchone()
    if row is None:
        raise ValueError("Unknown historical run")
    from .schema import ExecutionPlan

    plan = ExecutionPlan.model_validate(json.loads(row["data"])["plan"])
    repo = engine.settings.repository(row["repo"])
    from .receipts import get

    recorded = get(engine, row["id"]).get("economics", {})
    calls = recorded.get("calls", [])
    observed = {}
    for call in calls:
        if call["role"] == "executor" and call.get("task") not in observed:
            observed[call.get("task")] = call
    worker_ids = {c["call"] for c in observed.values()}
    overhead = [c.get("actual_cost") for c in calls if c["call"] not in worker_ids]
    overhead_cost = (
        sum((Decimal(c) for c in overhead), Decimal(0)) if calls and all(c is not None for c in overhead) else None
    )
    comparisons = []
    for name in ("maximum-savings", "balanced", "quality-first", "fully-local"):
        routing, control = preset(engine.settings, name)
        control.fully_local = control.fully_local or fully_local(engine.settings)
        candidate = engine.settings.model_copy(update={"routing": routing, "control_plane": control})
        tasks = []
        for task in plan.tasks:
            usage = observed.get(plan.request_id + "-" + task.id)
            inputs = usage["input_tokens"] if usage else task.estimated_input_tokens
            outputs = usage["output_tokens"] if usage else task.estimated_output_tokens
            with (
                repository_scope(repo),
                contract_scope(mode=task.privacy_class, quality=max(task.quality_slo or 0, plan.quality_contract or 0)),
            ):
                models, explanation = RoleResolver(candidate, engine.store).select(
                    deterministic(task.objective),
                    "executor",
                    task.minimum_capabilities,
                    budget=min(task.max_cost, plan.allowed_budget),
                    inputs=inputs,
                    outputs=outputs,
                )
            selected = next(
                (item for item in explanation["candidates"] if item["model"] == explanation["selected"]), None
            )
            model = next((m for m in candidate.models if m.id == explanation["selected"]), None)
            amount = (
                Decimal(0)
                if model and is_local(candidate.providers[model.provider])
                else cost(price(model), inputs, outputs)
                if model
                else None
            )
            tasks.append(
                {
                    "task": task.id,
                    "selected": explanation["selected"],
                    "estimated_cost": str(amount) if amount is not None else None,
                    "input_tokens": inputs,
                    "output_tokens": outputs,
                    "token_basis": "first observed worker attempt" if usage else "plan estimate",
                    "quality_estimate": selected["quality_estimate"] if selected else None,
                    "eligible_models": len(models),
                }
            )
        amount = (
            sum((Decimal(t["estimated_cost"]) for t in tasks), Decimal(0)) + overhead_cost
            if overhead_cost is not None and all(t["estimated_cost"] is not None for t in tasks)
            else None
        )
        baseline = recorded.get("baseline_estimated_cost")
        comparisons.append(
            {
                "preset": name,
                "tasks": tasks,
                "estimated_execution_cost": str(amount) if amount is not None else None,
                "estimated_savings_vs_recorded_baseline": str(Decimal(baseline) - amount)
                if baseline is not None and amount is not None
                else None,
            }
        )
    return {
        "run_id": row["id"],
        "recorded_costs": engine.store.costs(plan.request_id),
        "comparisons": comparisons,
        "inference_calls": 0,
        "recorded_economics": recorded,
        "recorded_nonworker_overhead": str(overhead_cost) if overhead_cost is not None else None,
        "direct_baseline": {
            "model": recorded.get("baseline_model"),
            "estimated_cost": recorded.get("baseline_estimated_cost"),
        },
        "limitations": "Current prices and eligibility applied to normalized historical worker tokens (plan estimates when unobserved). Recorded nonworker/retry overhead held fixed; cache discounts excluded from alternative workers. Different outputs, retries, tokenizers and quality may change costs. No shadow inference or measured alternative savings.",
    }
