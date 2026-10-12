"""CLI forwards to daemon, preserving command/revision identity and legacy parser."""

import json

import httpx
import pytest

from local_ai_router.app import create_app
from local_ai_router.cli import dispatch
from local_ai_router.cli_parser import completion, parser
from local_ai_router.workbench import cli as wb_cli
from local_ai_router.workbench.client import WorkbenchClient
from local_ai_router.workbench.store import WorkbenchError, identity


@pytest.fixture
async def context(engine, monkeypatch):
    app = create_app(engine.settings, engine)
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(wb_cli, "WorkbenchClient", lambda settings: WorkbenchClient(settings, transport=transport))
    return engine, app


async def run(engine, *arguments):
    return await dispatch(parser().parse_args(list(arguments)), engine.settings)


async def test_cli_workbench_does_not_construct_engine(context, monkeypatch):
    engine, _ = context

    def forbidden(*_):
        pytest.fail("Workbench CLI constructed Engine")

    monkeypatch.setattr("local_ai_router.cli.Engine", forbidden)
    workspaces = await run(engine, "workspace", "list", "--json")
    assert len(workspaces["items"]) == 1
    status = await run(engine, "workbench", "status")
    assert status["counts"] == {}
    assert engine.store.costs()["calls"] == 0


async def test_unknown_agent_status_is_a_domain_error(context):
    engine, _ = context
    ws = (await run(engine, "workspace", "list"))["items"][0]["id"]
    with pytest.raises(WorkbenchError, match="Agent not found"):
        await run(engine, "agent", "status", identity(), "--workspace", ws)


async def test_cli_json_parser_and_idempotency(context, tmp_path):
    engine, _ = context
    ws = await run(engine, "workspace", "create", "--name", "Test", "--json")
    project = await run(engine, "project", "create", "--workspace", ws["id"], "--key", "WB", "--name", "Test")
    path = tmp_path / "work.json"
    path.write_text(json.dumps({"title": "CLI task", "execution_kind": "MANUAL"}))
    command_id = identity()
    args = (
        "work",
        "create",
        "--workspace",
        ws["id"],
        "--project",
        project["id"],
        "--file",
        str(path),
        "--command-id",
        command_id,
    )
    item = await run(engine, *args)
    assert item == await run(engine, *args)
    shown = await run(engine, "work", "show", "WB-1", "--workspace", ws["id"])
    assert shown["id"] == item["id"]
    path.write_text(json.dumps({"title": "Edited"}))
    edited = await run(
        engine, "work", "edit", "WB-1", "--workspace", ws["id"], "--file", str(path), "--expected-revision", "1"
    )
    assert edited["revision"] == 2
    with pytest.raises(WorkbenchError, match="changed"):
        await run(
            engine, "work", "edit", "WB-1", "--workspace", ws["id"], "--file", str(path), "--expected-revision", "1"
        )
    assert (await run(engine, "work", "list", "--workspace", ws["id"], "--project", project["id"], "--limit", "1"))[
        "items"
    ][0]["title"] == "Edited"


async def test_stopped_daemon_and_legacy_commands(settings):
    def unavailable(request):
        raise httpx.ConnectError("stopped", request=request)

    client = WorkbenchClient(settings, transport=httpx.MockTransport(unavailable))
    try:
        with pytest.raises(WorkbenchError, match="accs start"):
            await client.request("GET", "/status")
    finally:
        await client.close()
    assert parser().parse_args(["route", "Task", "--json"]).command == "route"
    assert parser().parse_args(["mcp"]).command == "mcp"
    for shell in ("powershell", "bash", "fish", "zsh"):
        assert "workbench" in completion(shell) and "workspace" in completion(shell)


@pytest.mark.parametrize("mode", ["redirect", "wrong_service", "bad_json", "oversized"])
async def test_client_identity_and_response_bounds(settings, mode):
    def response(request):
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://example.invalid"})
        if mode == "wrong_service":
            return httpx.Response(200, json={"status": "ok", "service": "other"})
        if mode == "bad_json":
            return httpx.Response(200, content="not json")
        return httpx.Response(200, content=b"x" * 2_000_001)

    client = WorkbenchClient(settings, transport=httpx.MockTransport(response))
    try:
        with pytest.raises(WorkbenchError):
            await client.request("GET", "/status")
    finally:
        await client.close()


def test_bounded_data_only_input(tmp_path):
    path = tmp_path / "input.yaml"
    for raw in ("!!python/object/apply:os.system [no]", "x: &a [1]\ny: *a", "a: " + "[" * 18 + "1" + "]" * 18):
        path.write_text(raw)
        with pytest.raises(ValueError):
            wb_cli.input_file(path)
    path.write_text("title: Test\nexecution_kind: MANUAL")
    assert wb_cli.input_file(path)["execution_kind"] == "MANUAL"


async def test_template_export_and_prd_explicit_operator_approval(context, tmp_path):
    engine, _ = context
    target = tmp_path / "export.yaml"
    await run(engine, "template", "export", "prd-default", "--output", str(target))
    assert "schema_version: 1" in target.read_text()
    ws = await run(engine, "workspace", "create", "--name", "PRD")
    project = await run(engine, "project", "create", "--workspace", ws["id"], "--name", "PRD", "--key", "PR")
    path = tmp_path / "prd.json"
    path.write_text(
        json.dumps(
            {
                "title": "PRD",
                "spec": {
                    "problem_statement": "Problem",
                    "objectives": ["Goal"],
                    "scope": ["Scope"],
                    "functional_requirements": [{"id": "FR-1", "text": "Requirement"}],
                    "acceptance_criteria": [{"id": "AC-1", "text": "Criterion"}],
                },
            }
        )
    )
    prd = await run(engine, "prd", "create", "--workspace", ws["id"], "--project", project["id"], "--file", str(path))
    await run(engine, "prd", "submit-review", prd["id"], "--workspace", ws["id"], "--expected-revision", "1")
    approved = await run(engine, "prd", "approve", prd["id"], "--workspace", ws["id"], "--expected-revision", "2")
    assert approved["status"] == "APPROVED" and approved["updated_by"] == "local-cli"
