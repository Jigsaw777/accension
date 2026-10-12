"""Shared HTTP MCP and standalone forwarding expose the same Workbench state."""

import json

import httpx
import pytest

from local_ai_router.app import create_app
from local_ai_router.mcp_server import create_mcp
from local_ai_router.workbench import mcp as wb_mcp
from local_ai_router.workbench.client import WorkbenchClient
from local_ai_router.workbench.store import identity


def decode(result):
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


async def test_mcp_shared_and_stdio_same_state(engine, monkeypatch):
    app = create_app(engine.settings, engine)
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(wb_mcp, "WorkbenchClient", lambda settings: WorkbenchClient(settings, transport=transport))
    service = app.state.workbench.service
    ws = service.seed_defaults()
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer " + engine.settings.token},
    ) as client:
        project = (
            await client.post(
                "/accs/v1/workbench/projects",
                json={"command_id": identity(), "workspace_id": ws.id, "key": "MC", "name": "MCP"},
            )
        ).json()
    shared = create_mcp(engine.settings, engine)
    stdio = create_mcp(engine.settings)

    def forbidden(*_):
        pytest.fail("Standalone Workbench MCP constructed Engine")

    monkeypatch.setattr("local_ai_router.mcp_server.Engine", forbidden)
    async with stdio._mcp_server.lifespan(stdio._mcp_server) as state:
        assert state["engine"] is None
        arguments = {
            "workspace_id": ws.id,
            "project_id": project["id"],
            "command_id": identity(),
            "fields": {"title": "MCP task", "execution_kind": "MANUAL"},
        }
        created = decode(await shared.call_tool("create_work_item", arguments))
        read = decode(await stdio.call_tool("get_work_item", {"workspace_id": ws.id, "item_id": created["id"]}))
        assert read["id"] == created["id"] and read["revision"] == 1
        updated = decode(
            await stdio.call_tool(
                "update_work_item",
                {
                    "workspace_id": ws.id,
                    "item_id": created["id"],
                    "command_id": identity(),
                    "expected_revision": 1,
                    "fields": {"title": "Changed via stdio"},
                },
            )
        )
        shared_read = decode(await shared.call_tool("get_work_item", {"workspace_id": ws.id, "item_id": created["id"]}))
        assert shared_read["title"] == updated["title"]
        assert state["engine"] is None


async def test_machine_cannot_claim_human_approval(engine):
    app = create_app(engine.settings, engine)
    mcp = create_mcp(engine.settings, engine)
    names = {tool.name for tool in await mcp.list_tools()}
    assert "approve_completion" not in names and "approve_as_human" not in names
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer " + engine.settings.token},
    ) as client:
        ws = app.state.workbench.service.seed_defaults()
        project = (
            await client.post(
                "/accs/v1/workbench/projects",
                json={"command_id": identity(), "workspace_id": ws.id, "key": "MC", "name": "MCP"},
            )
        ).json()
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await mcp.call_tool(
            "create_work_item",
            {
                "workspace_id": ws.id,
                "project_id": project["id"],
                "command_id": identity(),
                "fields": {"title": "Injected", "execution_kind": "MANUAL", "actor_type": "HUMAN", "status": "DONE"},
            },
        )
