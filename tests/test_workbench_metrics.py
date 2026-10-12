"""Measured facts deduplicate calls, preserve unknowns, and use specified arithmetic."""

import json

import pytest
from test_workbench_agents import HUMAN
from test_workbench_execution import execution, item, queue  # noqa: F401

from local_ai_router.providers import reported_usage_fields
from local_ai_router.schema import CallModelSnapshot
from local_ai_router.workbench.metrics import agent_metrics, microusd, reconcile_attempt
from local_ai_router.workbench.store import identity


@pytest.mark.parametrize("value,expected", [("0.0000005", 1), ("1.2345674", 1234567), ("0", 0)])
def test_money_round_half_up(value, expected):
    assert microusd(value) == expected


def test_reported_usage_absent_zero_and_partial_categories():
    assert reported_usage_fields({}) == []
    assert reported_usage_fields({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}) == [
        "input_tokens",
        "output_tokens",
    ]
    assert reported_usage_fields({"usage": {"prompt_tokens": 7, "prompt_tokens_details": {"cached_tokens": 0}}}) == [
        "input_tokens",
        "cached_tokens",
    ]
    assert reported_usage_fields({"usage": {"inputTokens": 4, "outputTokens": 2, "cacheReadInputTokens": 1}}) == [
        "input_tokens",
        "output_tokens",
        "cached_tokens",
    ]


async def test_attempt_facts_replay_provenance_and_frozen_identity(execution):  # noqa: F811
    runtime, ws, _, agent, _ = execution
    attempt_id = queue(execution, item(execution))
    row, contract = runtime.execution.contract(attempt_id)
    model = runtime.engine.settings.models[0]
    snapshot = CallModelSnapshot.model_validate(
        {key: getattr(model, key) for key in CallModelSnapshot.model_fields}
    ).model_dump()
    snapshot.update(input_price=1, output_price=2, cached_input_price=0.5)
    call = runtime.engine.store.reserve(
        contract.request_id,
        "facts",
        "executor",
        model,
        0.01,
        1,
        provider_id=model.provider,
        occurred_at=1000,
        model_snapshot=snapshot,
    )
    runtime.engine.store.settle(
        call,
        0.01,
        {"input_tokens": 100, "output_tokens": 50, "cached_tokens": 20},
        1,
        usage_source="provider_reported",
        reported_usage_fields=["input_tokens", "output_tokens", "cached_tokens"],
        cost_source="priced_usage",
    )
    legacy = runtime.engine.store.reserve(contract.request_id, "facts", "repair", model, 0.001, 1)
    runtime.engine.store.settle(legacy, 0.001, {}, 1)
    db = runtime.service.store.db
    db.execute("UPDATE wb_attempts SET state='SUCCEEDED',started_at=2000,finished_at=3000 WHERE id=?", (attempt_id,))
    for _ in range(3):
        reconcile_attempt(runtime, attempt_id)
    assert db.execute("SELECT COUNT(*) FROM wb_attempt_facts").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM wb_attempt_call_facts").fetchone()[0] == 2
    facts = db.execute("SELECT * FROM wb_attempt_facts WHERE attempt_id=?", (attempt_id,)).fetchone()
    assert facts["priced_usage_cost_microusd"] == 190 and facts["unknown_call_count"] == 1
    assert facts["agent_name"] == agent["name"] and facts["agent_revision"] == 1
    db.execute("UPDATE wb_agent_definitions SET name='New identity' WHERE id=?", (agent["id"],))
    runtime.engine.settings.models[0].provider = "removed"
    reconcile_attempt(runtime, attempt_id)
    assert (
        db.execute("SELECT agent_name FROM wb_attempt_facts WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
        == agent["name"]
    )
    measured = agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=4000)
    assert measured["totals"]["priced_usage_cost_microusd"] == 190
    assert measured["totals"]["success_rate"] == 1 and measured["totals"]["p95_duration_ms"] == 1000
    assert measured["totals"]["mean_queue_ms"] is None


async def test_nearest_rank_nine_samples_and_retry_count(execution):  # noqa: F811
    from uuid import UUID

    runtime, ws, _, agent, _ = execution
    attempt_id = queue(execution, item(execution))
    db = runtime.service.store.db
    _, contract = runtime.execution.contract(attempt_id)
    source = dict(db.execute("SELECT * FROM wb_attempts WHERE id=?", (attempt_id,)).fetchone())
    for index in range(1, 10):
        value = {
            **source,
            "id": attempt_id if index == 1 else str(UUID(int=100 + index, version=4)),
            "ordinal": index,
            "state": "SUCCEEDED" if index != 2 else "FAILED",
            "started_at": 1000,
            "finished_at": 1000 + index * 100,
            "request_id": "facts" + str(index),
            "run_id": None,
        }
        raw = {**json.loads(source["contract_json"]), "attempt_id": value["id"], "request_id": value["request_id"]}
        from local_ai_router.workbench.service import payload_hash

        value.update(contract_json=json.dumps(raw), contract_hash=payload_hash(raw))
        cols = tuple(value)
        if index == 1:
            db.execute(
                "UPDATE wb_attempts SET " + ",".join(col + "=?" for col in cols) + " WHERE id=?",
                [*value.values(), attempt_id],
            )
        else:
            db.execute(
                "INSERT INTO wb_attempts(" + ",".join(cols) + ") VALUES(" + ",".join("?" for _ in cols) + ")",
                tuple(value.values()),
            )
        reconcile_attempt(runtime, value["id"])
    totals = agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=3000)["totals"]
    assert totals["duration_samples"] == 9
    assert totals["p50_duration_ms"] == 500 and totals["p95_duration_ms"] == 900
    assert totals["retries"] == 8
    assert totals["first_attempt_denominator"] == totals["first_attempt_successes"] == 1


async def test_metrics_current_load_queue_percentiles_and_kind(execution):  # noqa: F811
    runtime, ws, _, agent, _ = execution
    attempt = queue(execution, item(execution))
    runtime.service.store.db.execute("UPDATE wb_attempts SET state='QUEUED' WHERE id=?", (attempt,))
    current = agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=3000)
    assert current["current_load"]["load"] == 1 and current["current_load"]["queued"] == 1
    assert current["totals"]["attempts"] == 0 and current["totals"]["p95_queue_ms"] is None
    db = runtime.service.store.db
    db.execute(
        "UPDATE wb_attempts SET state='SUCCEEDED',queued_at=900,started_at=1000,finished_at=2000 WHERE id=?", (attempt,)
    )
    reconcile_attempt(runtime, attempt)
    result = agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=3000, execution_kind="ENGINE")
    assert result["totals"]["queue_samples"] == 1
    assert result["totals"]["p50_queue_ms"] == result["totals"]["p95_queue_ms"] == 100
    assert result["coverage"]["duration_samples"] == 1
    manual = agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=3000, execution_kind="MANUAL")
    assert manual["totals"]["attempts"] == manual["totals"]["retries"] == manual["current_load"]["load"] == 0
    with pytest.raises(Exception, match="window"):
        agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=3000, execution_kind="SQL")


@pytest.mark.parametrize(
    "state,queued,running",
    [
        ("QUEUED", 1, 0),
        ("CLAIMED", 0, 0),
        ("RUNNING", 0, 1),
        ("VERIFYING", 0, 1),
        ("RECONCILING", 0, 0),
        ("SUCCEEDED", 0, 0),
    ],
)
async def test_current_metrics_count_only_queued_and_running_states(execution, state, queued, running):  # noqa: F811
    runtime, ws, _, agent, _ = execution
    attempt_id = queue(execution, item(execution))
    runtime.service.store.db.execute("UPDATE wb_attempts SET state=? WHERE id=?", (state, attempt_id))
    result = agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=3000)
    assert result["current_load"]["queued"] == queued
    assert result["current_load"]["running"] == running


async def test_10k_metrics_bounded_queries_latency_and_fanout(execution, tmp_path):  # noqa: F811
    import time
    from uuid import UUID

    runtime, ws, _, agent, _ = execution
    attempt_id = queue(execution, item(execution))
    reconcile_attempt(runtime, attempt_id)
    db = runtime.service.store.db
    attempt = dict(db.execute("SELECT * FROM wb_attempts WHERE id=?", (attempt_id,)).fetchone())
    fact = dict(db.execute("SELECT * FROM wb_attempt_facts WHERE attempt_id=?", (attempt_id,)).fetchone())
    with runtime.engine.store.transaction():
        for index in range(1, 10001):
            key = str(UUID(int=100000 + index, version=4))
            row = {
                **attempt,
                "id": key,
                "ordinal": index + 1,
                "request_id": "scale" + str(index),
                "state": "SUCCEEDED",
                "started_at": 1000,
                "finished_at": 2000,
            }
            db.execute(
                "INSERT INTO wb_attempts(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")",
                tuple(row.values()),
            )
            values = {
                **fact,
                "attempt_id": key,
                "terminal_state": "SUCCEEDED",
                "started_at": 1000,
                "finished_at": 2000,
                "priced_usage_cost_microusd": 7,
            }
            db.execute(
                "INSERT INTO wb_attempt_facts(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")",
                tuple(values.values()),
            )
    elapsed, traces = [], []
    for _ in range(20):
        statements = []
        db.set_trace_callback(statements.append)
        start = time.perf_counter()
        result = agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=3000)
        elapsed.append((time.perf_counter() - start) * 1000)
        db.set_trace_callback(None)
        traces.append(len([s for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH"))]))
    p95 = sorted(elapsed)[18]
    evidence = {"attempts": 10000, "queries": traces, "latency_ms": elapsed, "p95_ms": p95}
    (tmp_path / "metrics-budget.json").write_text(json.dumps(evidence, indent=2))
    assert result["totals"]["attempts"] == 10000 and result["totals"]["priced_usage_cost_microusd"] == 70000
    assert max(traces) <= 8 and p95 <= 250 and len(result["buckets"]) <= 93


async def test_metric_backfill_progress_survives_uncertain_first_attempt(execution):  # noqa: F811
    from local_ai_router.workbench.metrics import backfill_metrics

    runtime, _, _, _, _ = execution
    first = queue(execution, item(execution))
    runtime.service.store.db.execute(
        "UPDATE wb_assignments SET status='RELEASED' WHERE id=(SELECT assignment_id FROM wb_attempts WHERE id=?)",
        (first,),
    )
    second = queue(execution, item(execution))
    for attempt_id in (first, second):
        _, contract = runtime.execution.contract(attempt_id)
        runtime.engine.store.reserve(
            contract.request_id, "uncertain", "executor", runtime.engine.settings.models[0], 0.001, 1
        )
    db = runtime.service.store.db
    db.execute("DELETE FROM wb_attempt_facts")
    assert backfill_metrics(runtime, limit=1) == 1
    saved = db.execute("SELECT value FROM metadata WHERE key='wb_metrics_backfill'").fetchone()[0]
    assert json.loads(saved) in {first, second}
    assert backfill_metrics(runtime, limit=1) == 1
    assert db.execute("SELECT COUNT(*) FROM wb_attempt_facts").fetchone()[0] == 2
    assert backfill_metrics(runtime, limit=1) == 0
    assert json.loads(db.execute("SELECT value FROM metadata WHERE key='wb_metrics_backfill'").fetchone()[0]) == ""


async def test_metrics_delivery_review_project_and_tokens_use_exact_attempt(execution):  # noqa: F811
    runtime, ws, project, agent, _ = execution
    attempt = queue(execution, item(execution))
    db = runtime.service.store.db
    db.execute("UPDATE wb_attempts SET state='SUCCEEDED',started_at=1000,finished_at=2000 WHERE id=?", (attempt,))
    reconcile_attempt(runtime, attempt)
    _, contract = runtime.execution.contract(attempt)
    call = runtime.engine.store.reserve(
        contract.request_id, "reported", "executor", runtime.engine.settings.models[0], 0.001, 1
    )
    runtime.engine.store.settle(
        call,
        0.001,
        {"input_tokens": 12},
        1,
        usage_source="provider_reported",
        reported_usage_fields=["input_tokens"],
        cost_source="budget_estimate",
    )
    reconcile_attempt(runtime, attempt)
    db.execute(
        "INSERT INTO wb_approvals(id,workspace_id,item_id,attempt_id,subject_kind,contract_revision,evidence_hash,decision,actor_type,actor_id,reason,created_at) VALUES(?,?,?,?,'COMPLETION',1,?,'APPROVE','HUMAN','fixture','verified fixture',2500)",
        (identity(), ws.id, contract.item_id, attempt, "a" * 64),
    )
    totals = agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=3000, project_id=project["id"])["totals"]
    assert totals["delivered_tickets"] == 1 and totals["human_review_denominator"] == 1
    assert totals["reported_tokens"] == {
        "input_tokens": 12,
        "output_tokens": None,
        "cached_tokens": None,
        "cache_write_tokens": None,
    }
    other = runtime.service.create(
        "project", {"command_id": identity(), "workspace_id": ws.id, "name": "Excluded", "key": "EXCLUDED"}, HUMAN
    ).body
    excluded = agent_metrics(runtime, ws.id, agent["id"], HUMAN, start=1, end=3000, project_id=other["id"])["totals"]
    assert excluded["attempts"] == excluded["delivered_tickets"] == excluded["human_review_denominator"] == 0


async def test_settlement_rolls_back_provenance_and_budget_together(execution):  # noqa: F811
    runtime, _, _, _, _ = execution
    attempt = queue(execution, item(execution))
    _, contract = runtime.execution.contract(attempt)
    store = runtime.engine.store
    call = store.reserve(contract.request_id, "atomic", "executor", runtime.engine.settings.models[0], 0.02, 1)
    store.db.execute(
        "CREATE TEMP TRIGGER abort_provenance BEFORE UPDATE OF usage_source ON calls BEGIN SELECT RAISE(ABORT, 'synthetic settlement crash'); END"
    )
    with pytest.raises(Exception, match="synthetic settlement crash"):
        store.settle(call, 0.001, {}, 1, usage_source="provider_reported", reported_usage_fields=[])
    row = store.db.execute("SELECT * FROM calls WHERE id=?", (call,)).fetchone()
    assert row["cost"] == 0.02 and row["state"] != "complete" and call in store.active_calls
    store.db.execute("DROP TRIGGER abort_provenance")
    store.settle(call, 0.001, {}, 1, usage_source="provider_reported", reported_usage_fields=[])
    assert store.db.execute("SELECT state FROM calls WHERE id=?", (call,)).fetchone()[0] == "complete"
