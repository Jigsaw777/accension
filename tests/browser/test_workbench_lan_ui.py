"""Two browser contexts use a scoped remote shell and host pairing controls."""

import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from test_workbench_manual import playwright
from workbench_tls import certificates

from local_ai_router.workbench.store import identity


@pytest.fixture
def lan_server(tmp_path):
    cert, key, trust = certificates(tmp_path)
    ports = []
    for _ in range(2):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    script = """
import asyncio,sys
from pathlib import Path
import uvicorn
from local_ai_router.config import load,WorkbenchLAN
from local_ai_router.app import create_app
from local_ai_router.workbench.lan import serve_pair
from local_ai_router.workbench.policy import Principal,CAPABILITIES
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import identity
settings=load(Path(sys.argv[1]),mock=True);settings.discovery.enabled=False;settings.port=int(sys.argv[2])
settings.workbench_lan=WorkbenchLAN(enabled=True,port=int(sys.argv[3]),certificate=sys.argv[4],private_key=sys.argv[5],origins=["https://127.0.0.1:"+sys.argv[3]],workspaces=["00000000-0000-4000-8000-000000000001"])
app=create_app(settings);runtime=app.state.workbench
human=Principal("fixture-human",ActorType.HUMAN,CAPABILITIES,frozenset({"*"}),True)
ws=runtime.service.seed_defaults();runtime.service.update("workspace",ws.id,ws.id,{"command_id":identity(),"expected_revision":ws.revision,"privacy_mode":"PRIVATE_LAN"},human)
project=runtime.service.create("project",{"command_id":identity(),"workspace_id":ws.id,"name":"LAN project","key":"LAN"},human).body
item=runtime.service.create("work_item",{"command_id":identity(),"workspace_id":ws.id,"project_id":project["id"],"title":"Review on phone","execution_kind":"MANUAL","spec":{"objective":"Review requirement notes","acceptance_criteria":["Notes reviewed"],"definition_of_done":["Attestation reviewed"]}},human).body
async def main():
 operation=runtime.service.item_command(ws.id,item["id"],{"command_id":identity(),"expected_revision":1,"command":"manual_submit","evidence":{"checklist":["Notes reviewed"],"references":["Fixture notes"]}},human)
 await runtime.planning.tasks[operation.body["id"]]
 server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=settings.port,access_log=False,log_level="error",proxy_headers=False))
 await serve_pair(server,runtime)
asyncio.run(main())
"""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), *map(str, ports), str(cert), str(key)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    )
    local, remote = f"http://127.0.0.1:{ports[0]}", f"https://127.0.0.1:{ports[1]}"
    try:
        with httpx.Client(verify=trust, trust_env=False) as client:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if client.get(remote + "/health").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                if process.poll() is not None:
                    pytest.fail(process.communicate()[1].decode())
                assert time.monotonic() < deadline
                time.sleep(0.05)
        yield local, remote, (tmp_path / ".router" / "api-token").read_text(), trust
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


@pytest.fixture
def browsers(lan_server):
    verified = httpx.Client(verify=lan_server[3], trust_env=False, timeout=30)

    class Bridge(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def forward(self):
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "content-length", "connection"}
            }
            if "Origin" in headers:
                headers["Origin"] = lan_server[1]
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                with verified.stream(self.command, lan_server[1] + self.path, headers=headers, content=body) as result:
                    self.send_response(result.status_code)
                    for key, value in result.headers.items():
                        if key.lower() not in {"transfer-encoding", "connection"}:
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in result.iter_raw():
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, httpx.HTTPError):
                pass
            self.close_connection = True

    bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    bridge.daemon_threads = True
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        host = browser.new_context()
        # Test-only loopback bridge verifies each TLS upstream request.
        # Native browser/OS CA trust remains a separate physical-device check.
        remote = browser.new_context(viewport={"width": 360, "height": 800})
        host_page, remote_page = host.new_page(), remote.new_page()
        host_page.goto(lan_server[0] + "/ui/workbench")
        playwright.expect(host_page.locator("#connection")).to_contain_text("Connected")
        remote_page.goto(f"http://127.0.0.1:{bridge.server_port}")
        yield host_page, remote_page, remote
        host.close()
        remote.close()
        browser.close()
    bridge.shutdown()
    bridge.server_close()
    verified.close()


def pair_browser(browsers, lan_server, capabilities="workspace.read,artifact.read"):
    host, remote, _ = browsers
    ws = host.locator("#workspace").input_value()
    with httpx.Client(
        base_url=lan_server[0], trust_env=False, headers={"authorization": "Bearer " + lan_server[2]}
    ) as client:
        result = client.post(
            "/accs/v1/workbench/devices/commands",
            json={"workspace_id": ws, "command_id": identity(), "action": "invite"},
        )
        assert result.status_code == 200, result.text
    remote.get_by_label("One-time pairing code").fill(result.json()["code"])
    remote.get_by_label("Device display name").fill("Test phone")
    remote.get_by_role("button", name="Request pairing").click()
    playwright.expect(remote.locator("#phrase")).to_contain_text("Compare with host:")
    phrase = remote.locator("#phrase").inner_text().split(": ")[-1]
    host.get_by_role("button", name="Settings", exact=True).click()
    host.get_by_label("Matching phrase from client").fill(phrase)
    host.get_by_label("Explicit capabilities (comma separated)").fill(capabilities)
    host.get_by_role("form").filter(has=host.get_by_role("heading", name="Compare client: Test phone")).get_by_role(
        "button", name="Save"
    ).click()
    playwright.expect(host.locator("#content")).to_contain_text("Workspace policy")
    remote.get_by_role("button", name="Host approved — connect").click()
    playwright.expect(remote.locator("#workspace-view")).to_be_visible()
    playwright.expect(remote.locator("#content")).to_contain_text("Review on phone")


def test_browser_pair_host_approval_readonly_client(browsers, lan_server, tmp_path):
    pair_browser(browsers, lan_server)
    host, page, context = browsers
    playwright.expect(page.locator("#notice")).to_have_text("")
    assert page.locator(".brand img").evaluate("node => node.complete && node.naturalWidth > 0")
    colors = "node => [getComputedStyle(node).backgroundColor, getComputedStyle(node).color]"
    for theme in ("light", "dark"):
        host.emulate_media(color_scheme=theme)
        page.emulate_media(color_scheme=theme)
        assert page.locator("body").evaluate(colors) == host.locator("body").evaluate(colors)
        screenshot = tmp_path / f"workbench-remote-{theme}-mobile.png"
        page.screenshot(path=str(screenshot), full_page=True)
        print(f"UI screenshot: {screenshot}")
    page.locator("#content button").click()
    playwright.expect(page.get_by_role("heading", name="Completion evidence")).to_be_visible()
    assert page.get_by_role("button", name="approve_completion", exact=True).count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0
    assert "accension_device" not in page.evaluate("document.cookie")
    assert page.get_by_role("link", name="Accension settings").count() == 0


def test_browser_mobile_review_scope(browsers, lan_server):
    pair_browser(browsers, lan_server, "workspace.read,artifact.read,review.approve")
    _, page, _ = browsers
    page.locator("#content button").focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#detail")).to_contain_text("REVIEW")
    playwright.expect(page.get_by_role("button", name="approve_completion", exact=True)).to_be_visible()
    page.get_by_role("button", name="approve_completion", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("DONE")


def test_browser_board_pagination(browsers, lan_server):
    pair_browser(browsers, lan_server)
    host, page, _ = browsers
    ws = host.locator("#workspace").input_value()
    with httpx.Client(
        base_url=lan_server[0], trust_env=False, headers={"authorization": "Bearer " + lan_server[2]}
    ) as client:
        projects = client.get("/accs/v1/workbench/projects", params={"workspace_id": ws}).json()["items"]
        for number in range(51):
            response = client.post(
                "/accs/v1/workbench/work-items",
                json={
                    "command_id": identity(),
                    "workspace_id": ws,
                    "project_id": projects[0]["id"],
                    "title": "Page item " + str(number),
                    "execution_kind": "MANUAL",
                },
            )
            assert response.status_code == 201
    page.get_by_role("button", name="Refresh", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Next page", exact=True)).to_be_visible()
    first = page.locator("#content button").first.inner_text()
    page.get_by_role("button", name="Next page", exact=True).click()
    playwright.expect(page.get_by_role("button", name="First page", exact=True)).to_be_visible()
    assert page.locator("#content button").first.inner_text() != first
    page.get_by_role("button", name="First page", exact=True).click()
    playwright.expect(page.locator("#content button").first).to_have_text(first)


def test_browser_revocation_and_offline_indicators(browsers, lan_server):
    pair_browser(browsers, lan_server, "workspace.read,artifact.read,review.approve")
    host, page, context = browsers
    page.locator("#content button").click()
    playwright.expect(page.get_by_role("button", name="approve_completion", exact=True)).to_be_visible()
    context.set_offline(True)
    playwright.expect(page.locator("#connection")).to_contain_text("Offline")
    playwright.expect(page.get_by_role("button", name="approve_completion", exact=True)).to_be_disabled()
    context.set_offline(False)
    page.get_by_role("button", name="Refresh", exact=True).click()
    playwright.expect(page.locator("#connection")).to_contain_text("Connected")
    host.get_by_role("button", name="Refresh pending devices").click()
    host.get_by_role("button", name="Revoke Test phone", exact=True).click()
    page.get_by_role("button", name="Refresh", exact=True).click()
    playwright.expect(page.locator("#connection")).to_contain_text("revoked")
