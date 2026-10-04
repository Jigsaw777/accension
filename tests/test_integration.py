import asyncio
import json
import sys

import httpx
import pytest

from local_ai_router.app import create_app
from local_ai_router.engine import Engine
from local_ai_router.providers import ProviderError
from local_ai_router.schema import Model, Provider, Request


async def test_mock_feature_e2e_and_stale_plan(engine, repo):
    request = Request(task="Add greeting feature with tests and documentation", repo_path=str(repo))
    result = await engine.run(request)
    assert result["status"] == "complete"
    assert set(result["files_changed"]) == {"greeting.py", "test_greeting.py", "USAGE.md"}
    assert result["costs"]["estimated_usd"] == 0
    assert all(r["passed"] for r in result["validation"])
    traces = engine.store.traces(result["request_id"])
    schedules = [x["tasks"] for x in traces if x["stage"] == "schedule"]
    assert schedules[0] == ["greeting", "docs"] and schedules[1] == ["tests"]
    assert not any("content" in x for x in traces)
    with pytest.raises(ValueError, match="already executed"):
        await engine.execute_plan(result["plan_id"], str(repo))
    plan = await engine.plan(request)
    (repo / "new.py").write_text("x=1")
    with pytest.raises(ValueError, match="changed since planning"):
        await engine.execute_plan(plan.plan_id, str(repo))


async def test_worker_repair_after_failed_test(engine, repo, monkeypatch):
    original = engine.providers._generate
    seen = []

    async def generate(provider, model, messages, role, maximum, schema, task_id):
        result = await original(provider, model, messages, role, maximum, schema, task_id)
        if role in ("executor", "repair"):
            data = json.loads(messages[-1]["content"])
            if data["task"]["id"] == "tests":
                seen.append(role)
                if role == "executor":
                    output = json.loads(result.text)
                    output["files_changed"][0]["content"] = (
                        "import unittest\nclass Broken(unittest.TestCase):\n def test_fail(self): self.fail('deliberate first attempt')\n"
                    )
                    result.text = json.dumps(output)
        return result

    monkeypatch.setattr(engine.providers, "_generate", generate)
    result = await engine.run(Request(task="Add greeting feature", repo_path=str(repo)))
    assert result["status"] == "complete" and seen == ["executor", "repair"]


async def test_final_failure_rolls_back_only_router_files(engine, repo):
    (repo / "important.txt").write_text("user work")
    (repo / "test_existing.py").write_text(
        "import unittest\nclass Existing(unittest.TestCase):\n def test_fail(self): self.fail('existing failure')\n"
    )
    with pytest.raises(RuntimeError, match="rolled back"):
        await engine.run(Request(task="Add greeting feature", repo_path=str(repo)))
    assert (repo / "important.txt").read_text() == "user work"
    assert (repo / "test_existing.py").exists()
    assert not (repo / "greeting.py").exists()


@pytest.mark.parametrize(
    "kind,api,path",
    [
        ("openai", "chat", "/chat/completions"),
        ("foundry", "responses", "/responses"),
        ("anthropic", "messages", "/messages"),
        ("ollama", "chat", "/chat/completions"),
    ],
)
async def test_provider_protocols(settings, kind, api, path):
    settings.providers = {"p": Provider(kind=kind, api=api, endpoint="https://mock.invalid/v1")}
    settings.models = [
        Model(
            id="m",
            provider="p",
            deployment_name="actual-deployment",
            input_price=1,
            output_price=2,
            quality_priors={"default": 0.99},
            supports_structured_output=True,
        )
    ]

    def handle(request):
        data = json.loads(request.content)
        assert request.url.path == "/v1" + path
        assert data["model"] == "actual-deployment"
        assert request.headers["X-Local-Router-Hop"] == "1"
        if api == "responses":
            assert data["store"] is False
            body = {
                "output": [{"content": [{"type": "output_text", "text": '{"ok":true}'}]}],
                "usage": {"input_tokens": 20, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 5}},
            }
        elif api == "messages":
            assert request.headers["anthropic-version"] == "2023-06-01"
            body = {
                "content": [{"type": "text", "text": '{"ok":true}'}],
                "usage": {"input_tokens": 20, "output_tokens": 10, "cache_read_input_tokens": 5},
            }
        else:
            body = {
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}},
            }
        return httpx.Response(200, json=body)

    e = Engine(settings, httpx.MockTransport(handle))
    try:
        r = await e.providers.generate(
            settings.models[0],
            [{"role": "system", "content": "schema"}, {"role": "user", "content": "test"}],
            "executor",
            "r",
            "s",
            0.5,
        )
        assert json.loads(r.text)["ok"] and r.usage.cached_tokens == 5
        assert e.store.costs()["estimated_usd"] > 0
    finally:
        await e.close()


async def test_gateway_auth_recursion_streaming_and_tools(settings):
    e = Engine(settings)
    app = create_app(settings, e)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost") as client:
            assert (await client.get("/v1/models")).status_code == 401
            headers = {"Authorization": "Bearer " + settings.token}
            assert (await client.get("/v1/models", headers=headers)).status_code == 200
            assert (await client.get("/v1/models", headers={**headers, "X-Local-Router-Hop": "1"})).status_code == 508
            assert (
                await client.get("/v1/models", headers={**headers, "Origin": "https://evil.invalid"})
            ).status_code == 403
            r = await client.post(
                "/v1/responses", headers=headers, json={"input": "hi", "previous_response_id": "unbounded-history"}
            )
            assert r.status_code == 400
            r = await client.post("/v1/chat/completions", headers=headers, json={"messages": [], "n": 128})
            assert r.status_code == 400
            r = await client.post(
                "/v1/responses",
                headers=headers,
                json={"model": "router-auto", "input": "Explain hello", "stream": True},
            )
            assert r.status_code == 200
            assert "response.created" in r.text and "response.completed" in r.text
            r = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "router-auto",
                    "messages": [{"role": "user", "content": "Explain hello"}],
                    "stream": True,
                },
            )
            assert "[DONE]" in r.text
    finally:
        await e.close()


async def test_gateway_native_tool_stream_passthrough_and_fallback(settings):
    settings.providers = {"p": Provider(kind="foundry", api="responses", endpoint="https://mock.invalid/v1")}
    settings.models = [
        Model(
            id=n,
            provider="p",
            deployment_name=n,
            input_price=0,
            output_price=0,
            supports_responses_api=True,
            supports_tools=True,
            supports_streaming=True,
            quality_priors={"default": 0.99},
        )
        for n in ["down", "healthy"]
    ]
    calls = []
    frames = 'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"read_file","arguments":"{}"}}\n\nevent: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":4}}}\n\n'

    def handle(request):
        data = json.loads(request.content)
        calls.append(data["model"])
        assert data["tools"][0]["name"] == "read_file"
        if data["model"] == "down":
            return httpx.Response(503)
        return httpx.Response(200, text=frames, headers={"content-type": "text/event-stream"})

    e = Engine(settings, httpx.MockTransport(handle))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(settings, e)), base_url="http://localhost"
        ) as c:
            r = await c.post(
                "/v1/responses",
                headers={"Authorization": "Bearer " + settings.token},
                json={
                    "input": "Explain file",
                    "stream": True,
                    "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}],
                },
            )
            assert r.status_code == 200 and r.text == frames and calls == ["down", "healthy"]
    finally:
        await e.close()


async def test_auto_discovery_adds_and_disables_without_name_assumptions(settings, monkeypatch):
    settings.mock = False
    settings.discovery.enabled = True
    settings.cache.enabled = False
    settings.discovery.inventory_providers = ["local"]
    settings.discovery.azure_subscription = ""
    settings.providers = {"local": Provider(kind="openai", endpoint="http://127.0.0.1:9999/v1", local=True)}
    settings.models = []
    inventory = ["brand-new-model"]

    def handle(request):
        return httpx.Response(200, json={"data": [{"id": m} for m in inventory]})

    e = Engine(settings, httpx.MockTransport(handle))
    try:
        await e.discovery.refresh(True)
        assert settings.models[0].deployment_name == "brand-new-model"
        assert settings.models[0].input_price == 0 and settings.models[0].tier == 1
        inventory.clear()
        await e.discovery.refresh(True)
        assert not settings.models[0].enabled
    finally:
        await e.close()


async def test_stdio_mcp_client(settings):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "local_ai_router.cli", "--home", str(settings.home), "--mock", "mcp"]
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "orchestrate_feature" in [x.name for x in tools.tools]
            result = await session.call_tool("router_info", {"action": "status"})
            assert not result.isError
            assert "ok" in result.content[0].text


async def test_stdio_mcp_full_orchestration(settings, repo):
    import yaml
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    (settings.home / "config/repositories.yaml").write_text(
        yaml.safe_dump({"repositories": [r.model_dump() for r in settings.repositories]})
    )
    settings.routing.timeouts["tests"] = 5
    (settings.home / "config/routing.yaml").write_text(yaml.safe_dump({"routing": settings.routing.model_dump()}))
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "local_ai_router.cli", "--home", str(settings.home), "--mock", "mcp"]
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "orchestrate_feature", {"task": "Add greeting feature", "repo_path": str(repo)}
            )
            assert not result.isError, result.content
            assert '"complete"' in result.content[0].text
            assert (repo / "greeting.py").exists()


async def test_only_failing_node_escalates(engine, repo, monkeypatch):
    from collections import deque

    stronger = engine.settings.models[0].model_copy(update={"id": "stronger", "tier": 2})
    engine.settings.models.append(stronger)
    engine.providers.semaphores["stronger"] = asyncio.Semaphore(1)
    engine.providers.windows["stronger"] = deque()
    original = engine.providers._generate
    calls = []

    async def generate(provider, model, messages, role, maximum, schema, task_id):
        result = await original(provider, model, messages, role, maximum, schema, task_id)
        if role in ("executor", "repair"):
            data = json.loads(messages[-1]["content"])
            calls.append((data["task"]["id"], model.id))
            if data["task"]["id"] == "tests" and model.id == "mock-worker":
                output = json.loads(result.text)
                output["files_changed"][0]["content"] = (
                    "import unittest\nclass Broken(unittest.TestCase):\n def test_fail(self): self.fail('escalation fixture')\n"
                )
                result.text = json.dumps(output)
        return result

    monkeypatch.setattr(engine.providers, "_generate", generate)
    result = await engine.run(Request(task="Add greeting feature", repo_path=str(repo)))
    assert result["status"] == "complete" and result["escalations"] == 1
    assert calls.count(("greeting", "mock-worker")) == 1
    assert sum(task == "docs" for task, model in calls) == 1
    assert ("tests", "stronger") in calls


async def test_jev_selects_only_eligible_candidates(engine):
    from local_ai_router.schema import Classification

    engine.settings.routing.jev_model = "mock-worker"
    c = Classification(task_family="architecture", confidence=0.2)
    result = await engine.router.arbitrate(c, "jev-test", "jev-test", 0.5, engine.settings.models)
    assert result.model_id == "mock-worker"
    with pytest.raises(ProviderError, match="ineligible"):
        await engine.router.arbitrate(c, "jev-test2", "jev-test2", 0.5, [])


async def test_request_secret_blocked_before_inference(engine, repo):
    with pytest.raises(ValueError, match="Secret-like"):
        await engine.plan(Request(task="Use api_key=FAKE_123456789_TOKEN to build", repo_path=str(repo)))
    assert engine.store.costs()["calls"] == 0


async def test_gateway_output_cap_and_unmetered_features(settings):
    settings.providers = {"p": Provider(kind="openai", endpoint="https://mock.invalid/v1")}
    settings.models = [
        Model(
            id="m",
            provider="p",
            deployment_name="m",
            input_price=1,
            output_price=1,
            max_output=64,
            quality_priors={"default": 0.99},
        )
    ]
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}})

    e = Engine(settings, httpx.MockTransport(handler))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(settings, e)), base_url="http://localhost"
        ) as client:
            headers = {"Authorization": "Bearer " + settings.token}
            base = {"messages": [{"role": "user", "content": "Explain hello"}]}
            r = await client.post(
                "/v1/chat/completions", headers=headers, json={**base, "max_completion_tokens": 999999}
            )
            assert (
                r.status_code == 200 and payloads[0]["max_completion_tokens"] == 64 and "max_tokens" not in payloads[0]
            )
            for extra in [
                {"background": True},
                {"tools": [{"type": "web_search"}]},
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}}],
                        }
                    ]
                },
            ]:
                r = await client.post("/v1/chat/completions", headers=headers, json={**base, **extra})
                assert r.status_code == 400
            r = await client.post("/v1/chat/completions", headers=headers, json={**base, "stream": True})
            assert r.status_code == 503
            assert len(payloads) == 1
    finally:
        await e.close()
