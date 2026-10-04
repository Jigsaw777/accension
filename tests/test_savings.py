import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal as D

import pytest

from local_ai_router.engine import Engine
from local_ai_router.savings import cost
from local_ai_router.schema import Model, Provider, Request


def configure(settings):
    settings.providers["cloud"] = Provider(kind="mock", local=False)
    settings.models += [
        Model(
            id="baseline",
            provider="cloud",
            deployment_name="baseline",
            input_price=10,
            output_price=20,
            tier=4,
            quality_priors={"default": 0.99},
        ),
        Model(
            id="cheap",
            provider="cloud",
            deployment_name="cheap",
            input_price=1,
            output_price=2,
            cached_input_price=0.1,
            cache_write_price=2,
            quality_priors={"default": 0.99},
        ),
    ]
    settings.savings.baseline_model = "baseline"
    return settings


def record(
    engine,
    request,
    model="mock-worker",
    inputs=1000,
    outputs=100,
    cached=0,
    writes=0,
    task="edit",
    role="executor",
    failed=False,
):
    m = next(m for m in engine.settings.models if m.id == model)
    call = engine.store.reserve(request, "test", role, m, 0, 1, task_id=request + "-" + task if task else None)
    engine.store.settle(
        call,
        None if failed else 0.1,
        {"input_tokens": inputs, "output_tokens": outputs, "cached_tokens": cached, "cache_write_tokens": writes},
        0,
        failed,
    )
    assert engine.store.savings.last_error is None
    return call


def finish(engine, request):
    result = engine.store.savings.finish(request)
    assert engine.store.savings.last_error is None
    return result


@pytest.mark.asyncio
async def test_local_cloud_context_cache_and_no_double_count(settings):
    async with managed(configure(settings)) as engine:
        s = engine.store.savings
        s.begin("a", "test")
        engine.store.trace("a", "context", task="edit", raw_estimated_tokens=1200, optimized_estimated_tokens=1000)
        record(engine, "a")
        record(engine, "a", "cheap", inputs=1000, outputs=100, cached=400, writes=100, task="cloud")
        data = finish(engine, "a")
        assert D(data["actual_cost"]) == D(".00094")  # 500 regular + 400 cached + 100 writes + 100 output
        assert D(data["baseline_estimated_cost"]) == D(".026")
        assert D(data["estimated_cost_saved"]) == D(".02506")
        assert data["local_tokens_processed"] == 1100
        assert data["cloud_tokens_processed"] == 1100
        assert data["paid_cloud_tokens_avoided"] == 1300
        assert data["actual_cached_tokens"] == 400
        assert data["context_tokens_avoided"] == 200
        assert data["local_execution_percentage"] == 50
        assert data["frontier_calls_avoided"] == 2
        assert data["attribution"]["kind"] == "contributing factors, not additive subtotals"
        assert data["token_estimation_method"].startswith("Observed stage tokens")
        receipt = json.loads(engine.store.db.execute("SELECT data FROM receipts WHERE run='a'").fetchone()[0])
        assert receipt["economics"] == data
        assert s.summary()["estimated_cost_saved"] == data["estimated_cost_saved"]
        finish(engine, "a")
        assert s.summary()["runs"] == 1


@pytest.mark.asyncio
async def test_baseline_selection_unknown_disabled_and_host(settings):
    async with managed(configure(settings)) as engine:
        s, config = engine.store.savings, settings.savings
        for method in ("DIRECT_MODEL", "USER_SELECTED_MODEL"):
            config.baseline_method = method
            assert s.baseline()["model"] == "baseline"
        config.baseline_method = "HOST_MODEL"
        assert s.baseline()["model"] is None
        assert s.baseline("cheap")["source"] == "host_model"
        assert s.baseline("cheap")["model"] == "cheap"
        config.baseline_method = "QUALITY_BASELINE"
        assert s.baseline()["model"] in {"cheap", "baseline"}
        assert s.baseline()["confidence"] == "LOW"
        config.baseline_method = "DISABLED"
        assert s.baseline()["model"] is None
        config.baseline_method = "DIRECT_MODEL"
        for model in (None, "missing", "mock-worker"):
            config.baseline_model = model
            assert s.baseline()["model"] is None
        s.begin("none")
        record(engine, "none")
        data = finish(engine, "none")
        assert data["actual_cost"] == "0"
        assert data["estimated_cost_saved"] is None
        assert data["paid_cloud_tokens_avoided"] is None
        assert data["local_tokens_processed"] == 1100


@pytest.mark.asyncio
async def test_unknown_price_and_missing_usage_do_not_fabricate(settings):
    settings = configure(settings)
    settings.models[-1].input_price = None
    settings.models[-1].pricing_status = "unknown"
    async with managed(settings) as engine:
        s = engine.store.savings
        s.begin("unknown")
        record(engine, "unknown", "cheap")
        data = finish(engine, "unknown")
        assert data["actual_cost"] is None and data["estimated_cost_saved"] is None
        assert data["paid_cloud_tokens_avoided"] == 0
        s.begin("missing")
        record(engine, "missing", inputs=0, outputs=0)
        data = finish(engine, "missing")
        assert data["actual_cost"] == "0"
        assert data["baseline_estimated_cost"] is None
        assert data["paid_cloud_tokens_avoided"] is None
        assert data["missing_usage_calls"] == 1


@pytest.mark.asyncio
async def test_retry_overhead_negative_savings_and_uncertain(settings):
    settings = configure(settings)
    settings.savings.baseline_model = "cheap"
    async with managed(settings) as engine:
        s = engine.store.savings
        s.begin("negative")
        record(engine, "negative", "baseline")
        record(engine, "negative", "baseline", role="repair")
        data = finish(engine, "negative")
        assert D(data["actual_cost"]) == D(".024")
        assert D(data["baseline_estimated_cost"]) == D(".0012")
        assert D(data["estimated_cost_saved"]) == D("-.0228")
        assert data["paid_cloud_tokens_avoided"] == -1100
        s.begin("uncertain")
        record(engine, "uncertain", "cheap", failed=True)
        data = finish(engine, "uncertain")
        assert data["actual_cost"] is None
        assert data["estimated_cost_saved"] is None
        assert data["uncertain_calls"] == 1


@pytest.mark.asyncio
async def test_price_and_baseline_freeze_reprice_restart(settings):
    settings = configure(settings)
    async with managed(settings) as engine:
        s = engine.store.savings
        s.begin("freeze")
        record(engine, "freeze")
        original = finish(engine, "freeze")
        settings.savings.baseline_model = "cheap"
        settings.models[1].input_price = 100
        repriced = s.calculate("freeze", reprice=True)
        assert D(repriced["estimated_cost_saved"]) > D(original["estimated_cost_saved"])
        assert s.summary("current")["estimated_cost_saved"] == original["estimated_cost_saved"]
        s.begin("pending")
        record(engine, "pending")
        s.pause("pending", "interrupted")
        assert s.summary("all")["runs"] == 1
    async with managed(settings) as engine:
        assert engine.store.savings.summary("all")["estimated_cost_saved"] == original["estimated_cost_saved"]
        receipt = json.loads(engine.store.db.execute("SELECT data FROM receipts WHERE run='freeze'").fetchone()[0])
        assert receipt["economics"] == original
        assert engine.store.savings.state()["active"] == []


@pytest.mark.asyncio
async def test_plan_cache_uses_observed_original_workload(settings):
    async with managed(configure(settings)) as engine:
        s = engine.store.savings
        s.begin("original")
        record(engine, "original", task=None, role="planner")
        finish(engine, "original")
        s.begin("reuse")
        engine.store.trace("reuse", "plan_cache", hit=True, source_request="original")
        engine.store.trace("reuse", "graph_cache", hits=25, misses=4)
        record(engine, "reuse")
        data = finish(engine, "reuse")
        assert data["baseline_input_tokens"] == 2000
        assert data["cache_tokens_saved_or_discounted"] == 1100
        assert data["plan_reuse"] == 1
        assert data["graph_cache_hits"] == 25
        assert D(data["estimated_cost_saved"]) == D(".024")


@pytest.mark.asyncio
async def test_optional_analytics_failure_does_not_break_task(settings, repo, monkeypatch):
    async with managed(settings) as engine:

        def broken(*args, **kwargs):
            raise RuntimeError("analytics unavailable")

        monkeypatch.setattr(engine.store.savings, "reserve", broken)
        monkeypatch.setattr(engine.store.savings, "finalize", broken)
        result = await engine.run(Request(task="Add a greeting feature with tests", repo_path=str(repo)))
        assert result["status"] == "complete"
        receipt = json.loads(
            engine.store.db.execute("SELECT data FROM receipts WHERE run=?", (result["plan_id"],)).fetchone()[0]
        )
        assert receipt["economics"]["available"] is False
        assert not engine.active_runs


@pytest.mark.asyncio
async def test_sse_coalescing_stream_state(settings):
    from local_ai_router.savings_stream import SavingsStream

    async with managed(configure(settings)) as engine:
        stream = SavingsStream(engine)
        queue = asyncio.Queue(maxsize=1)
        stream.queues.add(queue)
        watcher = asyncio.create_task(stream.watch())
        try:
            await asyncio.sleep(0.01)
            engine.store.savings.begin("live")
            for _ in range(12):
                engine.store.savings.changed()
            packet = await asyncio.wait_for(queue.get(), 2)
            assert "event: savings.updated" in packet
            payload = json.loads(packet.split("data: ", 1)[1])
            assert payload["active"][0]["request_id"] == "live"
            assert "calls" not in payload["active"][0]
            assert queue.maxsize == 1
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


def test_decimal_cache_billing_zero_price():
    prices = dict(
        input_price="10",
        output_price="20",
        cached_input_price="0",
        cache_write_price="15",
        currency="USD",
        status="known",
    )
    assert cost(prices, 1000, 100, 800, 100) == D(".0045")


@pytest.mark.asyncio
async def test_missing_analytics_and_disable_during_run_are_not_zero(settings, monkeypatch):
    async with managed(configure(settings)) as engine:
        s = engine.store.savings
        s.begin("gap")
        reserve = s.reserve
        monkeypatch.setattr(s, "reserve", lambda *args: None)
        record(engine, "gap", "cheap")
        monkeypatch.setattr(s, "reserve", reserve)
        data = finish(engine, "gap")
        assert data["actual_cost"] is None and data["estimated_cost_saved"] is None
        assert data["partial"] and len(data["accounting_gaps"]) == 1
        s.begin("toggle")
        record(engine, "toggle", "cheap")
        settings.savings.enabled = False
        record(engine, "toggle", "cheap", task="other")
        assert D(finish(engine, "toggle")["actual_cost"]) == D(".0024")


@pytest.mark.asyncio
async def test_standalone_reused_request_gets_distinct_immutable_receipts(settings):
    async with managed(configure(settings)) as engine:
        first = record(engine, "same", "cheap")
        before = json.loads(
            engine.store.db.execute("SELECT data FROM savings_runs WHERE request=?", (first,)).fetchone()[0]
        )
        second = record(engine, "same", "cheap")
        assert first != second
        assert engine.store.savings.summary("all")["actual_cost"] == "0.0024"
        after = json.loads(
            engine.store.db.execute("SELECT data FROM savings_runs WHERE request=?", (first,)).fetchone()[0]
        )
        assert after == before and after["finalized"]


@pytest.mark.asyncio
async def test_live_no_baseline_and_nonfrontier_periods(settings):
    async with managed(settings) as engine:
        s = engine.store.savings
        s.begin("no-baseline")
        assert s.state()["active"][0]["request_id"] == "no-baseline"
        record(engine, "no-baseline")
        assert s.state()["header"]["estimated_cost_saved"] is None
        finish(engine, "no-baseline")
        s.begin("new-active")
        assert s.state()["header"]["estimated_cost_saved"] is None


@pytest.mark.asyncio
async def test_abandoned_owner_is_interrupted(settings, monkeypatch):
    async with managed(settings) as engine:
        s = engine.store.savings
        s.begin("abandoned")
        monkeypatch.setattr("local_ai_router.savings.process_alive", lambda _: False)
        state = s.state()
        assert state["active"] == []
        assert state["current_status"] == "interrupted"
        assert s.summary("all")["runs"] == 0


@pytest.mark.asyncio
async def test_orphan_accounting_failure_survives_restart(settings, monkeypatch):
    configure(settings)
    async with managed(settings) as engine:

        def broken(*args):
            raise RuntimeError("analytics unavailable")

        monkeypatch.setattr(engine.store.savings, "reserve", broken)
        model = next(m for m in settings.models if m.id == "cheap")
        call = engine.store.reserve("orphan", "test", "executor", model, 0, 1)
        engine.store.settle(call, 0.0012, {"input_tokens": 1000, "output_tokens": 100}, 0, False)
        assert engine.store.costs()["estimated_usd"] == 0.0012
    async with managed(settings) as restarted:
        for period in ("all", "today", "current", "session", "7d", "30d"):
            data = restarted.store.savings.summary(period, "test")
            assert data["actual_cost"] is None and data["estimated_cost_saved"] is None
            assert data["partial"] and data["missing_usage_calls"] == 1
        assert restarted.store.savings.state()["header"]["actual_cost"] is None
        assert restarted.store.savings.state()["current"]["actual_cost"] is None
        assert restarted.store.savings.summary("session")["partial"]


@pytest.mark.asyncio
async def test_generic_receipt_markdown(settings):
    from local_ai_router.receipts import get, markdown

    async with managed(configure(settings)) as engine:
        call = record(engine, "standalone", "cheap")
        rendered = markdown(get(engine, call))
        assert "0.0012" in rendered and "inference" in rendered
        engine.store.savings.begin("gateway", kind="gateway")
        record(engine, "gateway", "cheap", role="gateway", task="")
        finish(engine, "gateway")
        assert "0.0012" in markdown(get(engine, "gateway"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["settle", "finalize"])
@pytest.mark.parametrize("explicit_group", [False, True])
async def test_late_accounting_failure_is_durable_until_receipt(settings, monkeypatch, failure, explicit_group):
    configure(settings)
    async with managed(settings) as engine:

        def broken(*args):
            raise RuntimeError("accounting fault")

        monkeypatch.setattr(engine.store.savings, failure, broken)
        if explicit_group:
            engine.store.savings.begin("late-fault", "test")
        model = next(m for m in settings.models if m.id == "cheap")
        call = engine.store.reserve("late-fault", "test", "executor", model, 0, 1)
        engine.store.settle(call, 0.0012, {"input_tokens": 1000, "output_tokens": 100}, 0, False)
        if explicit_group and failure == "finalize":
            engine.store.economics("finish", "late-fault")
        assert engine.store.costs()["estimated_usd"] == 0.0012
    async with managed(settings) as engine:
        for period in ("all", "today", "current", "session"):
            data = engine.store.savings.summary(period)
            assert data["actual_cost"] is None and data["partial"]
        if failure == "settle":
            engine.store.savings.settle(call, 0.0012, {"input_tokens": 1000, "output_tokens": 100}, False)
        if explicit_group or failure == "finalize":
            engine.store.savings.finish("late-fault" if explicit_group else call)
        assert engine.store.db.execute("SELECT COUNT(*) FROM savings_gaps").fetchone()[0] == 0
        assert engine.store.savings.summary("all")["actual_cost"] == "0.0012"


@asynccontextmanager
async def managed(settings):
    engine = Engine(settings)
    try:
        yield engine
    finally:
        await engine.close()
