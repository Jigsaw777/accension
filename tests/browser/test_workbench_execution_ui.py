"""Real Chromium actions drive one isolated mock Engine through human review."""

import os
import re
import socket
import subprocess
import sys
import time

import httpx
import pytest
from test_workbench_manual import page as page
from test_workbench_manual import playwright


@pytest.fixture
def server(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_contract.py").write_text(
        'import unittest\nfrom greeting import greet\nclass Contract(unittest.TestCase):\n    def test_greeting(self): self.assertEqual(greet(" "),"Hello, world!")\n'
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = """
import sys
from pathlib import Path
import uvicorn
from local_ai_router.config import load, Repository
from local_ai_router.engine import Engine
from local_ai_router.app import create_app
from local_ai_router.workbench.policy import Principal,CAPABILITIES
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import identity
settings=load(Path(sys.argv[1]),mock=True)
settings.port=int(sys.argv[2]);settings.discovery.enabled=False
settings.repositories=[Repository(path=sys.argv[3],validation={"tests":["{python}","-m","unittest","discover","-v"]})]
engine=Engine(settings);app=create_app(settings,engine);runtime=app.state.workbench
human=Principal("fixture-human",ActorType.HUMAN,CAPABILITIES,frozenset({"*"}),True)
ws=runtime.service.seed_defaults()
reference=next(iter(runtime.service.repositories()))
project=runtime.service.create("project",{"command_id":identity(),"workspace_id":ws.id,"key":"BX","name":"Execution","repository_ref":reference},human).body
runtime.agents.configure({"command_id":identity(),"name":"Browser engine","allowed_workspaces":[ws.id],"repository_permissions":[reference]},human)
runtime.service.create("work_item",{"command_id":identity(),"workspace_id":ws.id,"project_id":project["id"],"title":"Browser execution","execution_kind":"ENGINE","spec":{"objective":"Implement greeting","acceptance_criteria":["Blank input greets world"],"definition_of_done":["Independent checks pass"],"allowed_paths":["greeting.py"],"expected_artifacts":["greeting.py"],"validation_checks":["tests"]}},human)
uvicorn.run(app,host="127.0.0.1",port=settings.port,access_log=False,log_level="error")
"""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(port), str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
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
        yield url, (tmp_path / ".router" / "api-token").read_text(), repo
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def open_work(page):
    page.locator("#project").select_option(label="Execution")
    page.get_by_role("button", name="Board", exact=True).click()
    page.locator("#content").get_by_role("button", name="Browser execution", exact=True).click()


def approve_run(page, wait=True):
    page.get_by_role("button", name="ready", exact=True).click()
    page.get_by_role("button", name="Preview assignment", exact=True).click()
    page.locator("#detail").get_by_role("button", name=re.compile(r"^Assign [a-f0-9]")).click()
    page.get_by_role("button", name="Review execution contract", exact=True).click()
    page.get_by_role("button", name="Approve execution", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Approve execution", exact=True)).to_be_hidden()
    page.get_by_role("button", name="Review execution contract", exact=True).click()
    page.get_by_role("button", name="Run approved item", exact=True).click()
    if not wait:
        return
    playwright.expect(
        page.locator("#content .column[data-state='REVIEW']").get_by_role(
            "button", name="Browser execution", exact=True
        )
    ).to_be_visible(timeout=15000)
    page.locator("#content .column[data-state='REVIEW']").get_by_role(
        "button", name="Browser execution", exact=True
    ).click()
    playwright.expect(page.get_by_role("button", name="approve_completion", exact=True)).to_be_visible()


def test_browser_real_execution_board_events(page, server):
    # Keep the real approval response in flight so stale controls cannot hide a race.
    page.evaluate("""() => {
        const fetch = window.fetch.bind(window);
        window.fetch = async (url, options) => {
            const response = await fetch(url, options);
            if (options?.body && JSON.parse(options.body).command === "approve_execution")
                await new Promise(resolve => setTimeout(resolve, 500));
            return response;
        };
    }""")
    open_work(page)
    approve_run(page)
    playwright.expect(page.get_by_role("heading", name="Evidence for completion approval")).to_be_visible()
    page.get_by_label("Command reason").fill("Reviewed real test evidence")
    page.get_by_role("button", name="approve_completion", exact=True).click()
    playwright.expect(
        page.locator("#content .column[data-state='DONE']").get_by_role("button", name="Browser execution", exact=True)
    ).to_be_visible()
    page.get_by_role("button", name="Agents", exact=True).click()
    playwright.expect(page.locator("#content")).to_contain_text("Browser engine")
    page.get_by_role("button", name="Runs", exact=True).click()
    playwright.expect(page.locator("#content")).to_contain_text("SUCCEEDED")


def test_browser_approve_stale_evidence_rejected(page, server):
    open_work(page)
    approve_run(page)
    (server[2] / "greeting.py").write_text("user change\n")
    page.get_by_label("Command reason").fill("Approval displayed evidence")
    page.get_by_role("button", name="approve_completion", exact=True).click()
    playwright.expect(page.locator("#notice")).to_contain_text("Conflict")
    assert (server[2] / "greeting.py").read_text() == "user change\n"


def test_browser_cancel_pending_and_recovery(page, server):
    (server[2] / "test_slow.py").write_text(
        "import unittest,time\nclass Slow(unittest.TestCase):\n    def test_wait(self): time.sleep(30)\n"
    )
    open_work(page)
    approve_run(page, wait=False)
    deadline = time.monotonic() + 10
    while not (server[2] / "greeting.py").exists():
        assert time.monotonic() < deadline
        page.wait_for_timeout(50)
    page.locator("#content").get_by_role("button", name="Browser execution", exact=True).click()
    page.get_by_role("button", name="cancel", exact=True).click()
    playwright.expect(
        page.locator("#content .column[data-state='CANCELLED']").get_by_role(
            "button", name="Browser execution", exact=True
        )
    ).to_be_visible(timeout=15000)
    assert not (server[2] / "greeting.py").exists()


def test_browser_assignment_preview_vs_actual(page, server):
    open_work(page)
    page.get_by_role("button", name="ready", exact=True).click()
    page.get_by_role("button", name="Preview assignment", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("candidate_model_id")
    assert not (server[2] / "greeting.py").exists()
    page.locator("#detail").get_by_role("button", name=re.compile(r"^Assign [a-f0-9]")).click()
    playwright.expect(page.get_by_role("button", name="Review execution contract", exact=True)).to_be_visible()
    assert not (server[2] / "greeting.py").exists()


def test_browser_provider_failure_does_not_break_management(page, server):
    headers = {"Authorization": "Bearer " + server[1]}
    # Use an unavailable worker independently of optional V1 model action routes.
    agents = httpx.get(
        server[0] + "/accs/v1/workbench/agents",
        params={"workspace_id": "00000000-0000-4000-8000-000000000001"},
        headers=headers,
        trust_env=False,
    ).json()["items"]
    agent = agents[0]
    fields = {
        k: agent[k]
        for k in (
            "name",
            "role",
            "adapter_kind",
            "capabilities",
            "tools",
            "skills",
            "repository_permissions",
            "allowed_workspaces",
            "allowed_projects",
            "supported_model_roles",
            "privacy_policy",
            "concurrency_limit",
        )
    }
    fields.update(command_id=__import__("uuid").uuid4().__str__(), expected_revision=agent["revision"], enabled=False)
    assert (
        httpx.patch(
            server[0] + "/accs/v1/workbench/agents/" + agent["id"], json=fields, headers=headers, trust_env=False
        ).status_code
        == 200
    )
    open_work(page)
    page.get_by_role("button", name="ready", exact=True).click()
    page.get_by_role("button", name="Preview assignment", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("AGENT_DISABLED")
    page.get_by_role("button", name="PRDs", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Create PRD", exact=True)).to_be_visible()
