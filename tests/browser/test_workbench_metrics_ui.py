"""Empty performance is honest and accessible without a chart dependency."""

from test_workbench_agents_ui import page, playwright, server  # noqa: F401


def test_empty_agent_metrics_labels_and_period(page):  # noqa: F811
    page.get_by_role("button", name="Agents", exact=True).click()
    page.get_by_role("button", name="Create agent", exact=True).click()
    page.get_by_label("Agent name", exact=True).fill("Metric worker")
    page.locator("#detail").get_by_role("button", name="Save", exact=True).click()
    page.locator("#content").get_by_role("button", name="Metric worker", exact=True).click()
    page.get_by_role("button", name="Performance", exact=True).click()
    playwright.expect(page.locator("#detail")).to_contain_text("No completed work in this period")
    playwright.expect(page.locator("#detail")).to_contain_text("Success: Unknown")
    playwright.expect(page.get_by_role("table")).to_have_count(1)
    playwright.expect(page.get_by_role("img", name="Execution duration in ms over 30 days")).to_have_count(1)
    page.get_by_label("Period", exact=True).select_option("7")
    playwright.expect(page.get_by_role("img", name="Execution duration in ms over 7 days")).to_have_count(1)
