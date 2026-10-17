"""The suggested post-change check (2026-10-09): built by the server, confirmed by an authorised person, never saved by itself.
Real routes/store/worker, STUB provider. Requirement -> evidence:
  * file, key and kind come from the validated proposal edit; the expected value comes from the CONFIRMED REQUEST, not from the proposal      -> test 1, 2
  * a proposal that disagrees with the confirmed request is flagged, never silently accepted                                                  -> test 2
  * no single confirmed value (several edits) -> a person must state it, nothing is guessed                                                  -> test 3
  * confirmation is bound to the exact suggestion (same proposal, same revision, same item); the requester cannot confirm their own           -> test 4
  * the contract goes out of date when the files move on                                                                                     -> test 5"""
from __future__ import annotations

import copy

import pytest

from infra import v2_contract_suggestion as vcs
from tests.pitch_brief.chain import APPROVAL, build
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_draft_to_proposal import Y, _grant, chain_to_proposal


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _get(client, env, proposal):
    r = client.get("/v2/verification/suggestion", params={"workspace_id": env.ws, "proposal_job_id": proposal})
    assert r.status_code == 200, r.text
    return r.json()


def _post(client, env, proposal, item, sha=None, **over):
    body = {"workspace_id": env.ws, "proposal_job_id": proposal, "kind": item["kind"], "logical_path": item["logical_path"], "locator": item["locator"],
            "expected": item["expected"], "provenance": {"note": item["note"]}, **over}
    if sha is not None:
        body["suggestion_sha256"] = sha
    return client.post("/v2/verification/contracts", json=body)


class _Store:
    """The real store, with two things the test controls: the confirmed request and the proposal result."""

    def __init__(self, real, intent_form=None, edits=None):
        self.real, self.intent_form, self.edits = real, intent_form, edits

    def __getattr__(self, name):
        return getattr(self.real, name)

    def get_intent(self, t, w, i):
        r = copy.deepcopy(self.real.get_intent(t, w, i))
        if self.intent_form is not None:
            r["structured_form"] = self.intent_form
        return r

    def job_record(self, t, w, j):
        r = copy.deepcopy(self.real.job_record(t, w, j))
        if self.edits is not None and r.get("kind") == "proposal":
            r["execution_result"]["edits"] = self.edits
        return r


def test_the_suggestion_takes_where_from_the_proposal_and_what_from_the_confirmed_request_and_uses_no_ai(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    for who in (c.req, env.admin_client()):                                   # anyone who may read the workspace sees it; it carries no source text
        s = _get(who, env, c.proposal)
        assert s["uses_ai"] is False and s["mechanical_only"] is True and s["complete"] is True and not s["stale"]
        (item,) = s["items"]
        assert (item["kind"], item["locator"]) == ("yaml_value_equals", Y)
        assert item["logical_path"].endswith("config/approval.yaml")
        assert item["expected"] == 500000 and item["basis"] == "confirmed_request" and item["agrees_with_proposal"] is True
        assert "should be JPY 500,000 in approval.yaml" in item["sentence"]
        assert "does not show that the change achieves" in " ".join(s["limits"])                                       # the mechanical limit is said out loud
        assert len(s["suggestion_sha256"]) == 64


def test_a_proposal_that_disagrees_with_the_confirmed_request_is_flagged_and_its_value_is_not_used(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    wrong = {"subject": "Manager approval limit", "currency": "JPY", "old_value": "1,000,000", "new_value": "300,000"}      # the person confirmed 300,000
    s = vcs.build(_Store(env.store, intent_form=wrong), env.tenant, env.ws, c.proposal)
    (item,) = s["items"]
    assert item["expected"] == 300000 and item["proposal_shows"]["after"] == 500000 and item["agrees_with_proposal"] is False
    assert s["complete"] is False and any("confirmed request says 300000" in m for m in s["missing"])


def test_without_one_confirmed_value_a_person_must_state_it_and_nothing_is_guessed(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    two = [{"path": Y, "before": 1000000, "after": 500000}, {"path": "routing_rules.capex_finance_threshold_jpy", "before": 2000000, "after": 1500000}]
    s = vcs.build(_Store(env.store, edits=two), env.tenant, env.ws, c.proposal)
    assert [i["expected"] for i in s["items"]] == [None, None] and all(i["needs_human_value"] for i in s["items"])
    assert s["complete"] is False and all("a value you state" in i["sentence"] for i in s["items"])
    none = vcs.build(_Store(env.store, intent_form={"subject": "x"}), env.tenant, env.ws, c.proposal)
    assert none["items"][0]["expected"] is None and none["items"][0]["needs_human_value"] is True


def test_confirming_binds_to_the_exact_suggestion_and_the_requester_cannot_confirm_their_own(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    s = _get(c.req, env, c.proposal)
    item, sha = s["items"][0], s["suggestion_sha256"]
    assert _post(c.req, env, c.proposal, item, sha).status_code in (403, 409)                       # the person who asked is not the independent check
    approver = _grant(env, "change_approver")
    assert _post(approver, env, c.proposal, {**item, "expected": 999}, sha).status_code == 409       # not what was shown
    assert _post(approver, env, c.proposal, item, "0" * 64).status_code == 409                       # not this suggestion
    ok = _post(approver, env, c.proposal, item, sha)
    assert ok.status_code == 200, ok.text
    prov = ok.json()["provenance"]
    assert prov["origin"] == "system_suggestion_confirmed" and prov["suggestion_sha256"] == sha and ok.json()["author_account_id"] != env.req
    typed = _grant(env, "change_approver")                                                          # typing it by hand is still possible and is recorded as such
    other = {**item, "locator": "routing_rules.capex_finance_threshold_jpy", "expected": 2000000}
    r2 = _post(typed, env, c.proposal, other)
    assert r2.status_code == 200 and r2.json()["provenance"]["origin"] == "typed_by_approver"


def _confirmed(env, ch, c):
    approver = _grant(env, "change_approver")
    sug = _get(c.req, env, c.proposal)
    rc = _post(approver, env, c.proposal, sug["items"][0], sug["suggestion_sha256"])
    assert rc.status_code == 200, rc.text
    return approver, sug, rc


def test_a_proposal_that_was_applied_is_history_not_out_of_date(env, ch, monkeypatch):
    """The head moves BECAUSE of the apply: that must not make the proposal's own check, or its approval, look stale."""
    c = chain_to_proposal(env, ch, monkeypatch)
    approver, sug, rc = _confirmed(env, ch, c)
    rj, r = create_job(c.req, env, "review", {"proposal_job_id": c.proposal, "verification_contract_id": rc.json()["contract_id"]})
    drain(env)
    aj, r = create_job(c.req, env, "approval", {"proposal_job_id": c.proposal, "review_job_id": rj})
    drain(env)
    digest = job_body(ch.admin, env, aj)["execution_result"]["approval_digest"]
    assert approver.post(f"/v2/jobs/{aj}/decision", json={"workspace_id": env.ws, "action": "approve", "reason": "ok", "expected_approval_digest": digest}).status_code == 200
    apj, r = create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": digest})
    drain(env)
    assert job_body(ch.admin, env, apj)["execution_status"] == "COMPLETED"
    lst = approver.get("/v2/verification/contracts", params={"workspace_id": env.ws, "proposal_job_id": c.proposal}).json()
    assert lst["stale"] is False and lst["contracts"][0]["stale"] is False                       # the contract served the verification after the apply
    assert _get(c.req, env, c.proposal)["stale"] is False
    late = _post(approver, env, c.proposal, {**sug["items"][0], "locator": "routing_rules.capex_finance_threshold_jpy", "expected": 2000000})
    assert late.status_code == 409 and "already applied" in late.text                           # but a check of an applied proposal can no longer be changed


def test_the_contract_and_the_suggestion_go_out_of_date_when_ANOTHER_change_moves_the_files(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    approver, sug, rc = _confirmed(env, ch, c)
    assert approver.get("/v2/verification/contracts", params={"workspace_id": env.ws, "proposal_job_id": c.proposal}).json()["stale"] is False
    head = env.store.head(env.tenant, env.ws)                                                    # somebody else publishes a newer revision of the sources
    digest = env.store.stage_blob(b"other: 1" + bytes([10]))
    env.store.publish_source(env.tenant, env.ws, "src-other", {"artifact-other": digest}, expected_version=head.version, operation="source.import.commit", idempotency_key="other-1")
    lst = approver.get("/v2/verification/contracts", params={"workspace_id": env.ws, "proposal_job_id": c.proposal}).json()
    assert lst["stale"] is True and lst["contracts"][0]["stale"] is True and "newer revision" in lst["stale_reason"]
    s2 = _get(c.req, env, c.proposal)
    assert s2["stale"] is True and s2["complete"] is False
    late = _post(approver, env, c.proposal, sug["items"][0], sug["suggestion_sha256"])
    assert late.status_code == 409 and "out of date" in late.text
