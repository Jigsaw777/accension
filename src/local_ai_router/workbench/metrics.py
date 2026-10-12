"""Idempotent measured attempt facts; this projection never changes budget authority."""

import json
from contextlib import nullcontext
from decimal import ROUND_HALF_UP, Decimal

from ..providers import estimate_cost
from ..schema import CallModelSnapshot, ModelDescriptor, Usage
from .policy import authorize
from .store import WorkbenchError, now

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED", "RECONCILING"}


def microusd(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Cost must be finite and nonnegative")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def reconcile_attempt(runtime, attempt_id):
    store = runtime.service.store
    with store.router.lock, nullcontext() if store.db.in_transaction else store.router.transaction():
        row, contract = runtime.execution.contract(attempt_id)
        calls = store.db.execute("SELECT * FROM calls WHERE request=? ORDER BY id", (contract.request_id,)).fetchall()
        totals = {
            "priced_usage_cost_microusd": None,
            "budget_estimate_microusd": None,
            "connector_reported_cost_microusd": None,
        }
        unknown = uncertain = 0
        for call in calls:
            data = {
                "attempt_id": attempt_id,
                "call_id": call["id"],
                "model_id": call["model"],
                "provider_id": call["provider_id"],
                "role": call["role"],
                "usage_source": call["usage_source"],
                "cost_source": call["cost_source"],
                "reported_usage_json": None,
                **{key: None for key in totals},
            }
            usage = Usage.model_validate_json(call["usage"])
            fields = json.loads(call["reported_usage_fields_json"] or "[]")
            if (
                not isinstance(fields, list)
                or len(fields) > 4
                or any(
                    field not in {"input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens"}
                    for field in fields
                )
            ):
                raise WorkbenchError("CORRUPT_DATA", "Invalid call usage provenance", 500)
            if call["usage_source"] in {"provider_reported", "connector_reported"}:
                data["reported_usage_json"] = json.dumps({field: getattr(usage, field) for field in fields})
            if call["cost_source"] == "priced_usage" and call["model_snapshot_json"]:
                snapshot = CallModelSnapshot.model_validate_json(call["model_snapshot_json"])
                if (
                    {"input_tokens", "output_tokens"} <= set(fields)
                    and snapshot.input_price is not None
                    and snapshot.output_price is not None
                ):
                    priced = ModelDescriptor(**snapshot.model_dump())
                    data["priced_usage_cost_microusd"] = microusd(
                        estimate_cost(
                            priced,
                            usage.input_tokens,
                            usage.output_tokens,
                            usage.cached_tokens,
                            usage.cache_write_tokens,
                        )
                    )
                else:
                    unknown += 1
            elif call["cost_source"] == "known_zero_no_send":
                data["priced_usage_cost_microusd"] = 0
            elif call["cost_source"] == "budget_estimate":
                data["budget_estimate_microusd"] = microusd(call["cost"])
                unknown += 1
            elif call["cost_source"] == "connector_reported":
                data["connector_reported_cost_microusd"] = microusd(call["cost"])
            else:
                unknown += 1
            uncertain += call["state"] != "complete"
            for key in totals:
                if data[key] is not None:
                    totals[key] = (totals[key] or 0) + data[key]
            columns = tuple(data)
            store.db.execute(
                "INSERT INTO wb_attempt_call_facts("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ") ON CONFLICT(attempt_id,call_id) DO UPDATE SET "
                + ",".join(key + "=excluded." + key for key in columns[2:]),
                tuple(data.values()),
            )
        data = {
            "attempt_id": attempt_id,
            "assignment_id": contract.assignment_id,
            "agent_definition_id": contract.agent_definition_id,
            "agent_revision": contract.agent_definition_revision,
            "agent_name": contract.agent_name or "Unknown historical agent",
            "model_bindings_json": contract.model_bindings.model_dump_json(),
            "workspace_id": contract.workspace_id,
            "project_id": contract.project_id,
            "item_id": contract.item_id,
            "adapter": "engine",
            "terminal_state": row["state"],
            "enqueued_at": row["queued_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            **totals,
            "unknown_call_count": unknown,
            "uncertain_call_count": uncertain,
            "verified_outcome": int(row["state"] == "SUCCEEDED") if row["state"] in {"SUCCEEDED", "FAILED"} else None,
            "metric_version": 1,
            "updated_at": now(),
        }
        columns = tuple(data)
        store.db.execute(
            "INSERT INTO wb_attempt_facts("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ") ON CONFLICT(attempt_id) DO UPDATE SET "
            + ",".join(key + "=excluded." + key for key in columns[1:]),
            tuple(data.values()),
        )


def backfill_metrics(runtime, *, limit=200):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Metric backfill limit must be 1..200")
    store = runtime.service.store
    with store.router.transaction():
        saved = store.db.execute("SELECT value FROM metadata WHERE key='wb_metrics_backfill'").fetchone()
        after = json.loads(saved[0]) if saved else ""
        if not isinstance(after, str):
            raise WorkbenchError("CORRUPT_DATA", "Invalid metric backfill checkpoint", 500)
        rows = store.db.execute(
            "SELECT a.id FROM wb_attempts a LEFT JOIN wb_attempt_facts f ON f.attempt_id=a.id WHERE a.id>? AND (f.attempt_id IS NULL OR f.terminal_state<>a.state OR f.finished_at IS NOT a.finished_at OR EXISTS(SELECT 1 FROM calls c WHERE c.request=a.request_id AND (c.state<>'complete' OR NOT EXISTS(SELECT 1 FROM wb_attempt_call_facts cf WHERE cf.attempt_id=a.id AND cf.call_id=c.id)))) ORDER BY a.id LIMIT ?",
            (after, limit),
        ).fetchall()
        for row in rows:
            reconcile_attempt(runtime, row[0])
        store.db.execute(
            "INSERT OR REPLACE INTO metadata VALUES('wb_metrics_backfill',?)",
            (json.dumps(rows[-1][0] if rows else ""),),
        )
        return len(rows)


def agent_metrics(
    runtime, workspace_id, agent_id, principal, *, start, end, bucket="day", project_id=None, execution_kind=None
):
    authorize(principal, workspace_id, "workspace.read")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end
        or end - start > 90 * 86_400_000
        or bucket not in {"day", "week"}
        or execution_kind not in {None, "ENGINE", "MANUAL"}
    ):
        raise WorkbenchError("INVALID_WINDOW", "Metrics require a window of at most 90 days", 422)
    store = runtime.service.store
    clauses = ["workspace_id=?", "agent_definition_id=?", "finished_at>=?", "finished_at<?"]
    values = [workspace_id, agent_id, start, end]
    if project_id:
        clauses.append("project_id=?")
        values.append(project_id)
    if execution_kind == "MANUAL":
        clauses.append("adapter='manual'")
    where = " AND ".join(clauses)
    with store.router.lock:
        active = store.db.execute(
            "SELECT (EXISTS(SELECT 1 FROM wb_agent_workspaces WHERE workspace_id=? AND agent_id=?) OR EXISTS(SELECT 1 FROM wb_attempt_facts WHERE workspace_id=? AND agent_definition_id=?)) AS known_agent,"
            "(SELECT concurrency_limit FROM wb_agent_definitions WHERE id=?) AS capacity,"
            "(SELECT COUNT(*) FROM wb_assignments a JOIN wb_work_items w ON w.id=a.item_id WHERE a.workspace_id=? AND a.agent_definition_id=? AND a.status IN ('RESERVED','ACTIVE')"
            + (" AND w.project_id=?" if project_id else "")
            + (" AND w.execution_kind=?" if execution_kind else "")
            + ") AS load,"
            "(SELECT COUNT(*) FROM wb_attempts a JOIN wb_assignments s ON s.id=a.assignment_id WHERE a.workspace_id=? AND s.agent_definition_id=? AND a.state='QUEUED'"
            + (
                " AND EXISTS(SELECT 1 FROM wb_work_items w WHERE w.id=a.item_id AND w.project_id=?)"
                if project_id
                else ""
            )
            + (" AND 0" if execution_kind == "MANUAL" else "")
            + ") AS queued,"
            "(SELECT COUNT(*) FROM wb_attempts a JOIN wb_assignments s ON s.id=a.assignment_id WHERE a.workspace_id=? AND s.agent_definition_id=? AND a.state IN ('RUNNING','VERIFYING')"
            + (
                " AND EXISTS(SELECT 1 FROM wb_work_items w WHERE w.id=a.item_id AND w.project_id=?)"
                if project_id
                else ""
            )
            + (" AND 0" if execution_kind == "MANUAL" else "")
            + ") AS running"
            + (
                ",EXISTS(SELECT 1 FROM wb_projects WHERE workspace_id=? AND id=?) AS valid_project"
                if project_id
                else ""
            ),
            [
                workspace_id,
                agent_id,
                workspace_id,
                agent_id,
                agent_id,
                workspace_id,
                agent_id,
                *([project_id] if project_id else []),
                *([execution_kind] if execution_kind else []),
                workspace_id,
                agent_id,
                *([project_id] if project_id else []),
                workspace_id,
                agent_id,
                *([project_id] if project_id else []),
                *([workspace_id, project_id] if project_id else []),
            ],
        ).fetchone()
        if not active["known_agent"]:
            raise WorkbenchError("NOT_FOUND", "Agent not found", 404)
        if project_id and not active["valid_project"]:
            raise WorkbenchError("INVALID_REFERENCE", "Project does not belong to workspace")
        summary = dict(
            store.db.execute(
                "SELECT COUNT(*) AS attempts,SUM(terminal_state='SUCCEEDED') AS succeeded,SUM(terminal_state='FAILED') AS failed,SUM(terminal_state='CANCELLED') AS cancelled,SUM(terminal_state='INTERRUPTED') AS interrupted,SUM(terminal_state='RECONCILING') AS reconciling,SUM(priced_usage_cost_microusd) AS priced_usage_cost_microusd,SUM(budget_estimate_microusd) AS budget_estimate_microusd,SUM(connector_reported_cost_microusd) AS connector_reported_cost_microusd,COALESCE(SUM(unknown_call_count),0) AS unknown_calls,COALESCE(SUM(uncertain_call_count),0) AS uncertain_calls,AVG(CASE WHEN enqueued_at IS NOT NULL AND started_at>=enqueued_at THEN started_at-enqueued_at END) AS mean_queue_ms FROM wb_attempt_facts WHERE "
                + where,
                values,
            ).fetchone()
        )
        durations = store.db.execute(
            "WITH samples AS (SELECT finished_at-started_at AS duration,CASE WHEN enqueued_at IS NOT NULL AND started_at>=enqueued_at THEN started_at-enqueued_at END AS queue FROM wb_attempt_facts WHERE "
            + where
            + " AND terminal_state IN ('SUCCEEDED','FAILED','CANCELLED','INTERRUPTED') AND started_at IS NOT NULL AND finished_at>=started_at), ranked AS (SELECT duration,queue,ROW_NUMBER() OVER(ORDER BY duration) AS rank,COUNT(*) OVER() AS n,ROW_NUMBER() OVER(ORDER BY queue)-SUM(queue IS NULL) OVER() AS queue_rank,COUNT(queue) OVER() AS queue_n FROM samples) SELECT COUNT(*) AS samples,MAX(CASE WHEN rank=(n+1)/2 THEN duration END) AS p50,MAX(CASE WHEN rank=(n*95+99)/100 THEN duration END) AS p95,COUNT(queue) AS queue_samples,MAX(CASE WHEN queue_rank=(queue_n+1)/2 THEN queue END) AS p50_queue,MAX(CASE WHEN queue_rank=(queue_n*95+99)/100 THEN queue END) AS p95_queue FROM ranked",
            values,
        ).fetchone()
        summary.update(
            duration_samples=durations["samples"],
            p50_duration_ms=durations["p50"],
            p95_duration_ms=durations["p95"],
            queue_samples=durations["queue_samples"],
            p50_queue_ms=durations["p50_queue"],
            p95_queue_ms=durations["p95_queue"],
        )
        for key in ("succeeded", "failed", "cancelled", "interrupted", "reconciling"):
            summary[key] = summary[key] or 0
        denominator = summary["succeeded"] + summary["failed"]
        summary.update(
            success_denominator=denominator, success_rate=summary["succeeded"] / denominator if denominator else None
        )
        stride = 86_400_000 if bucket == "day" else 7 * 86_400_000
        rows = store.db.execute(
            "SELECT (finished_at/?)*? AS bucket_start,COUNT(*) AS attempts,SUM(terminal_state='SUCCEEDED') AS succeeded,SUM(terminal_state='FAILED') AS failed,SUM(priced_usage_cost_microusd) AS priced_usage_cost_microusd,AVG(CASE WHEN started_at IS NOT NULL AND finished_at>=started_at THEN finished_at-started_at END) AS mean_duration_ms FROM wb_attempt_facts WHERE "
            + where
            + " GROUP BY bucket_start ORDER BY bucket_start",
            [stride, stride, *values],
        ).fetchall()
        buckets = {row["bucket_start"]: dict(row) for row in rows}
        series = [
            buckets.get(
                stamp,
                {
                    "bucket_start": stamp,
                    "attempts": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "priced_usage_cost_microusd": None,
                    "mean_duration_ms": None,
                },
            )
            for stamp in range(start // stride * stride, end, stride)
        ]
        retry = store.db.execute(
            "SELECT COUNT(*) FROM wb_attempt_facts f JOIN wb_attempts a ON a.id=f.attempt_id WHERE f.workspace_id=? AND f.agent_definition_id=? AND a.ordinal>1 AND a.started_at<? AND EXISTS(SELECT 1 FROM wb_attempts first WHERE first.workspace_id=f.workspace_id AND first.item_id=f.item_id AND first.ordinal=1 AND first.started_at>=? AND first.started_at<?)"
            + (" AND f.project_id=?" if project_id else "")
            + (" AND f.adapter='manual'" if execution_kind == "MANUAL" else ""),
            [workspace_id, agent_id, end, start, end, *([project_id] if project_id else [])],
        ).fetchone()[0]
        summary["retries"] = retry
        project_filter = (" AND f.project_id=?" if project_id else "") + (
            " AND f.adapter='manual'" if execution_kind == "MANUAL" else ""
        )
        scope_values = [workspace_id, agent_id, start, end, *([project_id] if project_id else [])]
        subqueries = [
            (
                "delivered_tickets",
                "COUNT(DISTINCT p.item_id)",
                "wb_approvals p JOIN wb_attempt_facts f ON f.attempt_id=p.attempt_id",
                "p.created_at",
                "p.subject_kind='COMPLETION' AND p.decision='APPROVE'",
            ),
            (
                "first_attempt_denominator",
                "COUNT(*)",
                "wb_attempts a JOIN wb_attempt_facts f ON f.attempt_id=a.id",
                "a.started_at",
                "a.ordinal=1 AND a.state IN ('SUCCEEDED','FAILED')",
            ),
            (
                "first_attempt_successes",
                "COUNT(*)",
                "wb_attempts a JOIN wb_attempt_facts f ON f.attempt_id=a.id",
                "a.started_at",
                "a.ordinal=1 AND a.state='SUCCEEDED'",
            ),
            (
                "validation_events",
                "COUNT(*)",
                "wb_verifications v JOIN wb_attempt_facts f ON f.attempt_id=v.attempt_id",
                "v.created_at",
                "v.kind='ENGINE_VALIDATION' AND v.status IN ('PASSED','FAILED')",
            ),
            (
                "passed_validation_events",
                "COUNT(*)",
                "wb_verifications v JOIN wb_attempt_facts f ON f.attempt_id=v.attempt_id",
                "v.created_at",
                "v.kind='ENGINE_VALIDATION' AND v.status='PASSED'",
            ),
            (
                "human_accepted",
                "COUNT(*)",
                "wb_approvals p JOIN wb_attempt_facts f ON f.attempt_id=p.attempt_id",
                "p.created_at",
                "p.subject_kind='COMPLETION' AND p.actor_type='HUMAN' AND p.decision='APPROVE'",
            ),
            (
                "human_rejected",
                "COUNT(*)",
                "wb_approvals p JOIN wb_attempt_facts f ON f.attempt_id=p.attempt_id",
                "p.created_at",
                "p.subject_kind='COMPLETION' AND p.actor_type='HUMAN' AND p.decision='REJECT'",
            ),
            (
                "ai_accepted",
                "COUNT(*)",
                "wb_verifications v JOIN wb_attempt_facts f ON f.attempt_id=v.attempt_id",
                "v.created_at",
                "v.kind='AI_REVIEW' AND v.status='ACCEPTED'",
            ),
            (
                "ai_rejected",
                "COUNT(*)",
                "wb_verifications v JOIN wb_attempt_facts f ON f.attempt_id=v.attempt_id",
                "v.created_at",
                "v.kind='AI_REVIEW' AND v.status='REJECTED'",
            ),
        ]
        outcome = store.db.execute(
            "SELECT "
            + ",".join(
                "(SELECT "
                + aggregate
                + " FROM "
                + source.replace(" JOIN wb_attempt_facts", " CROSS JOIN wb_attempt_facts")
                + " WHERE f.workspace_id=? AND f.agent_definition_id=? AND "
                + stamp
                + ">=? AND "
                + stamp
                + "<? AND "
                + condition
                + project_filter
                + ") AS "
                + name
                for name, aggregate, source, stamp, condition in subqueries
            ),
            scope_values * len(subqueries),
        ).fetchone()
        summary.update(dict(outcome))
        for role in ("human", "ai"):
            n = summary[role + "_accepted"] + summary[role + "_rejected"]
            summary[role + "_review_denominator"] = n
            summary[role + "_review_acceptance_rate"] = summary[role + "_accepted"] / n if n else None
        tokens = store.db.execute(
            "SELECT "
            + ",".join(
                "SUM(json_extract(c.reported_usage_json,'$." + field + "')) AS " + field
                for field in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")
            )
            + " FROM wb_attempt_call_facts c JOIN wb_attempt_facts f ON f.attempt_id=c.attempt_id WHERE "
            + " AND ".join("f." + clause for clause in clauses),
            values,
        ).fetchone()
        summary["reported_tokens"] = dict(tokens)
        summary["first_attempt_success_rate"] = (
            summary["first_attempt_successes"] / summary["first_attempt_denominator"]
            if summary["first_attempt_denominator"]
            else None
        )
        summary["validation_pass_rate"] = (
            summary["passed_validation_events"] / summary["validation_events"] if summary["validation_events"] else None
        )
        sequence = runtime.journal.sequence()
    return {
        "totals": summary,
        "current_load": {
            "load": active["load"],
            "capacity": active["capacity"],
            "queued": active["queued"],
            "running": active["running"],
            "observed_at": now(),
        },
        "coverage": {
            "duration_samples": durations["samples"],
            "queue_samples": durations["queue_samples"],
            "unknown_calls": summary["unknown_calls"],
            "uncertain_calls": summary["uncertain_calls"],
        },
        "buckets": series,
        "metric_version": 1,
        "as_of_sequence": sequence,
        "window": {
            "from": start,
            "to": end,
            "bucket": bucket,
            "execution_kind": execution_kind,
            "cohort": "terminal outcomes by finished_at; retries by first attempt start",
        },
        "cost_label": "Usage-based estimate, not an invoice",
        "exclusions": [
            "cancelled/interrupted/reconciling excluded from success rate",
            "legacy missing times and usage remain unknown",
            "planning calls excluded",
        ],
    }
