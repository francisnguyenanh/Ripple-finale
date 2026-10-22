"""Import screen in the browser (2026-10-21, found by the journey harness: the screen never sent a source role, so every file was extracted as "unknown"): each admitted
file offers "Kind of source", pre-filled from the file type; the person changes one; the commit stores the chosen kinds as confirmed by a person, and a later refresh
keeps them. Real server and routes on the throwaway PostgreSQL; no model."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.ai10ui.contract import tid  # noqa: E402
from tests.ai10ui.harness import ServerThread  # noqa: E402
from tests.final_loop.test_kno04_browser import sign_in  # noqa: E402

WAIT = 30000
FILES = [("policy.md", b"# Policy\n\nPurchases of JPY 1,000,000 or more need manager approval.\n"),
         ("routing.py", b"def route(amount):\n    return 'MANAGER' if amount >= 1000000 else 'AUTO'\n"),
         ("orders.csv", b"id,amount\n1,1200000\n")]


def test_the_person_sets_the_kind_of_each_file_at_import_and_the_server_keeps_it_as_confirmed(env, pw_browser):
    server = ServerThread().start()
    try:
        page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")          # the React build (/app-next/)
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/sources")
        page.get_by_label("Upload source files").set_input_files([{"name": n, "mimeType": "application/octet-stream", "buffer": b} for n, b in FILES])
        page.get_by_role("button", name="Prepare import").click()
        expect(tid(page, "import-item")).to_have_count(3)
        expect(page.get_by_text("Check the kind of each file")).to_be_visible()
        policy = page.get_by_label("Kind of source: policy.md")
        code = page.get_by_label("Kind of source: routing.py")
        records = page.get_by_label("Kind of source: orders.csv")
        expect(policy).to_have_value("normative")
        expect(code).to_have_value("implementation")
        expect(records).to_have_value("observational")
        records.focus()                                                                         # a native select: reachable and changed by keyboard
        records.select_option("unknown")
        expect(records).to_have_value("unknown")
        expect(tid(page, "import-roles-note")).to_contain_text("not sent")                      # not ticked: nothing would be recorded as confirmed
        tid(page, "import-roles-ack").check()
        expect(tid(page, "import-roles-note")).to_contain_text("confirmed by you")
        page.get_by_role("button", name="Commit import").click()
        expect(tid(page, "import-item")).to_have_count(0, timeout=WAIT)                          # committed: the manifest closes (a refusal keeps it and shows the error)
        head = env.store.head(env.tenant, env.ws)
        arts = {a["logical_path"].split("/")[-1]: a for a in env.store.source_revision_view(env.tenant, env.ws, head.source_revision_id)["artifacts"]}
        assert {k: a["source_role"] for k, a in arts.items()} == {"policy.md": "normative", "routing.py": "implementation", "orders.csv": "unknown"}
        with env.store._tx() as cur:
            cur.execute("SELECT DISTINCT source_role_origin FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s",
                        (env.tenant, env.ws, head.source_revision_id))
            assert [r[0] for r in cur.fetchall()] == ["human_confirmed"]
        assert not page._errors, page._errors
    finally:
        server.stop()


def test_without_the_tick_no_kind_is_sent_and_nothing_is_recorded_as_confirmed_by_a_person(env, pw_browser):
    server = ServerThread().start()
    try:
        page = sign_in(pw_browser, server, env.ws, env.admin, f"cred-a-{env.nonce}")
        page.goto(f"{server.url}/app-next/#/ws/{env.ws}/sources")
        page.get_by_label("Upload source files").set_input_files([{"name": n, "mimeType": "application/octet-stream", "buffer": b} for n, b in FILES[:2]])
        page.get_by_role("button", name="Prepare import").click()
        expect(page.get_by_label("Kind of source: policy.md")).to_have_value("normative")       # the suggestion is visible
        page.get_by_role("button", name="Commit import").click()
        expect(tid(page, "import-item")).to_have_count(0, timeout=WAIT)
        head = env.store.head(env.tenant, env.ws)
        with env.store._tx() as cur:
            cur.execute("SELECT source_role, source_role_origin FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s",
                        (env.tenant, env.ws, head.source_revision_id))
            rows = cur.fetchall()
        assert rows and all(origin != "human_confirmed" and role == "unknown" for role, origin in rows), rows
        assert not page._errors, page._errors
    finally:
        server.stop()
