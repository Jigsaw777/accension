import base64
import json
import struct
import zlib

import httpx
import pytest

from local_ai_router.calibration import CASES, preview, probe_model, run
from local_ai_router.config import Settings
from local_ai_router.engine import Engine
from local_ai_router.probes import pixel_png
from local_ai_router.schema import Generation, Model, Provider


def configured(tmp_path, handler, local=True):
    provider = Provider(
        kind="openai-compatible",
        endpoint="http://localhost:1234/v1" if local else "https://fixture.invalid/v1",
        local=local,
        auth="none",
    )
    model = Model(
        id="test",
        provider="test",
        deployment_name="deployment",
        input_price=0 if local else 1,
        output_price=0 if local else 2,
    )
    return Engine(Settings(home=tmp_path, providers={"test": provider}, models=[model]), httpx.MockTransport(handler))


@pytest.mark.parametrize("answer", ["[]", "null", '"ok"', '{"answer":"ok","extra":1}', '{"answer":"ok",}'])
async def test_schema_probe_rejects_repaired_or_wrong_shape(tmp_path, answer):
    def handler(request):
        assert "response_format" in json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    engine = configured(tmp_path, handler)
    try:
        result = await probe_model(engine.providers, engine.settings.models[0], "structured_output")
        assert not result["supported"]
    finally:
        await engine.close()


async def test_tool_probe_requires_native_call_and_cache_is_deployment_scoped(tmp_path):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["tools"][0]["function"]["name"] == "echo"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"type": "function", "function": {"name": "echo", "arguments": '{"value":"ok"}'}}
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 10},
            },
        )

    engine = configured(tmp_path, handler)
    try:
        model = engine.settings.models[0]
        assert (await probe_model(engine.providers, model, "tools"))["supported"]
        assert (await probe_model(engine.providers, model, "tools"))["supported"] and len(calls) == 1
        model.deployment_name = "new-deployment"
        assert (await probe_model(engine.providers, model, "tools"))["supported"] and len(calls) == 2
    finally:
        await engine.close()


async def test_stream_probe_requires_sse_completion_and_preserves_usage(tmp_path):
    def handler(request):
        assert json.loads(request.content)["stream"]
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text='data: {"choices":[{"delta":{"content":"ok"}}],"usage":{"prompt_tokens":3,"completion_tokens":4}}\n\ndata: [DONE]\n\n',
        )

    engine = configured(tmp_path, handler)
    try:
        assert (await probe_model(engine.providers, engine.settings.models[0], "streaming"))["supported"]
        usage = json.loads(engine.store.db.execute("SELECT usage FROM calls").fetchone()[0])
        assert usage["input_tokens"] == 3 and usage["output_tokens"] == 4
    finally:
        await engine.close()


def test_vision_fixture_crc_and_pixel():
    for color in ["red", "blue", "green"]:
        blob = base64.b64decode(pixel_png(color))
        offset = 8
        types = []
        while offset < len(blob):
            n = struct.unpack(">I", blob[offset : offset + 4])[0]
            kind = blob[offset + 4 : offset + 8]
            data = blob[offset + 8 : offset + 8 + n]
            assert zlib.crc32(kind + data) == struct.unpack(">I", blob[offset + 8 + n : offset + 12 + n])[0]
            types.append(kind)
            offset += n + 12
        assert types == [b"IHDR", b"IDAT", b"IEND"]


async def test_paid_calibration_requires_single_use_unchanged_quote(tmp_path):
    engine = configured(
        tmp_path, lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]}), local=False
    )
    try:
        quote = preview(engine, ["test"], 0.05)
        assert quote["paid_approval_required"] and quote["estimated_maximum"] <= 0.05
        with pytest.raises(ValueError, match="explicitly allow"):
            await run(engine, quote["id"])
        result = await run(engine, quote["id"], allow_paid=True)
        assert len(result["results"]) == 11 and not any(r["passed"] for r in result["results"])
        assert result["costs"]["estimated_usd"] <= quote["estimated_maximum"]
        with pytest.raises(ValueError, match="already used"):
            await run(engine, quote["id"], allow_paid=True)
        quote = preview(engine, ["test"], 0.05)
        engine.settings.models[0].deployment_name = "replacement"
        with pytest.raises(ValueError, match="Configuration changed"):
            await run(engine, quote["id"], allow_paid=True)
    finally:
        await engine.close()


async def test_all_pass_calibration_makes_small_feature_eligible(tmp_path, monkeypatch):
    engine = configured(tmp_path, lambda request: pytest.fail("Unexpected network"))
    from local_ai_router.policy import preset

    engine.settings.routing, engine.settings.control_plane = preset(engine.settings, "balanced")

    async def generate(model, messages, *args, **kwargs):
        case = next(c for c in CASES if c.prompt == messages[-1]["content"])
        answer = {"python_add": "def add(a, b):\n    return a + b", "test_add": "assert add(2, 3) == 5"}.get(
            case.expected, case.expected
        )
        return Generation(text=json.dumps({"answer": answer}), raw={"probe_evidence": {"tools": True}})

    monkeypatch.setattr(engine.providers, "generate", generate)
    try:
        quote = preview(engine, ["test"], 0)
        result = await run(engine, quote["id"])
        assert all(row["passed"] for row in result["results"])
        route = await engine.route("Implement a greeting feature with tests")
        assert route["candidates"] == ["test"]
        assert route["classification"]["planning_depth"] == "IMPLEMENTATION_PLAN"
    finally:
        await engine.close()


async def test_fully_local_calibration_excludes_cloud_without_calls(tmp_path):
    engine = configured(tmp_path, lambda request: pytest.fail("Cloud called"), local=False)
    engine.settings.control_plane.fully_local = True
    try:
        quote = preview(engine, ["test"], 0.05)
        assert not quote["models"] and quote["rejected"][0]["reason"] == "PRIVACY_OR_PROVIDER_POLICY"
    finally:
        await engine.close()
