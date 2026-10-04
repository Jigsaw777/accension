import os
import sys
from types import SimpleNamespace

import pytest

from local_ai_router import credentials
from local_ai_router.auth import AuthManager, CommandAuth
from local_ai_router.errors import AuthenticationRequired
from local_ai_router.schema import Provider


def test_credential_references_and_environment(settings, monkeypatch):
    store = credentials.CredentialStore(settings)
    monkeypatch.setenv("FIXTURE_CREDENTIAL", "fixture_value_only")
    assert store.read("env:FIXTURE_CREDENTIAL") == "fixture_value_only"
    assert credentials.read_secret(settings, "FIXTURE_CREDENTIAL") == "fixture_value_only"
    assert credentials.read_secret(settings, "") == store.read("") == ""
    assert credentials.read_secret(settings, "MISSING_FIXTURE") == ""
    for value in ("../escape", "provider/../../outside", "env:bad-name", "/absolute"):
        with pytest.raises(ValueError):
            store.read(value)
    for value in ("", "short", "x" * 8193, 123):
        with pytest.raises(ValueError):
            store.save("provider/test", value)
    with pytest.raises(ValueError):
        credentials.secret_path(settings, "bad/path")
    with pytest.raises(ValueError):
        credentials.save_secret(settings, "FIXTURE", "short")


def test_windows_vault_atomic_roundtrip_without_plaintext(settings, monkeypatch):
    real_os = credentials.os
    monkeypatch.setattr(credentials, "os", SimpleNamespace(name="nt", getenv=real_os.getenv, environ=real_os.environ))

    # A deterministic stand-in exercises file handling on every CI platform.
    def crypt(value, decrypt=False):
        if decrypt:
            if not value.startswith(b"protected:"):
                raise RuntimeError("Corrupt ciphertext")
            return bytes(x ^ 173 for x in value[10:])
        return b"protected:" + bytes(x ^ 173 for x in value)

    monkeypatch.setattr(credentials, "_crypt", crypt)
    store = credentials.CredentialStore(settings)
    store.save("provider/test", "fixture_secret_value")
    path = store._path("provider/test")
    assert b"fixture_secret_value" not in path.read_bytes()
    assert store.read("provider/test") == "fixture_secret_value"
    assert not path.with_suffix(".tmp").exists()
    path.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="Corrupt"):
        store.read("provider/test")
    store.delete("provider/test")
    assert store.read("provider/test") == ""
    credentials.save_secret(settings, "LEGACY_FIXTURE", "fixture_legacy_value")
    assert credentials.read_secret(settings, "LEGACY_FIXTURE") == "fixture_legacy_value"


def test_keyring_rejects_plaintext_and_never_falls_back(settings, monkeypatch):
    monkeypatch.setattr(credentials, "os", SimpleNamespace(name="posix", environ=os.environ, getenv=os.getenv))
    values = {}
    backend = SimpleNamespace(priority=1)
    fake = SimpleNamespace(
        get_keyring=lambda: backend,
        set_password=lambda service, ref, value: values.update({ref: value}),
        get_password=lambda service, ref: values.get(ref),
        delete_password=lambda service, ref: values.pop(ref),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    store = credentials.CredentialStore(settings)
    store.save("provider/test", "fixture_secret_value")
    assert store.read("provider/test") == "fixture_secret_value"
    store.delete("provider/test")
    assert store.read("provider/test") == ""
    backend.priority = 0
    with pytest.raises(RuntimeError, match="vault unavailable"):
        store.save("provider/test", "fixture_secret_value")
    with pytest.raises(RuntimeError, match="vault unavailable"):
        store.read("provider/test")
    assert not list(settings.state.rglob("*.dpapi"))
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(RuntimeError, match="Install accension"):
        store._keyring()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration; mocked storage path is tested on every OS")
def test_real_dpapi_roundtrip():
    value = b"fixture_local_only_value"
    encrypted = credentials._crypt(value)
    assert encrypted != value
    assert credentials._crypt(encrypted, decrypt=True) == value


@pytest.mark.parametrize(
    "kind,header",
    [("openai", "Authorization"), ("foundry", "api-key"), ("anthropic", "x-api-key"), ("gemini", "x-goog-api-key")],
)
async def test_auth_headers_and_no_log_leak(settings, monkeypatch, kind, header):
    from local_ai_router.observability import EventLog

    monkeypatch.setenv("FIXTURE_CREDENTIAL", "fixture_value_only")
    manager = AuthManager(settings)
    provider = Provider(kind=kind, api_key_env="FIXTURE_CREDENTIAL")
    headers = await manager.headers(provider)
    assert headers[header].endswith("fixture_value_only")
    log = EventLog(settings)
    log.emit("auth", "headers", headers=headers, authorization=headers[header], provider=kind)
    assert "fixture_value_only" not in log.path.read_text()
    log.close()
    monkeypatch.delenv("FIXTURE_CREDENTIAL")
    with pytest.raises(AuthenticationRequired):
        await manager.headers(provider)
    manager.close()


async def test_auth_command_cache_and_failure_never_echoes_token(settings, monkeypatch):
    import asyncio

    manager = AuthManager(settings)
    provider = Provider(kind="openai", auth="command", token_command=["trusted-fixture"])
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"fixture_token_value", b""

        def kill(self):
            calls.append("killed")

        async def wait(self):
            return 0

    process = Process()

    async def spawn(*args, **kwargs):
        calls.append("spawn")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    first = await manager.headers(provider)
    assert (await manager.headers(provider)) == first and calls == ["spawn"]
    manager.cache.clear()
    process.returncode = 1
    with pytest.raises(AuthenticationRequired) as error:
        await manager.headers(provider)
    assert "fixture_token_value" not in str(error.value)
    with pytest.raises(AuthenticationRequired):
        await CommandAuth().headers(Provider(kind="openai"), manager)
    manager.close()
