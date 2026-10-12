"""Rough idea uses a selected real provider path and survives reload without resubmit."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from test_workbench_manual import page, playwright, project  # noqa: F401
from test_workbench_planning import SPEC


@pytest.fixture
def server(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_contract.py").write_text(
        'import unittest\nfrom greeting import greet\nclass Contract(unittest.TestCase):\n    def test_behavior(self):\n        self.assertEqual(greet(" Ada "), "Hello, Ada!")\n        self.assertEqual(greet(" "), "Hello, world!")\n'
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = """
import asyncio,json,sys
from pathlib import Path
import uvicorn
from local_ai_router.config import Settings,Repository
from local_ai_router.app import create_app
from local_ai_router.engine import Engine
from local_ai_router.schema import ModelDescriptor,Provider,Generation
settings=Settings(home=Path(sys.argv[1]),port=int(sys.argv[2]),mock=True)
settings.discovery.enabled=False
settings.models=[ModelDescriptor(id='selected-local',provider='fixture',deployment_name='Selected local')]
settings.providers={'fixture':Provider(kind='mock',local=True)}
settings.routing.allow_below_slo=True
settings.repositories=[Repository(path=str(settings.home/'repo'),validation={'tests':['{python}','-m','unittest','discover','-v']})]
engine=Engine(settings)
from local_ai_router import mock
async def generate(provider,model,messages,role,maximum,schema,task_id,**kwargs):
    with (settings.home/'captured.jsonl').open('a') as output: output.write(json.dumps({'model':model.id,'role':role})+'\\n')
    await asyncio.sleep(1)
    if role in {'executor','repair','reviewer'}:
        return mock.generate(messages,role)
    if schema.get('title')=='ExecutionPlan':
        return Generation(text=json.dumps({'goal':'Greeting','success_criteria':['Greeting verified'],'tasks':[{'id':'greeting','title':'Greeting task','objective':'Implement greeting','acceptance_criteria':['Blank inputs greet world'],'requirement_ids':['R1'],'expected_artifacts':['greeting.py'],'validation_commands':['tests']}],'global_validation':['tests']}),usage_source='estimated')
    return Generation(text=sys.argv[3],usage_source='estimated')
engine.providers._generate=generate
uvicorn.run(create_app(settings,engine),host='127.0.0.1',port=settings.port,access_log=False,log_level='error')
"""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(port), json.dumps(SPEC)],
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
        yield url, (tmp_path / ".router/api-token").read_text(), tmp_path
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


@pytest.mark.parametrize("reload", [False, True])
def test_idea_selected_planner_opens_draft_and_human_review(page, server, reload):  # noqa: F811
    project(page)
    page.get_by_role("button", name="PRDs", exact=True).click()
    page.get_by_role("button", name="Generate PRD", exact=True).click()
    page.get_by_label("Requirement text", exact=True).fill("Make greeting behavior clear")
    page.get_by_label("Generated title (optional)", exact=True).fill("Greeting requirements")
    page.get_by_label("Planner model", exact=True).select_option("selected-local")
    page.get_by_role("button", name="Quote inference", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("eligible planners selected-local")
    page.locator("#detail").get_by_role("button", name="Save", exact=True).click()
    if reload:
        playwright.expect(page.locator("#detail")).to_contain_text("Operation RUNNING")
        page.reload()
        page.locator("#project").select_option(label="Browser project")
        page.get_by_role("button", name="PRDs", exact=True).click()
        page.get_by_role("button", name="Operation · generate · RUNNING", exact=True).click()
    playwright.expect(page.get_by_label("PRD title", exact=True)).to_have_value("Greeting requirements", timeout=10000)
    page.get_by_role("button", name="submit review", exact=True).click()
    page.get_by_role("button", name="approve", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Decompose approved PRD", exact=True)).to_be_visible()
    calls = [json.loads(line) for line in (server[2] / "captured.jsonl").read_text().splitlines()]
    assert calls == [{"model": "selected-local", "role": "planner"}]
    headers = {"Authorization": "Bearer " + server[1]}
    with httpx.Client(base_url=server[0], headers=headers, trust_env=False) as client:
        workspaces = client.get("/accs/v1/workbench/workspaces").json()["items"]
        status = client.get("/accs/v1/workbench/status", params={"workspace_id": workspaces[0]["id"]}).json()
        assert not any(status["counts"].values())


def test_breakdown_review_import_backlog(page, server):  # noqa: F811
    headers = {"Authorization": "Bearer " + server[1]}
    with httpx.Client(base_url=server[0], headers=headers, trust_env=False) as client:
        workspace = client.get("/accs/v1/workbench/workspaces").json()["items"][0]
        repository = client.get("/accs/v1/workbench/repositories").json()["items"][0]
        from local_ai_router.workbench.store import identity

        project = client.post(
            "/accs/v1/workbench/projects",
            json={
                "command_id": identity(),
                "workspace_id": workspace["id"],
                "key": "PJ",
                "name": "Journey",
                "repository_ref": repository["id"],
            },
        ).json()
        client.post(
            "/accs/v1/workbench/prds",
            json={
                "command_id": identity(),
                "workspace_id": workspace["id"],
                "project_id": project["id"],
                "title": "Journey PRD",
                "spec": SPEC,
            },
        )
    page.reload()
    page.locator("#project").select_option(label="Journey")
    page.get_by_role("button", name="PRDs", exact=True).click()
    page.get_by_role("button", name="Journey PRD · DRAFT · revision 1", exact=True).click()
    page.get_by_role("button", name="submit review", exact=True).click()
    page.get_by_role("button", name="approve", exact=True).click()
    page.get_by_role("button", name="Decompose approved PRD", exact=True).click()
    page.get_by_label("Allowed paths, one per line", exact=True).fill("greeting.py")
    page.get_by_label("Planner model", exact=True).select_option("selected-local")
    page.get_by_role("button", name="Quote inference", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("eligible planners selected-local")
    page.locator("#detail").get_by_role("button", name="Save", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("Import preview · PRD revision 3", timeout=10000)
    playwright.expect(page.get_by_label("Import Greeting task", exact=True)).to_be_checked()
    playwright.expect(page.locator("#detail")).to_contain_text("Requirement coverage: R1")
    with page.expect_response(
        lambda response: (
            "/ui/workbench/api/plan-imports/" in response.url
            and "/commit?" in response.url
            and response.request.method == "POST"
        )
    ) as imported:
        page.get_by_role("button", name="Import selected tasks", exact=True).click()
    assert imported.value.status == 200
    with httpx.Client(base_url=server[0], headers=headers, trust_env=False) as client:
        work = client.get(
            "/accs/v1/workbench/work-items", params={"workspace_id": workspace["id"], "project_id": project["id"]}
        ).json()["items"]
        assert len(work) == 1 and work[0]["status"] == "BACKLOG"
