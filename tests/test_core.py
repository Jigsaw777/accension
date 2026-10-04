from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from local_ai_router.context import ContextGraph, file_names, optimize_messages
from local_ai_router.engine import conflicts
from local_ai_router.providers import ProviderError, estimate_cost
from local_ai_router.safety import Edits, digest, redact, repo_lock, safe_path, validation_argv
from local_ai_router.schema import Classification, ExecutionPlan, FileChange, Model, TaskNode
from local_ai_router.store import BudgetExceeded, Store, cache_key


def task(id="a", **kwargs):
    return TaskNode(
        id=id, title=id, objective=id, acceptance_criteria=["works"], expected_artifacts=[id + ".py"], **kwargs
    )


def test_schema_rejects_cycle_and_unknown_dependency():
    for tasks in (
        [task("a", dependencies=["b"]), task("b", dependencies=["a"])],
        [task("a", dependencies=["missing"])],
        [task("a"), task("a")],
    ):
        with pytest.raises(ValidationError):
            ExecutionPlan(goal="x", success_criteria=["x"], tasks=tasks)
    plan = ExecutionPlan(goal="x", success_criteria=["x"], tasks=[task("a"), task("b")], dependency_edges=[("a", "b")])
    assert plan.tasks[1].dependencies == ["a"]


async def test_thresholds_and_laya_acceptance(engine, monkeypatch):
    router = engine.router
    c = Classification(confidence=0.8, risk=69)
    assert not router.needs_jev(c)
    assert router.needs_jev(c.model_copy(update={"confidence": 0.79}))
    assert router.needs_jev(c.model_copy(update={"risk": 70}))
    assert router.needs_jev(c, failures=2)
    engine.settings.routing.laya_enabled = True
    engine.settings.routing.laya_command = "mock"

    async def classify(text):
        return {
            "values": {"task_family": "coding", "complexity": 15, "risk": 10, "planning_required": False},
            "confidence": {"task_family": 0.95},
        }

    monkeypatch.setattr(router.laya, "classify", classify)
    result = await router.classify("Implement parser", "one")
    assert "LAYA_ACCEPTED" in result.reason_codes
    critical = await router.classify("Fix authentication bypass", "two")
    assert critical.task_family == "critical" and critical.risk >= 85


async def test_capabilities_and_quality_before_price(engine):
    engine.settings.models[0].supports_tools = False
    assert engine.router.candidates(Classification(), capabilities=["tools"]) == []
    engine.settings.models[0].quality_priors = {"default": 0.1}
    assert engine.router.candidates(Classification()) == []


def test_cost_unknown_not_free():
    m = Model(id="m", provider="p", deployment_name="m", input_price=2, output_price=10, cached_input_price=0.2)
    assert estimate_cost(m, 1000, 100, 500) == pytest.approx(0.0021)
    m.input_price = None
    with pytest.raises(ProviderError):
        estimate_cost(m, 1, 1)


def test_anthropic_cache_usage_is_additive():
    from local_ai_router.providers import usage_from

    m = Model(
        id="a",
        provider="a",
        deployment_name="a",
        input_price=1,
        output_price=5,
        cached_input_price=0.1,
        cache_write_price=1.25,
    )
    u = usage_from(
        {"usage": {"input_tokens": 50, "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 1000}}
    )
    assert u.input_tokens == 101050
    assert estimate_cost(m, u.input_tokens, 0, u.cached_tokens, u.cache_write_tokens) == pytest.approx(0.0113)
    assert estimate_cost(m, 101050, 0, reserve=True) >= 0.0113


def test_budget_reservations_atomic(settings):
    settings.budgets.worker_budget = 0.10
    store1, store2 = Store(settings), Store(settings)

    def reserve(store):
        try:
            store.reserve("r", "s", "executor", settings.models[0], 0.08, 0.10)
            return True
        except BudgetExceeded:
            return False

    try:
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(reserve, [store1, store2]))
        assert sum(result) == 1
        assert store1.costs()["estimated_usd"] == 0.08
    finally:
        store1.close()
        store2.close()


def test_budgets_all_scopes_and_frontier(settings):
    store = Store(settings)
    try:
        settings.budgets.daily_hard_budget = 0.1
        store.reserve("r1", "s1", "executor", settings.models[0], 0.08, 0.5)
        with pytest.raises(BudgetExceeded, match="daily"):
            store.reserve("r2", "s2", "executor", settings.models[0], 0.08, 0.5)
        settings.models[0].tier = 4
        store.reserve("r3", "s3", "planner", settings.models[0], 0, 0.5)
        with pytest.raises(BudgetExceeded, match="Frontier"):
            store.reserve("r3", "s3", "planner", settings.models[0], 0, 0.5)
    finally:
        store.close()


async def test_circuit_breaker_and_learning(engine):
    for _ in range(3):
        engine.store.health_result("mock-worker", False)
    assert not engine.store.healthy("mock-worker")
    assert not engine.router.candidates(Classification())
    engine.store.health_result("mock-worker", True)
    m = engine.settings.models[0]
    prior = engine.store.quality(m, "coding")
    engine.store.success(m.id, "coding", False, 1)
    assert 0 < prior - engine.store.quality(m, "coding") < 0.02


@pytest.mark.parametrize(
    "path", ["../x", "C:/x", "/tmp/x", "a/../x", ".git/config", ".env", "dir/key.pem", "a:stream", "a./x", "CON"]
)
def test_path_traversal(repo, path):
    with pytest.raises(ValueError):
        safe_path(repo, path)


def test_edits_hash_scope_and_rollback(repo):
    f = repo / "a.py"
    f.write_text("original")
    with repo_lock(repo):
        edits = Edits(repo, "test")
        with pytest.raises(ValueError):
            edits.apply([FileChange(path="a.py", content="bad")], ["a.py"])
        edits.apply([FileChange(path="a.py", content="changed", original_sha256=digest(f))], ["a.py"])
        assert f.read_text() == "changed"
        assert edits.rollback() == []
        assert f.read_text() == "original"
        with pytest.raises(ValueError):
            edits.apply([FileChange(path="b.py", content="x")], ["a.py"])


def test_rollback_preserves_user_edit(repo):
    with repo_lock(repo):
        edits = Edits(repo, "test")
        edits.apply([FileChange(path="a.py", content="router")], ["a.py"])
        (repo / "a.py").write_text("user edit")
        assert edits.rollback() == ["a.py"]
        assert (repo / "a.py").read_text() == "user edit"


def test_rollback_canonicalizes_slash_aliases(repo):
    (repo / "src").mkdir()
    file = repo / "src/a.py"
    file.write_text("original")
    with repo_lock(repo):
        edits = Edits(repo, "aliases")
        edits.apply([FileChange(path="src/a.py", content="first", original_sha256=digest(file))], ["src/a.py"])
        edits.apply([FileChange(path="src\\a.py", content="second", original_sha256=digest(file))], ["src/a.py"])
        assert edits.rollback() == []
        assert file.read_text() == "original"


def test_metadata_hardlinks_rejected(repo, tmp_path):
    import os

    state = repo / ".router"
    state.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    os.link(outside, state / "execution.lock")
    with pytest.raises(ValueError, match="Hard-linked"):
        with repo_lock(repo):
            pass
    assert outside.read_text() == "untouched"


def test_command_safety_and_secret_redaction():
    for argv in [
        ["powershell", "-c", "Remove-Item x"],
        ["git", "push"],
        ["python", "-c", "print(1)"],
        ["python", "-m", "pip", "install", "x"],
    ]:
        with pytest.raises(ValueError):
            validation_argv({"test": argv}, "test")
    assert "FAKE_ABCDEF12345" not in redact("api_key=FAKE_ABCDEF12345")
    assert redact({"api_key": "abc"})["api_key"] == "[REDACTED]"


@pytest.mark.parametrize("label", ["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "])
def test_private_key_redaction_complete_incomplete_and_multiple_blocks(label):
    from local_ai_router.safety import SECRET

    start, end = "-----BEGIN " + label + "PRIVATE KEY-----", "-----END " + label + "PRIVATE KEY-----"
    key = start + "\nprivate body\n" + end
    assert SECRET.search(start)
    assert redact("before " + key + " between " + key + " after") == "before [REDACTED] between [REDACTED] after"
    assert redact("before " + start + "\nprivate body") == "before [REDACTED]"
    assert redact(key.lower()) == "[REDACTED]"


def test_secret_checks_handle_large_adversarial_input_promptly():
    import subprocess
    import sys

    # A child timeout bounds regressions without hanging pytest. The previous
    # patterns take tens of seconds/minutes for these repeated missing endings.
    code = """
from local_ai_router.safety import SECRET, redact
assert not SECRET.search('-----BEGIN x' * 40000)
header = '-----BEGIN ' + 'RSA ' + 'PRIVATE KEY-----'
assert redact((header + '\\n') * 40000) == '[REDACTED]'
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, timeout=10)


async def test_validation_redacts_bounded_output_without_reopening_path(repo, monkeypatch):
    from pathlib import Path

    from local_ai_router import safety

    original_open = Path.open
    opened = []

    def open_once(path, *args, **kwargs):
        if path.name == "validation.log":
            opened.append(path)
            assert len(opened) == 1, "Validation output must use the original handle after the child exits"
        return original_open(path, *args, **kwargs)

    async def spawn(*args, **kwargs):
        class Process:
            returncode = 0

            async def wait(self):
                output = kwargs["stdout"]
                output.write(b"x" * 7000 + b"\napi_key=FAKE_ABCDEF12345\n")
                output.flush()

        return Process()

    monkeypatch.setattr(Path, "open", open_once)
    monkeypatch.setattr(safety.asyncio, "create_subprocess_exec", spawn)
    result = await safety.validate(repo, {"tests": ["{python}", "-m", "unittest"]}, ["tests"], 2)
    assert result[0]["passed"]
    assert "FAKE_ABCDEF12345" not in result[0]["output"]
    assert len(result[0]["output"]) <= 6000
    with original_open(repo / ".router/validation.log", encoding="utf-8") as log:
        assert log.read() == result[0]["output"]


async def test_execution_paths_require_registered_repository(engine, repo, tmp_path):
    from local_ai_router.schema import Request

    outside = tmp_path / "unregistered"
    outside.mkdir()
    request = Request(task="Add a greeting", repo_path=str(outside))
    for action in (engine.plan(request), engine.run(request), engine.execute_plan("unknown", str(outside))):
        with pytest.raises(ValueError, match="not registered"):
            await action
    with pytest.raises(ValueError, match="not registered"):
        engine.skills.resolve(request, "coding")
    assert not (outside / ".router").exists()
    # Normalized aliases must still bind to the configured root.
    request.repo_path = str(repo / ".." / repo.name)
    plan = await engine.plan(request)
    assert (repo / ".router/plans" / (plan.plan_id + ".json")).is_file()


async def test_cache_invalidates_dirty_added_removed_files(engine, repo):
    (repo / "a.py").write_text("def f(): return 1\n")

    def fingerprint():
        graph = ContextGraph(repo, engine.store, engine.settings.repositories[0])
        return graph.build()["fingerprint"]

    a = fingerprint()
    (repo / "a.py").write_text("def f(): return 2\n")
    b = fingerprint()
    assert a != b
    (repo / "b.py").write_text("from a import f\n")
    c = fingerprint()
    assert c != b
    (repo / "b.py").unlink()
    assert fingerprint() == b
    assert cache_key("x", "prompt1") != cache_key("x", "prompt2")


def test_file_listing_does_not_inherit_parent_git(tmp_path):
    import subprocess

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("nested/\n")
    root = tmp_path / "nested"
    root.mkdir()
    (root / "a.py").write_text("value = 1\n")
    assert file_names(root) == ["a.py"]
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("ignore me\n")
    assert file_names(root) == [".gitignore", "a.py"]


def test_conflicts_and_token_optimizer():
    a, b = task("a"), task("b")
    assert not conflicts(a, b)
    b.relevant_files = ["A.py"]
    assert conflicts(a, b)
    messages = [{"role": "system", "content": "security constraints"}] * 2 + [
        {"role": "user", "content": "acceptance and contract"}
    ]
    result, savings = optimize_messages(messages, 1000)
    assert len(result) == 2 and savings["tokens_saved"] > 0
    assert result[-1] == messages[-1]
    with pytest.raises(ValueError):
        optimize_messages(messages, 1)


def test_prompt_cache_prices_must_be_explicit():
    from local_ai_router.config import CacheConfig
    from local_ai_router.schema import Model

    with pytest.raises(ValueError, match="cache_write_price"):
        Model(id="m", provider="p", deployment_name="m", supports_prompt_cache=True)
    with pytest.raises(ValueError, match="Semantic cache"):
        CacheConfig(semantic_cache_enabled=True)


async def test_unindexed_file_changes_invalidate_plan(engine, repo):
    file = repo / "resource.bin"
    file.write_bytes(b"v1")
    graph = ContextGraph(repo, engine.store, engine.settings.repositories[0])
    first = graph.build()["fingerprint"]
    file.write_bytes(b"v2")
    assert graph.build()["fingerprint"] != first
