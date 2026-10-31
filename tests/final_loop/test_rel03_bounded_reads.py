"""REL-03 (2026-10-31): bounded reads of long histories through the real routes. The workspace audit is served as keyset pages of the newest readable events
(default 200, at most 1,000; `before_id` = the previous page's `next_before`); the per-reader rules are applied before the page is cut, so a hidden row never
shortens a page and never moves the cursor past a readable one; paging back never repeats or skips an event while new ones are written; `all=1` keeps the
historical full read. A caller's assistant threads page the same way (`limit`, `before`)."""
from __future__ import annotations

import pytest

from tests.final_loop.test_chg04_review import login, prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import ask, ra_env, thread  # noqa: F401  (ra_env is a fixture)


def _page(client, env, **params):
    r = client.get(f"/v2/audit/{env.ws}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_audit_pages_are_bounded_contiguous_and_never_skip_while_events_are_written(qa_env):
    q = qa_env
    admin = q.env.admin_client() if hasattr(q.env, "admin_client") else q.admin
    for i in range(12):                                                                            # 12 audited membership grants = 12 events on top of the fixture's
        login(q, f"rel03-{i}-{q.env.nonce}", ("viewer",))
    full = _page(admin, q.env, all="1")
    assert "has_more" not in full and len(full["events"]) >= 12
    ids = [e["id"] for e in full["events"]]
    assert ids == sorted(ids)                                                                       # the full read keeps its oldest-first order
    first = _page(admin, q.env, limit=5)
    assert first["limit"] == 5 and len(first["events"]) == 5 and first["has_more"] is True and first["next_before"] == first["events"][0]["id"]
    assert [e["id"] for e in first["events"]] == ids[-5:]                                           # the newest five, oldest-first within the page
    login(q, f"rel03-between-{q.env.nonce}", ("viewer",))                                        # a new event never shifts an older page
    second = _page(admin, q.env, limit=5, before_id=first["next_before"])
    assert [e["id"] for e in second["events"]] == ids[-10:-5]
    seen = [e["id"] for e in second["events"]] + [e["id"] for e in first["events"]]
    cursor, pages = second["next_before"], 2
    while cursor is not None:
        page = _page(admin, q.env, limit=5, before_id=cursor)
        seen = [e["id"] for e in page["events"]] + seen
        cursor = page["next_before"]; pages += 1
        assert pages < 200
    assert seen == ids                                                                              # every event once, in order, none repeated or skipped
    default = _page(admin, q.env)
    assert default["limit"] == 200 and default["has_more"] is False and len(default["events"]) == len(ids) + 1
    assert admin.get(f"/v2/audit/{q.env.ws}", params={"limit": 0}).status_code == 400
    assert admin.get(f"/v2/audit/{q.env.ws}", params={"limit": 1001}).status_code == 400
    assert admin.get(f"/v2/audit/{q.env.ws}", params={"before_id": 0}).status_code == 400


def test_hidden_rows_never_shorten_a_page_or_hide_a_readable_older_event(ra_env):
    """A requester's private assistant question is an audited job another requester must not see; with a page size of 1 the other requester still gets
    one readable event per page and reaches the events older than the hidden one."""
    q = ra_env
    cid, approver = prepared(q)
    t = thread(q, approver, cid)
    r = ask(q, approver, t["thread_id"], "Where does this stand?", "rel03-hidden-0001", 0)
    assert r.status_code == 200, r.text
    other = login(q, f"other-rel03-{q.env.nonce}", ("requester",))
    full_other = _page(other, q.env, all="1")["events"]
    full_asker = _page(approver, q.env, all="1")["events"]
    assert len(full_asker) > len(full_other)                                                        # the asker sees the private job's events; the other does not
    seen = []
    cursor = None
    for _ in range(500):
        page = _page(other, q.env, limit=1, before_id=cursor) if cursor else _page(other, q.env, limit=1)
        assert len(page["events"]) == 1 or not page["has_more"]
        seen = [e["id"] for e in page["events"]] + seen
        if not page["has_more"]:
            break
        cursor = page["next_before"]
    assert seen == [e["id"] for e in full_other]                                                    # the paged read equals the full readable read


def test_assistant_threads_are_paged_newest_first(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    tids = [thread(q, approver, cid)["thread_id"] for _ in range(4)]
    base = f"/v2/requests/business_change/{cid}/assistant/threads"
    first = approver.get(base, params={"workspace_id": q.env.ws, "limit": 3}).json()
    assert first["limit"] == 3 and len(first["threads"]) == 3 and first["has_more"] is True and first["next_before"]
    second = approver.get(base, params={"workspace_id": q.env.ws, "limit": 3, "before": first["next_before"]}).json()
    assert len(second["threads"]) == 1 and second["has_more"] is False
    assert {t["thread_id"] for t in first["threads"]} | {t["thread_id"] for t in second["threads"]} == set(tids)
    assert not ({t["thread_id"] for t in first["threads"]} & {t["thread_id"] for t in second["threads"]})
    assert approver.get(base, params={"workspace_id": q.env.ws, "before": "not-a-cursor"}).status_code == 400
    assert approver.get(base, params={"workspace_id": q.env.ws, "limit": 0}).status_code == 400
    whole = approver.get(base, params={"workspace_id": q.env.ws}).json()
    assert whole["limit"] == 50 and len(whole["threads"]) == 4 and whole["has_more"] is False
