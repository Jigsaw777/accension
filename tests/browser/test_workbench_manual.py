"""Controlled Chromium, isolated provider-free daemon, real forms and journal updates."""

import re
import socket
import subprocess
import sys
import time

import httpx
import pytest

from local_ai_router.workbench.store import identity

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = """
import sys
from pathlib import Path
import uvicorn
from local_ai_router.config import Settings
from local_ai_router.app import create_app
settings = Settings(home=Path(sys.argv[1]), port=int(sys.argv[2]), mock=True)
settings.discovery.enabled = False
uvicorn.run(create_app(settings), host='127.0.0.1', port=settings.port, access_log=False, log_level='error')
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                if httpx.get(url + "/health", trust_env=False).status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode())
            assert time.monotonic() < deadline
            time.sleep(0.05)
        token = (tmp_path / ".router" / "api-token").read_text()
        yield url, token
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


@pytest.fixture
def page(server):
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.on(
            "response",
            lambda response: (
                print("browser response", response.status, response.url) if response.status >= 400 else None
            ),
        )
        page.on("requestfailed", lambda request: print("browser request failed", request.url, request.failure))
        page.on("pageerror", lambda error: print("browser script error", error))
        page.goto(server[0] + "/ui/workbench")
        playwright.expect(page.locator("#connection")).to_contain_text("Connected")
        yield page
        context.close()
        browser.close()


def project(page):
    page.get_by_role("button", name="Projects", exact=True).click()
    page.get_by_label("Project name", exact=True).fill("Browser project")
    page.get_by_label("Ticket prefix (e.g. ACC)", exact=True).fill("WB")
    page.get_by_role("button", name="Save", exact=True).click()
    playwright.expect(page.get_by_role("button", name="WB · Browser project")).to_be_visible()
    page.locator("#project").select_option(label="Browser project")


def create_item(page, title="Browser task"):
    page.get_by_role("button", name="Board", exact=True).click()
    page.get_by_role("button", name="Create work item", exact=True).click()
    page.get_by_label("Work item title", exact=True).fill(title)
    page.get_by_label("objective", exact=True).fill("Prove manual flow")
    page.get_by_label("acceptance criteria (one per line)", exact=True).fill("Flow retained")
    page.get_by_label("definition of done (one per line)", exact=True).fill("Evidence reviewed")
    page.locator("#detail").get_by_role("button", name="Save", exact=True).click()
    playwright.expect(page.locator("#content").get_by_role("button", name=title, exact=True)).to_be_visible()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_workbench_reuses_accension_theme(page, server, tmp_path, theme):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.emulate_media(color_scheme="dark" if theme == "light" else "light")
    page.evaluate("theme => localStorage.setItem('accension-theme', theme)", theme)
    page.goto(server[0] + "/ui")
    playwright.expect(page.locator("html")).to_have_attribute("data-theme", theme)
    colors = "node => [getComputedStyle(node).backgroundColor, getComputedStyle(node).color]"
    original = page.locator("body").evaluate(colors)
    sidebar = page.locator("aside").evaluate(colors)

    page.goto(server[0] + "/ui/workbench")
    playwright.expect(page.locator("#connection")).to_contain_text("Connected")
    project(page)
    create_item(page, "Restore the Accension theme")
    page.get_by_role("button", name="Overview", exact=True).click()
    page.get_by_role("button", name="Board", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Restore the Accension theme", exact=True)).to_be_visible()
    desktop = tmp_path / f"workbench-{theme}-desktop.png"
    page.screenshot(path=str(desktop), full_page=True)
    print(f"UI screenshot: {desktop}")
    assert page.locator("body").evaluate(colors) == original
    assert page.locator("aside").evaluate(colors) == sidebar
    assert page.locator(".brand img").evaluate("node => node.complete && node.naturalWidth > 0")
    playwright.expect(page.locator("header h1")).to_have_text("Board")
    playwright.expect(page.locator("#project-context")).to_contain_text("Browser project")
    playwright.expect(page.get_by_role("button", name="Board", exact=True)).to_have_attribute("aria-current", "page")
    assert page.locator("aside").evaluate("node => getComputedStyle(node).position") == "fixed"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    for width in (1024, 768):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 360, "height": 800})
    page.get_by_label("Visible board state").select_option("BACKLOG")
    playwright.expect(page.locator('.column[data-state="BACKLOG"]')).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    mobile = tmp_path / f"workbench-{theme}-mobile.png"
    page.screenshot(path=str(mobile), full_page=True)
    print(f"UI screenshot: {mobile}")


def test_workbench_uses_system_theme_when_storage_is_unavailable(page):
    page.emulate_media(color_scheme="dark")
    original = page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor")
    page.context.add_init_script("""
        Object.defineProperty(window, 'localStorage', {
            get() { throw new DOMException('Storage disabled', 'SecurityError'); }
        });
    """)
    page.reload()
    playwright.expect(page.locator("#connection")).to_contain_text("Connected")
    playwright.expect(page.locator("header h1")).to_have_text("Overview")
    assert page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor") == original


def test_browser_manual_create_edit_board_sse(page, server):
    project(page)
    create_item(page)
    page.locator("#content").get_by_role("button", name="Browser task", exact=True).click()
    page.locator("#detail").get_by_role("button", name="ready", exact=True).click()
    playwright.expect(page.locator('.column[data-state="READY"]')).to_contain_text("Browser task")
    ws = page.locator("#workspace").input_value()
    with httpx.Client(base_url=server[0], trust_env=False, headers={"Authorization": "Bearer " + server[1]}) as client:
        listing = client.get("/accs/v1/workbench/work-items", params={"workspace_id": ws}).json()["items"]
        item = listing[0]
        response = client.patch(
            "/accs/v1/workbench/work-items/" + item["id"],
            params={"workspace_id": ws},
            json={"command_id": identity(), "expected_revision": item["revision"], "title": "From API"},
        )
        assert response.status_code == 200
    playwright.expect(page.locator('.column[data-state="READY"]')).to_contain_text("From API")
    page.get_by_role("button", name="PRDs", exact=True).click()
    page.get_by_role("button", name="Create PRD", exact=True).click()
    page.get_by_label("PRD title", exact=True).fill("Manual requirements")
    page.locator("#detail").get_by_role("button", name="Save", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Manual requirements · DRAFT · revision 1")).to_be_visible()


def test_browser_keyboard_and_mobile_360px(page):
    page.set_viewport_size({"width": 360, "height": 800})
    page.get_by_role("button", name="Projects", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_label("Project name", exact=True).fill("Mobile")
    page.get_by_label("Ticket prefix (e.g. ACC)", exact=True).fill("MB")
    page.get_by_role("button", name="Save", exact=True).focus()
    page.keyboard.press("Enter")
    playwright.expect(page.get_by_role("button", name="MB · Mobile")).to_be_visible()
    page.get_by_role("button", name="Board", exact=True).click()
    playwright.expect(page.get_by_label("Visible board state")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_label("Visible board state").select_option("WAITING_FOR_HUMAN")
    playwright.expect(page.locator('.column[data-state="WAITING_FOR_HUMAN"]')).to_be_visible()


def test_browser_xss_and_stale_revision(page, server):
    project(page)
    payload = '<img src=x onerror="window.compromised=true">'
    create_item(page, payload)
    assert page.evaluate("window.compromised") is None
    page.locator("#content").get_by_role("button", name=payload, exact=True).click()
    page.get_by_label("Work item title", exact=True).fill("Unsaved draft")
    ws = page.locator("#workspace").input_value()
    with httpx.Client(base_url=server[0], trust_env=False, headers={"Authorization": "Bearer " + server[1]}) as client:
        item = client.get("/accs/v1/workbench/work-items", params={"workspace_id": ws}).json()["items"][0]
        client.patch(
            "/accs/v1/workbench/work-items/" + item["id"],
            params={"workspace_id": ws},
            json={"command_id": identity(), "expected_revision": 1, "title": "Other writer"},
        )
    page.locator("#detail").get_by_role("button", name="Save", exact=True).first.click()
    playwright.expect(page.locator("#notice")).to_contain_text("Conflict")
    assert page.get_by_label("Work item title", exact=True).input_value() == "Unsaved draft"
    page.get_by_role("link", name="Accension settings", exact=True).click()
    playwright.expect(page.locator("#nav")).to_contain_text("Providers")


def test_zero_provider_manual_workbench(page):
    project(page)
    create_item(page, "No providers required")
    page.context.set_offline(True)
    playwright.expect(page.locator("#connection")).to_contain_text("Offline")
    playwright.expect(page.get_by_role("button", name="Create work item")).to_be_disabled()
    page.context.set_offline(False)
    page.get_by_role("button", name="Refresh", exact=True).click()
    playwright.expect(page.locator("#connection")).to_contain_text("Connected")


def test_manual_comment_template_and_navigation_draft(page):
    project(page)
    create_item(page)
    page.locator("#content").get_by_role("button", name="Browser task", exact=True).click()
    page.get_by_label("Work item title", exact=True).fill("Retained across navigation")
    page.get_by_role("button", name="Overview", exact=True).click()
    page.get_by_role("button", name="Board", exact=True).click()
    page.locator("#content").get_by_role("button", name="Browser task", exact=True).click()
    assert page.get_by_label("Work item title", exact=True).input_value() == "Retained across navigation"
    page.get_by_label("Comment", exact=True).fill("Review note")
    page.get_by_role("form").filter(has=page.get_by_role("heading", name="Add comment", exact=True)).get_by_role(
        "button", name="Save"
    ).click()
    playwright.expect(page.get_by_label("Comment text", exact=True)).to_have_value("Review note")
    page.get_by_label("Comment text", exact=True).fill("Edited note")
    page.get_by_role("form").filter(has=page.get_by_role("heading", name="Edit comment", exact=True)).get_by_role(
        "button", name="Save"
    ).click()
    playwright.expect(page.get_by_label("Comment text", exact=True)).to_have_value("Edited note")
    page.get_by_role("button", name="Delete comment", exact=True).click()
    playwright.expect(page.get_by_label("Comment text", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Settings", exact=True).click()
    page.get_by_role("button", name="Export prd-default v1", exact=True).click()
    playwright.expect(page.get_by_label("Template YAML")).to_have_value(re.compile("prd-default"))
    page.get_by_role("button", name="Preview template", exact=True).click()
    playwright.expect(page.locator("#content pre").last).to_contain_text("problem_statement")


@pytest.mark.parametrize("action", ["refresh", "navigate"])
def test_render_discards_delayed_settings(page, action):
    held = []

    def delay_first_setup(route):
        if not held:
            held.append((route, route.fetch()))
        else:
            route.continue_()

    page.route("**/ui/workbench/api/lan/setup", delay_first_setup)
    with page.expect_request("**/ui/workbench/api/lan/setup"):
        page.get_by_role("button", name="Settings", exact=True).click()
    export = page.get_by_role("button", name="Export prd-default v1", exact=True)
    if action == "refresh":
        page.get_by_role("button", name="Refresh", exact=True).click()
        playwright.expect(export).to_have_count(1)
    else:
        page.get_by_role("button", name="Overview", exact=True).click()
        playwright.expect(page.locator("#content")).to_contain_text("Execution dispatcher:")
    with page.expect_response(re.compile(r"/ui/workbench/api/templates\?")):
        held[0][0].fulfill(response=held[0][1])
    playwright.expect(page.get_by_role("button", name="Settings", exact=True)).to_be_enabled()
    playwright.expect(export).to_have_count(1 if action == "refresh" else 0)
    if action == "refresh":
        export.click()
        playwright.expect(page.get_by_label("Template YAML")).to_have_value(re.compile("prd-default"))
    else:
        playwright.expect(page.locator("#content")).to_contain_text("Execution dispatcher:")
