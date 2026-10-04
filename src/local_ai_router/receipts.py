"""Durable receipts describe observations, never hidden reasoning or model attestation."""
import hashlib
import json
import os
import subprocess
import time
from .store import cache_key
from .safety import digest, safe_path, redact
from .contracts import egress_summary


def git_state(root):
    def git(*args):
        try:
            result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=10,
                stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            return result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    head, status = git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=no")
    return {"head": head, "tracked_dirty": bool(status) if status is not None else None,
            "tracked_status_hash": cache_key(status) if status is not None else None}


def save(engine, plan, root, edits, starting, status):
    from . import __version__
    store = engine.store
    calls = [dict(r) for r in store.db.execute("SELECT id,role,model,cost,state,usage,latency FROM calls WHERE request=? ORDER BY rowid", (plan.request_id,))]
    for call in calls:
        call["usage"] = json.loads(call["usage"])
        model = next((m for m in engine.settings.models if m.id == call["model"]), None)
        call["provider"] = model.provider if model else "unknown"
    traces = store.traces(plan.request_id)
    checks = [check for trace in traces for check in trace.get("validation", [])]
    files = [{"path": name, "before": hashlib.sha256(original).hexdigest() if original is not None else None,
              "written": edits.written.get(name), "after": digest(safe_path(root, name))} for name, original in sorted(edits.originals.items())]
    worker_events = [t for t in traces if t["stage"] == "worker"]
    receipt = {"schema_version": 1, "run_id": plan.plan_id, "request_id": plan.request_id, "plan_id": plan.plan_id,
        "axir_version": 1, "accension_version": __version__, "status": status,
        "plan_hash": cache_key(plan.model_dump(mode="json")), "repository_fingerprint": starting.get("fingerprint"),
        "starting_git_state": starting.get("git"), "ending_git_state": git_state(root),
        "started_at": starting.get("timestamp"), "finished_at": time.time(), "calls": calls,
        "models_used": sorted({c["model"] for c in calls}), "providers_used": sorted({c["provider"] for c in calls}),
        "role_assignments": [{"role": c["role"], "model": c["model"], "provider": c["provider"]} for c in calls],
        "costs": store.costs(plan.request_id), "usage": {key: sum(c["usage"].get(key, 0) for c in calls) for key in
            ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")},
        "cloud_egress": egress_summary(store, plan.request_id), "files_changed": files,
        "patch_hash": cache_key([{k: f[k] for k in ("path", "before", "written")} for f in files]),
        "patch_hash_kind": "ordered path/before/written SHA-256 manifest", "validation": checks,
        "validation_hash": cache_key(checks), "repairs": sum(c["role"] == "repair" for c in calls),
        "fallbacks": [t for t in traces if t["stage"] in {"planner_fallback", "escalation"}],
        "route_reason_codes": sorted({code for t in traces for code in t.get("reason_codes", [])}),
        "worker_outcomes": worker_events,
        "known_risks": ["Local observations only; no external model attestation."] +
                       (["Some inference charges or egress deliveries are uncertain."] if any(c["state"] != "complete" for c in calls) else [])}
    receipt = redact(receipt)
    economics = store.economics("finalize", receipt)
    if economics is not None:
        receipt["economics"] = economics
    else:
        receipt["economics"] = {"available": False, "reason": "Savings tracking disabled or temporarily unavailable"}
    with store.lock:
        store.db.execute("INSERT OR REPLACE INTO receipts VALUES(?,?,?,?)", (plan.plan_id, plan.request_id, time.time(), json.dumps(receipt)))
    return receipt


def get(engine, run_id):
    row = engine.store.db.execute("SELECT data FROM receipts WHERE run=? OR request=? ORDER BY stamp DESC LIMIT 1", (run_id, run_id)).fetchone()
    if not row:
        raise ValueError("Receipt not found; use a completed, failed or recovered run ID")
    return json.loads(row[0])


def markdown(receipt):
    lines = ["# Accension Execution Receipt", "", f"Run: `{receipt['run_id']}`", f"Status: **{receipt['status']}**",
        f"Kind: {receipt.get('kind', 'repository execution')}",
        f"Models: {', '.join(receipt.get('models_used', [])) or 'see economics call records'}"]
    if receipt.get("costs"):
        lines.append(f"Budget ledger estimate: ${receipt['costs']['estimated_usd']:.6f}")
    if receipt.get("cloud_egress"):
        lines.append(f"Cloud context bound: {receipt['cloud_egress']['context_tokens_upper_bound']} tokens")
    if "validation" in receipt:
        lines += ["", "## Validation", ""]
        lines += [f"- {c['check']}: {'PASS' if c['passed'] else 'FAIL'} (exit {c.get('exit_code')})" for c in receipt["validation"]]
    if "files_changed" in receipt:
        lines += ["", "## Changed files", ""] + [f"- `{f['path']}`: `{f['after'] or 'absent'}`" for f in receipt["files_changed"]]
    economics = receipt.get("economics", {})
    if economics:
        lines += ["", "## Economics", "", f"API cost: {economics.get('actual_cost', 'unavailable')} {economics.get('currency', 'USD')}",
            f"Estimated baseline: {economics.get('baseline_estimated_cost', 'unavailable')}",
            f"Estimated savings: {economics.get('estimated_cost_saved', 'unavailable')}",
            f"Estimated paid cloud tokens avoided: {economics.get('paid_cloud_tokens_avoided', 'unavailable')}",
            f"Local tokens processed: {economics.get('local_tokens_processed', 'unavailable')}",
            f"Cloud tokens processed (including cache): {economics.get('cloud_tokens_processed', 'unavailable')}"]
    if receipt.get("patch_hash"):
        lines += ["", f"Patch manifest hash: `{receipt['patch_hash']}`"]
    lines += ["", "Local observations; not remote attestation. Baseline and savings are estimates; API cost is not an invoice."]
    return "\n".join(lines) + "\n"
