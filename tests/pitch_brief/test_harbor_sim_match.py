"""S-MATCH / S-TERMS: Harbor `match` before/after over the 120 public invoices. Expectations are registered from Harbor's SHIPPED
baseline decisions (`run_output/invoice_decisions.jsonl`, which carries price_difference_jpy and po_net_jpy per invoice) and the
documented rule (price_diff <= fixed AND 100*price_diff <= percent*po_net) - not from the adapter's output."""
from __future__ import annotations

import json

import pytest

from infra.worker import harbor_sim
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job, drain, job_body

FIX = harbor_sim.FIXTURE
FIXED = "matching_rules.price_tolerance_fixed_jpy"
PCT = "matching_rules.price_tolerance_percent"
TERMS = "matching_rules.payment_term_days"


def _shipped():
    return [json.loads(x) for x in (FIX / "run_output" / "invoice_decisions.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def _expected_freed(fixed, pct):
    """HOLD_PRICE invoices of the shipped baseline that the new tolerance would pass (diff and PO net come from the shipped data)."""
    out = set()
    for r in _shipped():
        d = r["comparison_details"]
        if r["reason_code"] == "PRICE_OUT_OF_TOLERANCE" and d["price_difference_jpy"] <= fixed and 100 * d["price_difference_jpy"] <= pct * d["po_net_jpy"]:
            out.add(r["invoice_id"])
    return out


def _expected_newly_held(fixed, pct):
    """READY invoices of the baseline whose price difference the NEW (tighter) tolerance no longer allows."""
    out = set()
    for r in _shipped():
        d = r["comparison_details"]
        if r["status"] == "READY" and not (d["price_difference_jpy"] <= fixed and 100 * d["price_difference_jpy"] <= pct * d["po_net_jpy"]):
            out.add(r["invoice_id"])
    return out


def test_raising_the_fixed_tolerance_frees_exactly_the_registered_invoices():
    r = harbor_sim.simulate_match(baseline_yaml=None, baseline_ref=None, edits=[{"path": FIXED, "value": 20000, "expected_current": 10000}])
    assert r["integrity"]["seed_equals_shipped"] and r["integrity"]["rows"] == 120 and r["transactions"] == 120
    want = _expected_freed(20000, 2)
    assert want == {"INV-000006"}                                          # the registered expectation is small and explicit
    assert {c["invoice_id"] for c in r["changed"]} == want and r["unexplained_changes"] == 0
    c = r["changed"][0]
    assert c["before"]["status"] == "HOLD_PRICE" and c["after"]["status"] == "READY" and c["after"]["due_date"]
    assert c["cause"][0]["config_key"] == FIXED and "price_tolerance_fixed_jpy" in c["cause"][0]["code_ref"]["text"]
    assert c["price_difference_jpy"] == 10001 and r["inert_edits"] == []


def test_raising_both_tolerances_frees_the_percent_blocked_invoices_too():
    r = harbor_sim.simulate_match(baseline_yaml=None, baseline_ref=None,
                                  edits=[{"path": FIXED, "value": 20000}, {"path": PCT, "value": 3}])
    want = _expected_freed(20000, 3)
    assert len(want) == 3 and {c["invoice_id"] for c in r["changed"]} == want and r["unexplained_changes"] == 0


def test_tightening_the_tolerance_holds_more_invoices():
    r = harbor_sim.simulate_match(baseline_yaml=None, baseline_ref=None, edits=[{"path": FIXED, "value": 0}])
    want = _expected_newly_held(0, 2)
    assert want and {c["invoice_id"] for c in r["changed"]} >= want and r["unexplained_changes"] == 0


def test_payment_terms_key_is_inert_because_the_code_hardcodes_30_days():
    """S-TERMS, traced and verified: Harbor parses payment_term_days but calculate_due_date uses a literal 30."""
    r = harbor_sim.simulate_match(baseline_yaml=None, baseline_ref=None, edits=[{"path": TERMS, "value": 45, "expected_current": 30}])
    assert r["changed_count"] == 0 and r["transactions"] == 120
    assert len(r["inert_edits"]) == 1 and "config does not control this behaviour" in r["inert_edits"][0]["reason"]
    assert "hard-coded 30 days" in r["inert_edits"][0]["reason"] and r["inert_edits"][0]["consumers"] == []     # TRACED, not inferred
    assert r["no_change_edits"] == []
    ref = r["inert_edits"][0]["code_ref"]
    assert ref["file"] == "app/calculations.py" and "timedelta(days=30)" in ref["text"]
    assert any(d["due_date"] for d in _shipped() if d["status"] == "READY")        # due dates exist and stay +30 days either way


def test_noop_and_validation():
    assert harbor_sim.simulate_match(baseline_yaml=None, baseline_ref=None, edits=[{"path": FIXED, "value": 10000}])["changed_count"] == 0
    for bad in ([{"path": "routing_rules.standard_manager_threshold_jpy", "value": 1}], [{"path": PCT, "value": 101}],
                [{"path": PCT, "value": 2.5}], [{"path": TERMS, "value": 0}], [{"path": FIXED, "value": "1"}]):
        assert harbor_sim.validate_payload({"case": "match", "edits": bad}) is not None
    assert harbor_sim.validate_payload({"case": "route", "edits": [{"path": FIXED, "value": 1}]}) is not None   # wrong family for the case


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_match_through_the_real_job_route_and_workspace_config(env, ch):
    jid, r = create_job(env.req_client(), env, "simulation", {"adapter": "harbor_cli", "case": "match",
                                                              "edits": [{"path": FIXED, "value": 20000, "expected_current": 10000}]})
    assert r.status_code == 200, r.text
    drain(env)
    body = job_body(ch.admin, env, jid)
    assert body["execution_status"] == "COMPLETED", body
    h = body["execution_result"]["harbor"]
    assert h["case"] == "match" and h["baseline_source"]["kind"] == "workspace_revision"
    assert {c["invoice_id"] for c in h["changed"]} == {"INV-000006"} and body["execution_result"]["coverage"]["submitted"] == 120
    assert (body.get("execution_provider_calls") or 0) == 0


def test_zero_change_is_not_called_inert_unless_a_trace_proves_no_module_reads_the_key():
    """percent 2 -> 99 changes no invoice in this sample (the fixed 10,000 gate still blocks them), but the key IS read by the
    matching code: reported as 'no change in this sample' with the consuming code line, NOT as inert."""
    r = harbor_sim.simulate_match(baseline_yaml=None, baseline_ref=None, edits=[{"path": PCT, "value": 99}])
    assert r["changed_count"] == 0 and r["inert_edits"] == []
    nc = r["no_change_edits"][0]
    assert nc["path"] == PCT and "not inert" in nc["reason"] and nc["consumers"][0]["file"] == "app/matching.py"
    assert "price_tolerance_percent" in nc["consumers"][0]["text"]


def test_emergency_is_reported_as_not_controlled_by_config_with_code_evidence():
    r = harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None,
                                  edits=[{"path": "routing_rules.standard_manager_threshold_jpy", "value": 500000}])
    nc = r["not_controlled"][0]
    assert nc["reason"] == "config does not control this behaviour" and nc["evidence"]["file"] == "app/emergency.py"
    assert "1_000_000" in nc["evidence"]["text"] and "0 matches" in nc["proof"]
    # the CAPEX key, by contrast, IS read by the emergency path: no such statement for it
    c = harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None, edits=[{"path": "routing_rules.capex_finance_threshold_jpy", "value": 1000000}])
    assert c["not_controlled"] == []
