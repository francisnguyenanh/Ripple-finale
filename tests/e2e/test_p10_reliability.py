"""The reliability rows the earlier suites did not cover.

The rest of the reliability table is already tested elsewhere; see
docs/verification/reliability.md for the map. These close the gaps it named:

  * concurrent decisions on one PROPOSED ChangeSet (approve vs reject) and
    concurrent applies of one APPROVED ChangeSet: real threads, one event loop
  * why one instance cannot race itself: the command routes stay `async`
  * LLM timeout / Gemini transient error -> abstain + a durable Gap; a retry succeeds
  * Cloud Storage transient error on write (no commit) and on read (fails closed)
  * session reset after an infrastructure failure returns to the baseline

No claim of live Postgres, GCS or Vertex: the transports are controlled fakes.
The live CAS race is tests/live_extension_postgres.py.
"""
import inspect
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import main
from infra import judge
from tests.contract.test_extension_persistence import Bucket, Cursor, backend as sql_backend, session as sql_session
from tests.e2e.test_two_instance_session import create, durable  # noqa: F401  (fixture)
from tests.intent_support import model_result

ACTOR = "judge@example.com"
WHEN = "2026-09-20T14:00:00Z"
TEXT = "For all orders lower approval threshold to JPY 500000"


def rpc(client, operation, sid, request_id, **payload):
    return client.post("/rpc", json=dict(operation=operation, session_id=sid, request_id=request_id,
                                         actor=ACTOR, timestamp=WHEN, payload=payload))


def proposed_changeset(client, sid):
    """A ChangeSet in PROPOSED: nobody has decided it yet."""
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt", return_value=model_result()):
        cand = rpc(client, "intent.parse", sid, "p", text=TEXT).json()["data"]
    intent = rpc(client, "intent.create", sid, "c", candidate_id=cand["candidate_id"],
                 content_hash=cand["content_hash"]).json()["data"]
    shadow = rpc(client, "whatif.simulate", sid, "w", target="approval_threshold",
                 proposed_value=500000).json()["data"]
    cs = rpc(client, "changeset.prepare", sid, "r", intent_id=intent["intent_id"],
             shadow_id=shadow["shadow_id"]).json()["data"]
    assert cs["state"] == "PROPOSED", cs
    return cs["changeset_id"]


def approved_changeset(client, sid):
    cid = proposed_changeset(client, sid)
    decided = rpc(client, "changeset.decide", sid, "d", changeset_id=cid,
                  decision="approve", reason="evidence is clear")
    assert decided.status_code == 200, decided.text
    return cid


def _race(client, sid, calls):
    """Fire every (request_id, operation, payload) at the same instant."""
    start = threading.Barrier(len(calls))
    replies = {}

    def fire(key, operation, payload):
        start.wait()
        replies[key] = rpc(client, operation, sid, key, **payload)

    threads = [threading.Thread(target=fire, args=call) for call in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert len(replies) == len(calls)
    return replies


# --- concurrent approval ------------------------------------------------------


def test_eight_concurrent_approvals_of_one_proposed_changeset_record_one(durable):  # noqa: F811
    """Concurrent approval -> only one wins. Eight reviewers approve the SAME
    undecided ChangeSet at the same instant, each with its own request id."""
    _, registry, backend = durable
    with TestClient(main.app) as client:   # one portal == one event loop, like uvicorn
        sid = create(client)
        cid = proposed_changeset(client, sid)
        replies = _race(client, sid, [
            (f"approve-{n}", "changeset.decide",
             dict(changeset_id=cid, decision="approve", reason=f"reviewer {n}"))
            for n in range(8)])
    ok = [r for r in replies.values() if r.status_code == 200 and r.json()["ok"]]
    assert len(ok) == 1, {k: (r.status_code, r.json().get("code")) for k, r in replies.items()}
    assert ok[0].json()["data"]["state"] == "APPROVED"
    assert all(r.json()["code"] == "invalid_transition" for r in replies.values() if r not in ok)


def test_concurrent_decisions_on_one_proposed_changeset_never_approve_twice(durable):  # noqa: F811
    """Eight reviewers decide the SAME undecided ChangeSet at the same instant,
    four approving and four rejecting, each with its own request id.

    By design (docs/domain/state_machines.md) REJECTED is reachable from any
    state before APPLIED, so an approval may be revoked by a later
    rejection; REJECTED is terminal. The invariants are therefore: at most
    one approval and at most one rejection are recorded, every other command
    is refused, and the apply follows the FINAL decision."""
    _, registry, backend = durable
    with TestClient(main.app) as client:   # one portal == one event loop, like uvicorn
        sid = create(client)
        cid = proposed_changeset(client, sid)
        calls = [(f"decide-{n}", "changeset.decide",
                  dict(changeset_id=cid, decision="approve" if n % 2 == 0 else "reject",
                       reason=f"reviewer {n}"))
                 for n in range(8)]
        replies = _race(client, sid, calls)
        won = {k: r.json()["data"]["state"] for k, r in replies.items()
               if r.status_code == 200 and r.json()["ok"]}
        refused = [r for k, r in replies.items() if k not in won]
        applied = rpc(client, "changeset.apply", sid, "after", changeset_id=cid)
    states = sorted(won.values())
    assert states in (["APPROVED"], ["REJECTED"], ["APPROVED", "REJECTED"]), won
    assert all(r.status_code == 409 and r.json()["code"] == "invalid_transition" for r in refused)
    final = "REJECTED" if "REJECTED" in states else "APPROVED"
    assert (applied.status_code == 200) == (final == "APPROVED"), (final, applied.text)


def test_concurrent_applies_of_one_approved_changeset_apply_once(durable):  # noqa: F811
    _, registry, backend = durable
    with TestClient(main.app) as client:
        sid = create(client)
        cid = approved_changeset(client, sid)
        replies = _race(client, sid, [(f"apply-{n}", "changeset.apply", dict(changeset_id=cid))
                                      for n in range(8)])
    ok = [r for r in replies.values() if r.status_code == 200 and r.json()["ok"]]
    assert len(ok) == 1, [r.json().get("code") for r in replies.values()]
    assert ok[0].json()["data"]["snapshot_id"] == backend.rows[sid].overlay.head.snapshot_id


def test_the_command_routes_run_on_the_event_loop_so_one_instance_serialises_them():
    """A sync `def` route would run in FastAPI's thread pool, and two commands
    would then mutate the in-process session cache at once. The CAS only
    protects the durable row, not that cache. Keep them `async`."""
    assert inspect.iscoroutinefunction(judge.rpc)
    assert inspect.iscoroutinefunction(judge.login)
    assert inspect.iscoroutinefunction(main.rpc_endpoint)
    assert not inspect.iscoroutinefunction(main.handle_rpc)  # blocks the loop: that is the point


def test_the_service_runs_exactly_one_uvicorn_worker(monkeypatch):
    """The same guarantee needs ONE event loop per instance. Without an
    explicit --workers, uvicorn takes $WEB_CONCURRENCY, so the Dockerfile
    states `--workers 1`, and an explicit value wins over the variable.
    Measured 2026-09-25: with a loop per thread, 3 of 8 concurrent applies
    returned 200 against the in-process registry (reliability.md §4.2)."""
    import json
    from pathlib import Path

    import uvicorn

    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(encoding="utf-8")
    cmd = [line for line in dockerfile.splitlines() if line.startswith("CMD")]
    assert len(cmd) == 1
    argv = json.loads(cmd[0][len("CMD"):])
    assert argv[:2] == ["uvicorn", "infra.judge:app"]
    assert argv.count("--workers") == 1 and argv[argv.index("--workers") + 1] == "1"
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert uvicorn.Config("infra.judge:app", workers=1).workers == 1
    assert uvicorn.Config("infra.judge:app").workers == 4   # what an unpinned CMD would get


# --- Gemini transient error ------------------------------------------------------


def test_a_transient_model_error_abstains_and_a_retry_succeeds(durable):  # noqa: F811
    client, registry, backend = durable
    sid = create(client)
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt",
               side_effect=RuntimeError("503 UNAVAILABLE")):
        failed = rpc(client, "intent.parse", sid, "p1", text=TEXT)
    assert failed.status_code == 200 and failed.json()["data"]["abstained"] is True
    assert not backend.rows[sid].listing("candidate")
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt", return_value=model_result()):
        retried = rpc(client, "intent.parse", sid, "p2", text=TEXT)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["validation"]["ok"]


@pytest.mark.parametrize("error", [TimeoutError("deadline exceeded"), RuntimeError("503 UNAVAILABLE")])
def test_an_llm_timeout_abstains_with_a_gap_read_back_from_durable_state(durable, error):  # noqa: F811
    """Plan P10-T02: LLM timeout -> abstain + Gap, not a guess. The Gap is
    read back from what the backend COMMITTED, after the instance's cache is
    dropped (a restart), and the abstention cannot be confirmed as an intent."""
    from app.api.codec import decode_record
    client, registry, backend = durable
    sid = create(client)
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt", side_effect=error):
        reply = rpc(client, "intent.parse", sid, "timeout", text=TEXT)
    assert reply.status_code == 200, reply.text
    data = reply.json()["data"]
    assert data["abstained"] is True and data["target"] == "UNKNOWN"
    assert data["candidate_id"] is None and data["validation"]["ok"] is False
    registry._sessions.clear()
    stored = backend.rows[sid]
    assert stored.listing("candidate") == ()
    (gap_id,) = stored.listing("gap")
    gap = decode_record(stored.fetch("gap", gap_id), expected_kind="gap")
    assert gap.gap_id == data["gap"]["gap_id"] == gap_id
    assert type(error).__name__ in gap.reason and "nothing was guessed" in gap.reason
    refused = rpc(client, "intent.create", sid, "confirm", candidate_id=data["candidate_id"],
                  content_hash=data["content_hash"])
    assert refused.status_code >= 400 and not refused.json()["ok"]
    assert backend.rows[sid].listing("intent") == ()


def test_each_abstention_is_its_own_gap(durable):  # noqa: F811
    client, registry, backend = durable
    sid = create(client)
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt", side_effect=TimeoutError()):
        first = rpc(client, "intent.parse", sid, "t1", text=TEXT).json()["data"]["gap"]["gap_id"]
        second = rpc(client, "intent.parse", sid, "t2", text=TEXT).json()["data"]["gap"]["gap_id"]
    assert first != second
    assert set(backend.rows[sid].listing("gap")) == {first, second}


def test_malformed_model_output_is_still_blocked_by_the_schema_gate(durable):  # noqa: F811
    """The other half of the pair: malformed output is not an abstention."""
    client, registry, backend = durable
    sid = create(client)
    with patch("infra.adapters.cloud_adapters.generate_intent_from_prompt", return_value={"parsed": {}}):
        reply = rpc(client, "intent.parse", sid, "bad", text=TEXT)
    assert reply.status_code == 502 and reply.json()["code"] == "llm_schema_invalid"


# --- Cloud Storage transient error ------------------------------------------------


class FlakyBucket(Bucket):
    def __init__(self, fail_uploads=0, fail_downloads=0):
        super().__init__()
        self.fail_uploads, self.fail_downloads = fail_uploads, fail_downloads

    def blob(self, name):
        inner, bucket = super().blob(name), self

        class Blob:
            def upload_from_string(self, raw):
                if bucket.fail_uploads:
                    bucket.fail_uploads -= 1
                    raise ConnectionError("GCS 503")
                inner.upload_from_string(raw)

            def download_as_bytes(self):
                if bucket.fail_downloads:
                    bucket.fail_downloads -= 1
                    raise ConnectionError("GCS 503")
                return inner.download_as_bytes()
        return Blob()


def _session_with_a_blob():
    s = sql_session()
    s.overlay.write("workflow/test.yaml", b"value: 2")
    return s


def test_a_gcs_upload_failure_commits_nothing_and_the_retry_commits():
    """The blob upload runs inside the SQL transaction, before commit."""
    cur = Cursor(version=0)
    store, connection = sql_backend(cur)
    store.blob_bucket = FlakyBucket(fail_uploads=1)
    with pytest.raises(ConnectionError):
        store.persist_session(_session_with_a_blob(), expected_version=0)
    assert not connection.committed
    assert "overlay" not in cur.rows
    assert store.persist_session(_session_with_a_blob(), expected_version=0) == 1
    assert connection.committed and store.blob_bucket.data


def _external_edit(client, sid, key, value=300000):
    return rpc(client, "scenario.external_edit", sid, key,
               artifact="data/approval_matrix.xlsx", locator="Thresholds!C2", value=value)


def _flaky_fetch(backend, failures):
    real, left = backend.fetch_session, {"n": failures}

    def fetch(session_id, baseline):
        if left["n"]:
            left["n"] -= 1
            raise ConnectionError("GCS 503 while restoring session blobs")
        return real(session_id, baseline)
    backend.fetch_session = fetch


def test_a_single_gcs_read_failure_is_absorbed_by_the_second_read(durable):  # noqa: F811
    """handle_rpc reads the session once for the idempotency check (errors
    swallowed) and api.call reads it again: one transient failure costs a
    retry, and the command is still written exactly once."""
    client, registry, backend = durable
    sid = create(client)
    writes = backend.writes
    _flaky_fetch(backend, failures=1)
    registry._sessions.clear()
    assert _external_edit(client, sid, "e1").status_code == 200
    assert backend.writes == writes + 1
    assert len(backend.rows[sid].work["handled_event"]) == 1


def test_a_persistent_gcs_read_failure_fails_closed_and_writes_nothing(durable):  # noqa: F811
    client, registry, backend = durable
    sid = create(client)
    writes = backend.writes
    _flaky_fetch(backend, failures=10**6)
    registry._sessions.clear()
    failed = _external_edit(client, sid, "e1")
    assert failed.status_code >= 400 and not failed.json()["ok"], failed.text
    assert backend.writes == writes
    backend.fetch_session = type(backend).fetch_session.__get__(backend)
    retried = _external_edit(client, sid, "e1")
    assert retried.status_code == 200, retried.text
    assert len(backend.rows[sid].work["handled_event"]) == 1


# --- reset after an infrastructure failure ------------------------------------------


def test_reset_after_a_failed_commit_returns_to_the_baseline(durable):  # noqa: F811
    client, registry, backend = durable
    sid = create(client)
    fresh = client.post("/rpc", json={"operation": "state.observe", "session_id": sid}).json()["data"]
    assert rpc(client, "scenario.external_edit", sid, "e1", artifact="data/approval_matrix.xlsx",
               locator="Thresholds!C2", value=300000).status_code == 200
    backend.fail = True
    assert rpc(client, "scenario.external_edit", sid, "e2", artifact="data/approval_matrix.xlsx",
               locator="Thresholds!C2", value=200000).status_code == 503
    backend.fail = False
    assert rpc(client, "session.reset", sid, "reset").status_code == 200
    registry._sessions.clear()   # read back what is DURABLE, not the cache
    after = client.post("/rpc", json={"operation": "state.observe", "session_id": sid}).json()["data"]
    assert after["changed_in_session"] == []
    assert after["snapshot_id"] == fresh["snapshot_id"]
    assert after["artifacts"] == fresh["artifacts"]
