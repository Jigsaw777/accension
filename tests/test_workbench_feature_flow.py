"""Deterministic feature fixtures and forward-looking model-selection requirements."""

import json
from copy import deepcopy

import httpx
import pytest
from test_workbench_agents import HUMAN
from test_workbench_execution import execution  # noqa: F401
from test_workbench_planning import SPEC

from local_ai_router.app import create_app
from local_ai_router.schema import Generation, ModelDescriptor
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.schema import AgentCommand, PlanningInput
from local_ai_router.workbench.store import identity


@pytest.fixture
async def feature_dataset(engine, repo):
    runtime = WorkbenchRuntime(engine)
    first = runtime.service.seed_defaults()
    second = runtime.service.create("workspace", {"command_id": identity(), "name": "Other feature"}, HUMAN).body
    projects = []
    for index, workspace in enumerate((first.id, second["id"])):
        projects.append(
            runtime.service.create(
                "project",
                {
                    "command_id": identity(),
                    "workspace_id": workspace,
                    "name": "Feature " + str(index),
                    "key": "F" + str(index),
                },
                HUMAN,
            ).body
        )
    yield runtime, projects, repo
    await runtime.close()


@pytest.fixture
def feature_models(engine, monkeypatch):
    engine.settings.models = [
        ModelDescriptor(
            id="fixture-" + locality,
            provider="mock",
            deployment_name="synthetic-" + locality,
            locality=locality,
            input_price=0,
            output_price=0,
            supports_structured_output=True,
            roles=["planner", "executor", "repair", "reviewer"],
        )
        for locality in ("local", "cloud")
    ]
    calls = []

    async def generate(model, messages, role, rid, session, budget, **kwargs):
        calls.append({"model_id": model.id, "role": role, "repository_id": rid})
        return Generation(text=json.dumps(SPEC))

    monkeypatch.setattr(engine.providers, "generate", generate)
    return engine.settings.models, calls


@pytest.fixture
def proposal_connector():
    requests = []

    def reply(request):
        envelope = json.loads(request.content)
        requests.append(deepcopy(envelope))
        return httpx.Response(
            200,
            json={
                **{
                    key: envelope[key]
                    for key in (
                        "protocol_version",
                        "request_id",
                        "attempt_id",
                        "dispatch_nonce",
                        "expires_at",
                        "contract_hash",
                    )
                },
                "status": "completed",
                "result": {
                    "status": "complete",
                    "summary": "Synthetic proposal; host validation required.",
                    "files_changed": [],
                    "tests_added": [],
                    "commands_run": [],
                    "remaining_risks": [],
                    "confidence": 0.8,
                    "escalation_requested": False,
                },
                "usage": {
                    "source": "connector_reported",
                    "reported_fields": ["input_tokens", "output_tokens"],
                    "input_tokens": 12,
                    "output_tokens": 4,
                    "cached_tokens": None,
                    "cache_write_tokens": None,
                },
                "cost": {"source": "unknown", "microusd": None},
            },
        )

    return httpx.MockTransport(reply), requests


@pytest.mark.parametrize("target", ["agent", "planner"])
def test_explicit_model_selection_accepted(target):
    selection = {"mode": "pinned", "primary_model_id": "fixture-local", "fallback_model_ids": []}
    if target == "agent":
        value = AgentCommand.model_validate(
            {
                "command_id": identity(),
                "name": "Pinned worker",
                "allowed_workspaces": [identity()],
                "model_bindings": {"executor": selection},
            }
        ).model_dump(mode="json")["model_bindings"]["executor"]
    else:
        value = PlanningInput.model_validate(
            {
                "workspace_id": identity(),
                "project_id": identity(),
                "requirement": "Rough idea",
                "model_selection": selection,
            }
        ).model_dump(mode="json")["model_selection"]
    assert value == selection


async def test_feature_fixtures_are_scoped_and_offline(feature_dataset, feature_models, proposal_connector):
    runtime, projects, repo = feature_dataset
    models, calls = feature_models
    assert projects[0]["workspace_id"] != projects[1]["workspace_id"]
    assert repo.is_dir() and len(models) == 2 and calls == []
    transport, requests = proposal_connector
    envelope = {
        "protocol_version": 1,
        "request_id": "synthetic",
        "attempt_id": identity(),
        "dispatch_nonce": "f" * 64,
        "expires_at": 1791763500000,
        "contract_hash": "a" * 64,
    }
    with httpx.Client(transport=transport, base_url="https://connector.invalid", trust_env=False) as client:
        response = client.post("/proposals", json=envelope)
    assert response.status_code == 200 and requests == [envelope]
    assert response.json()["contract_hash"] == envelope["contract_hash"]
    assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_attempts").fetchone()[0] == 0


async def test_feature_read_baseline(feature_dataset, tmp_path):
    runtime, projects, _ = feature_dataset
    for project in projects:
        for number in range(8):
            runtime.service.create(
                "work_item",
                {
                    "command_id": identity(),
                    "workspace_id": project["workspace_id"],
                    "project_id": project["id"],
                    "title": "Feature " + str(number),
                    "execution_kind": "MANUAL",
                },
                HUMAN,
            )
    report = {}
    db = runtime.service.store.db
    for name, filters in (("board", {}), ("filtered", {"search": "Feature 1"})):
        queries = []
        db.set_trace_callback(queries.append)
        try:
            rows = runtime.service.list("work_item", projects[0]["workspace_id"], HUMAN, **filters)
        finally:
            db.set_trace_callback(None)
        report[name] = {
            "sql_statements": len(queries),
            "payload_bytes": len(json.dumps([row.model_dump(mode="json") for row in rows]).encode()),
            "items": len(rows),
        }
    queries = []
    db.set_trace_callback(queries.append)
    try:
        rows = runtime.agents.list(projects[0]["workspace_id"], HUMAN)
    finally:
        db.set_trace_callback(None)
    report["agents"] = {
        "sql_statements": len(queries),
        "payload_bytes": len(json.dumps(rows).encode()),
        "items": len(rows),
    }
    transport = httpx.ASGITransport(app=create_app(runtime.engine.settings, runtime.engine))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer " + runtime.engine.settings.token},
    ) as client:
        for name, path, extra in (
            ("board_http", "/work-items", {}),
            ("filter_http", "/work-items", {"query": "Feature 1"}),
            ("agents_http", "/agents", {}),
        ):
            queries = []
            db.set_trace_callback(queries.append)
            try:
                response = await client.get(
                    "/accs/v1/workbench" + path, params={"workspace_id": projects[0]["workspace_id"], **extra}
                )
            finally:
                db.set_trace_callback(None)
            assert response.status_code == 200
            report[name] = {
                "http_requests": 1,
                "sql_statements": len(queries),
                "payload_bytes": len(response.content),
                "items": len(response.json()["items"]),
            }
    assert report["board"]["items"] == 8 and report["filtered"]["items"] == 1
    assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    (tmp_path / "read-baseline.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


@pytest.mark.parametrize("worker", ["local", "cloud", "connector"])
async def test_rough_idea_to_reviewed_delivery_uses_actual_host_owner(execution, worker, monkeypatch, tmp_path):  # noqa: F811
    import asyncio
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from test_workbench_agents import definition
    from test_workbench_delivery import approve_delivery
    from test_workbench_imports import plan
    from workbench_tls import certificates

    from local_ai_router.mock import generate as mock_generate
    from local_ai_router.providers import Providers
    from local_ai_router.schema import ConnectorRegistration, Provider
    from local_ai_router.workbench.metrics import agent_metrics
    from local_ai_router.workbench.store import now

    runtime, ws, project, _, root = execution
    settings = runtime.engine.settings
    settings.control_plane.fully_local = False
    settings.routing.preset = "balanced"
    settings.repositories[0].allow_cloud = True
    ws = runtime.service.update(
        "workspace",
        ws.id,
        ws.id,
        {
            "command_id": identity(),
            "expected_revision": ws.revision,
            "privacy_mode": "HYBRID",
            "inference_policy": "CLOUD_ALLOWED",
        },
        HUMAN,
    ).body
    runtime.service.update(
        "project",
        ws["id"],
        project["id"],
        {"command_id": identity(), "expected_revision": project["revision"], "default_privacy": "CLOUD_ALLOWED"},
        HUMAN,
    )
    server = thread = None
    received = []
    trust = None

    def connector_reply(body):
        received.append(body)
        return {
            **{
                key: body[key]
                for key in (
                    "protocol_version",
                    "request_id",
                    "attempt_id",
                    "dispatch_nonce",
                    "expires_at",
                    "contract_hash",
                )
            },
            "status": "completed",
            "result": {
                "status": "complete",
                "summary": "Remote proposal; host must verify",
                "files_changed": [
                    {
                        "path": "greeting.py",
                        "content": "def greet(name): return f\"Hello, {name.strip() or 'world'}!\"\n",
                        "original_sha256": body["allowed_files"][0]["sha256"],
                    }
                ],
            },
        }

    if worker == "connector":
        cert, key, trust = certificates(tmp_path)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = json.dumps(
                    connector_reply(json.loads(self.rfile.read(int(self.headers["content-length"]))))
                ).encode()
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
            connector=ConnectorRegistration(name="Trusted synthetic worker"),
        )
    elif worker == "cloud":
        provider = Provider(kind="openai", endpoint="https://synthetic.invalid/v1", api_key_env="FIXTURE_TOKEN")
        monkeypatch.setenv("FIXTURE_TOKEN", "synthetic-test-only")
    else:
        provider = Provider(kind="mock", local=True)
    settings.providers["fixture-worker"] = provider
    settings.models.append(
        ModelDescriptor(
            id="fixture-worker",
            provider="fixture-worker",
            deployment_name="fixture-worker",
            roles=["executor", "repair"],
            input_price=0,
            output_price=0,
            supports_streaming=worker != "connector",
            quality_priors={"default": 0.99},
        )
    )
    await runtime.engine.providers.close()

    def cloud_reply(request):
        body = json.loads(request.content)
        received.append(body)
        proposal = mock_generate(body["messages"], "executor")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": proposal.text}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 24},
            },
        )

    manager = Providers(
        settings, runtime.engine.store, transport=httpx.MockTransport(cloud_reply) if worker == "cloud" else None
    )
    if trust:
        await manager.client.aclose()
        manager.client = httpx.AsyncClient(verify=trust, trust_env=False, follow_redirects=False)
    runtime.engine.providers = manager
    runtime.engine.router.roles.providers = manager
    original_generate = manager._generate
    document_calls = []

    async def documents(provider, model, messages, role, maximum, schema, task_id, probe=None):
        if schema and schema.get("title") == "PRDSpec":
            document_calls.append((model.id, "prd"))
            return Generation(text=json.dumps(SPEC), usage_source="estimated")
        if schema and schema.get("title") == "ExecutionPlan":
            document_calls.append((model.id, "decompose"))
            proposed = plan(1)
            proposed["tasks"][0]["expected_artifacts"] = ["greeting.py"]
            return Generation(text=json.dumps(proposed), usage_source="estimated")
        return await original_generate(provider, model, messages, role, maximum, schema, task_id, probe)

    monkeypatch.setattr(manager, "_generate", documents)
    try:
        body = {
            "workspace_id": ws["id"],
            "project_id": project["id"],
            "requirement": "Clarify greeting behavior",
            "model_selection": {"mode": "pinned", "primary_model_id": "mock-worker"},
        }
        quoted = runtime.planning.quote(body, HUMAN)
        command = {**body, "command_id": identity(), "quote": quoted["quote"]}
        operation = runtime.planning.enqueue(command, HUMAN)
        assert runtime.planning.enqueue(command, HUMAN).body == operation.body
        await asyncio.gather(*runtime.planning.tasks.values())
        completed = runtime.planning.get(ws["id"], operation.body["id"], HUMAN)
        assert completed["state"] == "COMPLETED", completed
        prd = runtime.service.get("prd", ws["id"], completed["result"]["prd_id"], HUMAN)
        assert prd.status == "DRAFT" and not runtime.service.store.db.execute("SELECT 1 FROM wb_attempts").fetchone()
        for action in ("submit_review", "approve"):
            runtime.service.prd_command(
                ws["id"],
                prd.id,
                {"command_id": identity(), "expected_revision": prd.revision, "command": action},
                HUMAN,
            )
            prd = runtime.service.get("prd", ws["id"], prd.id, HUMAN)
        decompose_body = {
            "workspace_id": ws["id"],
            "project_id": project["id"],
            "kind": "decompose",
            "prd_id": prd.id,
            "expected_revision": prd.revision,
            "allowed_paths": ["greeting.py"],
            "model_selection": {"mode": "pinned", "primary_model_id": "mock-worker"},
        }
        quote = runtime.planning.quote(decompose_body, HUMAN)
        decomposition = runtime.planning.enqueue(
            {**decompose_body, "command_id": identity(), "quote": quote["quote"]}, HUMAN
        ).body
        await asyncio.gather(*runtime.planning.tasks.values())
        breakdown = runtime.planning.get(ws["id"], decomposition["id"], HUMAN)
        assert breakdown["state"] == "COMPLETED", breakdown
        assert document_calls == [("mock-worker", "prd"), ("mock-worker", "decompose")]
        assert not runtime.service.store.db.execute("SELECT 1 FROM wb_attempts").fetchone()
        staged = runtime.planning.preview(
            {
                "command_id": identity(),
                "workspace_id": ws["id"],
                "project_id": project["id"],
                "prd_id": prd.id,
                "prd_revision": prd.revision,
                "operation_id": decomposition["id"],
            },
            HUMAN,
        ).body
        imported = runtime.planning.commit(
            ws["id"],
            staged["id"],
            {
                "command_id": identity(),
                "expected_revision": staged["revision"],
                "source_hash": staged["source_hash"],
                "actions": {p["proposal_id"]: "create" for p in staged["proposal"]["items"]},
            },
            HUMAN,
        ).body
        item_id = next(iter(imported["items"].values()))
        item = runtime.service.store.get("work_item", ws["id"], item_id)
        item = runtime.service.update(
            "work_item",
            ws["id"],
            item_id,
            {"command_id": identity(), "expected_revision": item.revision, "privacy_policy": "CLOUD_ALLOWED"},
            HUMAN,
        ).body
        agent = runtime.agents.configure(
            {
                **definition(
                    runtime.service.seed_defaults(),
                    repository_permissions=[project["repository_ref"]],
                    privacy_policy="CLOUD_ALLOWED",
                ),
                "model_bindings": {
                    "executor": {"mode": "pinned", "primary_model_id": "fixture-worker"},
                    "repairer": {"mode": "pinned", "primary_model_id": "fixture-worker"},
                },
            },
            HUMAN,
        ).body
        batch = approve_delivery(
            runtime,
            runtime.service.seed_defaults(),
            {
                "workspace_id": ws["id"],
                "project_id": project["id"],
                "import_id": staged["id"],
                "item_ids": [item_id],
                "contract_revisions": {item_id: item["contract_revision"]},
                "agents": {item_id: agent["id"]},
                "budget": "0.10",
                "expires_at": now() + 60000,
            },
        )
        assert runtime.scheduler.acquire()
        runtime.delivery.tick()
        claim = runtime.scheduler.claim()
        assert claim is not None
        await runtime.scheduler.work(*claim)
        item = runtime.service.store.get("work_item", ws["id"], item_id)
        assert item.status == "REVIEW", runtime.execution.read_attempt(ws["id"], claim[0], HUMAN)
        evidence = runtime.service.evidence(item)[0]
        runtime.service.item_command(
            ws["id"],
            item_id,
            {
                "command_id": identity(),
                "expected_revision": item.revision,
                "command": "approve_completion",
                "contract_revision": item.contract_revision,
                "evidence_hash": evidence["evidence_hash"],
                "reason": "Independent host checks reviewed",
            },
            HUMAN,
        )
        runtime.delivery.tick()
        final = runtime.delivery.get(ws["id"], batch["id"], HUMAN)
        assert final["state"] == "COMPLETED"
        runtime.delivery.tick()
        assert runtime.service.store.db.execute("SELECT COUNT(*) FROM wb_attempts").fetchone()[0] == 1
        measured = agent_metrics(runtime, ws["id"], agent["id"], HUMAN, start=now() - 60000, end=now() + 1000)
        assert measured["totals"]["delivered_tickets"] == 1
        if worker in {"cloud", "connector"}:
            assert len(received) == 1
        if worker == "connector":
            assert measured["totals"]["unknown_calls"] == 1
    finally:
        if server:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=3)
