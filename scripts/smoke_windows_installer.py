"""Verify the actual Windows installer and UI in a disposable profile, without inference."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tempfile
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    import winreg

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, default=ROOT / "dist/windows/Accension-latest-windows-x64.exe")
    args = parser.parse_args(argv)
    installer = args.installer.resolve()
    manifest = json.loads(installer.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    checksum = installer.with_suffix(".exe.sha256").read_text().split()[0]
    assert hashlib.sha256(installer.read_bytes()).hexdigest() == checksum

    def uninstall_registration():
        path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{8836BF19-0B56-4D39-98D0-6C1B4FD48970}_is1"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                return [winreg.EnumValue(key, index) for index in range(winreg.QueryInfoKey(key)[1])]
        except FileNotFoundError:
            return None

    existing_registration = uninstall_registration()
    workspace = ROOT / "build/windows"
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=workspace) as temporary:
        folder = Path(temporary)
        install, home = folder / "Installed App", folder / "profile"
        (home / "config").mkdir(parents=True)
        (home / "config/discovery.yaml").write_text("discovery:\n  enabled: false\n", encoding="utf-8")
        marker = home / "keep-user-data.txt"
        marker.write_text("preserved", encoding="utf-8")
        env = {**os.environ, "ROUTER_HOME": str(home), "PYTHONPATH": "", "PYTHONHOME": ""}
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        (home / "config/runtime.yaml").write_text(f"port: {port}\n", encoding="utf-8")

        def run(command):
            result = subprocess.run(
                [str(arg) for arg in command],
                cwd=folder,
                env=env,
                timeout=180,
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise RuntimeError(
                    f"{Path(str(command[0])).name} failed: " + result.stderr.decode(errors="replace")[-600:]
                )
            return result.stdout.decode()

        group = "Accension Smoke " + folder.name
        links = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs" / group
        flags = [
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/SmokeTest=1",
            f"/DIR={install}",
            f"/GROUP={group}",
            "/TASKS=",
        ]
        run([installer, *flags])
        try:
            python = install / "python/python.exe"
            launcher = [install / "python/pythonw.exe", install / "desktop.py", "--no-browser"]
            assert (install / "accension.ico").is_file()
            assert (links / "Accension.lnk").is_file() and (links / "Stop Accension.lnk").is_file()
            version = json.loads(run([python, "-m", "local_ai_router.cli", "version", "--json"]))
            assert version["version"] == manifest["version"]
            package = install / "python/Lib/site-packages/local_ai_router/static"
            for name, expected in manifest["ui_assets"].items():
                assert hashlib.sha256((package / name).read_bytes()).hexdigest() == expected, name
                assert (
                    hashlib.sha256((ROOT / "src/local_ai_router/static" / name).read_bytes()).hexdigest() == expected
                ), name
            run(launcher)
            endpoint = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=6) as client:
                assert client.get(endpoint + "/health").json()["service"] == "local-ai-router"
                for name in (
                    "index.html",
                    "workbench.html",
                    "app.js",
                    "style.css",
                    "workbench.js",
                    "workbench.css",
                    "logo.svg",
                ):
                    path = {"index.html": "/ui", "workbench.html": "/ui/workbench"}.get(name, "/static/" + name)
                    response = client.get(endpoint + path)
                    assert response.status_code == 200, name
                    assert hashlib.sha256(response.content).hexdigest() == manifest["ui_assets"][name], name
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                for theme in ("light", "dark"):
                    page.emulate_media(color_scheme="dark" if theme == "light" else "light")
                    page.goto(endpoint + "/ui")
                    page.evaluate("theme => localStorage.setItem('accension-theme', theme)", theme)
                    page.reload()
                    expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    colors = "n => [getComputedStyle(n).backgroundColor, getComputedStyle(n).color]"
                    original = page.locator("body").evaluate(colors)
                    page.goto(endpoint + "/ui/workbench")
                    expect(page.locator("#connection")).to_contain_text("Connected")
                    assert page.locator("body").evaluate(colors) == original
                    assert page.locator(".brand img").evaluate("n => n.complete && n.naturalWidth > 0")
                    page.get_by_role("button", name="Board", exact=True).click()
                    expect(page.locator("#page-title")).to_have_text("Board")
                    assert page.locator("aside").evaluate("n => getComputedStyle(n).position") == "fixed"
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                browser.close()
            costs = json.loads(run([python, "-m", "local_ai_router.cli", "costs", "--json"]))
            assert costs["calls"] == 0
            print("PASS: installed asset hashes, branded light/dark UI, shortcuts and zero inference", flush=True)
            run([installer, *flags])
            with httpx.Client(trust_env=False, timeout=4) as client:
                try:
                    client.get(endpoint + "/health")
                except httpx.ConnectError:
                    pass
                else:
                    raise AssertionError("Upgrade left the previous hub running")
            assert marker.read_text() == "preserved"
            run(launcher)
            print("PASS: upgrade stops hub, preserves profile and relaunches", flush=True)
        finally:
            if (install / "unins000.exe").exists():
                run([install / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        assert marker.read_text() == "preserved"
        assert not (install / "python").exists()
        assert not (links / "Accension.lnk").exists()
        assert uninstall_registration() == existing_registration
        with httpx.Client(trust_env=False, timeout=4) as client:
            try:
                client.get(f"http://127.0.0.1:{port}/health")
            except httpx.ConnectError:
                pass
            else:
                raise AssertionError("Uninstall left the hub running")
    print("PASS: uninstall removes runtime/shortcuts, preserves profile and stops hub", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
