"""Readable safe item activity uses bounded pages and existing detail controls."""

from test_workbench_manual import create_item, page, playwright, project, server  # noqa: F401


def test_ticket_activity_visible_and_safe(page):  # noqa: F811
    project(page)
    create_item(page, "<script>bad()</script>")
    page.locator("#content").get_by_role("button", name="<script>bad()</script>", exact=True).click()
    page.locator("#detail summary").filter(has_text="Activity").click()
    playwright.expect(page.locator("#detail")).to_contain_text("work item created")
    assert page.evaluate("typeof bad") == "undefined"


def test_agent_history_readable(page):  # noqa: F811
    page.get_by_role("button", name="Agents", exact=True).click()
    page.get_by_role("button", name="Create agent", exact=True).click()
    page.get_by_label("Agent name", exact=True).fill("History worker")
    page.get_by_role("button", name="Save", exact=True).click()
    page.get_by_role("button", name="History: History worker", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("agent configured · History worker")
    playwright.expect(page.locator("#detail")).to_contain_text("HUMAN")
