"""Which findings are in force now (the shared read model behind the Questions list, the knowledge overview and the completion gate). Real routes/store/worker, STUB provider.
  requirement -> evidence:
  * running the checks again does not add issues: the same problem found by two runs is ONE issue; the queue lists it once        -> test 1
  * a finding about a file that did not change stays in force; one about a replaced reading is history until the new reading
    is checked ("not re-checked yet" is said, not dropped)                                                                       -> test 2
  * a serious finding on the place the change edits blocks; any other finding is "needs review" (never called unrelated);
    a human decision resolves it; only a person with the right can decide                                                        -> test 3
  * a reading nobody has checked is reported, and when it is on a touched place it blocks as "not re-checked"                      -> test 4"""
from __future__ import annotations

import pytest

from infra import v2_current_findings as CF
from infra.worker import extraction
from tests.pitch_brief.chain import APPROVAL, FINGUIDE, StubClient, build
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal

Y = "routing_rules.standard_manager_threshold_jpy"
FG_QUOTE = "all purchasing commitments of **2,000,000 JPY or higher** must be reviewed and authorized by the Finance Department prior to purchase order commitment."


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _cand(subject, predicate, value, quote):
    return {"subject": subject, "predicate": predicate, "value": value, "unit": "JPY", "operator": ">=", "modality": "must", "scope": {}, "exceptions": [], "outcome": None,
            "quotes": [quote], "rationale": "no scope stated"}


def _extract_unscoped(env, ch, monkeypatch, path, marker, cand):
    """One reading of `path` whose scope is not stated (the extraction cannot tell where it applies): its finding is a HIGH `missing_scope` gap."""
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: StubClient([(marker, [cand])]))
    jid, r = create_job(env.req_client(), env, "extraction", {"artifact_id": ch.arts[path], "source_role": "unknown"})
    assert r.status_code == 200, r.text
    assert ch.admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
    drain(env)
    res = job_body(ch.admin, env, jid)["execution_result"]
    assert res["candidates"] and res["candidates"][0]["applicability"] == "UNKNOWN", res["candidates"]
    return [c["candidate_id"] for c in res["candidates"]]


def _finding_run(env, ch, cand_ids):
    jid, r = create_job(env.req_client(), env, "finding", {"candidate_ids": list(cand_ids)})
    assert r.status_code == 200, r.text
    assert ch.admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
    drain(env)
    return job_body(ch.admin, env, jid)["execution_result"]["findings"]


def _model(env):
    with env.store._tx() as cur:
        return CF.build(cur, env.tenant, env.ws)


def _queue(ch, env):
    r = ch.admin.get("/v2/hitl/queue", params={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def test_running_the_checks_again_does_not_add_issues(env, ch, monkeypatch):
    ids = _extract_unscoped(env, ch, monkeypatch, APPROVAL, Y, _cand("Manager approval", "unscoped_limit", 1000000, f"{Y}: 1000000"))
    first = _finding_run(env, ch, ids)
    assert [f["kind"] for f in first] == ["GAP"] and first[0]["severity"] == "HIGH"
    before = _model(env).counts
    again = _finding_run(env, ch, ids)                                  # the same checks, run again: new finding ids, same problem
    assert again and again[0]["finding_id"] != first[0]["finding_id"]
    m = _model(env)
    assert m.counts["current_issues"] == before["current_issues"] == 1 and m.counts["current_severe_open"] == 1
    assert m.counts["repeated_runs_collapsed"] == 1 and m.current[0]["runs"] == 2
    assert m.current[0]["finding_id"] == first[0]["finding_id"]          # the ORIGINAL id names the issue (decisions point at it)
    q = _queue(ch, env)
    mine = [i for i in q["items"] if i["question_id"] == "question-" + first[0]["finding_id"]]
    assert len(mine) == 1 and q["hidden"]["repeat"] == 1                # listed once; the repeat is counted as hidden, not silently dropped
    ov = ch.admin.get("/v2/knowledge/overview", params={"workspace_id": env.ws}).json()
    assert [f["finding_id"] for f in ov["findings"]].count(first[0]["finding_id"]) == 1 and len(ov["findings"]) == 1


def test_a_changed_file_makes_its_findings_history_and_an_unchanged_file_keeps_its_own(env, ch, monkeypatch):
    approval_ids = _extract_unscoped(env, ch, monkeypatch, APPROVAL, Y, _cand("Manager approval", "unscoped_limit", 1000000, f"{Y}: 1000000"))
    guide_ids = _extract_unscoped(env, ch, monkeypatch, FINGUIDE, "Finance Desk Guide", _cand("Finance review", "unscoped_guide", 2000000, FG_QUOTE))
    _finding_run(env, ch, approval_ids + guide_ids)
    assert _model(env).counts["current_issues"] == 2
    # the approval file is edited (a new revision) and its observations are written for the new revision, as the verification job after an apply does
    head = env.store.head(env.tenant, env.ws)
    aid = ch.arts[APPROVAL]
    content = env.store.read_artifact_blob(env.tenant, env.ws, head.source_revision_id, aid)["content"]
    env.store.publish_source(env.tenant, env.ws, "rev-edited", {aid: env.store.stage_blob(content + b"\n# edited\n")}, expected_version=head.version, operation="test.apply",
                             idempotency_key="t-edit", carry_forward=True)
    by_art: dict = {}
    for o in env.store.source_observations(env.tenant, env.ws, head.source_revision_id):
        by_art.setdefault(o["artifact_id"], []).append(o)
    for art_id, obs in by_art.items():
        env.store.record_source_observations(env.tenant, env.ws, "rev-edited", art_id, [
            {"kind": o["kind"], "locator": o["locator"], "raw_value": o.get("raw_value"), "fields": o.get("fields") or {}, "parser_version": o["parser_version"]} for o in obs])
    m = _model(env)
    assert m.counts["current_issues"] == 2 and m.historical == []                                                  # the file changed but nobody has read it again: its reading is still the latest known, its finding stays in force
    # the changed file is read again: the old reading is REPLACED, its finding is history and "not re-checked yet"; the unchanged file's finding is untouched
    new_ids = _extract_unscoped(env, ch, monkeypatch, APPROVAL, Y, _cand("Manager approval", "unscoped_limit", 1000000, f"{Y}: 1000000"))
    m = _model(env)
    assert [v["claims"][0]["logical_path"].endswith("finance_desk_guide.md") for v in m.current] == [True]
    assert len(m.historical) == 1 and m.historical[0]["rechecked"] is False
    assert m.counts["historical_not_rechecked"] == 1 and set(new_ids) <= set(m.unchecked)                         # read, but not checked: said, not assumed fine
    q = _queue(ch, env)
    assert q["hidden"]["history"] == 1 and all("question-" + m.historical[0]["finding_id"] != i["question_id"] for i in q["items"])
    # the new reading is checked (one run over every current reading, as "Check them now" does): the history is "re-checked", the new reading has its own finding in force
    _finding_run(env, ch, [cid for cid, info in m.claims.items() if info["valid"]])
    m2 = _model(env)
    assert m2.unchecked == [] and m2.historical[0]["rechecked"] is True and m2.counts["current_issues"] == 2


def test_a_serious_finding_on_the_edited_place_blocks_others_need_review_and_a_human_decision_resolves(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    on_edit = _extract_unscoped(env, ch, monkeypatch, APPROVAL, Y, _cand("Manager approval", "unscoped_limit", 1000000, f"{Y}: 1000000"))
    elsewhere = _extract_unscoped(env, ch, monkeypatch, FINGUIDE, "Finance Desk Guide", _cand("Finance review", "unscoped_guide", 2000000, FG_QUOTE))
    f_edit = _finding_run(env, ch, on_edit)[0]["finding_id"]
    f_else = _finding_run(env, ch, elsewhere)[0]["finding_id"]

    def classify():
        with env.store._tx() as cur:
            m = CF.build(cur, env.tenant, env.ws)
            return CF.classify_for_change(cur, env.tenant, env.ws, m, c.proposal, [ch.arts[APPROVAL]])
    rel = classify()
    assert rel["basis"] == "edit_and_impact"
    assert [v["finding_id"] for v in rel["blocking"]] == [f_edit]                                   # on the place the proposal edits
    assert [v["finding_id"] for v in rel["needs_review"]] == [f_else]                               # not known to be related: shown, never called unrelated
    assert "never says a rule is unaffected" in rel["needs_review"][0]["why_review"]
    # only a person who may resolve decides; the decision names the claim and the scope the person states
    claim = rel["blocking"][0]["claims"][0]["candidate_id"]
    body = {"workspace_id": env.ws, "conflict_id": f_edit, "authoritative_claim_id": claim, "scope": {"stated_scope": "standard orders"}, "rationale": "stated by the owner"}
    assert env.req_client().post("/v2/conflict/resolve", json=body).status_code == 403
    assert ch.admin.post("/v2/conflict/resolve", json=body).status_code == 200
    after = classify()
    assert after["blocking"] == [] and [v["finding_id"] for v in after["resolved"]] == [f_edit]
    assert [v["finding_id"] for v in after["needs_review"]] == [f_else]                              # the other one is still visible


def test_a_reading_nobody_checked_on_a_touched_place_is_reported_and_blocks_as_not_rechecked(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    ids = _extract_unscoped(env, ch, monkeypatch, APPROVAL, Y, _cand("Manager approval", "unscoped_limit", 1000000, f"{Y}: 1000000"))

    def rel_for():
        with env.store._tx() as cur:
            m = CF.build(cur, env.tenant, env.ws)
            return m, CF.classify_for_change(cur, env.tenant, env.ws, m, c.proposal, [ch.arts[APPROVAL]])
    m, rel = rel_for()
    assert set(ids) <= set(m.unchecked)
    assert set(ids) <= {u["candidate_id"] for u in rel["unchecked_related"]}                        # on the edited place and never checked: the gate says "not re-checked"
    assert rel["unchecked_other"] >= 1                                                              # other files' readings are unchecked too: counted, not blocking
    _finding_run(env, ch, [cid for cid, info in m.claims.items() if info["valid"]])                  # what "Check them now" does: one check over every current reading
    m, rel = rel_for()
    assert m.unchecked == [] and rel["unchecked_related"] == [] and rel["unchecked_other"] == 0
    assert len(rel["blocking"]) == 1                                                                # checked now: the finding itself blocks


def test_recording_where_a_rule_applies_is_stored_exactly_closes_only_that_finding_and_edits_no_source(env, ch, monkeypatch):
    on_edit = _extract_unscoped(env, ch, monkeypatch, APPROVAL, Y, _cand("Manager approval", "unscoped_limit", 1000000, f"{Y}: 1000000"))
    other = _extract_unscoped(env, ch, monkeypatch, FINGUIDE, "Finance Desk Guide", _cand("Finance review", "unscoped_guide", 2000000, FG_QUOTE))
    f1 = _finding_run(env, ch, on_edit)[0]
    f2 = _finding_run(env, ch, other)[0]
    head_before = env.store.head(env.tenant, env.ws)
    approval_hash = env.store.artifact_head_hash(env.tenant, env.ws, ch.arts[APPROVAL])
    claim = f1["involved_claim_ids"][0]
    body = {"workspace_id": env.ws, "conflict_id": f1["finding_id"], "authoritative_claim_id": claim, "scope": {"stated_scope": "Standard orders only."}, "rationale": "Stated by the owner of the rule."}
    assert ch.admin.post("/v2/conflict/resolve", json=body).status_code == 200
    with env.store._tx() as cur:
        cur.execute("SELECT actor_id, target, action, rationale, scope, payload FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s AND action='resolve'", (env.tenant, env.ws))
        rows = cur.fetchall()
    assert len(rows) == 1                                                                           # exactly one decision, for exactly one finding
    actor, target, action, rationale, scope, payload = rows[0]
    assert (actor, target, action) == (env.admin, f1["finding_id"], "resolve")                      # who decided, on which current finding
    assert rationale == "Stated by the owner of the rule." and scope["stated_scope"] == "Standard orders only." and scope["authoritative_claim_id"] == claim
    bound = payload["evidence_binding"]                                                              # the evidence the decision was taken ON: the reading, its file and that file's bytes at this revision
    assert bound["head_source_revision_id"] == head_before.source_revision_id
    assert bound["claims"][claim]["artifact_id"] == ch.arts[APPROVAL] and bound["claims"][claim]["blob_hash"] == approval_hash
    # nothing else moved: the source is untouched, the other finding is still open
    assert env.store.head(env.tenant, env.ws).source_revision_id == head_before.source_revision_id
    assert env.store.artifact_head_hash(env.tenant, env.ws, ch.arts[APPROVAL]) == approval_hash
    m = _model(env)
    state = {v["finding_id"]: v["resolved"] for v in m.current}
    assert state == {f1["finding_id"]: True, f2["finding_id"]: False}
