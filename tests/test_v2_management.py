import hashlib
import json
import time
from pathlib import Path
import httpx
import pytest

from local_ai_router.app import create_app
from local_ai_router.config import Settings, load, RolePolicy, Repository, Privacy
from local_ai_router.engine import Engine
from local_ai_router.management import Management
from local_ai_router.schema import Provider, Model
from local_ai_router.privacy import preflight, repository_scope
from local_ai_router.errors import PrivacyViolation


async def test_zero_model_ui_sessions_csrf_and_rebinding(tmp_path):
    engine = Engine(Settings(home=tmp_path))
    app = create_app(engine.settings, engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.get("/ui/state")).status_code == 401
        response = await client.get("/ui")
        assert response.status_code == 200 and "Accension" in response.text
        assert "login-form" not in response.text and "Sign in" not in response.text and "Sign out" not in response.text
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert (await client.get("/", headers={"Host": "attacker.invalid"})).status_code == 403
        assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
        csrf = (await client.get("/ui/session")).json()["csrf"]
        for origin in ("http://evil.invalid", "null", "http://localhost:9999"):
            assert (await client.post("/ui/action/policy-set", headers={"Origin":origin,"X-CSRF-Token":csrf}, json={"preset":"fully-local"})).status_code == 403
        assert (await client.get("/ui", headers={"Sec-Fetch-Site":"cross-site"})).status_code == 403
        state = (await client.get("/ui/state")).json()
        assert state["models"] == [] and len(state["roles"]) == 12
        assert (await client.post("/ui/action/policy-set", json={"preset":"fully-local"})).status_code == 403
        assert (await client.post("/ui/action/policy-set", json={"preset":"fully-local"}, headers={"Origin":"http://localhost","X-CSRF-Token":csrf})).status_code == 200
        assert load(tmp_path).control_plane.fully_local
        assert (await client.get("/v1/models")).status_code == 401  # UI cookie is not a gateway credential.
        client.cookies.clear()
        assert (await client.get("/ui/state")).status_code == 401
    await engine.close()


async def test_internal_session_and_no_secret_echo(engine):
    app = create_app(engine.settings, engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        await client.get("/ui")
        csrf = (await client.get("/ui/session")).json()["csrf"]
        assert len(csrf) >= 32 and csrf != engine.settings.token
        state = (await client.get("/ui/state")).text
        assert csrf not in state and engine.settings.token not in state
        headers={"Origin":"http://localhost","X-CSRF-Token":csrf}
        assert (await client.post("/ui/login", headers=headers, json={})).status_code == 404
        assert (await client.post("/ui/logout", headers=headers, json={})).status_code == 404


async def test_shared_typed_settings_backups_and_stale_writer(engine):
    a, b = Management(engine), Management(engine)
    a.set_policy({"preset":"balanced", "budgets":{"daily_hard_budget":2}})
    assert load(engine.settings.home).budgets.daily_hard_budget == 2
    with pytest.raises(ValueError, match="another process"):
        b.set_policy({"budgets":{"daily_hard_budget":10}})
    a.set_role("executor", {"strategy":"disabled"})
    assert list((engine.settings.state / "backups").glob("local-*.yaml"))
    before = a.path.read_bytes()
    with pytest.raises(ValueError):
        a.set_policy({"budgets":{"daily_hard_budget":-1}})
    assert a.path.read_bytes() == before


async def test_profile_cannot_execute_or_relax_privacy(engine):
    manager = Management(engine)
    manager.set_policy({"preset":"fully-local"})
    profile = manager.export_profile()
    encoded = json.dumps(profile)
    assert "command" not in encoded and "credential" not in encoded and str(engine.settings.home) not in encoded
    profile["routing"]["classifier_command"] = "untrusted"
    with pytest.raises(ValueError, match="executable"):
        manager.import_profile(profile)
    profile = manager.export_profile()
    profile["routing"]["preset"] = "balanced"
    profile["control_plane"]["fully_local"] = False
    with pytest.raises(ValueError, match="relax privacy"):
        manager.import_profile(profile)
    assert engine.settings.control_plane.fully_local


async def test_provider_connect_is_manifest_validated_and_vault_only(engine, monkeypatch):
    from local_ai_router.credentials import CredentialStore
    saved = {}
    monkeypatch.setattr(CredentialStore, "save", lambda self,k,v:saved.update({k:v}))
    manager = Management(engine)
    secret = "fixture-credential-value"
    response = manager.connect("custom", "openrouter", {}, secret)
    assert response["credential_saved"] and saved[engine.settings.providers["custom"].credential_ref] == secret
    assert secret not in manager.path.read_text() and secret not in json.dumps(manager.providers())
    with pytest.raises(ValueError):
        manager.connect("unsafe", "openai-compatible", {"endpoint":"http://remote.invalid/v1","local":True})
    assert "unsafe" not in engine.settings.providers


async def test_reconnecting_model_override_keeps_dynamic_registry(engine):
    dynamic = engine.settings.models[0].model_copy(update={"id":"dynamic"})
    engine.settings.models.append(dynamic)
    engine.store.save_model(dynamic)
    manager = Management(engine)
    manager.update_model("dynamic", {"input_price":1,"output_price":2,"supports_tools":False})
    assert len(engine.settings.models) == 2
    model = next(m for m in engine.settings.models if m.id == "dynamic")
    assert model.capability_evidence["tools"]["source"] == "user_override"
    assert model.pricing_status == "user_supplied"


def test_structured_and_embedded_json_secret_preflight():
    provider = Provider(kind="openai", endpoint="https://example.invalid/v1")
    secret = "fictional-password-value"
    payload = {"messages":[{"role":"user","content":json.dumps({"api_key":secret})}],"metadata":{"password":secret}}
    with repository_scope(Repository(path="unused", privacy=Privacy(mode="CLOUD_REDACTED"))):
        assert secret not in json.dumps(preflight(payload, provider))
    with pytest.raises(PrivacyViolation):
        preflight(payload, provider)


async def test_disabled_classifier_never_starts_process(engine, monkeypatch):
    engine.settings.routing.classifier_enabled = True
    engine.settings.routing.classifier_command = "fixture"
    engine.settings.roles["classifier"] = RolePolicy(strategy="disabled")
    async def forbidden(*args):
        pytest.fail("Disabled classifier ran")
    monkeypatch.setattr(engine.router.classifier,"classify",forbidden)
    assert (await engine.router.classify("Implement a greeting feature", "r")).task_family == "coding"


async def test_legacy_registry_survives_partial_provider_refresh(tmp_path):
    from local_ai_router.store import Store
    providers = {name:Provider(kind="openai-compatible", endpoint="http://localhost:1234/v1", local=True) for name in ["a","b"]}
    settings = Settings(home=tmp_path, providers=providers)
    store = Store(settings)
    records = [Model(id=name+":m",provider=name,deployment_name="m",input_price=0,output_price=0) for name in providers]
    store.metadata("discovered-models",[m.model_dump() for m in records]); store.close()
    first = Engine(settings)
    first.store.save_model(records[0]); await first.close()
    second = Engine(Settings(home=tmp_path,providers=providers))
    assert {m.id for m in second.settings.models} == {"a:m","b:m"}
    await second.close()


async def test_integration_preview_never_returns_existing_config_and_checks_stale(engine, monkeypatch, tmp_path):
    from local_ai_router import integrations
    path = tmp_path/"client.json"
    path.write_text(json.dumps({"unrelated_secret":"fixture-value", "mcpServers":{"other":{"command":"existing"}}}))
    monkeypatch.setattr(integrations,"target",lambda client:path)
    quote = integrations.preview(engine,"claude-code")
    assert "fixture-value" not in json.dumps(quote)
    result = integrations.install(engine,quote["id"])
    assert Path(result["backup"]).is_file()
    data=json.loads(path.read_text())
    assert data["unrelated_secret"]=="fixture-value" and "other" in data["mcpServers"]
    quote=integrations.preview(engine,"claude-code")
    path.write_text(path.read_text()+" ")
    with pytest.raises(ValueError,match="changed since preview"):
        integrations.install(engine,quote["id"])


async def test_missing_plugin_does_not_block_gateway(engine):
    from local_ai_router.gateway import forward
    engine.settings.providers["broken"] = Provider(kind="absent-plugin",local=True)
    model=engine.settings.models[0].model_copy(update={"id":"broken","provider":"broken"})
    engine.settings.models.insert(0,model)
    response=await forward(engine,"chat",{"model":"accension-auto","messages":[{"role":"user","content":"Explain hello"}]})
    assert response.status_code==200


def test_cli_bare_entry_and_calibration_are_safe():
    from local_ai_router.cli import parser
    assert parser().parse_args([]).command=="ui"
    args=parser().parse_args(["calibrate","--model","one"])
    assert args.action=="preview" and not args.allow_paid


async def test_planning_blocks_privacy_mutation_before_inference(engine,repo,monkeypatch):
    import asyncio
    import threading
    from local_ai_router.context import ContextGraph
    from local_ai_router.schema import Request
    entered, release = threading.Event(), threading.Event()
    def blocked_build(self):
        entered.set()
        release.wait(5)
        return {}
    monkeypatch.setattr(ContextGraph,"build",blocked_build)
    task=asyncio.create_task(engine.plan(Request(task="Implement a greeting feature",repo_path=str(repo))))
    try:
        assert await asyncio.to_thread(entered.wait,2)
        with pytest.raises(ValueError,match="active"):
            Management(engine).register_repository(str(repo),"CLOUD_ALLOWED")
    finally:
        task.cancel();release.set()
        await asyncio.gather(task,return_exceptions=True)
    assert engine.active_operations==0


async def test_profile_cannot_relax_local_role(engine):
    manager=Management(engine)
    manager.set_role("executor",{"locality":"local-only"})
    profile=manager.export_profile()
    profile["roles"]["executor"]["locality"]="any"
    with pytest.raises(ValueError,match="role restrictions"):
        manager.import_profile(profile)


async def test_orphan_charge_survives_without_blocking_config(tmp_path):
    from local_ai_router.store import Store
    settings=Settings(home=tmp_path)
    model=Model(id="charged",provider="p",deployment_name="x",input_price=1,output_price=1)
    store=Store(settings);store.reserve("r","s","gateway",model,.01,.1);store.close()
    engine=Engine(settings)
    try:
        Management(engine).set_policy({"preset":"fully-local"})
        assert engine.store.costs()["uncertain_calls"]==1
        assert engine.store.costs()["estimated_usd"]==.01
    finally:await engine.close()
