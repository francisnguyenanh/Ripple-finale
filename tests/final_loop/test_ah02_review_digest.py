"""AH-02: the review digest gives the same effective verdicts as the full history while reading a bounded page: an old unresolved `request_changes` behind
thousands of newer comments stays open; a later `ready` from the same reviewer clears it; equal timestamps are ordered by id; the newest page is stable.
Throwaway PostgreSQL; rows are inserted directly into the audit table the review-note route writes (same event and payload shape)."""
from __future__ import annotations

import datetime as dt
import json
import uuid

import psycopg2
import pytest

from infra import v2_work_routes as w
from tests.pitch_brief.conftest import DSN


@pytest.fixture
def store():
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("needs the disposable PostgreSQL (PITCH_BRIEF_TEST_DSN)")
    from infra.adapters.v2_postgres import PostgresV2Store
    s = PostgresV2Store(DSN, None)
    s.migrate()
    return s


def _insert(store, tenant, ws, pid, rows):
    with store._tx() as cur:
        cur.executemany("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload, created_at) VALUES (%s,%s,%s,%s,%s,%s)""",
                        [(tenant, ws, w.NOTE_EVENT, actor, json.dumps({"proposal_job_id": pid, "verdict": verdict, "text": text}), at) for actor, verdict, text, at in rows])


def test_an_old_open_request_behind_thousands_of_comments_stays_open_and_ready_clears_it(store):
    tenant, ws, pid = f"t-{uuid.uuid4().hex[:8]}", f"ws-{uuid.uuid4().hex[:8]}", f"prop-{uuid.uuid4().hex[:8]}"
    store.create_workspace(tenant, f"p-{ws}", ws)
    t0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    rows = [("rev-old", "request_changes", "Please state the finance scope.", t0),
            ("rev-b", "request_changes", "Wrong threshold.", t0 + dt.timedelta(seconds=1)),
            ("rev-b", "ready", "Fixed, thanks.", t0 + dt.timedelta(seconds=2))]
    rows += [(f"rev-c{i % 7}", "comment", f"comment {i}", t0 + dt.timedelta(minutes=10 + i // 3)) for i in range(2000)]   # equal timestamps in threes
    _insert(store, tenant, ws, pid, rows)
    full = w.review_notes(store, tenant, ws, pid)
    digest = w.review_digest(store, tenant, ws, pid, newest=80)
    assert digest["total"] == len(full) == 2003
    assert w.open_change_requests(digest["latest"]) == w.open_change_requests(full) == ["rev-old"]          # the old request is still the open one
    assert len(digest["notes"]) == 80
    assert [n["note_id"] for n in digest["notes"]] == [n["note_id"] for n in sorted(full, key=lambda n: (n["created_at"], n["note_id"]))[-80:]]   # stable newest page
    assert {r["reviewer"] for r in digest["latest"]} == {n["reviewer"] for n in full}
    with store._tx() as cur:                                                                                  # AH-02.4: the page query's plan on this data
        cur.execute("""EXPLAIN (ANALYZE, BUFFERS) SELECT id, actor_id, payload, created_at FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type=%s
                       AND payload->>'proposal_job_id'=%s ORDER BY created_at DESC, id DESC LIMIT 80""", (tenant, ws, w.NOTE_EVENT, pid))
        print("PLAN\n" + "\n".join(r[0] for r in cur.fetchall()))
    _insert(store, tenant, ws, pid, [("rev-old", "ready", "Scope is fine now.", t0 + dt.timedelta(days=5))])
    assert w.open_change_requests(w.review_digest(store, tenant, ws, pid, newest=0)["latest"]) == []


def test_paging_back_with_the_cursor_neither_repeats_nor_skips_while_notes_are_written(store):
    tenant, ws, pid = f"t-{uuid.uuid4().hex[:8]}", f"ws-{uuid.uuid4().hex[:8]}", f"prop-{uuid.uuid4().hex[:8]}"
    store.create_workspace(tenant, f"p-{ws}", ws)
    t0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    _insert(store, tenant, ws, pid, [("rev-old", "request_changes", "Scope?", t0)] +
            [(f"rev-c{i % 5}", "comment", f"c {i}", t0 + dt.timedelta(minutes=1 + i // 4)) for i in range(250)])   # equal timestamps in fours
    full_ids = [n["note_id"] for n in sorted(w.review_notes(store, tenant, ws, pid), key=lambda n: (n["created_at"], n["note_id"]))]
    seen, before, pages = [], None, 0
    while True:
        d = w.review_digest(store, tenant, ws, pid, newest=40, before=w._cursor(before))
        assert w.open_change_requests(d["latest"]) == ["rev-old"]                    # every page carries the effective blocker, whatever it shows
        seen = [n["note_id"] for n in d["notes"]] + seen
        pages += 1
        if pages == 2:                                                              # a note written while the reader pages back: newer than every cursor
            _insert(store, tenant, ws, pid, [("rev-new", "comment", "late", t0 + dt.timedelta(days=9))])
        if not d["has_more"]:
            break
        before = d["next_before"]
    assert seen == full_ids and len(set(seen)) == len(seen) and pages == 7          # 251 notes in pages of 40: none repeated, none skipped
    with pytest.raises(ValueError):
        w._cursor("not-a-cursor")
