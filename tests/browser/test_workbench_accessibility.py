"""Keyboard decision forms, retained drafts and graph alternatives across layouts."""

import pytest
from test_workbench_manual import page, playwright, project, server  # noqa: F401


@pytest.mark.parametrize("width,theme", [(1280, "light"), (360, "dark")])
def test_structured_prd_keyboard_draft_and_mobile(page, server, width, theme):  # noqa: F811
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("theme => localStorage.setItem('accension-theme', theme)", theme)
    page.reload()
    project(page)
    page.get_by_role("button", name="PRDs", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="Create PRD", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_label("PRD title", exact=True).fill("Keyboard draft")
    page.get_by_label("problem statement", exact=True).fill("Keep feature decisions understandable")
    page.get_by_label("objectives (one per line)", exact=True).fill("Accessible review")
    page.get_by_label("scope (one per line)", exact=True).fill("One feature")
    page.get_by_role("button", name="Add functional requirements", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_label("functional requirements text 1", exact=True).fill("Keep the user's draft")
    page.get_by_role("button", name="Add acceptance criteria", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_label("acceptance criteria text 1", exact=True).fill("Keyboard can approve a complete PRD")
    assert page.get_by_label("functional requirements id 1", exact=True).input_value() == "FR-1"
    page.get_by_role("button", name="Board", exact=True).click()
    page.get_by_role("button", name="PRDs", exact=True).click()
    page.get_by_role("button", name="Create PRD", exact=True).click()
    playwright.expect(page.get_by_label("functional requirements text 1", exact=True)).to_have_value(
        "Keep the user's draft"
    )
    page.locator("#detail").get_by_role("button", name="Save", exact=True).focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#notice")).to_contain_text("Saved")
    page.get_by_role("button", name="Keyboard draft · DRAFT · revision 1", exact=True).click()
    page.get_by_role("button", name="submit review", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="approve", exact=True).focus()
    page.keyboard.press("Enter")
    playwright.expect(page.get_by_role("button", name="Decompose approved PRD", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate(
        "({width:document.documentElement.scrollWidth,viewport:innerWidth,nodes:[...document.querySelectorAll('*')].filter(n=>n.scrollWidth>n.clientWidth||n.getBoundingClientRect().right>innerWidth).map(n=>[n.tagName,n.className,n.textContent.slice(0,30),n.scrollWidth,n.clientWidth,n.getBoundingClientRect().right])})"
    )
    assert page.locator("#detail textarea").evaluate_all("nodes => nodes.every(n => n.labels.length > 0)")


def test_performance_graphs_have_units_and_table_at_360px(page):  # noqa: F811
    page.set_viewport_size({"width": 360, "height": 900})
    page.get_by_role("button", name="Agents", exact=True).click()
    page.get_by_role("button", name="Create agent", exact=True).click()
    page.get_by_label("Agent name", exact=True).fill("Accessible worker")
    page.locator("#detail").get_by_role("button", name="Save", exact=True).click()
    page.locator("#content").get_by_role("button", name="Accessible worker", exact=True).click()
    page.get_by_role("button", name="Performance", exact=True).focus()
    page.keyboard.press("Enter")
    playwright.expect(page.get_by_role("table")).to_have_count(1)
    playwright.expect(page.get_by_role("img", name="Execution duration in ms over 30 days")).to_have_count(1)
    assert page.get_by_role("columnheader", name="Duration ms", exact=True).count() == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
