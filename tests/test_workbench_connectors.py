"""Strict untrusted protocol envelopes and explicit provider registration."""

import json

import pytest
from test_workbench_agents import HUMAN
from test_workbench_execution import execution, item, queue  # noqa: F401

from local_ai_router.config import Settings
from local_ai_router.schema import ConnectorRegistration, ModelDescriptor, Provider
from local_ai_router.workbench.connectors import ConnectorRequest, request_bytes, validate_response
from local_ai_router.workbench.store import identity, now


@pytest.fixture
def connector_envelopes():
    request = {
        "protocol_version": 1,
        "request_id": "request-1",
        "attempt_id": identity(),
        "dispatch_nonce": "f" * 64,
        "expires_at": now() + 60000,
        "contract_hash": "a" * 64,
        "operation": "propose_changes",
        "task": {"id": "work", "objective": "Implement greeting", "done_when": ["Independent checks pass"]},
        "allowed_files": [{"path": "greeting.py", "sha256": None, "content": None}],
        "privacy": "LOCAL_ONLY",
        "remaining_cap_microusd": 100000,
    }
    reply = {
        key: value
        for key, value in request.items()
        if key in {"protocol_version", "request_id", "attempt_id", "dispatch_nonce", "expires_at", "contract_hash"}
    }
    reply.update(
        status="completed",
        result={
            "status": "complete",
            "summary": "Unverified proposal",
            "files_changed": [
                {
                    "path": "greeting.py",
                    "content": "def greet(name): return f\"Hello, {name.strip() or 'world'}!\"\n",
                    "original_sha256": None,
                }
            ],
        },
    )
    return ConnectorRequest.model_validate(request), reply, ConnectorRegistration(name="Synthetic connector")


def test_strict_connector_identity_paths_and_size(connector_envelopes):
    request, reply, registration = connector_envelopes
    assert validate_response(json.dumps(reply).encode(), request, registration, now()).result.status == "complete"
    for field, value in [
        ("dispatch_nonce", "e" * 64),
        ("contract_hash", "b" * 64),
        ("attempt_id", identity()),
        ("protocol_version", 2),
        ("host_tests_passed", True),
    ]:
        with pytest.raises(ValueError):
            validate_response(json.dumps({**reply, field: value}).encode(), request, registration, now())
    with pytest.raises(ValueError, match="expired"):
        validate_response(json.dumps(reply).encode(), request, registration, request.expires_at)
    with pytest.raises(ValueError, match="size"):
        validate_response(b"x" * (registration.max_response_bytes + 1), request, registration, now())
    for path in ["../escape.py", "C:/secret.py", "/root/file.py", "other.py"]:
        changed = json.loads(json.dumps(reply))
        changed["result"]["files_changed"][0]["path"] = path
        with pytest.raises(ValueError):
            validate_response(json.dumps(changed).encode(), request, registration, now())
    assert b"repository_root" not in request_bytes(request, registration)


def test_registration_roles_endpoint_and_profile_are_explicit(tmp_path):
    registration = ConnectorRegistration(name="Worker", local_development=True)
    provider = Provider(kind="connector", endpoint="http://127.0.0.1:8099", local=True, connector=registration)
    model = ModelDescriptor(
        id="proposal",
        provider="worker",
        deployment_name="proposal",
        roles=["executor", "repair"],
        supports_streaming=False,
    )
    settings = Settings(home=tmp_path, providers={"worker": provider}, models=[model])
    assert settings.models[0].provider == "worker"
    assert "credential_ref" not in str(model.profile())
    for change in [
        {"local": False},
        {"endpoint": "http://remote.example"},
        {"command": "cmd.exe"},
        {"protocol": "openai_chat"},
        {"connector": None},
    ]:
        with pytest.raises(ValueError):
            Provider.model_validate({**provider.model_dump(), **change})
    with pytest.raises(ValueError, match="roles"):
        Settings(
            home=tmp_path,
            providers={"worker": provider},
            models=[model.model_copy(update={"roles": ["planner", "executor"]})],
        )
    with pytest.raises(ValueError):
        ConnectorRegistration(name="Worker", roles=["reviewer"])


@pytest.mark.parametrize("outcome", ["complete", "accepted", "redirect", "auth", "oversize", "timeout", "malformed"])
async def test_bounded_registered_transport(connector_envelopes, tmp_path, outcome):
    import httpx

    from local_ai_router.providers import Providers
    from local_ai_router.store import Store
    from local_ai_router.workbench.connectors import ConnectorUncertain, exchange

    request, reply, registration = connector_envelopes
    registration.local_development = True
    provider = Provider(
        kind="connector", endpoint="http://127.0.0.1:8099/proposals", local=True, connector=registration
    )
    settings = Settings(home=tmp_path, providers={"worker": provider}, models=[])
    calls = []

    def respond(incoming):
        calls.append(incoming)
        if outcome == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.invalid"})
        if outcome == "auth":
            return httpx.Response(401, json={"secret": "must not be returned"})
        if outcome == "oversize":
            return httpx.Response(200, content=b"x" * (registration.max_response_bytes + 1))
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if outcome == "malformed":
            return httpx.Response(200, content=b"invalid")
        if outcome == "accepted" and incoming.method == "POST":
            return httpx.Response(
                202,
                json={
                    **{key: value for key, value in reply.items() if key not in {"result", "status"}},
                    "status": "accepted",
                    "job_id": "job-1",
                },
            )
        return httpx.Response(200, json=reply)

    store = Store(settings)
    manager = Providers(settings, store, transport=httpx.MockTransport(respond))
    try:
        if outcome in {"complete", "accepted"}:
            result = await exchange(manager.context(provider, "worker"), request)
            assert result.status == "completed"
            assert all(call.url.host == "127.0.0.1" for call in calls)
            assert len({call.headers["idempotency-key"] for call in calls}) == 1
            if outcome == "accepted":
                assert str(calls[-1].url).endswith("/proposals/jobs/job-1")
        else:
            with pytest.raises(Exception) as error:
                await exchange(manager.context(provider, "worker"), request)
            if outcome == "timeout":
                assert isinstance(error.value, ConnectorUncertain)
            assert len(calls) == 1
    finally:
        await manager.close()
        store.close()


async def test_remote_connector_cannot_egress_local_contract(connector_envelopes, tmp_path):
    import httpx

    from local_ai_router.contracts import contract_scope
    from local_ai_router.providers import Providers
    from local_ai_router.store import Store
    from local_ai_router.workbench.connectors import exchange

    request, _, registration = connector_envelopes
    provider = Provider(
        kind="connector",
        endpoint="https://worker.invalid/proposals",
        credential_ref="fixture-only",
        connector=registration,
    )
    settings = Settings(home=tmp_path, providers={"worker": provider}, models=[])
    calls = []
    manager = Providers(
        settings, Store(settings), transport=httpx.MockTransport(lambda incoming: calls.append(incoming))
    )
    try:
        from local_ai_router.errors import PrivacyViolation

        with contract_scope(mode="LOCAL_ONLY"), pytest.raises(PrivacyViolation):
            await exchange(manager.context(provider, "worker"), request)
        assert calls == []
    finally:
        await manager.close()
        manager.store.close()


async def test_connector_real_verified_tls_and_untrusted_ca(connector_envelopes, tmp_path):
    import asyncio
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx
    from workbench_tls import certificates

    from local_ai_router.providers import Providers
    from local_ai_router.store import Store
    from local_ai_router.workbench.connectors import ConnectorUncertain, exchange

    request, reply, registration = connector_envelopes
    cert, key, trust = certificates(tmp_path)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            raw = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = Provider(
        kind="connector",
        endpoint=f"https://127.0.0.1:{server.server_port}/proposals",
        local=True,
        connector=registration,
    )
    settings = Settings(home=tmp_path, providers={"worker": provider}, models=[])
    manager = Providers(settings, Store(settings))
    try:
        with pytest.raises(ConnectorUncertain):
            await exchange(manager.context(provider, "worker"), request)
        assert received == []
        await manager.client.aclose()
        manager.client = httpx.AsyncClient(verify=trust, trust_env=False, follow_redirects=False)
        result = await exchange(manager.context(provider, "worker"), request)
        assert result.status == "completed" and len(received) == 1
    finally:
        await manager.close()
        manager.store.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("outcome", ["correct", "escape", "base_hash", "remote_claim", "timeout", "stale"])
async def test_actual_connector_proposals_keep_host_checks_and_receipts(execution, outcome):  # noqa: F811
    import httpx

    from local_ai_router.providers import Providers

    runtime, ws, _, agent, root = execution
    settings = runtime.engine.settings
    provider = Provider(
        kind="connector",
        endpoint="http://127.0.0.1:8099/proposals",
        local=True,
        connector=ConnectorRegistration(name="Synthetic worker", local_development=True),
    )
    settings.providers["synthetic"] = provider
    settings.models.append(
        ModelDescriptor(
            id="synthetic-worker",
            provider="synthetic",
            deployment_name="proposal",
            roles=["executor", "repair"],
            input_price=0,
            output_price=0,
            supports_streaming=False,
            quality_priors={"default": 0.99},
        )
    )
    request_bodies = []

    def respond(incoming):
        body = json.loads(incoming.content)
        request_bodies.append(body)
        if outcome == "timeout":
            raise httpx.ReadTimeout("accepted outcome unknown")
        if outcome == "stale":
            runtime.service.store.db.execute("UPDATE wb_attempts SET fence=fence+1 WHERE id=?", (body["attempt_id"],))
        content = "def greet(name): return f\"Hello, {name.strip() or 'world'}!\"\n"
        if outcome == "remote_claim":
            content = 'def greet(name): return "incorrect"\n'
        result = {
            key: body[key]
            for key in {"protocol_version", "request_id", "attempt_id", "dispatch_nonce", "expires_at", "contract_hash"}
        }
        result.update(
            status="completed",
            result={
                "status": "complete",
                "summary": "Unverified remote tests passed claim",
                "commands_run": ["unsafe shell text must never execute"],
                "files_changed": [
                    {
                        "path": "../escape.py" if outcome == "escape" else "greeting.py",
                        "content": content,
                        "original_sha256": "b" * 64 if outcome == "base_hash" else body["allowed_files"][0]["sha256"],
                    }
                ],
            },
            usage={
                "source": "connector_reported",
                "reported_fields": ["input_tokens", "output_tokens"],
                "input_tokens": 10,
                "output_tokens": 20,
            },
            cost={"source": "connector_reported", "microusd": 100},
        )
        return httpx.Response(200, json=result)

    old = runtime.engine.providers
    await old.close()
    manager = Providers(settings, runtime.engine.store, transport=httpx.MockTransport(respond))
    runtime.engine.providers = manager
    runtime.engine.router.roles.providers = manager
    from local_ai_router.workbench.model_options import model_options

    choices = model_options(runtime, {"workspace_id": ws.id, "purpose": "executor", "query": "synthetic"}, HUMAN)
    assert choices["items"][0]["eligible"], choices["items"][0]["reason_codes"]
    configured = runtime.agents.configure(
        {
            **{
                key: value
                for key, value in runtime.agents.definition(agent["id"]).model_dump(mode="json").items()
                if key not in {"id", "revision", "created_at", "updated_at", "created_by", "updated_by"}
            },
            "command_id": identity(),
            "expected_revision": agent["revision"],
            "model_bindings": {
                "executor": {"mode": "pinned", "primary_model_id": "synthetic-worker"},
                "repairer": {"mode": "pinned", "primary_model_id": "synthetic-worker"},
                "reviewer": {"mode": "auto"},
            },
        },
        HUMAN,
        agent["id"],
    )
    execution = (runtime, ws, execution[2], configured.body, root)
    before = runtime.agents.profile(ws.id, agent["id"], HUMAN)
    assert before["connectors"][0]["status"] == "unknown"
    assert before["connectors"][0]["observed_at"] is None
    attempt = queue(execution, item(execution))
    if outcome == "correct":
        result = await runtime.execution.execute(attempt, runtime.agents.generation, 1)
        assert result["status"] == "complete"
        assert (
            runtime.service.store.get("work_item", ws.id, runtime.execution.contract(attempt)[1].item_id).status
            == "REVIEW"
        )
        assert runtime.execution.read_attempt(ws.id, attempt, HUMAN, "receipt")["receipt"]["status"] == "complete"
        assert len(request_bodies) == 1
        assert request_bodies[0]["allowed_files"][0]["path"] == "greeting.py"
        metadata = runtime.service.store.db.execute(
            "SELECT value FROM metadata WHERE key LIKE 'connector-dispatch:%'"
        ).fetchone()[0]
        assert "content" not in metadata and "unsafe shell" not in metadata
        after = runtime.agents.profile(ws.id, agent["id"], HUMAN)
        assert after["connectors"][0]["status"] == "reachable" and after["connectors"][0]["observed_at"] is not None
        from local_ai_router.workbench.metrics import reconcile_attempt

        reconcile_attempt(runtime, attempt)
        fact = runtime.service.store.db.execute(
            "SELECT connector_reported_cost_microusd,priced_usage_cost_microusd FROM wb_attempt_facts WHERE attempt_id=?",
            (attempt,),
        ).fetchone()
        assert fact[0] == 100 and fact[1] is None
        events = [
            row[0]
            for row in runtime.service.store.db.execute(
                "SELECT event_type FROM wb_activity_entries WHERE attempt_id=?", (attempt,)
            )
        ]
        assert "connector_dispatch" in events and "connector_reply" in events
    else:
        with pytest.raises(RuntimeError):
            await runtime.execution.execute(attempt, runtime.agents.generation, 1)
        assert not (root / "greeting.py").exists()
        assert not (root.parent / "escape.py").exists()
        if outcome in {"timeout", "stale"}:
            assert len(request_bodies) == 1
        if outcome == "timeout":
            row, contract = runtime.execution.contract(attempt)
            with runtime.engine.store.transaction():
                runtime.scheduler.block(
                    attempt, runtime.service.store.get("work_item", ws.id, contract.item_id), "ConnectorUncertain"
                )
            runtime.scheduler.reconcile()
            assert runtime.execution.contract(attempt)[0]["state"] == "RECONCILING"
            assert runtime.engine.store.costs(contract.request_id)["uncertain_calls"] == 1
