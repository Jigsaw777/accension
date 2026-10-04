import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import ValidationError

from local_ai_router import axir, dna, receipts
from local_ai_router.config import Runtime, Settings
from local_ai_router.context import ContextGraph
from local_ai_router.contracts import contract_scope, egress_summary, reserve_egress
from local_ai_router.engine import Engine
from local_ai_router.errors import PrivacyViolation, ProviderError
from local_ai_router.schema import EgressBudget, ExecutionPlan, Provider, Request, TaskNode
from local_ai_router.store import Store


def node(name="edit", **fields):
    return TaskNode(
        id=name,
        title=name,
        objective="Update " + name,
        expected_artifacts=[name + ".py"],
        acceptance_criteria=["tests pass"],
        **fields,
    )


async def test_axir_portable_binding_and_stale_content(engine, repo, tmp_path):
    (repo / "edit.py").write_text("value = 1\n")
    plan = await engine.plan(Request(task="Update edit.py documentation", repo_path=str(repo)))
    ir = axir.AXIR.model_validate(axir.export(engine, plan.plan_id))
    assert str(repo) not in ir.model_dump_json()
    assert not any(key in ir.model_dump() for key in ("provider", "credentials", "repo_path", "planner_model"))
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "edit.py").write_text("value = 1\n")
    engine.settings.repositories.append(engine.settings.repositories[0].model_copy(update={"path": str(clone)}))
    first, second = axir.bind(engine, ir, str(clone)), axir.bind(engine, ir, str(clone))
    assert len({plan.plan_id, first.plan_id, second.plan_id}) == 3
    assert first.request_id != second.request_id
    assert len(list(engine.store.db.execute("SELECT id FROM runs"))) == 3
    assert axir.reroute(engine, ir)["inference_calls"] == 0
    (clone / "edit.py").write_text("value = 2\n")
    with pytest.raises(ValueError, match="stale"):
        axir.bind(engine, ir, str(clone))


async def test_axir_validation_rejects_commands_cycles_and_unknown_versions(engine, repo):
    graph = ContextGraph(repo, engine.store, engine.settings.repositories[0])
    graph.build()
    values = dict(
        plan_id="portable",
        goal="Update",
        repository_fingerprint=graph.portable_fingerprint,
        cost_budget=0.1,
        task_graph=[node()],
        completion_contract=["tests pass"],
    )
    for version in (0, 2):
        with pytest.raises(ValidationError):
            axir.AXIR(**{**values, "schema_version": version})
    with pytest.raises(ValidationError, match="cycle"):
        axir.AXIR(**{**values, "task_graph": [node("a", dependencies=["b"]), node("b", dependencies=["a"])]})
    for task in [
        node(validation_commands=["echo unsafe"]),
        node(tools_required=["shell"]),
        node().model_copy(update={"expected_artifacts": ["../outside.py"]}),
    ]:
        with pytest.raises(ValueError):
            axir.bind(engine, axir.AXIR(**{**values, "task_graph": [task]}), str(repo))
    old = axir.AXIR(**values)
    new = old.model_copy(update={"quality_contract": 0.98, "task_graph": [node(), node("tests")]})
    changes = axir.diff(old, new)
    assert changes["tasks_added"] == ["tests"]
    assert changes["contracts_changed"]["quality_contract"]["after"] == 0.98


async def test_local_node_taints_dependencies_and_shared_files(engine, repo):
    engine.settings.repositories[0].allow_cloud = True
    plan = ExecutionPlan(
        goal="mixed",
        success_criteria=["works"],
        tasks=[
            node("secret", privacy_class="LOCAL_ONLY"),
            node("child", dependencies=["secret"]),
            node("reader", relevant_files=["secret.py"]),
            node("independent"),
        ],
    )
    engine.validate_plan(plan, repo, engine.settings.repositories[0])
    assert [n.privacy_class for n in plan.tasks] == ["LOCAL_ONLY", "LOCAL_ONLY", "LOCAL_ONLY", "CLOUD_ALLOWED"]


def test_egress_atomic_across_connections(settings):
    settings.providers["remote"] = Provider(kind="openai", endpoint="https://example.invalid/v1")
    model = settings.models[0].model_copy(update={"provider": "remote"})
    stores = [Store(settings), Store(settings)]

    def attempt(pair):
        i, store = pair
        with contract_scope(budget=EgressBudget(max_cloud_context_tokens_per_request=100), files=["edit.py"]):
            try:
                reserve_egress(store, str(i), "shared", model, "executor", 80)
                return True
            except PrivacyViolation:
                return False

    try:
        with ThreadPoolExecutor(2) as pool:
            assert sum(pool.map(attempt, enumerate(stores))) == 1
        assert egress_summary(stores[0], "shared")["context_tokens_upper_bound"] == 80
    finally:
        for store in stores:
            store.close()


async def test_node_budget_secret_protection_and_uncertain_egress(engine, monkeypatch):
    engine.settings.providers["remote"] = Provider(kind="openai", endpoint="https://example.invalid/v1")
    model = engine.settings.models[0].model_copy(update={"id": "remote-m", "provider": "remote"})
    engine.settings.models.append(model)
    attempted = []

    async def timeout(*args, **kwargs):
        attempted.append(True)
        raise TimeoutError("no reply")

    monkeypatch.setattr(engine.providers, "_generate", timeout)
    with contract_scope(node="n", node_tokens=1):
        with pytest.raises(PrivacyViolation):
            await engine.providers.generate(model, [{"role": "user", "content": "x"}], "executor", "node", "s", 0.5)
    assert not attempted and engine.store.costs("node")["estimated_usd"] == 0
    with contract_scope(files=["edit.py"]):
        with pytest.raises(ProviderError):
            await engine.providers.generate(
                model, [{"role": "user", "content": "source"}], "executor", "uncertain", "s", 0.5
            )
    assert attempted and egress_summary(engine.store, "uncertain")["calls"][0]["state"] == "uncertain"
    with contract_scope(mode="LOCAL_ONLY"):
        with pytest.raises(PrivacyViolation):
            await engine.providers.generate(model, [], "reviewer", "local", "s", 0.5)


async def test_dna_unknown_stable_evidence_specialization_and_reset(engine):
    model = engine.settings.models[0]
    assert dna.profile(engine.store, model)["dimensions"]["coding"]["value"] is None
    dna.observe(engine.store, model.id, "coding", True, source="calibration")
    first = dna.profile(engine.store, model)["dimensions"]["coding"]
    assert first["value"] == pytest.approx(0.525) and first["uncertainty"] == "limited"
    dna.observe(engine.store, model.id, "coding", False)
    assert 0.49 < dna.profile(engine.store, model)["dimensions"]["coding"]["value"] < 0.53
    for _ in range(4):
        dna.observe(engine.store, model.id, "coding", True, specialization="language:.py")
    assert not dna.profile(engine.store, model)["specializations"]
    dna.observe(engine.store, model.id, "coding", True, specialization="language:.py")
    assert dna.profile(engine.store, model)["specializations"]["language:.py"]["coding"]["samples"] == 5
    dna.reset(engine.store, model.id)
    assert dna.profile(engine.store, model)["dimensions"]["coding"]["samples"] == 0


async def test_receipt_real_mock_run_and_failed_validation(engine, repo):
    result = await engine.run(Request(task="Add a greeting feature with tests", repo_path=str(repo)))
    receipt = receipts.get(engine, result["plan_id"])
    assert receipt["status"] == "complete" and receipt["models_used"] == ["mock-worker"]
    assert receipt["validation"] and all(c["passed"] for c in receipt["validation"])
    assert receipt["files_changed"] and all(f["after"] == f["written"] for f in receipt["files_changed"])
    assert receipt["cloud_egress"]["context_tokens_upper_bound"] == 0
    assert receipt["usage"]["input_tokens"] > 0
    assert "greeting feature" not in json.dumps(receipt)
    assert "Execution Receipt" in receipts.markdown(receipt)
    from local_ai_router.lab import compare

    calls_before = engine.store.costs()["calls"]
    laboratory = compare(engine, result["plan_id"])
    assert laboratory["recorded_economics"] == receipt["economics"]
    assert laboratory["comparisons"][0]["tasks"][0]["token_basis"] == "first observed worker attempt"
    assert laboratory["comparisons"][0]["estimated_execution_cost"] == "0"
    assert engine.store.costs()["calls"] == calls_before and laboratory["inference_calls"] == 0
    (repo / "test_fail.py").write_text(
        "import unittest\nclass Failure(unittest.TestCase):\n def test_bad(self): self.fail('fixture')\n"
    )
    with pytest.raises(RuntimeError):
        await engine.run(Request(task="Update test_fail.py comments", repo_path=str(repo)))
    last = engine.store.db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert receipts.get(engine, last)["status"] == "failed"


async def test_scale_hundred_instances_five_thousand_models_lazy_bounded(tmp_path):
    active, peak, calls = 0, 0, 0

    async def handle(request):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(
            200, json={"data": [{"id": f"same-name-{i}", "capabilities": {"code": True}} for i in range(50)]}
        )

    settings = Settings(
        home=tmp_path,
        providers={
            f"instance-{i}": Provider(kind="openai-compatible", endpoint=f"http://127.0.0.1:{9000 + i}/v1", local=True)
            for i in range(100)
        },
        runtime=Runtime(discovery_concurrency=4, health_probe_concurrency=3),
    )
    settings.discovery.local_scan = False
    engine = Engine(settings, httpx.MockTransport(handle))
    try:
        await engine.discovery.refresh(True)
        assert len(settings.providers) == 100 and len(settings.models) == 5000
        assert len({m.id for m in settings.models}) == 5000
        assert calls == 100 and 1 < peak <= 4
        assert engine.store.costs()["calls"] == 0
        page = engine.store.models_page("instance-1", "same-name", offset=10, limit=20)
        assert page["total"] == 50 and len(page["items"]) == 20 and page["next_offset"] == 30
        assert engine.store.models_page(offset=4990)["next_offset"] is None
        with pytest.raises(ValueError):
            engine.store.models_page(limit=5000)
        calls, peak = 0, 0
        await asyncio.gather(*(engine.providers.health(p) for p in settings.providers.values()))
        assert calls == 100 and peak <= 3
    finally:
        await engine.close()
