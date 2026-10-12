"""Structured agent creation, policy choices, profile and revision-safe changes."""

from test_workbench_manual import page, playwright, server  # noqa: F401


def test_create_edit_disable_agent_without_json(page):  # noqa: F811
    page.get_by_role("button", name="Agents", exact=True).click()
    page.get_by_role("button", name="Create agent", exact=True).click()
    page.get_by_label("Agent name", exact=True).fill("Form worker")
    page.get_by_label("Worker capacity", exact=True).fill("2")
    page.locator("#detail").get_by_role("button", name="Save", exact=True).click()
    playwright.expect(page.locator("#notice")).to_contain_text("Saved")
    page.locator("#content").get_by_role("button", name="Form worker", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("Load 0/2")
    page.get_by_role("button", name="Edit agent", exact=True).click()
    page.get_by_label("Agent name", exact=True).fill("Renamed worker")
    page.locator("#detail").get_by_role("button", name="Save", exact=True).click()
    page.locator("#content").get_by_role("button", name="Renamed worker", exact=True).click()
    page.get_by_role("button", name="Disable agent", exact=True).click()
    playwright.expect(page.locator("#content")).to_contain_text("Worker DISABLED")
    page.locator("#content").get_by_role("button", name="History: Renamed worker", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("Form worker")
    playwright.expect(page.locator("#detail")).to_contain_text("Renamed worker")
