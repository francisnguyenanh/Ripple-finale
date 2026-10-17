"""J3: one-snapshot impact context -> impact job (relation/finding context passed), paths + reasons + citations,
UNKNOWN for uncovered rules, freight/other rules never promoted. Stub provider only."""
from __future__ import annotations

import pytest

from tests.pitch_brief.chain import APPROVAL, FINGUIDE, POLICY, build, cand_id
from tests.pitch_brief.conftest import create_job, drain, job_body


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _ctx(client, env, target):
    r = client.get("/v2/change/impact-context", params={"workspace_id": env.ws, "target_candidate_id": target})
    return r


def _impact(env, ch, cx):
    payload = {"candidate_ids": cx["candidate_ids"], "target_candidate_id": cx["target_candidate_id"],
               "relation_job_ids": cx["relation_job_ids"], "existing_finding_ids": cx["existing_finding_ids"],
               "expected_decision_epoch": cx["expected_decision_epoch"], "context_truncated": cx["context_truncated"]}
    jid, r = create_job(ch.admin, env, "impact", payload)
    assert r.status_code == 200, r.text
    drain(env)
    body = job_body(ch.admin, env, jid)
    assert body["execution_status"] == "COMPLETED", body
    return body["execution_result"]


def test_uncovered_rules_are_unknown_then_relation_evidence_moves_them(env, ch):
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    freight = cand_id(ch, POLICY, "free_freight_threshold")
    cx = _ctx(ch.admin, env, tid).json()
    assert tid in cx["candidate_ids"] and cx["relation_job_ids"] == []
    assert set(cx["uncovered_candidate_ids"]) == set(cx["candidate_ids"])
    before = _impact(env, ch, cx)
    assert {u["candidate_id"] for u in before["unknown"]} >= {freight}
    assert all(u["reason"].startswith("no_confirmed_relation_evidence") for u in before["unknown"])
    # NOTE: the executor's coverage.complete ignores UNKNOWN rules; the UI therefore derives "incomplete" itself
    assert before["unknown"]
    # compute relation coverage (deterministic, no model call) and take a FRESH snapshot context
    rj, r = create_job(ch.admin, env, "relation", {"candidate_ids": cx["candidate_ids"]})
    assert r.status_code == 200, r.text
    drain(env)
    cx2 = _ctx(ch.admin, env, tid).json()
    assert rj in cx2["relation_job_ids"] and cx2["uncovered_candidate_ids"] == []
    after = _impact(env, ch, cx2)
    assert len(after["unknown"]) < len(before["unknown"])
    # the freight rule (same literal as nothing here, different subject) is never DIRECT
    assert freight not in {d["candidate_id"] for d in after["direct"]}
    for d in after["direct"]:
        assert d["path"] and d["evidence"] is not None
    assert after["pinned_input"]["pinned_decision_epoch"] == cx2["expected_decision_epoch"]


def test_stale_epoch_is_refused_and_foreign_target_is_404(env, ch):
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    cx = _ctx(ch.admin, env, tid).json()
    other = cand_id(ch, POLICY, "manager_approval_required_from")
    assert ch.admin.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": other}).status_code == 200
    jid, r = create_job(ch.admin, env, "impact", {"candidate_ids": cx["candidate_ids"], "target_candidate_id": tid,
                                                  "expected_decision_epoch": cx["expected_decision_epoch"]})
    assert r.status_code in (409, 400, 422), (r.status_code, r.text)      # epoch moved: not run on a stale snapshot
    assert _ctx(ch.admin, env, "cand-not-here").status_code == 404
    assert _ctx(env.req_client(), env, tid).status_code == 200            # read allowed for project.read


def test_coverage_flags_have_one_meaning_in_api_and_ui(env, ch):
    """traversal_complete (== the legacy `complete`) and impact_complete are different questions: a finished traversal that
    still leaves rules UNKNOWN is NOT an impact_complete result. Same fields drive the UI."""
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    cx = _ctx(ch.admin, env, tid).json()
    res = _impact(env, ch, cx)
    cov = res["coverage"]
    assert res["unknown"], "precondition: uncovered rules are UNKNOWN"
    assert cov["unknown_count"] == len(res["unknown"])
    assert cov["traversal_complete"] is True and cov["complete"] == cov["traversal_complete"]    # legacy alias, same meaning
    assert cov["impact_complete"] is False                                                        # traversal done, impact NOT resolved
    assert cov["impact_complete"] == (cov["traversal_complete"] and cov["unknown_count"] == 0)
    assert "UNKNOWN" in cov["meaning"] and "not 'unaffected'" in cov["meaning"]
    import pathlib
    js = (pathlib.Path(__file__).resolve().parents[2] / "app" / "ui" / "app_change.js").read_text(encoding="utf-8")
    assert "cov.impact_complete" in js and "cov.traversal_complete" in js     # the UI reads the API fields, no own rule
    assert "cov.complete" not in js
