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


def test_asset_hashes_detect_stale_installed_ui(tmp_path):
    builder = module("windows_builder", "scripts/build_windows_installer.py")
    source, installed = tmp_path / "source", tmp_path / "installed"
    for folder in (source, installed):
        (folder / "static").mkdir(parents=True)
        (folder / "static/workbench.html").write_text("current UI")
    assert builder.asset_hashes(source) == builder.asset_hashes(installed)
    (installed / "static/workbench.html").write_text("old UI")
    assert builder.asset_hashes(source) != builder.asset_hashes(installed)


def test_shared_logo_is_inert_and_referenced_by_every_shell():
    import xml.etree.ElementTree as xml

    from local_ai_router import ui

    svg = xml.fromstring((ui.STATIC / "logo.svg").read_text(encoding="utf-8"))
    assert {node.tag.rsplit("}", 1)[-1] for node in svg.iter()} == {"svg", "rect", "text"}
    for name, base in (("index.html", "/static/"), ("workbench.html", "/static/"), ("workbench-lan.html", "/assets/")):
        shell = (ui.STATIC / name).read_text(encoding="utf-8")
        assert 'src="' + base + 'logo.svg"' in shell
        assert 'href="' + base + 'logo.svg"' in shell


@pytest.mark.asyncio
async def test_logo_served_publicly_with_svg_media_type(engine):
    import httpx

    from local_ai_router.app import create_app
    from local_ai_router.ui import STATIC

    app = create_app(engine.settings, engine)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.get("/static/logo.svg")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("image/svg+xml")
            assert response.content == (STATIC / "logo.svg").read_bytes()
    finally:
        await app.state.workbench.close()


def test_stop_waits_for_cleanup_marker_after_listener_closes(tmp_path, monkeypatch):
    from local_ai_router import service

    marker = tmp_path / "server.pid"
    marker.write_text("123")
    settings = SimpleNamespace(state=tmp_path, token="fixture", host="127.0.0.1", port=8765)
    states = iter([True, False, False])
    monkeypatch.setattr(service, "status", lambda _: {"running": next(states)})
    sleeps = []

    def sleep(_):
        sleeps.append(True)
        if len(sleeps) == 2:
            marker.unlink()

    monkeypatch.setattr(service.time, "sleep", sleep)

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(service.httpx, "Client", lambda **kwargs: Client())
    assert service.stop(settings) == {"status": "stopped", "running": False}
    assert len(sleeps) == 2
