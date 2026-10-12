"""Independent HTTP, CLI and MCP clients share one authoritative attempt."""

import httpx
import pytest
from test_workbench_execution import execution as execution
from test_workbench_execution import item, queue
from test_workbench_mcp import decode

from local_ai_router.app import create_app
from local_ai_router.cli import dispatch
from local_ai_router.cli_parser import parser
from local_ai_router.mcp_server import create_mcp
from local_ai_router.workbench import cli as wb_cli
from local_ai_router.workbench import mcp as wb_mcp
from local_ai_router.workbench.client import WorkbenchClient
from local_ai_router.workbench.store import identity


@pytest.fixture
async def interfaces(execution, monkeypatch):
    runtime = execution[0]
    app = create_app(runtime.engine.settings, runtime.engine)
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(wb_cli, "WorkbenchClient", lambda settings: WorkbenchClient(settings, transport=transport))
    monkeypatch.setattr(wb_mcp, "WorkbenchClient", lambda settings: WorkbenchClient(settings, transport=transport))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer " + runtime.engine.settings.token},
    ) as http:
        yield http, create_mcp(runtime.engine.settings, runtime.engine), create_mcp(runtime.engine.settings)


async def test_http_cli_mcp_execution_same_attempt(execution, interfaces):
    runtime, ws, _, _, _ = execution
    http, shared, stdio = interfaces
    work = item(execution)
    attempt = queue(execution, work)
    latest = await http.get("/accs/v1/workbench/work-items/" + work["id"] + "/attempts", params={"workspace_id": ws.id})
    cli = await dispatch(
        parser().parse_args(["work", "attempts", work["id"], "--workspace", ws.id, "--json"]), runtime.engine.settings
    )
    for mcp in (shared, stdio):
        result = decode(await mcp.call_tool("list_work_attempts", {"workspace_id": ws.id, "item_id": work["id"]}))
        assert result == cli == latest.json()
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    receipt = await http.get("/accs/v1/workbench/attempts/" + attempt + "/receipt", params={"workspace_id": ws.id})
    assert receipt.json() == decode(
        await shared.call_tool("get_work_attempt", {"workspace_id": ws.id, "attempt_id": attempt, "section": "receipt"})
    )


async def test_duplicate_execute_one_run(execution, interfaces):
    runtime, ws, _, _, _ = execution
    http, shared, _ = interfaces
    attempt = queue(execution, item(execution))
    row, contract = runtime.execution.contract(attempt)
    current = runtime.service.store.get("work_item", ws.id, contract.item_id)
    # New execution is rejected while an authoritative attempt owns this item.
    body = {"command_id": identity(), "expected_revision": current.revision, "command": "request_execution"}
    result = await http.post(
        "/accs/v1/workbench/work-items/" + current.id + "/commands", params={"workspace_id": ws.id}, json=body
    )
    assert result.status_code == 409
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert runtime.engine.store.db.execute("SELECT COUNT(*) FROM wb_attempts").fetchone()[0] == 1


async def test_mcp_cannot_approve_as_human(execution, interfaces):
    runtime, ws, _, _, _ = execution
    _, shared, stdio = interfaces
    attempt = queue(execution, item(execution))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    current = runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt)[1].item_id)
    evidence, _ = runtime.service.evidence(current)
    for mcp in (shared, stdio):
        with pytest.raises(Exception, match="granted|human"):
            await mcp.call_tool(
                "work_item_command",
                {
                    "workspace_id": ws.id,
                    "item_id": current.id,
                    "command_id": identity(),
                    "expected_revision": current.revision,
                    "command": "approve_completion",
                    "fields": {
                        "contract_revision": current.contract_revision,
                        "evidence_hash": evidence["evidence_hash"],
                        "reason": "model text",
                    },
                },
            )
    assert runtime.service.store.get("work_item", ws.id, current.id).status == "REVIEW"


async def test_scoped_artifact_and_receipt_access(execution, interfaces):
    runtime, ws, _, _, repo = execution
    http, _, _ = interfaces
    attempt = queue(execution, item(execution))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    artifacts = (
        await http.get("/accs/v1/workbench/attempts/" + attempt + "/artifacts", params={"workspace_id": ws.id})
    ).json()["items"]
    path = "/accs/v1/workbench/artifacts/" + artifacts[0]["id"] + "/content"
    content = await http.get(path, params={"workspace_id": ws.id})
    assert content.status_code == 200 and "def greet" in content.json()["content"]
    assert (await http.get(path, params={"workspace_id": identity()})).status_code == 404
    (repo / "greeting.py").write_text("user change\n")
    assert (await http.get(path, params={"workspace_id": ws.id})).json()["error"]["code"] == "ARTIFACT_CHANGED"


async def test_explicit_cli_completion_operator(execution, interfaces):
    runtime, ws, _, _, _ = execution
    attempt = queue(execution, item(execution))
    await runtime.execution.execute(attempt, runtime.agents.generation, 1)
    current = runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt)[1].item_id)
    evidence, _ = runtime.service.evidence(current)
    result = await dispatch(
        parser().parse_args(
            [
                "work",
                "approve",
                current.id,
                "--workspace",
                ws.id,
                "--expected-revision",
                str(current.revision),
                "--evidence-hash",
                evidence["evidence_hash"],
                "--reason",
                "Reviewed exact files",
            ]
        ),
        runtime.engine.settings,
    )
    assert result["status"] == "DONE"
    assert runtime.engine.store.db.execute("SELECT actor_type FROM wb_approvals").fetchone()[0] == "HUMAN"


async def test_lifecycle_reads_make_no_provider_calls(execution, interfaces, monkeypatch):
    runtime, ws, _, _, _ = execution
    http, _, _ = interfaces

    def fail(*a, **k):
        pytest.fail("read invoked provider")

    monkeypatch.setattr(runtime.engine.providers, "generate", fail)
    for path in ("/agents", "/status", "/privacy/diagnostics", "/activity"):
        assert (await http.get("/accs/v1/workbench" + path, params={"workspace_id": ws.id})).status_code == 200
