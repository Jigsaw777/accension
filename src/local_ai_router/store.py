"""SQLite WAL cache, traces, budget reservations and gradual quality estimates."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from .config import Settings
from .db_migrations import initialize_database


class BudgetExceeded(RuntimeError):
    pass


def cache_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        path = settings.state / "router.sqlite3"
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            initialize_database(self.db, path)
        except BaseException:
            self.db.close()
            raise

        from .observability import EventLog

        try:
            self.log = EventLog(settings)
        except BaseException:
            self.db.close()
            raise
        self.lock = threading.RLock()
        # In-process activity is separate from durable uncertain charges. A killed
        # gateway must not permanently prevent configuration after restart.
        self.active_calls = set()
        self.hot: OrderedDict = OrderedDict()
        self.hits = self.misses = 0
        self.trace_listener = None
        self.savings = None

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        self.db.close()
        self.log.close()

    def economics(self, method, *args, **kwargs):
        """Optional analytics must never change inference or repository outcomes."""
        if self.savings is None:
            return None
        try:
            return getattr(self.savings, method)(*args, **kwargs)
        except Exception as exc:
            self.savings.last_error = type(exc).__name__
            if method in {"reserve", "settle", "finalize", "finish"}:
                # Preserve failed accounting until a receipt materializes it.
                # A reserve failure inside an open group is reconciled by
                # calculate(); later-stage failures need a durable marker too.
                try:
                    with self.lock:
                        stamp_day = datetime.now(timezone.utc).date().isoformat()
                        if method == "reserve":
                            call, request, session = args[:3]
                            covered = self.db.execute(
                                "SELECT 1 FROM savings_runs WHERE request IN (?,?) AND status!='final'", (call, request)
                            ).fetchone()
                        elif method == "settle":
                            call = args[0]
                            ledger = self.db.execute("SELECT session,day FROM calls WHERE id=?", (call,)).fetchone()
                            session, stamp_day = ledger["session"], ledger["day"]
                            group = self.db.execute(
                                "SELECT s.request,s.status FROM usage_records u JOIN savings_runs s ON s.request=u.request WHERE u.call=?",
                                (call,),
                            ).fetchone()
                            covered = group and group["status"] == "final"
                            if group:
                                call = group["request"]
                        else:
                            call = args[0]["request_id"] if method == "finalize" else args[0]
                            group = self.db.execute(
                                "SELECT session,day,status FROM savings_runs WHERE request=?", (call,)
                            ).fetchone()
                            session, stamp_day = (group["session"], group["day"]) if group else ("default", stamp_day)
                            covered = group and group["status"] == "final"
                        if not covered:
                            self.db.execute(
                                "INSERT OR IGNORE INTO savings_gaps VALUES(?,?,?,?)",
                                (call, session, stamp_day, time.time()),
                            )
                            self.db.execute("UPDATE savings_revision SET value=value+1 WHERE id=1")
                except Exception:
                    pass  # A database outage must not take down inference.
            return None

    def metadata(self, key, value=None):
        with self.lock:
            if value is not None:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps(value)))
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def get(self, key: str):
        if not self.settings.cache.enabled:
            return None
        now = time.time()
        with self.lock:
            if key in self.hot:
                value, expires = self.hot.pop(key)
                if expires > now:
                    self.hot[key] = (value, expires)
                    self.hits += 1
                    return json.loads(value)
            row = self.db.execute("SELECT value, expires FROM cache WHERE key=? AND expires>?", (key, now)).fetchone()
            if row:
                self._hot(key, row[0], row[1])
                self.hits += 1
                return json.loads(row[0])
            self.misses += 1
            return None

    def _hot(self, key, value, expires):
        self.hot[key] = (value, expires)
        while len(self.hot) > self.settings.cache.lru_entries:
            self.hot.popitem(last=False)

    def put(self, key, value, ttl=None):
        if not self.settings.cache.enabled:
            return
        payload = json.dumps(value, default=str)
        expiry = time.time() + (ttl or self.settings.cache.ttl_seconds)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, payload, expiry))
            self.db.execute("DELETE FROM cache WHERE expires<?", (time.time(),))
            self.db.execute(
                "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires DESC LIMIT -1 OFFSET ?)",
                (self.settings.cache.max_entries,),
            )
            self._hot(key, payload, expiry)

    def cache_stats(self):
        return {
            "entries": self.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0],
            "process_hits": self.hits,
            "process_misses": self.misses,
        }

    def clear_cache(self):
        with self.lock:
            self.hot.clear()
            self.db.execute("DELETE FROM cache")

    def trace(self, request: str, stage: str, **data):
        # Call sites provide metadata only. Defense in depth removes secret-like keys/values.
        from .safety import redact

        self.log.emit("execution", stage, request_id=request, **{k: v for k, v in data.items() if k != "request_id"})
        with self.lock:
            self.db.execute(
                "INSERT INTO traces(request,stage,stamp,data) VALUES(?,?,?,?)",
                (request, stage, time.time(), json.dumps(redact(data), default=str)),
            )
        if self.trace_listener:
            self.trace_listener({"stage": stage, "request": request, **redact(data)})
        self.economics("trace", request, stage, data)

    def traces(self, request: str):
        return [
            {"stage": r["stage"], "timestamp": r["stamp"], **json.loads(r["data"])}
            for r in self.db.execute("SELECT * FROM traces WHERE request=? ORDER BY seq", (request,))
        ]

    def reserve(
        self,
        request,
        session,
        role,
        model,
        estimate,
        limit,
        task_id=None,
        *,
        occurred_at=None,
        provider_id=None,
        model_snapshot=None,
    ):
        if model_snapshot is not None:
            from .schema import CallModelSnapshot

            model_snapshot = CallModelSnapshot.model_validate(model_snapshot).model_dump_json()
        b = self.settings.budgets
        day = datetime.now(timezone.utc).date().isoformat()
        if estimate < 0 or not math.isfinite(estimate):
            raise BudgetExceeded("Unknown or invalid price")
        with self.transaction():

            def total(clause, values):
                return self.db.execute("SELECT COALESCE(SUM(cost),0) FROM calls WHERE " + clause, values).fetchone()[0]

            checks = [
                (total("request=?", (request,)), limit, "request"),
                (total("session=?", (session,)), b.session_budget, "session"),
                (total("day=?", (day,)), b.daily_hard_budget, "daily"),
            ]
            bucket = "planner" if role == "planner" else "verification" if role in ("reviewer", "arbiter") else "worker"
            roles = (
                ("planner",)
                if bucket == "planner"
                else ("reviewer", "arbiter")
                if bucket == "verification"
                else ("executor", "repair", "gateway")
            )
            spent = total("request=? AND role IN (" + ",".join("?" for _ in roles) + ")", (request, *roles))
            checks.append((spent, getattr(b, bucket + "_budget"), bucket))
            for used, cap, name in checks:
                if used + estimate > cap + 1e-10:
                    raise BudgetExceeded(f"{name} budget exceeded")
            if model.tier == 4:
                count = self.db.execute(
                    "SELECT COUNT(*) FROM calls WHERE request=? AND frontier=1", (request,)
                ).fetchone()[0]
                plans = self.db.execute(
                    "SELECT COUNT(*) FROM calls WHERE request=? AND frontier=1 AND role='planner'", (request,)
                ).fetchone()[0]
                if count >= b.max_frontier_calls or (role == "planner" and plans >= b.max_initial_frontier_plans):
                    raise BudgetExceeded("Frontier call limit reached")
            call = uuid4().hex
            self.db.execute(
                "INSERT INTO calls(id,request,session,day,role,model,frontier,cost,state,usage) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (call, request, session, day, role, model.id, int(model.tier == 4), estimate, "reserved", "{}"),
            )
            self.active_calls.add(call)
            self.db.execute(
                "UPDATE calls SET occurred_at=?,provider_id=?,model_snapshot_json=? WHERE id=?",
                (occurred_at, provider_id, model_snapshot, call),
            )
            if task_id:
                self.db.execute("INSERT INTO task_calls VALUES(?,?)", (call, task_id))
        self.economics("reserve", call, request, session, role, model, task_id, estimate)
        self.log.emit(
            "provider",
            "reserved",
            request_id=request,
            session_id=session,
            model=model.id,
            provider=model.provider,
            task_id=task_id,
        )
        return call

    def task_cost(self, task_id):
        return self.db.execute(
            "SELECT COALESCE(SUM(c.cost),0) FROM calls c JOIN task_calls t ON c.id=t.call_id WHERE t.task_id=?",
            (task_id,),
        ).fetchone()[0]

    def settle(
        self,
        call,
        cost,
        usage,
        latency,
        failed=False,
        *,
        usage_source=None,
        cost_source=None,
        reported_usage_fields=None,
    ):
        if usage_source is not None and usage_source not in {
            "provider_reported",
            "connector_reported",
            "estimated",
            "unknown",
            "legacy_unknown",
        }:
            raise ValueError("Invalid usage provenance")
        if cost_source is not None and cost_source not in {
            "priced_usage",
            "budget_estimate",
            "connector_reported",
            "known_zero_no_send",
            "unknown",
            "legacy_unknown",
        }:
            raise ValueError("Invalid cost provenance")
        if reported_usage_fields is not None and (
            len(reported_usage_fields) > 4
            or not set(reported_usage_fields)
            <= {"input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens"}
        ):
            raise ValueError("Invalid reported usage fields")
        # Uncertain/failed calls retain their reservation; a timeout may still be billable.
        with self.transaction():
            self.db.execute(
                "UPDATE calls SET cost=COALESCE(?,cost), state=?, usage=?, latency=? WHERE id=?",
                (cost, "uncertain" if failed else "complete", json.dumps(usage), latency, call),
            )
            self.db.execute(
                "UPDATE calls SET usage_source=COALESCE(?,usage_source),cost_source=COALESCE(?,cost_source),reported_usage_fields_json=COALESCE(?,reported_usage_fields_json) WHERE id=?",
                (
                    usage_source,
                    cost_source,
                    json.dumps(reported_usage_fields) if reported_usage_fields is not None else None,
                    call,
                ),
            )
            self.db.execute("UPDATE egress SET state=? WHERE call=?", ("uncertain" if failed else "sent", call))
        self.active_calls.discard(call)
        self.economics("settle", call, cost, usage, failed)
        row = self.db.execute("SELECT request,session,model FROM calls WHERE id=?", (call,)).fetchone()
        if row:
            self.log.emit(
                "provider",
                "settled",
                request_id=row["request"],
                session_id=row["session"],
                model=row["model"],
                status="uncertain" if failed else "complete",
                duration_ms=round(latency * 1000),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )

    def costs(self, request=None):
        rows = list(
            self.db.execute(
                "SELECT * FROM calls" + (" WHERE request=?" if request else ""), (request,) if request else ()
            )
        )
        day = datetime.now(timezone.utc).date().isoformat()
        today = sum(r["cost"] for r in rows if r["day"] == day)
        return {
            "estimated_usd": round(sum(r["cost"] for r in rows), 8),
            "today_usd": round(today, 8),
            "calls": len(rows),
            "frontier_calls": sum(r["frontier"] for r in rows),
            "uncertain_calls": sum(r["state"] != "complete" for r in rows),
            "soft_budget_exceeded": today >= self.settings.budgets.daily_soft_budget,
            "by_model": {
                m: round(sum(r["cost"] for r in rows if r["model"] == m), 8) for m in {r["model"] for r in rows}
            },
        }

    def success(self, model, family, passed, latency):
        with self.transaction():
            r = self.db.execute("SELECT * FROM profiles WHERE model=? AND family=?", (model, family)).fetchone()
            n, old, old_latency = (r["n"], r["success"], r["latency"]) if r else (0, 0.5, latency)
            self.db.execute(
                "INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?)",
                (model, family, n + 1, 0.1 * int(passed) + 0.9 * old, 0.1 * latency + 0.9 * old_latency),
            )

    def outcome(self, model, family, **metrics):
        from .safety import redact

        with self.lock:
            self.db.execute(
                "INSERT INTO outcomes(model,family,stamp,data) VALUES(?,?,?,?)",
                (model, family, time.time(), json.dumps(redact(metrics))),
            )

    def reset_profiles(self):
        with self.transaction():
            self.db.execute("DELETE FROM profiles")
            self.db.execute("DELETE FROM outcomes")
            self.db.execute("DELETE FROM benchmarks")
            self.db.execute("DELETE FROM dna")

    def models_page(self, provider=None, search=None, offset=0, limit=50):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Page size must be 1-200 and offset nonnegative")
        where, args = [], []
        if provider:
            where.append("provider=?")
            args.append(provider)
        if search:
            where.append(
                "(instr(lower(id),lower(?))>0 OR instr(lower(json_extract(data,'$.deployment_name')),lower(?))>0)"
            )
            args.extend([search, search])
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = self.db.execute("SELECT COUNT(*) FROM model_registry" + clause, args).fetchone()[0]
        rows = self.db.execute(
            "SELECT data FROM model_registry" + clause + " ORDER BY id LIMIT ? OFFSET ?", (*args, limit, offset)
        )
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def save_model(self, model):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO model_registry VALUES(?,?,?,?)",
                (model.id, model.provider, model.model_dump_json(), time.time()),
            )

    def registry(self):
        from .schema import Model

        return [
            Model.model_validate_json(row[0]) for row in self.db.execute("SELECT data FROM model_registry ORDER BY id")
        ]

    def provider_health(self, provider, status=None, cooldown=0, error_code=None):
        with self.lock:
            if status is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO provider_health VALUES(?,?,?,?,?)",
                    (provider, status, time.time() + cooldown, error_code, time.time()),
                )
            row = self.db.execute("SELECT * FROM provider_health WHERE provider=?", (provider,)).fetchone()
            result = dict(row) if row else {"provider": provider, "status": "UNKNOWN", "until": 0}
            if (
                result["status"] in {"RATE_LIMITED", "DEGRADED", "UNAVAILABLE"}
                and result.get("until", 0) <= time.time()
            ):
                result["status"] = "UNKNOWN"
            return result

    def quality(self, model, family, prior=None):
        prior = model.quality_priors.get(family, model.quality_priors.get("default", 0.8)) if prior is None else prior
        row = self.db.execute("SELECT * FROM profiles WHERE model=? AND family=?", (model.id, family)).fetchone()
        if not row:
            return prior
        weight = min(0.5, row["n"] / 100)
        return prior * (1 - weight) + row["success"] * weight

    def healthy(self, model):
        row = self.db.execute("SELECT until FROM health WHERE model=?", (model,)).fetchone()
        return not row or row[0] <= time.time()

    def health_result(self, model, ok):
        with self.transaction():
            row = self.db.execute("SELECT failures FROM health WHERE model=?", (model,)).fetchone()
            failures = 0 if ok else (row[0] if row else 0) + 1
            until = (
                time.time() + self.settings.routing.circuit_cooldown
                if failures >= self.settings.routing.circuit_failures
                else 0
            )
            self.db.execute("INSERT OR REPLACE INTO health VALUES(?,?,?)", (model, failures, until))
