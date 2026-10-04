import json
import time
from pathlib import Path

import httpx
import pytest

from local_ai_router.config import Settings, Routing, Repository, Privacy, RolePolicy, load
from local_ai_router.schema import Model, Provider, Classification
from local_ai_router.engine import Engine
from local_ai_router.errors import PrivacyViolation, RateLimited, AuthenticationRequired, normalize_error
from local_ai_router.migration import migrate_files
from local_ai_router.privacy import repository_scope
from local_ai_router.providers import parse_json


async def test_zero_models_preview_does_not_invoke_classifier(tmp_path, monkeypatch):
    settings = Settings(home=tmp_path)
    engine = Engine(settings)
    async def forbidden(*args, **kwargs):
        pytest.fail("Route preview invoked a model or network")
    monkeypatch.setattr(engine.router.classifier, "classify", forbidden)
    monkeypatch.setattr(engine.providers, "generate", forbidden)
    monkeypatch.setattr(engine.providers.client, "get", forbidden)
    settings.routing.classifier_enabled = True
    settings.routing.classifier_command = "present"
    try:
        result = await engine.route("Architect a multi-module migration")
        assert result["inference_calls"] == 0
        assert result["classification"]["planning_depth"] == "FULL_ARCHITECTURE_PLAN"
        assert result["status"] == "configuration_required"
        assert result["roles"]["arbiter"]["fallback"] == "local deterministic policy"
    finally:
        await engine.close()


def test_legacy_routing_has_mutable_accessors_but_generic_serialization():
    with pytest.warns(DeprecationWarning):
        routing = Routing(laya_enabled=True, laya_command="classifier", jev_model="my-arbiter")
    assert routing.classifier_enabled and routing.arbiter_model == "my-arbiter"
    routing.laya_timeout = 3
    routing.jev_model = "another-model"
    assert routing.classifier_timeout == 3 and routing.arbiter_model == "another-model"
    assert not any(key.startswith(("laya", "jev")) for key in routing.model_dump())


@pytest.mark.parametrize("executable", ["python3.11", "python3.12", "python3.13", "python3.14"])
def test_supported_python_validation_commands_keep_guards(executable):
    from local_ai_router.safety import validation_argv
    assert validation_argv({"tests": [executable, "-m", "pytest", "-q"]}, "tests") == [executable, "-m", "pytest", "-q"]
    for args in (["-c", "print('untrusted')"], ["-m", "http.server"], ["-m"]):
        with pytest.raises(ValueError, match="test/lint"):
            validation_argv({"tests": [executable, *args]}, "tests")


def test_config_migration_backups_and_idempotence(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    old = "routing:\n  laya_enabled: false\n  jev_model: custom-arbiter\nrepositories:\n  - path: project\n    allow_cloud: true\n"
    (config / "local.yaml").write_text(old)
    result = migrate_files(tmp_path)
    assert (Path(result["backup"]) / "local.yaml").read_text() == old
    assert load(tmp_path).routing.arbiter_model == "custom-arbiter"
    assert load(tmp_path).repositories[0].privacy.mode == "CLOUD_ALLOWED"
    assert migrate_files(tmp_path)["changed"] == []


def test_nested_model_profile_preserves_evidence():
    model = Model(id="one", provider="remote", remote_id="deployment", capabilities={"vision": True},
                  economics={"input_price": 1, "output_price": 2}, context={"input": 10000, "output": 1000})
    assert model.supports_vision and model.deployment_name == "deployment"
    assert model.profile()["context"]["input"] == 10000
    assert model.profile()["economics"]["pricing_status"] == "user_supplied"


async def test_migration_locks_out_concurrent_privacy_save(tmp_path, monkeypatch):
    import local_ai_router.migration as migration
    from local_ai_router.management import Management
    config = tmp_path / "config"
    config.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (config / "local.yaml").write_text("routing:\n  laya_enabled: false\n")
    engine = Engine(load(tmp_path))
    management = Management(engine)
    original_copy = migration.shutil.copy2
    attempted = []
    def concurrent_save(source, destination):
        with pytest.raises(ValueError, match="owns this repository"):
            management.register_repository(str(project), "LOCAL_ONLY")
        attempted.append(True)
        return original_copy(source, destination)
    monkeypatch.setattr(migration.shutil, "copy2", concurrent_save)
    try:
        migrate_files(tmp_path)
        assert attempted
        assert load(tmp_path).routing.classifier_enabled is False
    finally:
        await engine.close()


def test_migration_detects_manual_edit_during_backup(tmp_path, monkeypatch):
    import local_ai_router.migration as migration
    config = tmp_path / "config"
    config.mkdir()
    path = config / "local.yaml"
    path.write_text("routing:\n  laya_enabled: false\n")
    new = "routing:\n  classifier_enabled: false\ncontrol_plane:\n  fully_local: true\n"
    original_copy = migration.shutil.copy2
    def manual_edit(source, destination):
        result = original_copy(source, destination)
        path.write_text(new)
        return result
    monkeypatch.setattr(migration.shutil, "copy2", manual_edit)
    with pytest.raises(ValueError, match="changed during migration"):
        migrate_files(tmp_path)
    assert path.read_text() == new


async def test_remote_arbiter_cannot_bypass_local_control(engine, monkeypatch):
    engine.settings.providers["remote"] = Provider(kind="openai", endpoint="https://example.invalid/v1")
    remote = engine.settings.models[0].model_copy(update={"id": "remote", "provider": "remote"})
    engine.settings.models.append(remote)
    engine.settings.routing.arbiter_model = "remote"
    async def forbidden(*args, **kwargs):
        pytest.fail("Remote arbiter was called")
    monkeypatch.setattr(engine.providers, "generate", forbidden)
    assert await engine.router.arbitrate(Classification(risk=90), "request", "session", .5, [remote]) is None
    engine.settings.control_plane.routing_location = "hybrid"
    assert await engine.router.arbitrate(Classification(risk=90), "request", "session", .5, [remote], allow_cloud=False) is None


async def test_privacy_is_enforced_at_transport_boundary(engine):
    engine.settings.providers["remote"] = Provider(kind="openai", endpoint="https://example.invalid/v1")
    remote = engine.settings.models[0].model_copy(update={"id": "remote", "provider": "remote"})
    with repository_scope(Repository(path="unused", privacy=Privacy(mode="LOCAL_ONLY"))):
        with pytest.raises(PrivacyViolation):
            await engine.providers.generate(remote, [{"role": "user", "content": "private source"}], "executor", "r", "s", .5)
    assert engine.store.costs()["calls"] == 0


async def test_fully_local_blocks_cloud_metadata_and_inference(engine, monkeypatch):
    engine.settings.mock = False
    engine.settings.providers["remote"] = Provider(kind="openai", endpoint="https://example.invalid/v1")
    engine.settings.control_plane.fully_local = True
    engine.settings.discovery.enabled = True
    async def forbidden(*args, **kwargs):
        pytest.fail("Fully local mode attempted cloud traffic")
    monkeypatch.setattr(engine.providers.client, "get", forbidden)
    assert (await engine.discovery.refresh(True))["providers"]["remote"] == "privacy_blocked"
    assert (await engine.providers.health(engine.settings.providers["remote"]))["error"] == "PRIVACY_VIOLATION"


async def test_role_pin_falls_back_and_unknown_price_is_never_free(engine):
    s = engine.settings
    s.roles["executor"] = RolePolicy(strategy="pinned", model="removed-model")
    result = engine.router.roles.resolve("executor")
    assert result["selected"] == "mock-worker" and "fallback" in result["notice"]
    s.providers["remote"] = Provider(kind="openai", endpoint="https://example.invalid/v1")
    s.models = [s.models[0].model_copy(update={"id": "remote", "provider": "remote", "input_price": None, "output_price": None})]
    assert not engine.router.candidates(Classification())
    assert "PRICE_UNKNOWN" in engine.router.roles.resolve("executor")["candidates"][0]["reason_codes"]
    s.routing.allow_unknown_pricing = True
    candidate = engine.router.candidates(Classification())[0]
    assert candidate.input_price > 0 and candidate.pricing_status == "estimated"
    assert s.models[0].input_price is None


async def test_roles_use_capability_context_health_and_resources(engine):
    m = engine.settings.models[0]
    assert engine.router.roles.resolve("vision")["selected"] is None
    m.supports_vision = True
    assert engine.router.roles.resolve("vision")["selected"] == m.id
    assert not engine.router.candidates(Classification(), inputs=m.context_window)
    engine.store.provider_health("mock", "RATE_LIMITED", 60)
    assert not engine.router.candidates(Classification())
    engine.store.provider_health("mock", "HEALTHY")
    m.ram_estimate_mb = 30000
    engine.settings.routing.available_ram_mb = 8000
    assert not engine.router.candidates(Classification())
    m.loaded = True
    assert engine.router.candidates(Classification())


@pytest.mark.parametrize("text,expected", [('```json\n{"ok": true}\n```', {"ok": True}), ('{"items": [1,2,],}', {"items": [1,2]}), ('{"ok": true', {"ok": True})])
def test_bounded_json_syntax_repair(text, expected):
    assert parse_json(text) == expected


def test_malformed_json_does_not_invent_values():
    with pytest.raises(ValueError):
        parse_json('{"token": "unfinished')


@pytest.mark.parametrize("code,expected", [(401, AuthenticationRequired), (403, AuthenticationRequired), (429, RateLimited)])
def test_normalized_error_excludes_secret_body(code, expected):
    request = httpx.Request("POST", "https://example.invalid/?secret=hidden")
    response = httpx.Response(code, request=request, text="secret material", headers={"Retry-After": "2"})
    error = normalize_error(httpx.HTTPStatusError("secret material", request=request, response=response))
    assert isinstance(error, expected) and "secret" not in str(error)
    if code == 429:
        assert error.retry_after == 2


async def test_registry_survives_failed_inventory_and_restart(tmp_path):
    settings = Settings(home=tmp_path, providers={"local": Provider(kind="openai-compatible", endpoint="http://127.0.0.1:9998/v1", local=True)})
    settings.discovery.local_scan = False
    failed = False
    def handle(request):
        if failed:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"data": [{"id": "arbitrary-name", "capabilities": {"tools": True}}]})
    engine = Engine(settings, httpx.MockTransport(handle))
    await engine.discovery.refresh(True)
    assert settings.models[0].supports_tools
    failed = True
    await engine.discovery.refresh(True)
    assert settings.models[0].enabled and settings.models[0].inventory_stale
    await engine.close()
    restored = Settings(home=tmp_path, providers=settings.providers)
    again = Engine(restored, httpx.MockTransport(handle))
    try:
        assert restored.models[0].deployment_name == "arbitrary-name"
        assert restored.models[0].inventory_stale
    finally:
        await again.close()


@pytest.mark.parametrize("provider", [
    Provider(kind="bedrock", local=True, endpoint="http://127.0.0.1:9999", region="us-east-1"),
    Provider(kind="openai", local=True, endpoint="http://127.0.0.1:9999", auth="google_adc"),
    Provider(kind="foundry", local=True, endpoint="http://127.0.0.1:9999", azure_identity=True),
])
async def test_cloud_transports_cannot_claim_loopback_locality(engine, provider):
    from local_ai_router.privacy import is_local
    assert not is_local(provider)
    engine.settings.control_plane.fully_local = True
    with pytest.raises(PrivacyViolation):
        await engine.providers.auth.headers(provider)


async def test_optional_arbiter_failure_and_disabled_policy(engine, monkeypatch):
    engine.settings.routing.arbiter_model = "mock-worker"
    async def unavailable(*args, **kwargs):
        raise AuthenticationRequired("expired")
    monkeypatch.setattr(engine.providers, "generate", unavailable)
    assert await engine.router.arbitrate(Classification(risk=90), "r", "s", .5, engine.settings.models) is None
    engine.settings.roles["arbiter"].strategy = "disabled"
    async def forbidden(*args, **kwargs):
        pytest.fail("Disabled arbiter invoked")
    monkeypatch.setattr(engine.providers, "generate", forbidden)
    assert await engine.router.arbitrate(Classification(risk=90), "r", "s", .5, engine.settings.models) is None


async def test_repair_role_enforced_at_boundary(engine):
    engine.settings.roles["repairer"].strategy = "disabled"
    with pytest.raises(PrivacyViolation):
        await engine.providers.generate(engine.settings.models[0], [{"role": "user", "content": "repair"}], "repair", "r", "s", 0)


async def test_calibration_prior_does_not_override_failure_history(engine):
    model = engine.settings.models[0]
    model.quality["coding"] = .99
    before = engine.router.roles.resolve("executor")["candidates"][0]["quality_estimate"]
    for _ in range(20):
        engine.store.success(model.id, "coding", False, 1)
    after = engine.router.roles.resolve("executor")["candidates"][0]["quality_estimate"]
    assert after < before


async def test_legacy_anthropic_provider_gets_version_header(engine):
    headers = await engine.providers.headers(Provider(kind="anthropic", endpoint="https://example.invalid/v1"))
    assert headers["anthropic-version"] == "2023-06-01"
