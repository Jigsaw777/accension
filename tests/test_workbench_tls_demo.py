"""M3 real verified HTTPS clients inspect the actual M2 Engine workflow."""

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest
import uvicorn
from test_workbench_lan_access import pair
from workbench_tls import certificates, port

from local_ai_router.config import WorkbenchLAN
from local_ai_router.workbench.lan import LAN_PREFIX, create_lan_app
from local_ai_router.workbench.store import identity


async def test_full_tls_two_client_demo(tmp_path):
    cert, key, trust = certificates(tmp_path)
    remote_port = port()
    url = f"https://127.0.0.1:{remote_port}"
    devices, previous, preserved = [], [], []
    source = Path(__file__).resolve().parents[1] / "scripts/demo_workbench.py"
    spec = importlib.util.spec_from_file_location("tls_demo_workbench", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def verify(runtime, phase):
        ws = runtime.service.seed_defaults()
        runtime.engine.settings.workbench_lan = WorkbenchLAN(
            enabled=True,
            port=remote_port,
            certificate=str(cert),
            private_key=str(key),
            origins=[url],
            workspaces=[ws.id],
        )
        if phase == "completed":
            devices.extend(
                [pair(runtime, ws, ["workspace.read", "artifact.read"], name=name) for name in ("One", "Two")]
            )
        server = uvicorn.Server(
            uvicorn.Config(
                create_lan_app(runtime),
                host="127.0.0.1",
                port=remote_port,
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
                lifespan="off",
                access_log=False,
                log_level="error",
                proxy_headers=False,
            )
        )
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(200):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            assert server.started
            async with (
                httpx.AsyncClient(base_url=url, verify=trust, trust_env=False) as one,
                httpx.AsyncClient(base_url=url, verify=trust, trust_env=False) as two,
            ):
                views = []
                for index, client in enumerate((one, two)):
                    client.headers["authorization"] = "Bearer " + devices[index]["token"]
                    snapshot = await client.post(
                        LAN_PREFIX + "/snapshots",
                        json={"workspace_id": ws.id, "filters": {"kinds": ["work_item"]}, "limit": 100},
                    )
                    assert snapshot.status_code == 200, snapshot.text
                    data = snapshot.json()
                    views.append(
                        [
                            (entry["payload"]["display_key"], entry["payload"]["revision"], entry["payload"]["status"])
                            for entry in data["items"]
                        ]
                    )
                    assert all(entry[2] == "DONE" for entry in views[-1]) and len(views[-1]) == 3
                    activity = await client.get(
                        LAN_PREFIX + "/activity",
                        params={"workspace_id": ws.id, "after": previous[index] if phase == "restarted" else 0},
                    )
                    assert activity.status_code == 200
                    if phase == "completed":
                        previous.append(data["sequence"])
                    else:
                        assert activity.json()["items"] == []
                assert views[0] == views[1]
                if phase == "completed":
                    preserved.extend(views[0])
                else:
                    assert views[0] == preserved
                wrong = await two.get(LAN_PREFIX + "/projects", params={"workspace_id": identity()})
                assert wrong.status_code == 404
                if phase == "restarted":
                    from test_workbench_agents import HUMAN

                    runtime.devices.revoke(ws.id, devices[1]["device_id"], 1, HUMAN)
                    assert (
                        await two.get(LAN_PREFIX + "/work-items", params={"workspace_id": ws.id})
                    ).status_code == 401
                    assert (
                        await one.get(LAN_PREFIX + "/work-items", params={"workspace_id": ws.id})
                    ).status_code == 200
            async with httpx.AsyncClient(base_url=url, trust_env=False) as untrusted:
                with pytest.raises(httpx.ConnectError):
                    await untrusted.get("/health")
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)

    result = await module.demo(tmp_path / "home", tmp_path / "repo", verify_clients=verify)
    assert result["attempts"] == 3 and result["checks"] == "executed unittest"
