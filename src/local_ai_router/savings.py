"""Local, decimal counterfactual accounting. Receipts freeze finalized economics.

Each logical planner/worker/reviewer stage has one baseline equivalent. Retries
and control-plane overhead remain actual costs. Attribution is never summed.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .privacy import is_local

D = Decimal
MILLION = D(1_000_000)
MONEY = ("actual_cost", "baseline_estimated_cost", "estimated_cost_saved")
COUNTS = (
    "actual_paid_input_tokens",
    "actual_paid_output_tokens",
    "actual_cached_tokens",
    "cache_write_tokens",
    "local_input_tokens",
    "local_output_tokens",
    "baseline_input_tokens",
    "baseline_output_tokens",
    "paid_cloud_tokens_avoided",
    "context_tokens_avoided",
    "cache_tokens_saved_or_discounted",
    "frontier_calls_actual",
    "frontier_calls_baseline_estimated",
    "frontier_calls_avoided",
    "local_calls",
    "cloud_calls",
    "uncertain_calls",
    "missing_usage_calls",
    "plan_reuse",
    "graph_cache_hits",
    "graph_cache_misses",
)


def day(stamp=None):
    return datetime.fromtimestamp(stamp or time.time(), timezone.utc).date().isoformat()


def price(model):
    return {
        k: str(getattr(model, k)) if getattr(model, k) is not None else None
        for k in ("input_price", "output_price", "cached_input_price", "cache_write_price")
    } | {
        "currency": model.currency,
        "status": model.pricing_status,
        "source": model.pricing_source,
        "updated_at": model.pricing_updated_at,
        "reference": model.id,
    }


def cost(prices, inputs, outputs, cached=0, writes=0):
    if (
        prices.get("currency") != "USD"
        or prices.get("status") == "unknown"
        or any(prices.get(k) is None for k in ("input_price", "output_price"))
    ):
        return None
    cached = min(inputs, cached)
    writes = min(max(0, inputs - cached), writes)
    ip, op = D(prices["input_price"]), D(prices["output_price"])
    cp, wp = D(prices.get("cached_input_price") or ip), D(prices.get("cache_write_price") or ip)
    return ((inputs - cached - writes) * ip + outputs * op + cached * cp + writes * wp) / MILLION


def derived(data):
    value = dict(data)
    baseline, saved = value.get("baseline_estimated_cost"), value.get("estimated_cost_saved")
    value["estimated_cost_saved_percent"] = (
        str(D(saved) * 100 / D(baseline)) if saved is not None and baseline is not None and D(baseline) > 0 else None
    )
    local = value.get("local_input_tokens", 0) + value.get("local_output_tokens", 0)
    cloud = value.get("actual_paid_input_tokens", 0) + value.get("actual_paid_output_tokens", 0)
    value["local_tokens_processed"], value["cloud_tokens_processed"] = local, cloud
    calls = value.get("local_calls", 0) + value.get("cloud_calls", 0)
    value["local_execution_percentage"] = round(value.get("local_calls", 0) * 100 / calls, 1) if calls else None
    value["cloud_execution_percentage"] = round(value.get("cloud_calls", 0) * 100 / calls, 1) if calls else None
    hits, misses = value.get("graph_cache_hits", 0), value.get("graph_cache_misses", 0)
    value["graph_cache_hit_percent"] = round(hits * 100 / (hits + misses), 1) if hits + misses else None
    return value


def empty_total():
    return {
        "runs": 0,
        **{k: 0 for k in COUNTS},
        "known": {k: "0" for k in MONEY},
        "missing": {k: 0 for k in (*MONEY, "paid_cloud_tokens_avoided", "frontier_calls_avoided")},
    }


def add_snapshot(total, snapshot):
    total["runs"] += 1
    for key in COUNTS:
        total[key] = (total[key] or 0) + (snapshot.get(key) or 0)
    for key in MONEY:
        if snapshot.get(key) is not None:
            total["known"][key] = str(D(total["known"][key]) + D(snapshot[key]))
    for key in total["missing"]:
        total["missing"][key] += int(snapshot.get(key) is None)
    return total


def total_view(total):
    result = {
        "currency": "USD",
        **total,
        **{k: total["known"][k] if not total["missing"][k] else None for k in MONEY},
        "partial": any(total["missing"][key] for key in MONEY),
    }
    for key in ("paid_cloud_tokens_avoided", "frontier_calls_avoided"):
        if total["missing"][key]:
            result[key] = None
    return derived(result)


class SavingsEngine:
    def __init__(self, store):
        self.store, self.settings = store, store.settings
        self.last_error = None
        self.on_change = None
        from uuid import uuid4

        self.owner = uuid4().hex

    def pause_owned(self):
        for row in self.store.db.execute("SELECT request,baseline FROM savings_runs WHERE status='active'").fetchall():
            if json.loads(row["baseline"]).get("owner") == self.owner:
                self.pause(row["request"], "interrupted")

    def recover_abandoned(self):
        for row in self.store.db.execute("SELECT request,baseline FROM savings_runs WHERE status='active'").fetchall():
            pid = json.loads(row["baseline"]).get("owner_pid")
            if pid and not process_alive(pid):
                self.pause(row["request"], "interrupted")

    def baseline(self, host_model=None):
        settings = self.settings.savings
        method = settings.baseline_method
        selected = host_model if method == "HOST_MODEL" else settings.baseline_model
        models = {m.id: m for m in self.settings.models}
        if method == "QUALITY_BASELINE":
            # Eligible quality-first evidence; never select by highest price.
            from .roles import RoleResolver
            from .schema import Classification

            clone = self.settings.model_copy(deep=True)
            from .policy import preset

            clone.routing, clone.control_plane = preset(clone, "quality-first")
            candidates, _ = RoleResolver(clone, self.store).select(
                Classification(task_family="coding", complexity=50, risk=20, confidence=1),
                "executor",
                ["code"],
                allow_cloud=True,
                inputs=2000,
                outputs=2000,
            )
            candidates = [m for m in candidates if not is_local(self.settings.providers[m.provider])]
            selected = candidates[0].id if candidates else None
        model = models.get(selected)
        valid = bool(
            settings.enabled
            and method != "DISABLED"
            and model
            and not is_local(self.settings.providers[model.provider])
            and self.settings.providers[model.provider].enabled
            and model.enabled
            and model.status in {"available", "degraded"}
        )
        return {
            "method": method,
            "model": model.id if valid else None,
            "source": "host_model"
            if method == "HOST_MODEL"
            else "quality_heuristic"
            if method == "QUALITY_BASELINE"
            else "user_configuration",
            "price": price(model) if valid else None,
            "frontier": model.tier == 4 if valid else False,
            "confidence": "UNAVAILABLE" if not valid else "LOW" if method == "QUALITY_BASELINE" else "MEDIUM",
            "reason": None
            if valid
            else "Tracking disabled"
            if not settings.enabled or method == "DISABLED"
            else "No available registered cloud baseline",
            "token_estimation_method": "Observed stage tokens used as normalized cross-model equivalents; not exact cross-vendor tokenization",
            "methodology": "One equivalent per logical planner, worker and reviewer stage; retries/control overhead only in actual cost. Observed cache tokens use baseline cache rates when known; context reduction added once.",
        }

    def changed(self):
        self.store.db.execute("UPDATE savings_revision SET value=value+1 WHERE id=1")
        if self.on_change:
            self.on_change()

    def revision(self):
        return self.store.db.execute("SELECT value FROM savings_revision WHERE id=1").fetchone()[0]

    def begin(self, request, session="default", kind="task", run=None, host_model=None):
        if not self.settings.savings.enabled:
            return
        with self.store.transaction():
            baseline = self.baseline(host_model)
            baseline.update(owner=self.owner, owner_pid=os.getpid())
            self.store.db.execute(
                "INSERT OR IGNORE INTO savings_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (request, run, session, day(), "active", kind, time.time(), time.time(), json.dumps(baseline), "{}"),
            )
            row = self.store.db.execute(
                "SELECT baseline FROM savings_runs WHERE request=? AND status!='final'", (request,)
            ).fetchone()
            if row:
                frozen = json.loads(row[0])
                frozen.update(owner=self.owner, owner_pid=os.getpid())
                self.store.db.execute(
                    "UPDATE savings_runs SET baseline=? WHERE request=?", (json.dumps(frozen), request)
                )
            self.store.db.execute(
                "UPDATE savings_runs SET run=COALESCE(?,run),status='active',updated=? WHERE request=? AND status!='final'",
                (run, time.time(), request),
            )
            snapshot = self.calculate(request)
            if snapshot:
                self.store.db.execute(
                    "UPDATE savings_runs SET data=? WHERE request=? AND status!='final'",
                    (json.dumps(snapshot), request),
                )
            self.changed()

    def pause(self, request, status="planned"):
        with self.store.lock:
            self.store.db.execute(
                "UPDATE savings_runs SET status=?,updated=? WHERE request=? AND status!='final'",
                (status, time.time(), request),
            )
            self.changed()

    def reserve(self, call, request, session, role, model, task, reservation):
        row = self.store.db.execute("SELECT status FROM savings_runs WHERE request=?", (request,)).fetchone()
        if not row or row["status"] == "final":
            if not self.settings.savings.enabled:
                return
            # Calls outside an explicitly opened task/gateway/calibration group
            # each get their own receipt. Reused caller request IDs cannot mutate
            # an earlier finalized snapshot (embedding batches also use this path).
            request = call
            self.begin(request, session, "inference")
        # Use the original model, not routing's conservative unknown-price substitute.
        original = next((m for m in self.settings.models if m.id == model.id), model)
        record = {
            "provider": original.provider,
            "model": model.id,
            "price": price(original),
            "local": is_local(self.settings.providers[original.provider]),
            "frontier": original.tier == 4,
            "state": "reserved",
            "usage": {},
            "reservation": str(reservation),
            "actual_cost": None,
        }
        with self.store.lock:
            self.store.db.execute(
                "INSERT OR IGNORE INTO usage_records VALUES(?,?,?,?,?,?)",
                (call, request, task, role, time.time(), json.dumps(record)),
            )
            self.changed()

    def settle(self, call, settled_cost, usage, failed):
        row = self.store.db.execute("SELECT * FROM usage_records WHERE call=?", (call,)).fetchone()
        if not row:
            return
        state = self.store.db.execute("SELECT status FROM savings_runs WHERE request=?", (row["request"],)).fetchone()
        if state and state[0] == "final":
            return
        record = json.loads(row["data"])
        # A refund before transmission carries no inferred workload.
        cancelled = not failed and not any(usage.values()) and settled_cost == 0
        known = bool(usage.get("input_tokens") or usage.get("output_tokens"))
        amount = (
            D(0)
            if record["local"] or cancelled
            else cost(
                record["price"],
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cached_tokens", 0),
                usage.get("cache_write_tokens", 0),
            )
            if known and not failed
            else None
        )
        record.update(
            state="uncertain" if failed else "cancelled" if cancelled else "complete",
            usage=usage,
            actual_cost=str(amount) if amount is not None else None,
            usage_source="provider_reported" if known else "unavailable",
        )
        with self.store.lock:
            self.store.db.execute("UPDATE usage_records SET data=? WHERE call=?", (json.dumps(record), call))
            snapshot = self.calculate(row["request"])
            self.store.db.execute(
                "UPDATE savings_runs SET data=?,updated=? WHERE request=?",
                (json.dumps(snapshot), time.time(), row["request"]),
            )
            self.changed()
        kind = self.store.db.execute("SELECT kind FROM savings_runs WHERE request=?", (row["request"],)).fetchone()[0]
        if kind == "inference":
            receipt = {
                "schema_version": 1,
                "run_id": row["request"],
                "request_id": row["request"],
                "kind": "inference",
                "status": "uncertain" if failed else "complete",
                "finished_at": time.time(),
                "models_used": [record["model"]],
                "providers_used": [record["provider"]],
            }
            self.finalize(receipt)

    def trace(self, request, stage, data):
        if not self.settings.savings.enabled:
            return
        kind, key, value = None, "", None
        if stage == "context":
            kind, key = "context", data.get("task", "")
            value = {"raw": data.get("raw_estimated_tokens"), "sent": data.get("optimized_estimated_tokens")}
        elif stage == "plan_cache":
            kind, value = "plan_cache", {"hit": bool(data.get("hit")), "source": data.get("source_request")}
        elif stage == "graph_cache":
            kind, key, value = (
                "graph_cache",
                str(time.time_ns()),
                {"hits": data.get("hits", 0), "misses": data.get("misses", 0)},
            )
        if kind:
            with self.store.lock:
                # First logical context is the baseline, never sum retries.
                self.store.db.execute(
                    "INSERT OR IGNORE INTO savings_factors VALUES(?,?,?,?)", (request, kind, key, json.dumps(value))
                )
                self.changed()

    def calculate(self, request, reprice=False):
        row = self.store.db.execute("SELECT * FROM savings_runs WHERE request=?", (request,)).fetchone()
        if not row:
            return None
        baseline = json.loads(row["baseline"])
        models = {m.id: m for m in self.settings.models}
        if reprice and baseline["model"] in models:
            baseline["price"] = price(models[baseline["model"]])
        factors = [
            (r["kind"], r["stage"], json.loads(r["data"]))
            for r in self.store.db.execute("SELECT * FROM savings_factors WHERE request=?", (request,))
        ]
        context = {
            stage: max(0, data["raw"] - data["sent"])
            for kind, stage, data in factors
            if kind == "context" and data["raw"] is not None and data["sent"] is not None
        }
        result = {k: 0 for k in COUNTS}
        actual, base, actual_known, base_known = D(0), D(0), True, bool(baseline["price"])
        stages, details, observed_calls = {}, [], set()
        for usage_row in self.store.db.execute(
            "SELECT * FROM usage_records WHERE request=? ORDER BY stamp,call", (request,)
        ):
            observed_calls.add(usage_row["call"])
            record = json.loads(usage_row["data"])
            if record["state"] == "cancelled":
                continue
            u = record["usage"]
            inputs, outputs = u.get("input_tokens", 0), u.get("output_tokens", 0)
            known = bool(inputs or outputs)
            result["missing_usage_calls"] += int(not known)
            result["uncertain_calls"] += int(record["state"] != "complete")
            local = record["local"]
            result["local_calls" if local else "cloud_calls"] += 1
            result["local_input_tokens" if local else "actual_paid_input_tokens"] += inputs
            result["local_output_tokens" if local else "actual_paid_output_tokens"] += outputs
            if not local:
                result["actual_cached_tokens"] += u.get("cached_tokens", 0)
                result["cache_write_tokens"] += u.get("cache_write_tokens", 0)
            result["frontier_calls_actual"] += int(record["frontier"] and not local)
            amount = record["actual_cost"]
            if reprice and record["model"] in models and not local and known and record["state"] == "complete":
                amount = cost(
                    price(models[record["model"]]),
                    inputs,
                    outputs,
                    u.get("cached_tokens", 0),
                    u.get("cache_write_tokens", 0),
                )
            actual_known &= amount is not None
            actual += D(amount) if amount is not None else 0
            role, task = usage_row["role"], usage_row["task"] or ""
            family = (
                "worker"
                if task and role in {"executor", "repair"}
                else "planner"
                if not task and role in {"planner", "repair"}
                else role
            )
            key = family + ":" + task
            details.append(
                {
                    "call": usage_row["call"],
                    "task": task,
                    "role": role,
                    "model": record["model"],
                    "provider": record["provider"],
                    "local": local,
                    "state": record["state"],
                    "actual_cost": str(amount) if amount is not None else None,
                    "input_tokens": inputs,
                    "output_tokens": outputs,
                    "cached_tokens": u.get("cached_tokens", 0),
                    "price_used": record["price"],
                }
            )
            if family in {"planner", "worker", "executor", "reviewer", "gateway"} and (
                key not in stages or not stages[key]["known"]
            ):
                suffix = task.removeprefix(request + "-")
                stages[key] = {
                    "known": known,
                    "inputs": inputs,
                    "outputs": outputs,
                    "cached": u.get("cached_tokens", 0),
                    "writes": u.get("cache_write_tokens", 0),
                    "context": context.get(suffix, 0) if family == "worker" else 0,
                }
        clause = "id=?" if row["kind"] == "inference" else "request=?"
        missing = [
            dict(r)
            for r in self.store.db.execute("SELECT id,model,state FROM calls WHERE " + clause, (request,))
            if r["id"] not in observed_calls
        ]
        if missing:
            # The budget ledger is authoritative about a call existing. An
            # optional analytics failure must produce unknown cost, never zero.
            actual_known = base_known = False
            result["missing_usage_calls"] += len(missing)
            result["uncertain_calls"] += sum(r["state"] != "complete" for r in missing)
        # Plan reuse is valued only from recorded original planner usage, not guessed.
        for kind, _, factor in factors:
            if kind == "plan_cache" and factor["hit"]:
                result["plan_reuse"] += 1
                source = self.store.db.execute(
                    "SELECT data FROM usage_records WHERE request=? AND role='planner' ORDER BY stamp LIMIT 1",
                    (factor.get("source"),),
                ).fetchone()
                if source:
                    u = json.loads(source[0])["usage"]
                    stages["planner:"] = {
                        "known": bool(u.get("input_tokens") or u.get("output_tokens")),
                        "inputs": u.get("input_tokens", 0),
                        "outputs": u.get("output_tokens", 0),
                        "cached": u.get("cached_tokens", 0),
                        "writes": u.get("cache_write_tokens", 0),
                        "context": 0,
                    }
                    result["cache_tokens_saved_or_discounted"] += u.get("input_tokens", 0) + u.get("output_tokens", 0)
            if kind == "graph_cache":
                result["graph_cache_hits"] += factor["hits"]
                result["graph_cache_misses"] += factor["misses"]
        for stage in stages.values():
            result["baseline_input_tokens"] += stage["inputs"] + stage["context"]
            result["baseline_output_tokens"] += stage["outputs"]
            result["context_tokens_avoided"] += stage["context"]
            value = (
                cost(
                    baseline["price"],
                    stage["inputs"] + stage["context"],
                    stage["outputs"],
                    stage["cached"],
                    stage["writes"],
                )
                if baseline["price"] and stage["known"]
                else None
            )
            base_known &= value is not None
            base += value if value is not None else 0
        token_known = (
            bool(baseline["model"]) and all(s["known"] for s in stages.values()) and not result["missing_usage_calls"]
        )
        result["frontier_calls_baseline_estimated"] = len(stages) if baseline["frontier"] else 0
        result["frontier_calls_avoided"] = (
            result["frontier_calls_baseline_estimated"] - result["frontier_calls_actual"]
            if baseline["frontier"]
            else None
        )
        result["cache_tokens_saved_or_discounted"] += result["actual_cached_tokens"]
        result["paid_cloud_tokens_avoided"] = (
            result["baseline_input_tokens"]
            + result["baseline_output_tokens"]
            - result["actual_paid_input_tokens"]
            - result["actual_paid_output_tokens"]
            if token_known
            else None
        )
        result.update(
            actual_cost=str(actual) if actual_known else None,
            baseline_estimated_cost=str(base) if base_known else None,
            estimated_cost_saved=str(base - actual) if base_known and actual_known else None,
            currency="USD",
            baseline=baseline,
            baseline_model=baseline["model"],
            baseline_method=baseline["method"],
            baseline_confidence=baseline["confidence"],
            token_estimation_method=baseline["token_estimation_method"],
            calls=details,
            actual_cost_basis="Provider-reported tokens × price snapshot; API cost only, not an invoice",
            known_actual_cost=str(actual),
            reservation_usd=str(
                sum(
                    (
                        D(json.loads(r[0])["reservation"])
                        for r in self.store.db.execute("SELECT data FROM usage_records WHERE request=?", (request,))
                    ),
                    D(0),
                )
            ),
            attribution={
                "kind": "contributing factors, not additive subtotals",
                "model_routing": any(d["model"] != baseline["model"] for d in details),
                "local_execution": result["local_calls"],
                "context_optimization": result["context_tokens_avoided"],
                "provider_cache": result["actual_cached_tokens"],
                "plan_reuse": result["plan_reuse"],
            },
            local_compute_cost_estimate=None,
            repriced=reprice,
            partial=not actual_known or not base_known,
        )
        result["accounting_gaps"] = missing
        return derived(result)

    def finalize(self, receipt):
        request = receipt["request_id"]
        with self.store.transaction():
            row = self.store.db.execute("SELECT * FROM savings_runs WHERE request=?", (request,)).fetchone()
            if not row:
                return None
            if row["status"] == "final":
                return json.loads(row["data"])
            snapshot = self.calculate(request)
            snapshot.update(
                finalized=True, run_id=receipt["run_id"], request_id=request, finished_at=receipt["finished_at"]
            )
            receipt["economics"] = snapshot
            self.store.db.execute(
                "INSERT OR REPLACE INTO receipts VALUES(?,?,?,?)",
                (receipt["run_id"], request, receipt["finished_at"], json.dumps(receipt)),
            )
            self.store.db.execute(
                "UPDATE savings_runs SET status='final',run=?,data=?,updated=? WHERE request=?",
                (receipt["run_id"], json.dumps(snapshot), time.time(), request),
            )
            self.store.db.execute(
                "DELETE FROM savings_gaps WHERE call=? OR call IN (SELECT call FROM usage_records WHERE request=?)",
                (request, request),
            )
            for bucket, key in (("all", "*"), ("day", row["day"]), ("session", row["session"])):
                previous = self.store.db.execute(
                    "SELECT data FROM savings_totals WHERE bucket=? AND key=?", (bucket, key)
                ).fetchone()
                total = add_snapshot(json.loads(previous[0]) if previous else empty_total(), snapshot)
                self.store.db.execute(
                    "INSERT OR REPLACE INTO savings_totals VALUES(?,?,?)", (bucket, key, json.dumps(total))
                )
            self.changed()
        self.store.economics("observe_economics", receipt, snapshot)
        return snapshot

    def observe_economics(self, receipt, snapshot):
        if receipt.get("status") != "complete" or not receipt.get("validation") or not snapshot["baseline"]["price"]:
            return
        from .dna import observe

        # Verified repository completion only. Price evidence never bypasses quality gates.
        per_model = {}
        for call in snapshot["calls"]:
            if call["actual_cost"] is None or not (call["input_tokens"] or call["output_tokens"]):
                continue
            equivalent = cost(
                snapshot["baseline"]["price"], call["input_tokens"], call["output_tokens"], call["cached_tokens"]
            )
            if equivalent is not None and equivalent > 0:
                totals = per_model.setdefault(call["model"], [D(0), D(0)])
                totals[0] += D(call["actual_cost"])
                totals[1] += equivalent
        for model, (actual, baseline) in per_model.items():
            observe(self.store, model, "cost_efficiency", actual < baseline, source="verified_receipt_economics")

    def finish(self, request, status="complete"):
        row = self.store.db.execute("SELECT kind FROM savings_runs WHERE request=?", (request,)).fetchone()
        if not row:
            return None
        return self.finalize(
            {
                "schema_version": 1,
                "run_id": request,
                "request_id": request,
                "kind": row[0],
                "status": status,
                "finished_at": time.time(),
            }
        )

    def summary(self, period="today", session=None):
        total = empty_total()
        if period in {"all", "session"}:
            if period == "session" and session is None:
                latest = self.store.db.execute(
                    "SELECT session FROM (SELECT session,updated AS stamp FROM savings_runs UNION ALL SELECT session,stamp FROM savings_gaps) ORDER BY stamp DESC LIMIT 1"
                ).fetchone()
                session = latest[0] if latest else "default"
            rows = self.store.db.execute(
                "SELECT data FROM savings_totals WHERE bucket=? AND key=?",
                (period, "*" if period == "all" else session),
            )
        elif period in {"today", "7d", "30d"}:
            start = (
                datetime.now(timezone.utc).date() - timedelta(days={"today": 0, "7d": 6, "30d": 29}[period])
            ).isoformat()
            rows = self.store.db.execute(
                "SELECT data FROM savings_totals WHERE bucket='day' AND key>=? AND key<=?", (start, day())
            )
        elif period == "current":
            row = self.store.db.execute(
                "SELECT data,updated,request FROM savings_runs ORDER BY updated DESC LIMIT 1"
            ).fetchone()
            gap = self.store.db.execute("SELECT call,stamp FROM savings_gaps ORDER BY stamp DESC LIMIT 1").fetchone()
            # Pausing a failed group updates its timestamp without resolving
            # accounting. Its own durable gap still overrides that snapshot.
            own_gap = (
                self.store.db.execute("SELECT call,stamp FROM savings_gaps WHERE call=?", (row["request"],)).fetchone()
                if row
                else None
            )
            gap = own_gap or gap
            if gap is not None and (own_gap or not row or gap["stamp"] >= row["updated"]):
                self.add_gaps(total, 1)
                return {"period": period, "accounting_gap": True, "request_id": gap["call"], **total_view(total)}
            snapshot = json.loads(row[0]) if row else None
            return {"period": period, **(snapshot or total_view(total))}
        else:
            raise ValueError("Unknown savings period")
        for row in rows:
            item = json.loads(row[0])
            total["runs"] += item["runs"]
            for key in COUNTS:
                total[key] += item[key]
            for key in MONEY:
                total["known"][key] = str(D(total["known"][key]) + D(item["known"][key]))
            for key in total["missing"]:
                total["missing"][key] += item["missing"][key]
        condition, values = (
            ("session=?", (session,))
            if period == "session"
            else ("1=1", ())
            if period == "all"
            else ("day>=? AND day<=?", (start, day()))
        )
        gaps = self.store.db.execute("SELECT COUNT(*) FROM savings_gaps WHERE " + condition, values).fetchone()[0]
        self.add_gaps(total, gaps)
        result = {
            "period": period,
            "session": session if period == "session" else None,
            "timezone": "UTC",
            **total_view(total),
        }
        if not total["runs"] and not self.baseline()["model"]:
            result.update(
                baseline_estimated_cost=None,
                estimated_cost_saved=None,
                estimated_cost_saved_percent=None,
                paid_cloud_tokens_avoided=None,
            )
        return result

    @staticmethod
    def add_gaps(total, count):
        if count:
            total["missing_usage_calls"] += count
            for key in total["missing"]:
                total["missing"][key] += count

    def simulate(self, selected, inputs, outputs):
        model = next((m for m in self.settings.models if m.id == selected), None)
        baseline = self.baseline()
        actual = (
            D(0)
            if model and is_local(self.settings.providers[model.provider])
            else cost(price(model), inputs, outputs)
            if model
            else None
        )
        direct = cost(baseline["price"], inputs, outputs) if baseline["price"] else None

        # Illustrative workload range, not a prediction of the full compiled DAG.
        def bounds(value):
            return [str(value / D(2)), str(value * D(2))] if value is not None else None

        return {
            "currency": "USD",
            "actual_estimated_range": bounds(actual),
            "baseline_estimated_range": bounds(direct),
            "baseline": baseline,
            "workload": {"input_tokens": inputs, "output_tokens": outputs},
            "basis": "0.5–2 times a normalized single-stage workload; compilation may add stages, validation, repairs and control calls",
            "estimated_savings_range": [str(direct / D(2) - actual * 2), str(direct * 2 - actual / D(2))]
            if actual is not None and direct is not None
            else None,
        }

    def state(self, period=None):
        self.recover_abandoned()
        period = period or self.settings.savings.header_period
        active = []
        for row in self.store.db.execute("SELECT request,session,day,data FROM savings_runs WHERE status='active'"):
            data = json.loads(row["data"])
            if data:
                active.append(
                    {
                        "request_id": row["request"],
                        "session": row["session"],
                        "day": row["day"],
                        **{k: v for k, v in data.items() if k != "calls"},
                    }
                )
        latest = self.store.db.execute(
            "SELECT request,session,data,status FROM savings_runs ORDER BY updated DESC LIMIT 1"
        ).fetchone()
        current = (
            self.summary("current")
            if latest or self.store.db.execute("SELECT 1 FROM savings_gaps LIMIT 1").fetchone()
            else None
        )
        if current:
            current.pop("calls", None)
        header = self.summary(period)
        # Final totals and active estimates are distinct but use identical arithmetic.
        total = {k: header[k] for k in empty_total()} if "known" in header else None
        if total is not None and active:
            total = json.loads(json.dumps(total))
            start = (
                datetime.now(timezone.utc).date() - timedelta(days={"today": 0, "7d": 6, "30d": 29}.get(period, 0))
            ).isoformat()
            for snapshot in active:
                if (
                    period == "all"
                    or period == "session"
                    and snapshot["session"] == header.get("session")
                    or period in {"today", "7d", "30d"}
                    and start <= snapshot["day"] <= day()
                ):
                    add_snapshot(total, snapshot)
            header = {"period": period, **total_view(total)}
        return {
            "revision": self.revision(),
            "settings": self.settings.savings.model_dump(),
            "header": header,
            "today": self.summary("today"),
            "all": self.summary("all"),
            "current": current,
            "active": active,
            "current_status": "incomplete"
            if current and current.get("accounting_gap")
            else latest["status"]
            if latest
            else None,
            "baseline": self.baseline(),
            "error": self.last_error,
            "note": "Estimated API savings; local compute costs excluded. Dates use UTC. Provider cached tokens remain cloud tokens.",
        }


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is not evidence of death.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
