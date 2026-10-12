"""Remote trust boundary reuses scoped commands without local authority."""

import httpx
import pytest
from test_workbench_agents import HUMAN

from local_ai_router.config import WorkbenchLAN
from local_ai_router.workbench.lan import LAN_PREFIX, create_lan_app
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.store import identity


def pair(runtime, ws, capabilities, browser=False, name="Remote"):
    issued = runtime.devices.invitation(ws.id, HUMAN)
    pending = runtime.devices.request(issued["code"], name, name)
    runtime.devices.approve(pending["id"], ws.id, capabilities, pending["verification_phrase"], HUMAN)
    return runtime.devices.claim(pending["id"], pending["challenge"], browser, name)


@pytest.fixture
async def lan(engine):
    runtime = WorkbenchRuntime(engine)
    ws = runtime.service.seed_defaults()
    runtime.service.update(
        "workspace",
        ws.id,
        ws.id,
        {"command_id": identity(), "expected_revision": ws.revision, "privacy_mode": "PRIVATE_LAN"},
        HUMAN,
    )
    engine.settings.workbench_lan = WorkbenchLAN(origins=["https://localhost:8766"], workspaces=[ws.id])
    project = runtime.service.create(
        "project", {"command_id": identity(), "workspace_id": ws.id, "name": "Remote project", "key": "LAN"}, HUMAN
    ).body
    app = create_lan_app(runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://localhost:8766") as client:
        yield runtime, ws, project, app, client
    await runtime.close()


async def test_lan_read_only_and_mutation_scopes(lan):
    runtime, ws, project, _, client = lan
    readonly = pair(runtime, ws, ["workspace.read"])
    headers = {"authorization": "Bearer " + readonly["token"]}
    assert (
        await client.get(LAN_PREFIX + "/projects", params={"workspace_id": ws.id}, headers=headers)
    ).status_code == 200
    data = {
        "command_id": identity(),
        "workspace_id": ws.id,
        "project_id": project["id"],
        "title": "Remote card",
        "execution_kind": "MANUAL",
    }
    assert (await client.post(LAN_PREFIX + "/work-items", json=data, headers=headers)).status_code == 403
    writer = pair(runtime, ws, ["workspace.read", "work.write"])
    headers = {"authorization": "Bearer " + writer["token"]}
    created = await client.post(LAN_PREFIX + "/work-items", json=data, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["created_by"] == "device-" + writer["device_id"]
    assert (await client.post(LAN_PREFIX + "/work-items", json=data, headers=headers)).json() == created.json()
    for field, value in (("review_policy", "AUTO_VERIFY"), ("budget_limit", "10"), ("privacy_policy", "ALLOW_CLOUD")):
        denied = await client.patch(
            LAN_PREFIX + "/work-items/" + created.json()["id"],
            params={"workspace_id": ws.id},
            headers=headers,
            json={"command_id": identity(), "expected_revision": 1, field: value},
        )
        assert denied.status_code in {400, 403}, denied.text
    forged = await client.post(
        LAN_PREFIX + "/work-items", json={**data, "command_id": identity(), "actor_type": "HUMAN"}, headers=headers
    )
    assert forged.status_code == 400


async def test_cross_workspace_idor_and_device_only_denied(lan):
    runtime, ws, project, _, client = lan
    device = pair(runtime, ws, ["workspace.read"])
    headers = {"authorization": "Bearer " + device["token"]}
    wrong = identity()
    result = await client.get(
        LAN_PREFIX + "/projects/" + project["id"], params={"workspace_id": wrong}, headers=headers
    )
    assert result.status_code == 404 and project["name"] not in result.text
    current = runtime.service.store.get("workspace", ws.id, ws.id)
    runtime.service.update(
        "workspace",
        ws.id,
        ws.id,
        {"command_id": identity(), "expected_revision": current.revision, "privacy_mode": "DEVICE_ONLY"},
        HUMAN,
    )
    assert (
        await client.get(LAN_PREFIX + "/projects", params={"workspace_id": ws.id}, headers=headers)
    ).status_code == 403


async def test_host_origin_csrf_csp_and_revocation(lan):
    runtime, ws, project, _, client = lan
    device = pair(runtime, ws, ["workspace.read", "work.write"], True)
    client.cookies.set("accension_device", device["token"])
    path = LAN_PREFIX + "/work-items"
    data = {
        "command_id": identity(),
        "workspace_id": ws.id,
        "project_id": project["id"],
        "title": "Browser",
        "execution_kind": "MANUAL",
    }
    assert (await client.post(path, json=data)).status_code == 403
    headers = {"origin": "https://localhost:8766", "x-csrf-token": device["csrf"]}
    result = await client.post(path, json=data, headers=headers)
    assert result.status_code == 201, result.text
    assert "default-src 'self'" in result.headers["content-security-policy"]
    for hostile in ({"host": "evil.test:8766"}, {"origin": "https://evil.test"}, {"sec-fetch-site": "cross-site"}):
        assert (await client.get(path, params={"workspace_id": ws.id}, headers=hostile)).status_code == 403
    runtime.devices.revoke(ws.id, device["device_id"], 1, HUMAN)
    assert (await client.get(path, params={"workspace_id": ws.id})).status_code == 401


async def test_lan_never_exposes_v1_admin_or_secrets(lan):
    runtime, ws, _, app, client = lan
    device = pair(runtime, ws, ["workspace.read"])
    headers = {"authorization": "Bearer " + device["token"]}
    for path in (
        "/v1/models",
        "/mcp",
        "/ui/action",
        "/router/costs",
        LAN_PREFIX + "/repositories",
        LAN_PREFIX + "/devices",
        LAN_PREFIX + "/devices/commands",
        LAN_PREFIX + "/agents/configure",
        LAN_PREFIX + "/privacy/diagnostics",
    ):
        result = await client.get(path, headers=headers)
        assert result.status_code == 404, (path, result.text)
    assert (
        await client.get(
            LAN_PREFIX + "/workspaces", headers={"authorization": "Bearer " + runtime.engine.settings.token}
        )
    ).status_code == 401
    public = await client.get(LAN_PREFIX + "/agents", params={"workspace_id": ws.id}, headers=headers)
    assert public.status_code == 200
    assert str(runtime.engine.settings.home) not in public.text and runtime.engine.settings.token not in public.text
    assert all(not route.path.startswith(("/ui", "/v1", "/router", "/mcp")) for route in app.routes)
