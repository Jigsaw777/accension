"""Human import/start/completion gates with actual mock Engine execution."""

import httpx
import pytest
from test_workbench_manual import page, playwright  # noqa: F401
from test_workbench_prd_journey import server  # noqa: F401
from test_workbench_prd_journey import test_breakdown_review_import_backlog as import_flow

from local_ai_router.workbench.store import identity


@pytest.mark.parametrize("width", [1280, 360])
def test_review_start_delivery_and_current_human_completion(page, server, width):  # noqa: F811
    page.set_viewport_size({"width": width, "height": 900})
    import_flow(page, server)
    playwright.expect(page.locator("#detail")).to_contain_text("Review feature execution")
    # Agent administration is deliberately separate from human execution approval.
    with httpx.Client(base_url=server[0], headers={"Authorization": "Bearer " + server[1]}, trust_env=False) as client:
        ws = client.get("/accs/v1/workbench/workspaces").json()["items"][0]
        project = client.get("/accs/v1/workbench/projects", params={"workspace_id": ws["id"]}).json()["items"][0]
        repository = client.get("/accs/v1/workbench/repositories").json()["items"][0]
        response = client.post(
            "/accs/v1/workbench/agents",
            json={
                "command_id": identity(),
                "name": "Delivery worker",
                "allowed_workspaces": [ws["id"]],
                "repository_permissions": [repository["id"]],
            },
        )
        assert response.status_code == 201, response.text
    # Reload the bounded selection screen with a registered eligible agent.
    page.reload()
    page.locator("#project").select_option(label="Journey")
    page.get_by_role("button", name="PRDs", exact=True).click()
    page.get_by_role("button", name="Journey PRD · APPROVED · revision 3", exact=True).click()
    page.get_by_role("button", name="Review imported feature", exact=True).click()
    page.get_by_role("button", name="Preview feature execution", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("Execution approval preview")
    page.get_by_label("Feature maximum USD", exact=True).fill("0.10")
    playwright.expect(page.get_by_role("button", name="Start approved feature", exact=True)).to_be_hidden()
    page.get_by_role("button", name="Preview feature execution", exact=True).click()
    page.get_by_role("button", name="Start approved feature", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("Feature delivery")
    playwright.expect(page.locator("#detail")).to_contain_text("REVIEW · coordination queued", timeout=20000)
    playwright.expect(page.locator("#detail")).not_to_contain_text("Delivery COMPLETED")
    page.locator("#detail").get_by_role("button", name="PJ-1 · Greeting task", exact=True).click()
    playwright.expect(page.get_by_role("button", name="approve_completion", exact=True)).to_be_visible()
    page.get_by_label("Command reason", exact=True).fill("Independent greeting check and changes reviewed")
    page.get_by_role("button", name="approve_completion", exact=True).click()
    with httpx.Client(base_url=server[0], headers={"Authorization": "Bearer " + server[1]}, trust_env=False) as client:
        work = client.get(
            "/accs/v1/workbench/work-items", params={"workspace_id": ws["id"], "project_id": project["id"]}
        ).json()["items"]
        assert work[0]["status"] == "DONE"
