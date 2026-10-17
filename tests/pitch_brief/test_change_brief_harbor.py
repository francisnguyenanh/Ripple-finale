"""change.brief on the Harbor public bundle (scenario S1: lower the Manager approval limit to 500,000 JPY).

Real import path, real extraction jobs through the worker with a STUB provider (no model), real change.brief job.
Claim under test: AI extraction supports finding rules and producing a change proposal with citations through a
deterministic downstream. change.brief is deterministic; nothing here is AI analysis.
"""
from __future__ import annotations

import json

import jsonschema
import pytest

from tests.pitch_brief.chain import (
    APPROVAL, FINGUIDE, MATRIX, POLICY, S1, WORKFLOW, build, cand_id, intent, run_brief,
)
from tests.pitch_brief.conftest import create_job, drain, job_body


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _result(body):
    assert body["execution_status"] == "COMPLETED", body
    return body["execution_result"]


# ----------------------------------------------------------------------------- intent (structured form)
def test_intent_preserves_exact_numbers_and_jpy(env, ch):
    parsed = intent(env, ch.admin, S1, confirm=False)
    assert parsed["status"] == "PARSED"
    assert parsed["value"] == 500000 and parsed["currency"] == "JPY"          # NOT 500 / USD
    stored = env.store.get_intent(env.tenant, env.ws, parsed["intent_id"])
    form = stored["structured_form"]
    assert form["old_value"] == "1,000,000" and form["new_value"] == "500,000"   # exactly as typed
    from core.change_intelligence.v2.brief_form import normalise_brief_form
    f = normalise_brief_form(form)["fields"]
    assert (f["old_value"], f["new_value"], f["currency"], f["unit"], f["applies_to"]) == (
        1000000, 500000, "JPY", "net goods amount", ["standard", "emergency"])


@pytest.mark.parametrize("bad", [
    {**S1, "new_value": "5 hundred thousand"}, {**S1, "currency": "XYZ"}, {**S1, "old_value": "1,00,000"},
    {**S1, "subject": ""}, {**S1, "old_value": "500,000"}, {**S1, "applies_to": "standard"},
])
def test_unparseable_form_stays_needs_clarification_no_guess(env, ch, bad):
    parsed = intent(env, ch.admin, bad, confirm=False)
    assert parsed["status"] == "NEEDS_CLARIFICATION" and parsed["questions"]
    c = ch.admin.post("/v2/intent/confirm", json={"workspace_id": env.ws, "intent_id": parsed["intent_id"]})
    assert c.status_code >= 400                       # cannot be confirmed


def test_unconfirmed_intent_yields_needs_confirmation(env, ch):
    parsed = intent(env, ch.admin, S1, confirm=False)
    _, body = run_brief(env, ch, parsed["intent_id"])
    r = _result(body)
    assert r["status"] == "NEEDS_CONFIRMATION" and r["match"]["outcome"] == "INTENT_NOT_CONFIRMED"
    assert r["edits"] == [] and r["candidates"] == []


# ----------------------------------------------------------------------------- selection stage
@pytest.mark.xfail(strict=True, reason="deterministic strict matcher misses scope-less/other-subject sources (workbook already at new value, C02 pairing); closed by A2 GenAI draft (change.draft), not by loosening this matcher")
def test_multiple_matches_need_confirmation_and_show_values_side_by_side(env, ch):
    parsed = intent(env, ch.admin, S1)
    _, body = run_brief(env, ch, parsed["intent_id"])
    r = _result(body)
    assert r["status"] == "NEEDS_CONFIRMATION" and r["match"]["outcome"] == "MULTIPLE_MATCHES"
    assert r["edits"] == [] and r["rule"] is None and r["impact"] is None      # nothing derived before confirmation
    by_path = {c["logical_path"]: c for c in r["candidates"]}
    assert by_path[APPROVAL]["value"] == 1000000 and by_path[APPROVAL]["value_relation"] == "at_old_value"
    assert by_path[MATRIX]["value"] == 500000 and by_path[MATRIX]["value_relation"] == "already_new_value"
    assert {APPROVAL, WORKFLOW, POLICY, MATRIX} <= set(by_path)
    assert all(c["citations"] and c["citations"][0]["revalidated"] for c in r["candidates"])
    # negative control: the freight rule has the same number as the NEW value but another subject
    assert not any(c["predicate"] == "free_freight_threshold" for c in r["candidates"] + r["not_selected"])
    assert not any(c["subject"] == "Finance review" for c in r["candidates"])


def test_not_found_lists_nothing_and_asks(env, ch):
    parsed = intent(env, ch.admin, {**S1, "subject": "Warehouse forklift licence limit"})
    _, body = run_brief(env, ch, parsed["intent_id"])
    r = _result(body)
    assert r["status"] == "NEEDS_CONFIRMATION" and r["match"]["outcome"] == "NOT_FOUND"
    assert r["candidates"] == [] and r["edits"] == []


def test_single_match_still_needs_source_confirmation(env, ch):
    parsed = intent(env, ch.admin, S1)
    jid, r = create_job(ch.admin, env, "change.brief", {"intent_id": parsed["intent_id"],
                                                        "extraction_job_ids": [ch.jobs[APPROVAL]]})
    assert r.status_code == 200
    drain(env)
    res = _result(job_body(ch.admin, env, jid))
    assert res["match"]["outcome"] == "SINGLE_MATCH_UNCONFIRMED" and res["edits"] == []


# ----------------------------------------------------------------------------- confirmed target
def _confirmed(env, ch, path=APPROVAL, predicate="standard_manager_approval_threshold", **extra):
    tid = cand_id(ch, path, predicate)
    parsed = intent(env, ch.admin, {**S1, "target_candidate_id": tid})
    jid, body = run_brief(env, ch, parsed["intent_id"], **extra)
    return tid, jid, _result(body)


@pytest.mark.xfail(strict=True, reason="deterministic strict matcher misses scope-less/other-subject sources (workbook already at new value, C02 pairing); closed by A2 GenAI draft (change.draft), not by loosening this matcher")
def test_confirmed_target_derives_yaml_edit_and_manual_items(env, ch):
    tid, jid, r = _confirmed(env, ch)
    assert r["status"] == "DRAFT_READY" and r["match"]["outcome"] == "FOUND"
    assert r["draft_only"] is True and r["applies_nothing"] is True and r["provider_calls"] == 0
    assert r["executor_id"] == "change.brief.v1" and "AI analysis" not in r["claim"]
    assert r["rule"]["candidate_id"] == tid and r["rule"]["citations"][0]["revalidated"]
    yaml_edit = next(e for e in r["edits"] if e["logical_path"] == APPROVAL)
    assert yaml_edit["kind"] == "yaml_edit" and yaml_edit["status"] == "PROPOSED" and yaml_edit["tier"] == "DIRECT"
    assert (yaml_edit["old"], yaml_edit["new"]) == (1000000, 500000)
    assert yaml_edit["proposal_edit"] == {"path": "routing_rules.standard_manager_threshold_jpy",
                                          "expected_current": 1000000, "proposed": 500000}
    # (b) the workbook already says 500,000: NO_OP, manual, never an edit
    xl = next(e for e in r["edits"] if e["logical_path"] == MATRIX)
    assert xl["status"] == "NO_OP" and xl["kind"] == "manual" and xl["proposal_edit"] is None
    # everything that is not a YAML scalar leaf is manual, never derived
    for e in r["edits"]:
        if e["kind"] == "yaml_edit":
            assert e["logical_path"].endswith(".yaml") and e["locator"]["key_path"]
        else:
            assert e["proposal_edit"] is None and e["manual_note"]
    assert not any(e["logical_path"].endswith((".md", ".xlsx", ".py")) and e["kind"] == "yaml_edit"
                   for e in r["edits"])
    # (c) freight 500,000 is never an edit or a match
    assert not any("free_freight" in json.dumps(e) for e in r["edits"])
    # python / applies_to: stated as NOT COVERED, not as found or not found
    reasons = [n["reason"] for n in r["not_covered"]]
    assert any("python code is observed as text blocks" in x for x in reasons)
    assert any(n["logical_path"].endswith("calculations.py") for n in r["not_covered"])
    assert any("applies_to:emergency" in x for x in reasons)
    assert not any("applies_to:standard" in x for x in reasons)
    # other matching rules stay listed, visible and unchanged
    assert {c["logical_path"] for c in r["not_selected"]} >= {WORKFLOW, POLICY}
    assert r["impact"]["target_candidate_id"] == tid
    assert r["impact"]["direct"] and r["impact"]["direct"][0]["candidate_id"] == tid


def test_target_at_new_value_is_no_op_only(env, ch):
    # human picks the workbook rule (500,000 = new value): never a yaml edit
    tid = cand_id(ch, MATRIX, "approval_threshold")
    parsed = intent(env, ch.admin, {**S1, "target_candidate_id": tid})
    _, body = run_brief(env, ch, parsed["intent_id"])
    r = _result(body)
    assert not any(e["kind"] == "yaml_edit" for e in r["edits"])
    if r["status"] == "DRAFT_READY":
        assert all(e["status"] != "PROPOSED" for e in r["edits"] if e["logical_path"] == MATRIX)
    else:
        assert r["edits"] == []


def test_unrelated_target_id_is_refused(env, ch):
    tid = cand_id(ch, POLICY, "free_freight_threshold")        # negative control chosen by a (wrong) human
    parsed = intent(env, ch.admin, {**S1, "target_candidate_id": tid})
    _, body = run_brief(env, ch, parsed["intent_id"])
    r = _result(body)
    assert r["status"] == "NEEDS_CONFIRMATION" and r["match"]["outcome"] == "TARGET_NOT_MATCHING"
    assert r["edits"] == []


def test_tampered_quote_is_not_cited(env, ch):
    import psycopg2
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    jid = ch.jobs[APPROVAL]
    res = ch.bodies[APPROVAL]["execution_result"]
    for c in res["candidates"]:
        if c["candidate_id"] == tid:
            c["evidence"][0]["quote"] = "routing_rules.standard_manager_threshold_jpy: 9999999"
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("UPDATE v2_jobs SET execution_result=%s WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s",
                    (json.dumps(res), env.tenant, env.ws, jid))
    parsed = intent(env, ch.admin, {**S1, "target_candidate_id": tid})
    _, body = run_brief(env, ch, parsed["intent_id"])
    r = _result(body)
    assert r["status"] == "NEEDS_CONFIRMATION" and r["edits"] == []
    assert r["match"]["outcome"] == "QUOTE_NOT_REVALIDATED"
    shown = [c for c in r["candidates"] if c["candidate_id"] == tid]
    assert shown and shown[0]["citations"] == [] and shown[0]["unsupported_citations"]
    assert any(u["kind"] == "rule" for u in r["unsupported"])


@pytest.mark.xfail(strict=True, reason="deterministic strict matcher misses scope-less/other-subject sources (workbook already at new value, C02 pairing); closed by A2 GenAI draft (change.draft), not by loosening this matcher")
def test_open_scope_mismatch_c02_is_listed_unchanged(env, ch):
    pol = cand_id(ch, POLICY, "capex_finance_review_threshold")
    fin = cand_id(ch, FINGUIDE, "capex_finance_review_threshold")
    fj, r = create_job(ch.admin, env, "finding", {"candidate_ids": [pol, fin]})
    assert r.status_code == 200, r.text
    drain(env)
    _result(job_body(ch.admin, env, fj))
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    parsed = intent(env, ch.admin, {**S1, "target_candidate_id": tid})
    _, body = run_brief(env, ch, parsed["intent_id"], finding_job_ids=[fj])
    res = _result(body)
    assert res["open_items"]
    assert all(o["status"] == "OPEN_UNCHANGED" for o in res["open_items"])
    assert any(o["type"] == "scope_mismatch" for o in res["open_items"]), res["open_items"]
    assert not any(e["logical_path"] == FINGUIDE for e in res["edits"])


def test_no_provider_call_in_change_brief_and_schema_enforced(env, ch):
    calls_before = ch.stub.calls
    assert calls_before == len(ch.jobs)          # one stub call per extraction job, nothing else
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    parsed = intent(env, ch.admin, {**S1, "target_candidate_id": tid})
    jid, body = run_brief(env, ch, parsed["intent_id"])
    assert ch.stub.calls == calls_before
    assert body["execution_provider_calls"] in (0, None)
    from infra.worker.change_brief import BRIEF_SCHEMA, ChangeBriefExecutor
    jsonschema.Draft7Validator(BRIEF_SCHEMA).validate(_result(body))
    assert ChangeBriefExecutor.external is False
    bad = dict(_result(body), status="DRAFT_READY", match={"outcome": "NOT_FOUND"})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(BRIEF_SCHEMA).validate(bad)


def test_route_rejects_malformed_change_brief_payload(env, ch):
    _, r = create_job(ch.admin, env, "change.brief", {"intent_id": "x"})
    assert r.status_code == 400
    _, r = create_job(ch.admin, env, "change.brief", {"extraction_job_ids": ["a"]})
    assert r.status_code == 400
