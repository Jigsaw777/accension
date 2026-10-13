"""Capture an isolated mock demo, or package verified public PNGs for LinkedIn."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "docs/guides/screenshots"
MEDIA = (
    ("01-workbench-board.png", "feature-board-dark-desktop.png"),
    ("02-prd-editor.png", "feature-prd-light-desktop.png"),
    ("03-feature-execution-review.png", "feature-approval-dark-desktop.png"),
    ("04-completion-evidence.png", "feature-review-light-desktop.png"),
    ("05-agent-performance.png", "feature-agents-dark-desktop.png"),
    ("06-ai-model-routing.png", "routing-overview-dark-desktop.png"),
)


def image_record(path):
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError(f"Not a PNG: {path.name}")
    width, height = struct.unpack(">II", data[16:24])
    if width < 300 or height < 300:
        raise ValueError(f"Undersized screenshot: {path.name}")
    return {"file": path.name, "width": width, "height": height, "sha256": hashlib.sha256(data).hexdigest()}


def package(source, output):
    manifest = json.loads((source / "capture-manifest.json").read_text(encoding="utf-8"))
    expected = {row["file"]: row for row in manifest["images"]}
    # Verify all inputs before touching an existing publication artifact.
    for _, name in MEDIA:
        actual = image_record(source / name)
        if actual != {key: expected[name][key] for key in actual}:
            raise ValueError(f"Manifest mismatch: {name}")
    output.mkdir(parents=True, exist_ok=True)
    folder = output / "accension-1.1.0-linkedin"
    archive = output / "Accension-1.1.0-LinkedIn-Screenshots.zip"
    with tempfile.TemporaryDirectory(prefix="accension-media-", dir=output) as directory:
        staged = Path(directory)
        notes = [
            "Accension 1.1.0 — LinkedIn screenshots",
            "Attach 01–05 in order; 06 routing is optional.",
            "Actual browser captures. Isolated synthetic data and mock inference; real local tests.",
            "Metrics/costs are demonstration data, not production benchmarks or provider invoices.",
            "No paid calls or personal working profile. Original resolution, no retouching.",
            "Rebuild: python scripts/capture_docs_screenshots.py --package-only",
            "Sources and provenance: docs/guides/screenshots/capture-manifest.json",
            "",
        ]
        for name, original in MEDIA:
            shutil.copyfile(source / original, staged / name)
            notes.append(f"{name} <- docs/guides/screenshots/{original}")
        (staged / "README.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
        temporary_zip = staged / "screenshots.zip"
        with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staged.iterdir()):
                if path.suffix in {".png", ".txt"}:
                    bundle.write(path, path.name)
        with zipfile.ZipFile(temporary_zip) as bundle:
            assert bundle.testzip() is None
            for name, original in MEDIA:
                assert bundle.read(name) == (source / original).read_bytes()
        folder.mkdir(exist_ok=True)
        for path in staged.iterdir():
            if path.suffix in {".png", ".txt"}:
                shutil.copyfile(path, folder / path.name)
        shutil.copyfile(temporary_zip, archive)
    return {"folder": str(folder.resolve()), "archive": str(archive.resolve()), "images": len(MEDIA)}


def serve(home, port):
    import uvicorn

    from local_ai_router.app import create_app
    from local_ai_router.config import Repository, load
    from local_ai_router.engine import Engine
    from local_ai_router.workbench.runtime import WorkbenchRuntime
    from local_ai_router.workbench.store import identity
    from scripts.demo_workbench import HUMAN, demo

    repo = home.parent / "repo"
    asyncio.run(demo(home, repo, feature=True))
    settings = load(home, mock=True)
    settings.discovery.enabled = False
    settings.port = port
    settings.repositories = [
        Repository(path=str(repo), validation={"tests": ["{python}", "-m", "unittest", "discover", "-v"]})
    ]
    engine = Engine(settings)
    runtime = WorkbenchRuntime(engine)
    ws = runtime.service.seed_defaults()
    runtime.service.update(
        "workspace",
        ws.id,
        ws.id,
        {"command_id": identity(), "expected_revision": ws.revision, "name": "Demo workspace"},
        HUMAN,
    )
    project = runtime.service.list("project", ws.id, HUMAN)[0]
    runtime.service.update(
        "project",
        ws.id,
        project.id,
        {"command_id": identity(), "expected_revision": project.revision, "name": "Greeting feature (demo)"},
        HUMAN,
    )
    runtime.service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "project_id": project.id,
            "title": "Verify the greeting contract",
            "execution_kind": "ENGINE",
            "spec": {
                "objective": "Implement greeting with tests and usage",
                "acceptance_criteria": ["Named and blank inputs pass"],
                "definition_of_done": ["Checks and artifacts reviewed"],
                "allowed_paths": ["greeting.py", "test_greeting.py", "USAGE.md"],
                "expected_artifacts": ["greeting.py"],
                "validation_checks": ["tests"],
            },
        },
        HUMAN,
    )
    uvicorn.run(create_app(settings, engine), host="127.0.0.1", port=port, access_log=False, log_level="error")


def capture(output):
    import httpx
    from playwright.sync_api import expect, sync_playwright

    if output.exists():
        raise ValueError(
            "Capture output already exists; choose a new directory. Verified images are never overwritten."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="accension-doc-capture-") as directory:
        base = Path(directory)
        staged = base / "images"
        staged.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with (base / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve", str(base / "home"), "--port", str(port)],
                stdout=log,
                stderr=log,
                cwd=ROOT,
                **options,
            )
            try:
                deadline = time.monotonic() + 60
                with httpx.Client(trust_env=False) as client:
                    while True:
                        try:
                            if client.get(url + "/health").is_success:
                                break
                        except httpx.ConnectError:
                            pass
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Isolated demo failed: " + (base / "server.log").read_text()[-3000:])
                        time.sleep(0.05)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.add_init_script("localStorage.setItem('accension-theme','dark')")
                    page.goto(url + "/ui/workbench")
                    expect(page.locator("#connection")).to_contain_text("Connected")
                    page.locator("#project").select_option(label="Greeting feature (demo)")

                    def shot(name, *, full_page=False):
                        page.evaluate("document.fonts.ready")
                        expect(page.locator(".brand img")).to_be_visible()
                        page.wait_for_function("document.querySelector('.brand img').complete")
                        assert not errors, errors
                        page.screenshot(path=str(staged / name), full_page=full_page)

                    page.get_by_role("button", name="Board", exact=True).click()
                    expect(page.locator("#content")).to_contain_text("Verify the greeting contract")
                    page.set_viewport_size({"width": 1920, "height": 1301})
                    page.locator(".board").evaluate("node => node.scrollLeft = node.scrollWidth - node.clientWidth")
                    shot("feature-board-dark-desktop.png")
                    page.set_viewport_size({"width": 390, "height": 900})
                    shot("feature-board-dark-mobile.png", full_page=True)
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.get_by_role("button", name="PRDs", exact=True).click()
                    page.get_by_role(
                        "button", name=re.compile(r"^Greeting function, tests and documentation ·")
                    ).click()
                    expect(page.get_by_label("PRD title", exact=True)).to_be_visible()
                    page.evaluate("document.documentElement.dataset.theme='light'")
                    page.get_by_label("PRD title", exact=True).scroll_into_view_if_needed()
                    shot("feature-prd-light-desktop.png")
                    page.evaluate("document.documentElement.dataset.theme='dark'")
                    page.get_by_role("button", name="Review imported feature", exact=True).click()
                    expect(page.locator("#detail")).to_contain_text("Review feature execution")
                    page.set_viewport_size({"width": 1440, "height": 1500})
                    page.get_by_role(
                        "heading", name="Review feature execution", exact=True
                    ).scroll_into_view_if_needed()
                    shot("feature-approval-dark-desktop.png")
                    page.get_by_role("button", name="Board", exact=True).click()
                    page.get_by_role("button", name="Verify the greeting contract", exact=True).click()
                    page.get_by_role("button", name="ready", exact=True).click()
                    page.get_by_role("button", name="Preview assignment", exact=True).click()
                    page.locator("#detail").get_by_role("button", name=re.compile(r"^Assign [a-f0-9]")).click()
                    page.get_by_role("button", name="Review execution contract", exact=True).click()
                    page.get_by_role("button", name="Approve execution", exact=True).click()
                    page.get_by_role("button", name="Review execution contract", exact=True).click()
                    page.get_by_role("button", name="Run approved item", exact=True).click()
                    item = page.locator(".column[data-state='REVIEW']").get_by_role(
                        "button", name="Verify the greeting contract", exact=True
                    )
                    expect(item).to_be_visible(timeout=20000)
                    item.click()
                    expect(
                        page.get_by_role("heading", name="Evidence for completion approval", exact=True)
                    ).to_be_visible()
                    page.evaluate("document.documentElement.dataset.theme='light'")
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.get_by_role("heading", name="Evidence for completion approval", exact=True).evaluate(
                        "node => scrollTo(0, node.getBoundingClientRect().top + scrollY - 160)"
                    )
                    shot("feature-review-light-desktop.png")
                    page.evaluate("document.documentElement.dataset.theme='dark'")
                    page.get_by_role("button", name="Agents", exact=True).click()
                    page.get_by_role("button", name="Fixture engine", exact=True).click()
                    page.get_by_role("button", name="Performance", exact=True).click()
                    expect(page.locator("#detail")).to_contain_text("duration samples")
                    page.set_viewport_size({"width": 1440, "height": 1500})
                    page.locator("#detail").evaluate(
                        "node => scrollTo(0, node.getBoundingClientRect().top + scrollY - 160)"
                    )
                    shot("feature-agents-dark-desktop.png")
                    page.goto(url + "/ui")
                    expect(page.locator(".brand img")).to_be_visible()
                    page.set_viewport_size({"width": 1440, "height": 1245})
                    shot("routing-overview-dark-desktop.png")
                    browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
        manifest = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
            "fixture": "Isolated greeting demo; mock inference; actual unittest checks; scripted fixture operator",
            "approval_image": "Review form over previously completed imported demo tasks; no second batch started",
            "paid_inference": False,
            "images": [image_record(path) for path in sorted(staged.glob("*.png"))],
        }
        (staged / "capture-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        shutil.copytree(staged, output)
    return {"capture": str(output.resolve()), "images": len(manifest["images"])}


def main():
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-only", action="store_true", help="Copy verified public captures into the media ZIP")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/docs-capture")
    parser.add_argument("--source", type=Path, default=SOURCES, help="Reviewed capture directory for packaging")
    parser.add_argument("--media-output", type=Path, default=ROOT / "dist/media")
    parser.add_argument("--serve", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve, args.port)
        return
    result = package(args.source, args.media_output) if args.package_only else capture(args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
