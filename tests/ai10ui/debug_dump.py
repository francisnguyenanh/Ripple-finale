"""Optional pytest plugin (`-p tests.ai10ui.debug_dump`): when a browser test fails, print every open page's URL and
visible text so the failing screen can be read from the run log. Diagnostics only; it changes no result."""
from __future__ import annotations

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when != "call" or not rep.failed:
        return
    world = item.funcargs.get("world")
    pages = []
    for ctx in getattr(world, "_contexts", []) or []:
        pages.extend(getattr(ctx, "pages", []))
    for k, v in item.funcargs.items():
        if hasattr(v, "inner_text") and hasattr(v, "url"):
            pages.append(v)
    browser = item.funcargs.get("pw_browser")
    for ctx in getattr(browser, "contexts", []) or []:
        pages.extend(p for p in getattr(ctx, "pages", []) if p not in pages)
    out = []
    for i, p in enumerate(pages):
        try:
            recent = [f"{r['status']} {r['url'].split('/', 3)[-1][:110]}" for r in getattr(getattr(p, "_ai10", None), "responses", [])[-14:]]
            out.append(f"--- page {i} {p.url}\nrecent responses: {recent}\n{p.locator('body').inner_text(timeout=2000)[-1800:]}")
        except Exception as exc:  # page closed
            out.append(f"--- page {i}: {type(exc).__name__}")
    rep.sections.append(("browser pages at failure", "\n".join(out)))
