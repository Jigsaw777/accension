import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def desktop(monkeypatch):
    launcher = module("accension_desktop", "installer/windows/desktop.py")
    settings = SimpleNamespace(host="127.0.0.1", port=8765)
    monkeypatch.setattr(launcher, "load", lambda home: settings)
    return launcher


def test_launcher_starts_before_opening_workbench(desktop, monkeypatch):
    actions = []
    monkeypatch.setattr(desktop, "start", lambda settings: actions.append("start"))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: actions.append(url) or True)
    assert desktop.main([]) == 0
    assert actions == ["start", "http://127.0.0.1:8765/ui/workbench"]


def test_silent_launcher_and_stop_do_not_open_browser(desktop, monkeypatch):
    actions = []
    monkeypatch.setattr(desktop, "start", lambda settings: actions.append("start"))
    monkeypatch.setattr(desktop, "stop", lambda settings: actions.append("stop"))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: pytest.fail("Unexpected browser launch"))
    assert desktop.main(["--no-browser"]) == 0
    assert desktop.main(["--stop"]) == 0
    assert actions == ["start", "stop"]


def test_failed_hub_start_has_safe_error_and_never_opens_browser(desktop, monkeypatch, capsys):
    def fail(settings):
        raise RuntimeError("PRIVATE_DIAGNOSTIC")

    monkeypatch.setattr(desktop, "start", fail)
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: pytest.fail("Hub not ready"))
    assert desktop.main(["--no-browser"]) == 2
    error = capsys.readouterr().err
    assert "could not open" in error and "service.log" in error and "PRIVATE_DIAGNOSTIC" not in error


def test_profile_override_and_invalid_port(desktop, monkeypatch):
    from local_ai_router.config import Settings

    homes = []
    monkeypatch.setattr(desktop, "load", lambda home: homes.append(home) or Settings(home=Path("test-profile")))
    ports = []
    monkeypatch.setattr(desktop, "start", lambda settings: ports.append(settings.port))
    assert desktop.main(["--home", "test-profile", "--port", "8767", "--no-browser"]) == 0
    assert homes == ["test-profile"] and ports == [8767]
    assert desktop.main(["--port", "80", "--no-browser"]) == 2
    assert ports == [8767]


def test_runtime_download_rejects_modified_cache(tmp_path):
    builder = module("windows_builder", "scripts/build_windows_installer.py")
    (tmp_path / builder.PYTHON_ARCHIVE).write_bytes(b"modified-runtime")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        builder.download_runtime(tmp_path)


def test_runtime_download_rejects_modified_response(tmp_path, monkeypatch):
    import httpx

    builder = module("windows_builder", "scripts/build_windows_installer.py")
    response = httpx.Response(200, content=b"modified-runtime", request=httpx.Request("GET", builder.PYTHON_URL))
    monkeypatch.setattr(builder.httpx, "get", lambda *a, **kw: response)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        builder.download_runtime(tmp_path)
    assert not (tmp_path / builder.PYTHON_ARCHIVE).exists()


def test_build_version_matches_public_package():
    from local_ai_router import __version__

    builder = module("windows_builder", "scripts/build_windows_installer.py")
    assert builder.version() == __version__
