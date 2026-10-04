import json

import httpx
import pytest

from local_ai_router.cli import dispatch
from local_ai_router.cli_parser import parser
from local_ai_router.conformance import check_provider
from local_ai_router.mcp_server import create_mcp


@pytest.mark.parametrize(
    "arguments,key",
    [
        (["doctor"], "database"),
        (["provider", "test", "mock"], "checks"),
        (["model", "search", "mock"], "items"),
        (["model", "info", "mock-worker"], "id"),
        (["model", "disable", "mock-worker"], "updated"),
        (["role", "explain", "executor"], "selected"),
        (["role", "reset", "executor"], "updated"),
        (["plugin", "list"], "loaded"),
        (["plugin", "info", "mock"], "id"),
        (["config", "validate"], "valid"),
        (["config", "export"], "schema_version"),
        (["cache", "stats"], "entries"),
        (["cache", "clear"], "entries"),
        (["logs", "path"], "path"),
        (["mode", "companion"], "mode"),
        (["calibrate", "preview"], "id"),
        (["integrate", "list"], "clients"),
    ],
)
async def test_management_commands_have_real_results(settings, arguments, key):
    result = await dispatch(parser().parse_args(arguments), settings)
    assert isinstance(result, dict)
    if key in {"updated", "clients"}:
        assert result  # Mutation/list shape is already validated by management tests.
    else:
        assert key in result
    if arguments == ["doctor"]:
        assert result["database"] == "ok" and result["logging"]["writable"]
        assert result["skill_presets"]["count"] == 0


async def test_cli_skill_preset_lifecycle_and_logs(settings, tmp_path):
    async def command(*args):
        return await dispatch(parser().parse_args(args), settings)

    path = tmp_path / "review.md"
    path.write_text("Review Python changes. Check independent tests.")
    assert (await command("skill", "add", str(path), "--trust"))["trusted"]
    assert (await command("skill", "search", "Python"))["items"][0]["id"] == "review"
    assert (await command("skill", "validate", "review"))["valid"]
    await command("preset", "create", "focus", "--skill", "review")
    exported = tmp_path / "preset.json"
    await command("preset", "export", "focus", "--output", str(exported))
    await command("preset", "delete", "focus")
    await command("preset", "import", str(exported))
    assert (await command("preset", "use", "focus"))["scope"] == "global"
    assert (await command("preset", "list"))["total"] == 1
    bundle = tmp_path / "diagnostics.zip"
    await command("doctor", "--bundle", str(bundle))
    assert bundle.exists()
    assert isinstance(await command("logs", "tail"), list)


async def test_provider_conformance_bounds_and_offline_inference(tmp_path):
    from local_ai_router.config import Settings
    from local_ai_router.engine import Engine
    from local_ai_router.schema import Model, Provider

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"answer":"ok"}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )

    settings = Settings(
        home=tmp_path,
        providers={
            "fixture": Provider(kind="openai-compatible", local=True, auth="none", endpoint="http://localhost:1234/v1")
        },
        models=[Model(id="fixture", provider="fixture", deployment_name="fixture", input_price=0, output_price=0)],
    )
    engine = Engine(settings, httpx.MockTransport(handle))
    with pytest.raises(ValueError, match="Unknown"):
        await check_provider(engine, "missing")
    with pytest.raises(ValueError, match="budget"):
        await check_provider(engine, "fixture", budget=float("inf"))
    try:
        result = await check_provider(engine, "fixture", inference=True, budget=0.01)
        assert any(c["check"] == "text" and c["status"] == "passed" for c in result["checks"]), result
        assert engine.store.costs()["estimated_usd"] == 0
    finally:
        await engine.close()


def test_cli_reports_safe_error_id_and_records_duration(settings, capsys):
    from local_ai_router.cli import main
    from local_ai_router.observability import read_logs

    with pytest.raises(SystemExit) as failure:
        main(["skill", "info", "missing", "--home", str(settings.home), "--mock", "--json", "--debug"])
    assert failure.value.code != 0
    lines = capsys.readouterr().err.splitlines()
    error = json.loads(lines[0])
    assert error["error_id"] and error["exit_code"] == failure.value.code
    logs = read_logs(settings)
    assert any(item.get("error_id") == error["error_id"] for item in logs)
    end = next(item for item in logs if item["event"] == "end" and item["component"] == "cli")
    assert end["duration_ms"] >= 0 and end["exit_code"] == failure.value.code
    assert "missing" not in json.dumps(logs)


@pytest.mark.parametrize("arguments", [["version"], ["completion", "powershell"], ["init"], ["doctor"], ["status"]])
def test_cli_entrypoint_offline_commands(settings, capsys, arguments):
    from local_ai_router.cli import main

    try:
        main([*arguments, "--home", str(settings.home), "--mock", "--json"])
    except SystemExit as failure:
        assert arguments == ["status"] and failure.code == 6
    output = capsys.readouterr().out
    assert output and "Traceback" not in output


@pytest.mark.parametrize(
    "action",
    [
        "status",
        "skills",
        "skill-search",
        "skill-suggest",
        "skill-recipes",
        "presets",
        "models",
        "roles",
        "providers",
        "costs",
        "budget",
        "savings",
        "cache",
        "recovery",
        "trace",
    ],
)
async def test_grouped_mcp_reads_work_without_inference(engine, settings, action):
    mcp = create_mcp(settings, engine)
    result = await mcp.call_tool("router_info", {"action": action, "query": "debug"})
    assert result
    assert engine.store.costs()["calls"] == 0


async def test_mcp_preset_selection_and_orchestration(engine, settings, repo):
    engine.skills.save_preset("empty", [])
    mcp = create_mcp(settings, engine)
    await mcp.call_tool("router_info", {"action": "select-preset", "preset": "empty", "session": "test"})
    result = await mcp.call_tool(
        "orchestrate_feature",
        {
            "task": "Add a greeting feature with tests",
            "repo_path": str(repo),
            "preset": "empty",
            "skill_mode": "manual",
            "execution_mode": "plan-only",
        },
    )
    assert "planned" in str(result)
    assert not (repo / "greeting.py").exists()
