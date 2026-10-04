import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from local_ai_router.app import create_app
from local_ai_router.cli import main
from local_ai_router.config import Settings
from local_ai_router import integrations, receipts
from local_ai_router.recovery import recover
from local_ai_router.schema import Request


def cli(home, *arguments):
    return subprocess.run([sys.executable, "-m", "local_ai_router.cli", "--home", str(home), *arguments],
        capture_output=True, encoding="utf-8", timeout=40)


def test_cli_init_human_json_run_receipt_and_errors(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = cli(tmp_path, "init", "--non-interactive", "--repo", str(repo), "--validation", "python-unittest", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "initialized"
    route = cli(tmp_path, "--mock", "route", "Update documentation", "--repo", str(repo), "--explain")
    assert route.returncode == 0 and "Inference calls: 0" in route.stdout and "Route: mock-worker" in route.stdout
    result = cli(tmp_path, "--mock", "run", "Add a greeting feature with tests", "--repo", str(repo), "--json")
    assert result.returncode == 0, result.stderr
    run = json.loads(result.stdout)
    assert run["status"] == "complete" and "Final validation passed" in result.stderr
    result = cli(tmp_path, "--mock", "receipt", run["plan_id"], "--json")
    receipt = json.loads(result.stdout)
    assert receipt["economics"]["actual_cost"] == "0"
    assert receipt["validation"] and receipt["cloud_egress"]["context_tokens_upper_bound"] == 0
    savings = cli(tmp_path, "savings", "--today", "--json")
    assert json.loads(savings.stdout)["actual_cost"] == "0"
    assert "API cost" in cli(tmp_path, "savings").stdout
    assert "Samples" in cli(tmp_path, "--mock", "model", "dna", "mock-worker").stdout
    missing = cli(tmp_path, "model", "info", "missing", "--json")
    assert missing.returncode == 2 and json.loads(missing.stderr)["type"] == "ValueError"
    for shell in ("bash", "zsh", "fish", "powershell"):
        result = cli(tmp_path, "completion", shell)
        assert result.returncode == 0 and "accs" in result.stdout
    assert cli(tmp_path, "serve", "--host", "0.0.0.0").returncode == 2
    assert cli(tmp_path, "serve", "--remote").returncode == 2


def test_launch_exit_and_ipv6(monkeypatch, settings, capsys):
    monkeypatch.setattr("local_ai_router.cli.load", lambda *_: settings)
    monkeypatch.setattr(integrations, "launch", lambda *args: {"exit_code": 17})
    with pytest.raises(SystemExit) as error:
        main(["launch", "codex"])
    assert error.value.code == 17
    from local_ai_router.launcher import announce
    settings.host = "::1"
    announce(settings, True)
    assert "http://[::1]:8765/ui" in capsys.readouterr().out


async def test_integration_apply_undo_fallback_and_no_token_output(engine, tmp_path, monkeypatch):
    target = tmp_path / "client.toml"
    target.write_text('model = "user-model"\n')
    monkeypatch.setattr(integrations, "target", lambda _: target)
    monkeypatch.setattr(integrations, "capabilities", lambda _: {"installed": True, "sovereign": True})
    quote = integrations.preview(engine, "codex")
    assert target.read_text() == 'model = "user-model"\n'
    installed = integrations.install(engine, quote["id"])
    assert Path(installed["backup"]).is_file()
    assert "mcp_servers.local-ai-router" in target.read_text()
    integrations.undo(engine, "codex")
    assert target.read_text() == 'model = "user-model"\n'
    quote = integrations.preview(engine, "codex", "sovereign")
    assert quote["path"].endswith("codex.json")
    assert "wire_api=\"responses\"" in str(quote["registration"]["args"])
    assert engine.settings.token not in json.dumps(quote)
    integrations.install(engine, quote["id"])
    assert target.read_text() == 'model = "user-model"\n'
    integrations.undo(engine, "codex")
    assert not Path(quote["path"]).exists()
    monkeypatch.setattr(integrations, "capabilities", lambda _: {"installed": False, "sovereign": False})
    monkeypatch.setattr(integrations, "target", lambda _: tmp_path / "claude.json")
    fallback = integrations.preview(engine, "claude-desktop", "sovereign")
    assert fallback["mode"] == "companion" and fallback["fallback_reason"]


async def test_receipt_write_failure_cleanup_and_recovery_receipts(engine, repo, monkeypatch):
    original = receipts.save
    def unavailable(*args, **kwargs):
        raise OSError("disk fixture unavailable")
    monkeypatch.setattr(receipts, "save", unavailable)
    with pytest.raises(OSError):
        await engine.run(Request(task="Add a greeting feature with tests", repo_path=str(repo)))
    assert not engine.active_runs and engine.active_operations == 0
    monkeypatch.setattr(receipts, "save", original)
    run = engine.store.db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    result = await recover(engine, run, "rollback")
    assert result["status"] == "rolled_back"
    assert receipts.get(engine, run)["status"] == "rolled_back"


async def test_native_api_and_companion_mcp_share_engine_and_receipt(engine, repo):
    app = create_app(engine.settings, engine)
    # ASGI tests drive lifespan so the HTTP MCP session manager is active.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer " + engine.settings.token}) as client:
            route = await client.post("/accs/v1/route", json={"task": "Update documentation", "repo_path": str(repo)})
            assert route.status_code == 200 and route.json()["inference_calls"] == 0
            response = await client.post("/accs/v1/tasks", json={"task": "Add a greeting feature with tests", "repo_path": str(repo)})
            assert response.status_code == 200, response.text
            run = response.json()
            proof = (await client.get("/accs/v1/receipts/" + run["plan_id"])).json()
            status = (await client.get("/accs/v1/runs/" + run["plan_id"])).json()
            assert status["receipt_available"] and proof["validation"]
            mcp = await client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": "receipt", "arguments": {"run_id": run["plan_id"]}}})
            assert mcp.status_code == 200, mcp.text
            assert run["plan_id"] in mcp.text and "economics" in mcp.text
            for name, arguments, expected in [("estimate_cost", {"task": "Update documentation"}, "economics"), ("router_info", {"action": "savings"}, "header")]:
                mcp = await client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 2,
                    "method": "tools/call", "params": {"name": name, "arguments": arguments}})
                assert mcp.status_code == 200 and expected in mcp.text and not mcp.json()["result"].get("isError")
    # The fixture closes again; sqlite.close and provider.close are idempotent.


async def test_gateway_three_protocols_stream_and_nonstream_receipts(engine):
    from local_ai_router.gateway import forward
    for protocol, payload in [("chat", {"messages": [{"role": "user", "content": "hi"}]}),
                               ("responses", {"input": "hi"}), ("messages", {"messages": [{"role": "user", "content": "hi"}]})]:
        for stream in (False, True):
            result = await forward(engine, protocol, {"model": "accension-auto", "stream": stream, **payload})
            if stream:
                body = "".join([chunk async for chunk in result.body_iterator])
                assert "data:" in body
            else:
                assert json.loads(result.body)["model"] == "router-auto"
    assert engine.store.db.execute("SELECT COUNT(*) FROM receipts WHERE json_extract(data,'$.kind')='gateway'").fetchone()[0] == 6


async def test_custom_loopback_provider_no_key_required(engine, monkeypatch):
    from local_ai_router.cli_setup import provider_add
    from local_ai_router.cli_parser import parser
    from local_ai_router.management import Management
    args = parser().parse_args(["provider", "add", "custom", "--protocol", "openai", "--endpoint", "http://127.0.0.1:9000/v1", "--non-interactive"])
    result = provider_add(Management(engine), args)
    assert result["provider"] == "openai-compatible"
    assert engine.settings.providers["openai-compatible"].local is True
