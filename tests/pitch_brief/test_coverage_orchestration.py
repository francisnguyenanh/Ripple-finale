"""Review claim 1: extraction -> relation/finding -> draft. The steps behind a draft are run for the person, once, for the current revision, with a state a person can read;
the draft uses the finding of THIS revision and never reads "no finding" as "no conflict". Real routes/store/worker, STUB provider.
  requirement -> evidence:
  * what exists is reported per kind: none / running / done / failed, and older-revision jobs are counted, not used           -> tests 1, 4
  * ensure creates the missing deterministic jobs once; a second call, or another tab, creates nothing                         -> test 2
  * only a person who may import sources can start it; any reader can read it                                                -> test 3
  * a failed step is shown as failed and is not silently re-created (retry is the existing bounded route)                     -> test 5
  * the draft built afterwards says it had a finding for this revision; without one it says that is not 'no conflict'         -> test 6"""
from __future__ import annotations

import pytest

from infra.worker import findings as findings_mod
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_change_draft import Model, _res, run
from tests.pitch_brief.test_role_model import member


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _get(client, env):
    r = client.get("/v2/change/coverage", params={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def _ensure(client, env, expect=200):
    r = client.post("/v2/change/coverage/ensure", json={"workspace_id": env.ws})
    assert r.status_code == expect, r.text
    return r.json()


def _count(env, kind):
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s", (env.tenant, env.ws, kind))
        return cur.fetchone()[0]


def test_status_says_what_exists_per_kind_for_the_current_revision(env, ch):
    s = _get(env.req_client(), env)
    ex = s["extraction"]
    assert ex["files_in_scope"] >= 1 and ex["files_with_extraction"] >= 1 and ex["candidates"] >= 1 and s["uses_ai"] is False
    assert s["relation"]["state"] == "none" and s["finding"]["state"] == "none" and s["ready"] is False


def test_ensure_creates_the_missing_jobs_once_and_then_nothing(env, ch):
    req = env.req_client()
    s = _ensure(req, env)
    assert s["relation"]["state"] == "running" and s["finding"]["state"] == "running"                    # created, not yet run
    before = (_count(env, "relation"), _count(env, "finding"))
    again = _ensure(req, env)                                                                            # a second click, another tab
    assert (_count(env, "relation"), _count(env, "finding")) == before and again["relation"]["job_id"] == s["relation"]["job_id"]
    drain(env)
    done = _get(req, env)
    assert done["relation"]["state"] == "done" and done["finding"]["state"] == "done" and done["ready"] is True
    assert isinstance(done["finding"]["findings"], int)
    assert _ensure(req, env)["ready"] is True and (_count(env, "relation"), _count(env, "finding")) == before        # still nothing new
    for kind in ("relation", "finding"):                                                                 # deterministic, no model
        assert job_body(ch.admin, env, done[kind]["job_id"])["execution_provider_calls"] in (0, None)


def test_only_a_person_who_may_import_starts_it_but_any_reader_reads_it(env, ch):
    viewer = member(env, "viewer")
    assert _get(viewer, env)["extraction"]["files_in_scope"] >= 1
    assert _ensure(viewer, env, expect=403) is not None
    assert (_count(env, "relation"), _count(env, "finding")) == (0, 0)                                    # nothing was created by the refused call


def test_jobs_of_an_older_revision_are_counted_but_never_used(env, ch):
    req = env.req_client()
    _ensure(req, env)
    drain(env)
    assert _get(req, env)["ready"] is True
    head = env.store.head(env.tenant, env.ws)                                                             # the sources move on (another publish)
    digest = env.store.stage_blob(b"other: 1" + bytes([10]))
    env.store.publish_source(env.tenant, env.ws, "src-other", {"artifact-other": digest}, expected_version=head.version, operation="source.import.commit", idempotency_key="other-cov")
    s = _get(req, env)
    assert s["source_revision_id"] != head.source_revision_id and s["ready"] is False
    assert s["relation"]["state"] == "none" and s["finding"]["state"] == "none"                          # nothing of the old revision stands in for the new one
    assert s["relation"]["older_revisions_ignored"] >= 1 and s["finding"]["older_revisions_ignored"] >= 1


def test_a_failed_step_is_shown_as_failed_and_is_not_silently_recreated(env, ch, monkeypatch):
    def boom(self, ctx):
        from infra.worker.execution_types import NonRetryableExecutionError
        raise NonRetryableExecutionError("finding_boom", "test failure")
    monkeypatch.setattr(findings_mod.FindingExecutor if hasattr(findings_mod, "FindingExecutor") else findings_mod.FindingsExecutor, "prepare", boom)
    req = env.req_client()
    _ensure(req, env)
    drain(env)
    s = _get(req, env)
    assert s["finding"]["state"] == "failed" and "finding_boom" in (s["finding"]["reason"] or "") and s["relation"]["state"] == "done" and s["ready"] is False
    n = _count(env, "finding")
    assert _ensure(req, env)["finding"]["state"] == "failed" and _count(env, "finding") == n             # not re-created behind the person's back


def test_the_draft_says_it_had_a_conflict_check_for_this_revision_or_that_none_exists(env, ch, monkeypatch):
    _jid, body = run(env, ch, monkeypatch, Model())
    assert "NOT the same as 'no conflict'" in _res(body)["coverage"]["statement"]                         # nothing ran before this draft
    _ensure(env.req_client(), env)
    drain(env)
    _jid2, body2 = run(env, ch, monkeypatch, Model())
    cov = _res(body2)["coverage"]
    assert cov["findings_for_this_revision"] == 1 and cov["statement"].startswith("A conflict and gap check (finding) exists for this revision.")


def test_after_an_apply_the_files_that_did_not_change_keep_their_extraction_for_coverage_and_for_the_draft(env, ch, monkeypatch):
    """Found in the real-AI self-check: after the first apply only the edited file had an extraction pinned to the head, so the panel said '1 of 13' and the draft
    saw one file. A file whose bytes are identical at the head keeps its earlier reading (the rule the Sources page and the Explorer already use)."""
    from tests.pitch_brief.chain import APPROVAL, S1, intent
    from infra.worker import change_draft
    head = env.store.head(env.tenant, env.ws)
    before = _get(env.req_client(), env)
    read_before = before["extraction"]["files_with_extraction"]
    assert read_before >= 4
    aid = ch.arts[APPROVAL]
    content = env.store.read_artifact_blob(env.tenant, env.ws, head.source_revision_id, aid)["content"]
    new_hash = env.store.stage_blob(content + b"\n# edited by an applied change\n")
    env.store.publish_source(env.tenant, env.ws, "rev-test-after-apply", {aid: new_hash}, expected_version=head.version, operation="test.apply", idempotency_key="t-after-apply", carry_forward=True)
    by_art: dict = {}
    for o in env.store.source_observations(env.tenant, env.ws, head.source_revision_id):      # what the verification job after a real apply does: the new revision gets its observations
        by_art.setdefault(o["artifact_id"], []).append(o)
    for art_id, obs in by_art.items():
        env.store.record_source_observations(env.tenant, env.ws, "rev-test-after-apply", art_id, [
            {"kind": o["kind"], "locator": o["locator"], "raw_value": o.get("raw_value"), "fields": o.get("fields") or {}, "parser_version": o["parser_version"]} for o in obs])
    after = _get(env.req_client(), env)
    n = read_before
    assert after["extraction"]["files_in_scope"] == before["extraction"]["files_in_scope"] and after["extraction"]["files_with_extraction"] == n - 1     # the edited file has no reading of its new bytes; the others still do
    assert any(x.endswith("approval.yaml") for x in after["extraction"]["files_without_extraction"])
    # the draft, asked for the default way (no explicit job list), is based on those same files
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: Model())
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    iid = intent(env, ch.admin, S1, confirm=True)["intent_id"]
    jid, r = create_job(env.req_client(), env, "change.draft", {"intent_id": iid})
    assert r.status_code == 200, r.text
    drain(env)
    res = _res(job_body(ch.admin, env, jid))
    assert res.get("coverage"), {k: res.get(k) for k in ("status", "reasons", "executor_id", "extraction_jobs_collected", "unsupported")}
    assert res["coverage"]["files_with_current_extraction"] == n - 1 and len(res["extraction_jobs_collected"]) == n - 1
