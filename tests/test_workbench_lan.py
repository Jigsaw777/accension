"""Real verified TLS and isolated listener ownership, with local fallback."""

import asyncio
import socket

import httpx
import pytest
import uvicorn
from workbench_tls import certificates, port

from local_ai_router.app import create_app
from local_ai_router.config import WorkbenchLAN
from local_ai_router.workbench.lan import create_lan_app, serve_pair, tls_context
from local_ai_router.workbench.runtime import WorkbenchRuntime


async def ready(server, task):
    for _ in range(300):
        if server.started:
            return
        if task.done():
            return await task
        await asyncio.sleep(0.01)
    raise AssertionError("Listener did not start")


def test_lan_disabled_default_no_socket(settings):
    assert not settings.workbench_lan.enabled


def test_tls_required_and_invalid_identity_rejected(settings, tmp_path):
    cert, key, _ = certificates(tmp_path)
    config = WorkbenchLAN(
        enabled=True,
        port=8766,
        certificate=str(cert),
        private_key=str(key),
        origins=["https://127.0.0.1:8766"],
        workspaces=["00000000-0000-4000-8000-000000000001"],
    )
    assert tls_context(config, 8765)
    with pytest.raises(ValueError, match="differ"):
        tls_context(config, 8766)
    config.origins = ["https://127.0.0.2:8766"]
    with pytest.raises(ValueError, match="SAN"):
        tls_context(config, 8765)


async def test_two_listeners_one_runtime(engine, tmp_path):
    settings = engine.settings
    settings.port = port()
    cert, key, trust = certificates(tmp_path)
    remote_port = port()
    settings.workbench_lan = WorkbenchLAN(
        enabled=True,
        port=remote_port,
        certificate=str(cert),
        private_key=str(key),
        origins=[f"https://127.0.0.1:{remote_port}"],
        workspaces=["00000000-0000-4000-8000-000000000001"],
    )
    app = create_app(settings, engine)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=settings.port, access_log=False, log_level="error", proxy_headers=False
        )
    )
    task = asyncio.create_task(serve_pair(server, app.state.workbench))
    try:
        await ready(server, task)
        async with httpx.AsyncClient(verify=trust, trust_env=False) as client:
            for _ in range(100):
                try:
                    result = await client.get(f"https://127.0.0.1:{remote_port}/health")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.02)
            else:
                pytest.fail("Verified LAN listener did not become ready")
            assert result.json()["service"] == "accension-workbench-lan"
            assert app.state.workbench.engine is engine and app.state.workbench.scheduler.loop_task
            for path in ("/v1/models", "/router/costs", "/mcp", "/ui/action", "/ui/workbench/api/workspaces"):
                assert (await client.get(f"https://127.0.0.1:{remote_port}" + path)).status_code == 404
            assert (await client.get(f"http://127.0.0.1:{settings.port}/health")).status_code == 200
        async with httpx.AsyncClient(trust_env=False) as untrusted:
            with pytest.raises(httpx.ConnectError):
                await untrusted.get(f"https://127.0.0.1:{remote_port}/health")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)


async def test_lan_failure_preserves_loopback_service(engine):
    settings = engine.settings
    settings.port = port()
    remote_port = port()
    settings.workbench_lan = WorkbenchLAN(
        enabled=True,
        port=remote_port,
        certificate="missing",
        private_key="missing",
        origins=[f"https://127.0.0.1:{remote_port}"],
        workspaces=["00000000-0000-4000-8000-000000000001"],
    )
    app = create_app(settings, engine)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=settings.port, log_level="error", access_log=False)
    )
    task = asyncio.create_task(serve_pair(server, app.state.workbench))
    try:
        await ready(server, task)
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(f"http://127.0.0.1:{settings.port}/health")).status_code == 200
        assert app.state.workbench.lan_error == "ValueError"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)


async def test_lan_startup_system_exit_preserves_loopback(engine, tmp_path, monkeypatch):
    cert, key, _ = certificates(tmp_path)
    engine.settings.port = port()
    remote = port()
    engine.settings.workbench_lan = WorkbenchLAN(
        enabled=True,
        port=remote,
        certificate=str(cert),
        private_key=str(key),
        origins=[f"https://127.0.0.1:{remote}"],
        workspaces=["00000000-0000-4000-8000-000000000001"],
    )
    original = uvicorn.Server.serve

    async def serve(server, sockets=None):
        if server.config.port == remote:
            raise SystemExit(1)
        return await original(server, sockets=sockets)

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    app = create_app(engine.settings, engine)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=engine.settings.port, access_log=False, log_level="error")
    )
    task = asyncio.create_task(serve_pair(server, app.state.workbench))
    try:
        await ready(server, task)
        for _ in range(100):
            if app.state.workbench.lan_error:
                break
            await asyncio.sleep(0.01)
        assert app.state.workbench.lan_error == "SystemExit"
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(f"http://127.0.0.1:{engine.settings.port}/health")).status_code == 200
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)


async def test_v1_routes_absent_on_lan_and_exact_host(engine):
    runtime = WorkbenchRuntime(engine)
    engine.settings.workbench_lan = WorkbenchLAN(origins=["https://localhost:8766"])
    app = create_lan_app(runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://localhost:8766") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/router/models")).status_code == 404
        assert (await client.get("/health", headers={"host": "evil.test:8766"})).status_code == 403
        assert (await client.get("/health", headers={"origin": "https://evil.test"})).status_code == 403
    await runtime.close()


async def test_lan_port_conflict_preserves_local(engine, tmp_path):
    cert, key, _ = certificates(tmp_path)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        remote = occupied.getsockname()[1]
        engine.settings.port = port()
        engine.settings.workbench_lan = WorkbenchLAN(
            enabled=True,
            port=remote,
            certificate=str(cert),
            private_key=str(key),
            origins=[f"https://127.0.0.1:{remote}"],
            workspaces=["00000000-0000-4000-8000-000000000001"],
        )
        app = create_app(engine.settings, engine)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=engine.settings.port, access_log=False, log_level="error")
        )
        task = asyncio.create_task(serve_pair(server, app.state.workbench))
        try:
            await ready(server, task)
            for _ in range(100):
                if app.state.workbench.lan_error:
                    break
                await asyncio.sleep(0.01)
            assert app.state.workbench.lan_error == "OSError"
            async with httpx.AsyncClient(trust_env=False) as client:
                assert (await client.get(f"http://127.0.0.1:{engine.settings.port}/health")).status_code == 200
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 15)
