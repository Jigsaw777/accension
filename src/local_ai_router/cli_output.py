"""Plain terminal rendering and stable error categories; JSON stays on stdout alone."""

import json


def human(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "estimated_cost_saved" in value:
        percent = value.get("estimated_cost_saved_percent")
        saved = value["estimated_cost_saved"]
        return "\n".join(
            [
                str(value.get("period", "Run economics")).upper(),
                "API cost from usage: " + money(value.get("actual_cost")),
                "Estimated direct baseline: " + money(value.get("baseline_estimated_cost")),
                (
                    "Above baseline: " + money(-float(saved))
                    if saved is not None and float(saved) < 0
                    else "Estimated savings: " + money(saved)
                ),
                "Estimated reduction: " + (f"{float(percent):.1f}%" if percent is not None else "unavailable"),
                "Paid cloud tokens avoided (est.): "
                + str(
                    value.get("paid_cloud_tokens_avoided")
                    if value.get("paid_cloud_tokens_avoided") is not None
                    else "unavailable"
                ),
                "Local tokens processed: " + str(value.get("local_tokens_processed", 0)),
                "Cloud tokens processed (including cache): " + str(value.get("cloud_tokens_processed", 0)),
                "Provider cached tokens: " + str(value.get("actual_cached_tokens", 0)),
                "Coverage: "
                + (
                    "partial; see receipt for unknown usage/prices or configure a baseline"
                    if value.get("partial")
                    else "recorded usage; counterfactual remains estimated"
                ),
            ]
        )
    if isinstance(value, dict) and "classification" in value and "selection" in value:
        c, s = value["classification"], value["selection"]
        economics = value.get("economics") or {}

        def bounds(key):
            return " – ".join(money(n) for n in economics[key]) if economics.get(key) else "unavailable"

        return "\n".join(
            [
                f"Route: {s['selected'] or 'no eligible model'}",
                f"Task: {c['task_family']} · Planning: {c['planning_depth']}",
                f"Privacy: {value['privacy']}",
                f"Expected model cost: {money(value['cost_simulation']['expected'])} (estimate)",
                "Fallbacks: " + (" → ".join(value["candidates"][:10]) or "none"),
                "Reasons: " + ", ".join(c["reason_codes"]),
                "Inference calls: 0 · Repository edits: 0",
                "Baseline model: " + (economics.get("baseline", {}).get("model") or "not configured"),
                "Illustrative routed cost: " + bounds("actual_estimated_range"),
                "Estimated baseline range: " + bounds("baseline_estimated_range"),
                "Estimated savings range: " + bounds("estimated_savings_range"),
                "Basis: normalized single-stage workload; compilation/retries can add cost. Use --json for full methodology.",
            ]
        )
    if isinstance(value, dict) and "dimensions" in value and "model" in value:
        lines = ["Model DNA: " + value["model"], "Capability             Estimate   Samples   Evidence"]
        for dimension, evidence in value["dimensions"].items():
            estimate = "unknown" if evidence["value"] is None else f"{evidence['value']:.3f}"
            lines.append(f"{dimension:<22} {estimate:<10} {evidence['samples']:<9} {evidence['uncertainty']}")
        return "\n".join(lines)
    if isinstance(value, dict) and "patch_hash" in value and "run_id" in value:
        lines = [
            "Execution receipt: " + value["run_id"],
            "Status: " + value["status"],
            "Models: " + ", ".join(value["models_used"]),
            "Estimated cost: " + money(value["costs"]["estimated_usd"]),
            f"Cloud context: {value['cloud_egress']['context_tokens_upper_bound']} tokens (upper bound)",
            f"Validation: {sum(c['passed'] for c in value['validation'])}/{len(value['validation'])} checks passed",
            "Files: " + ", ".join(f["path"] for f in value["files_changed"]),
            "Patch manifest SHA-256: " + value["patch_hash"],
        ]
        if value.get("economics"):
            lines.append(human(value["economics"]))
        return "\n".join(lines)
    if isinstance(value, dict) and "items" in value and "total" in value:
        lines = [f"{value['total']} models · page starts at {value['offset']}"]
        lines += [f"{m['id']}  {m.get('status', '')}  {m.get('locality', '')}" for m in value["items"]]
        if value.get("next_offset") is not None:
            lines.append(f"Next: accs model list --offset {value['next_offset']} --limit {value['limit']}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "No entries."
        if all(isinstance(item, dict) for item in value):
            return "\n".join(
                " · ".join(str(item[k]) for k in ("id", "name", "kind", "status", "stage", "model") if k in item)
                or json.dumps(item)
                for item in value
            )
    if isinstance(value, dict):
        return "\n".join(
            k.replace("_", " ").capitalize()
            + ": "
            + (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
            for k, v in value.items()
        )
    return str(value)


def money(value):
    return "unknown" if value is None else f"USD {float(value):.6f}"


def exit_code(exc):
    import httpx

    from .errors import PrivacyViolation, ProviderError
    from .store import BudgetExceeded

    if isinstance(exc, (PrivacyViolation, BudgetExceeded)):
        return 4
    if isinstance(exc, ProviderError):
        return 3
    if isinstance(exc, (ValueError, KeyError, FileNotFoundError)):
        return 2
    if isinstance(exc, (httpx.HTTPError, ConnectionError, OSError)):
        return 6
    return 5


def progress(event):
    stage = event["stage"]
    if stage == "planning":
        return f"Plan compiled: {event['tasks']} tasks · {event['model']}"
    if stage == "worker":
        return f"Task {event['task']}: {'passed' if event['passed'] else 'failed'} · {event['model']} · attempt {event['attempt']}"
    if stage == "schedule":
        return "Executing: " + ", ".join(event["tasks"])
    if stage == "final_validation":
        return "Final validation passed"
    if stage == "escalation":
        return f"Task {event['task']}: escalating to {event['to_model']}"
    return None
