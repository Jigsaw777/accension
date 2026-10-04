import asyncio
import zipfile
from pathlib import Path

import pytest

from local_ai_router.observability import EventLog, export_bundle, read_logs, redact, scope


def test_log_redaction_drops_payload_and_sensitive_values(settings):
    log = EventLog(settings)
    fixtures = [
        "Authorization: Bearer " + "fake_bearer_value_for_test",
        "password: " + "fake_password_value",
        "Cookie: " + "fake_cookie_value",
        "gateway_token=" + "fake_gateway_value",
        "vault: " + "fake_vault_value",
        "https://example.test/?sig=fake_signed_value",
        "-----BEGIN " + "PRIVATE KEY-----\nfake_private_value\n-----END PRIVATE KEY-----",
        "api_key=" + "fake_api_value",
    ]
    for value in fixtures:
        log.emit("test", "redact", error_type=value, body=value, prompt=value, source=value)
    log.close()
    raw = log.path.read_text()
    for value in (
        "fake_bearer_value",
        "fake_password_value",
        "fake_cookie_value",
        "fake_gateway_value",
        "fake_vault_value",
        "fake_signed_value",
        "fake_private_value",
        "fake_api_value",
    ):
        assert value not in raw
    assert "body" not in raw and "prompt" not in raw
    assert redact({"Cookie": "x", "vault": {"key": "x"}, "authorization": "x"}) == {
        "Cookie": "[REDACTED]",
        "vault": "[REDACTED]",
        "authorization": "[REDACTED]",
    }


def test_rotation_levels_filters_and_bundle(settings, tmp_path):
    settings.log_max_bytes, settings.log_backups = 1024, 2
    log = EventLog(settings)
    log.emit("test", "hidden", "DEBUG")
    for i in range(50):
        log.emit("test", "row", request_id=str(i), status=200)
    log.close()
    assert len(list(log.path.parent.glob("accension.jsonl*"))) == 3
    assert all(p.stat().st_size < 1024 for p in log.path.parent.glob("accension.jsonl*"))
    assert read_logs(settings, run="49")[0]["request_id"] == "49"
    assert not read_logs(settings, level="DEBUG")
    assert not read_logs(settings, component="other")
    assert not read_logs(settings, since="2099-01-01")
    archive = tmp_path / "bundle.zip"
    export_bundle(settings, archive)
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {"logs.json", "diagnostics.json"}


async def test_concurrent_correlation_and_debug_exception(settings):
    settings.log_level = "DEBUG"
    log = EventLog(settings)

    async def task(i):
        with scope(request_id=str(i), session_id="s"):
            await asyncio.sleep(0)
            log.emit("test", "parallel", task_id=str(i))

    await asyncio.gather(*(task(i) for i in range(20)))
    try:
        raise ValueError("a private exception message")
    except ValueError as exc:
        error_id = log.failure("test", exc)
    rows = read_logs(settings)
    assert all(r["request_id"] == r["task_id"] for r in rows if r["event"] == "parallel")
    assert len(read_logs(settings, run=error_id)) == 2
    assert "private exception message" not in log.path.read_text()
    log.close()


def test_log_directory_and_disk_failures_do_not_break_operation(settings, monkeypatch, capsys):
    def denied(*args, **kwargs):
        raise PermissionError("private path must not be printed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", denied)
        failed = EventLog(settings)
        failed.emit("test", "still_running")
        assert not failed.status()["writable"]
        failed.close()
    log = EventLog(settings)
    monkeypatch.setattr(log.handler, "shouldRollover", denied)
    log.emit("test", "disk_failure")
    assert not log.status()["writable"]
    assert "private path" not in capsys.readouterr().err
    log.close()


async def test_provider_and_failed_run_trace_log_link(engine, repo):
    from local_ai_router.observability import read_logs
    from local_ai_router.receipts import get
    from local_ai_router.schema import Request

    plan = await engine.plan(Request(task="Add a greeting feature with tests", repo_path=str(repo)))
    engine.settings.repositories[0].validation["tests"] = ["{python}", "-m", "unittest", "missing_test_module"]
    with pytest.raises(RuntimeError, match="Error ID"):
        await engine.execute_plan(plan.plan_id, str(repo))
    trace = engine.store.traces(plan.request_id)
    failure = next(t for t in trace if t["stage"] == "failure")
    assert failure["error_id"]
    logs = read_logs(engine.settings, run=plan.request_id)
    assert any(r["component"] == "provider" and r["event"] == "end" for r in logs)
    assert any(r.get("error_id") == failure["error_id"] for r in logs)
    assert get(engine, plan.plan_id)["status"] == "failed"


def test_cli_log_events_and_safe_exception(settings, capsys):
    from local_ai_router.cli import main

    main(["--home", str(settings.home), "--mock", "skill", "list", "--json"])
    with pytest.raises(SystemExit):
        main(["--home", str(settings.home), "--mock", "skill", "info", "missing", "--json"])
    rows = read_logs(settings, component="cli")
    assert [r["event"] for r in rows] == ["start", "end", "start", "error", "end"]
    assert rows[-1]["exit_code"] != 0
    assert "error_id" in capsys.readouterr().err
