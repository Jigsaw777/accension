"""Real ASGI requests through local bearer and cookie/CSRF adapters."""

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from local_ai_router.app import create_app
from local_ai_router.workbench.api import install, transport
from local_ai_router.workbench.policy import Principal
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import WorkbenchError, identity

MACHINE = "/accs/v1/workbench"
BROWSER = "/ui/workbench/api"


@pytest.mark.parametrize("kind", ["workbench", "http", "REVISION_CONFLICT", "FORBIDDEN", "HUMAN_REQUIRED"])
async def test_transport_errors_do_not_expose_exception_details(kind):
    app = FastAPI()

    @app.get("/failure")
    @transport
    async def failure(request: Request):
        if kind == "workbench":
            raise WorkbenchError("CORRUPT_DATA", "private path and injected provider secret", 500)
        if kind != "http":
            raise WorkbenchError(kind, "private path and injected provider secret", 409)
        raise HTTPException(400, "private path and injected provider secret")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/failure")
    assert response.status_code == (500 if kind == "workbench" else 400 if kind == "http" else 409)
    assert response.json()["error"]["code"] == (
        "CORRUPT_DATA" if kind == "workbench" else "INVALID_REQUEST" if kind == "http" else kind
    )
    assert "private path" not in response.text and "provider secret" not in response.text


@pytest.fixture
async def app_client(engine):
    app = create_app(engine.settings, engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        yield app, client, {"Authorization": "Bearer " + engine.settings.token}


async def setup(client, headers, prefix=MACHINE):
    ws = await client.post(prefix + "/workspaces", headers=headers, json={"command_id": identity(), "name": "Test"})
    assert ws.status_code == 201, ws.text
    ws = ws.json()
    project = await client.post(
        prefix + "/projects",
        headers=headers,
        json={"command_id": identity(), "workspace_id": ws["id"], "key": "WB", "name": "Test"},
    )
    assert project.status_code == 201, project.text
    return ws, project.json()


async def test_local_api_and_browser_service_consistency(app_client):
    app, client, headers = app_client
    assert app.state.workbench.engine is app.state.engine
    ws, project = await setup(client, headers)
    req = {
        "command_id": identity(),
        "workspace_id": ws["id"],
        "project_id": project["id"],
        "title": "Shared",
        "execution_kind": "MANUAL",
    }
    result = await client.post(MACHINE + "/work-items", headers=headers, json=req)
    assert result.status_code == 201, result.text
    item = result.json()
    assert item["created_by"] == "service"
    await client.get("/ui")
    csrf = (await client.get("/ui/session")).json()["csrf"]
    read = await client.get(BROWSER + "/work-items/" + item["id"], params={"workspace_id": ws["id"]})
    assert read.status_code == 200 and read.json()["title"] == "Shared"
    changed = await client.patch(
        BROWSER + "/work-items/" + item["id"],
        params={"workspace_id": ws["id"]},
        headers={"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": csrf},
        json={"command_id": identity(), "expected_revision": 1, "title": "Human changed"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["updated_by"].startswith("browser-")
    assert (await client.post(MACHINE + "/work-items", headers=headers, json=req)).json() == item
    assert app.state.engine.settings.token not in read.text + changed.text


async def test_workbench_csrf_and_host_guard(app_client):
    _, client, headers = app_client
    req = {"command_id": identity(), "name": "Blocked"}
    assert (await client.post(MACHINE + "/workspaces", json=req)).status_code == 401
    await client.get("/ui")
    assert (await client.post(BROWSER + "/workspaces", json=req)).status_code == 403
    csrf = (await client.get("/ui/session")).json()["csrf"]
    browser_headers = {"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": csrf}
    assert (await client.post(BROWSER + "/workspaces", headers=browser_headers, json=req)).status_code == 201
    for hostile in ({"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
        assert (await client.get(MACHINE + "/status", headers={**headers, **hostile})).status_code == 403
    assert (await client.get(MACHINE + "/status")).status_code == 401


async def test_scoped_uuid_lookup_denied(app_client):
    app, client, headers = app_client
    ws, project = await setup(client, headers)
    req = {
        "command_id": identity(),
        "workspace_id": ws["id"],
        "project_id": project["id"],
        "title": "Private",
        "execution_kind": "MANUAL",
    }
    item = (await client.post(MACHINE + "/work-items", headers=headers, json=req)).json()
    wrong = identity()
    denied = await client.get(MACHINE + "/work-items/" + item["id"], params={"workspace_id": wrong}, headers=headers)
    assert denied.status_code == 404
    scoped = FastAPI()
    principal = Principal("scoped", ActorType.MACHINE, frozenset({"workspace.read"}), frozenset({wrong}))
    install(scoped, app.state.workbench, prefixes=("/restricted",), principal_resolver=lambda _: principal)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=scoped), base_url="http://127.0.0.1") as restricted:
        response = await restricted.get("/restricted/work-items/" + item["id"], params={"workspace_id": ws["id"]})
        assert response.status_code == 404 and "Private" not in response.text


async def test_body_and_unknown_field_limits(app_client):
    _, client, headers = app_client
    ws, project = await setup(client, headers)
    for data in (
        {
            "command_id": identity(),
            "workspace_id": ws["id"],
            "project_id": project["id"],
            "title": "Injected",
            "execution_kind": "MANUAL",
            "status": "DONE",
        },
        {
            "command_id": identity(),
            "workspace_id": ws["id"],
            "project_id": project["id"],
            "title": "Injected",
            "execution_kind": "MANUAL",
            "actor_type": "HUMAN",
        },
    ):
        response = await client.post(MACHINE + "/work-items", headers=headers, json=data)
        assert response.status_code == 400 and response.json()["error"]["code"] == "INVALID_SCHEMA"
    assert (await client.post(MACHINE + "/workspaces", headers=headers, content="[1]")).status_code == 400
    assert (await client.post(MACHINE + "/workspaces", headers=headers, content=b"x" * 2_000_001)).status_code == 413
    assert (
        await client.get(MACHINE + "/work-items", headers=headers, params={"workspace_id": ws["id"], "limit": 201})
    ).status_code == 422
    assert (
        await client.get(MACHINE + "/work-items", headers=headers, params={"workspace_id": ws["id"], "cursor": "bad!"})
    ).status_code == 422


async def test_v1_routes_unchanged(app_client):
    _, client, headers = app_client
    assert (await client.get("/health")).json()["status"] == "ok"
    models = await client.get("/v1/models", headers=headers)
    assert models.status_code == 200 and models.json()["object"] == "list"
    costs = await client.get("/router/costs", headers=headers)
    assert costs.status_code == 200
    missing = await client.get(
        MACHINE + "/attempts/" + identity(),
        params={"workspace_id": "00000000-0000-4000-8000-000000000001"},
        headers=headers,
    )
    assert missing.status_code == 404


async def test_prd_templates_and_revision_routes(app_client):
    _, client, headers = app_client
    ws, project = await setup(client, headers)
    prd = await client.post(
        MACHINE + "/prds",
        headers=headers,
        json={"command_id": identity(), "workspace_id": ws["id"], "project_id": project["id"], "title": "Draft"},
    )
    assert prd.status_code == 201
    revisions = await client.get(
        MACHINE + "/prds/" + prd.json()["id"] + "/revisions", headers=headers, params={"workspace_id": ws["id"]}
    )
    assert len(revisions.json()["items"]) == 1
    templates = await client.get(MACHINE + "/templates", headers=headers)
    assert len(templates.json()["items"]) == 2
    exported = await client.get(MACHINE + "/templates/prd-default/export", headers=headers)
    assert exported.status_code == 200 and "schema_version: 1" in exported.text
