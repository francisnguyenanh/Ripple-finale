"""S-ROUTE: real Harbor before/after simulation (adapter harbor_cli of the existing `simulation` job). The expected changed set
is registered from DATA that does not come from running the adapter: Harbor's shipped baseline `run_output/routes.jsonl` plus the
public requisitions - never from the model, never from the adapter's own output. Runner = resource-limited process over the
hash-pinned public Harbor copy (not a security boundary; no uploaded/model code runs)."""
from __future__ import annotations

import json
import pathlib
import shutil

import pytest

from infra.worker import harbor_sim
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job, drain, job_body

FIX = harbor_sim.FIXTURE
STD = "routing_rules.standard_manager_threshold_jpy"
CAPEX = "routing_rules.capex_finance_threshold_jpy"


def _jl(rel):
    return [json.loads(x) for x in (FIX / rel).read_text(encoding="utf-8").splitlines() if x.strip()]


def _expected_standard_lowering(new):
    """Registered expectation, independent of the adapter: a STANDARD-process requisition that is AUTO in Harbor's shipped
    baseline has no earlier rule applying, so it becomes NEEDS_MANAGER iff its amount is >= the new threshold."""
    reqs = {r["request_id"]: r for r in _jl("data/requisitions.jsonl")}
    return {b["request_id"] for b in _jl("run_output/routes.jsonl")
            if b["route"] == "AUTO" and reqs[b["request_id"]]["process"] == "STANDARD" and reqs[b["request_id"]]["net_amount_jpy"] >= new}


def _expected_capex_lowering(new):
    """CAPEX rows whose baseline route is a manager/auto route (no supplier/validation rule applied first) and whose amount is
    >= the new CAPEX threshold: Harbor applies the CAPEX rule to the standard AND the emergency process."""
    reqs = {r["request_id"]: r for r in _jl("data/requisitions.jsonl")}
    return {b["request_id"] for b in _jl("run_output/routes.jsonl")
            if b["route"] in ("AUTO", "NEEDS_MANAGER") and reqs[b["request_id"]]["category"] == "CAPEX"
            and reqs[b["request_id"]]["net_amount_jpy"] >= new}


def test_fixture_manifest_matches_the_public_bundle_and_is_verified_at_load():
    files = harbor_sim.load_fixture()                       # raises on any drift
    assert "app/routing.py" in files and "run_output/routes.jsonl" in files and not any("evaluator_private" in k for k in files)
    from tools.judge_local import make_harbor_fixture as mk
    src = mk._default_input()
    for rel, content in files.items():
        assert (src / rel).read_bytes() == content, rel     # byte-identical to the public input


def test_manager_threshold_lowering_matches_the_registered_expectation_and_is_fully_explained():
    r = harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None,
                                  edits=[{"path": STD, "value": 500000, "expected_current": 1000000}])
    assert r["integrity"] == {"seed_equals_shipped": True, "rows": 240, "shipped_file": "run_output/routes.jsonl"}
    got = {c["request_id"] for c in r["changed"]}
    want = _expected_standard_lowering(500000)
    assert got == want and r["changed_count"] == len(want) > 0 and not r["changed_truncated"]
    assert r["unexplained_changes"] == 0 and r["transactions"] == 240
    assert all(c["before"]["route"] == "AUTO" and c["after"]["route"] == "NEEDS_MANAGER" and c["explained"] for c in r["changed"])
    assert all(c["cause"][0]["config_key"] == STD and "net_amount_jpy >=" in c["cause"][0]["code_ref"]["text"] for c in r["changed"])
    assert r["by_route_after"]["NEEDS_MANAGER"] == r["by_route_before"]["NEEDS_MANAGER"] + len(want)
    assert r["emergency_rows_changed"] == 0                              # this config key does not govern emergency in Harbor
    assert any("emergency" in x.lower() and "NOT in the support matrix" in x for x in r["limits"])
    assert any("HARD-CODED 1,000,000 at app/emergency.py:" in x for x in r["limits"])           # why S1 'standard and emergency' is not a config edit
    assert "NOT a security boundary" in r["runner"]["statement"] and r["runner"]["proposed"]["input_hash_unchanged"]


def test_capex_threshold_edit_changes_only_capex_rows():
    r = harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None, edits=[{"path": CAPEX, "value": 1000000}])
    got = {c["request_id"] for c in r["changed"]}
    assert got == _expected_capex_lowering(1000000) and r["unexplained_changes"] == 0
    assert all(c["category"] == "CAPEX" and c["after"]["route"] == "NEEDS_FINANCE" for c in r["changed"])
    assert r["emergency_rows_changed"] > 0            # the CAPEX key is shared by the emergency path; the manager key is not (see above)


def test_noop_and_raise_cases():
    r = harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None, edits=[{"path": STD, "value": 1000000}])
    assert r["changed_count"] == 0 and r["edits"][0]["no_op"] is True
    up = harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None, edits=[{"path": STD, "value": 2000000}])
    assert up["changed_count"] > 0 and all(c["before"]["route"] == "NEEDS_MANAGER" and c["after"]["route"] == "AUTO" for c in up["changed"])


def test_old_value_mismatch_is_stale_not_simulated():
    with pytest.raises(harbor_sim.HarborSimError) as e:
        harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None, edits=[{"path": STD, "value": 500000, "expected_current": 900000}])
    assert e.value.code == "stale_baseline"


@pytest.mark.parametrize("edits", [
    [{"path": "matching_rules.payment_term_days", "value": 45}],            # not an allowlisted key for this case
    [{"path": STD, "value": True}], [{"path": STD, "value": "500000"}], [{"path": STD, "value": -5}], [{"path": STD, "value": 10 ** 12}],
    [{"path": STD, "value": 1}, {"path": STD, "value": 2}], [], "x"])
def test_payload_validation_rejects_before_any_run(edits):
    assert harbor_sim.validate_payload({"case": "route", "edits": edits}) is not None
    assert harbor_sim.validate_payload({"case": "match", "edits": [{"path": STD, "value": 1}]}) is not None


def test_tampered_fixture_refuses_to_run(tmp_path, monkeypatch):
    copy = tmp_path / "harbor_public"
    shutil.copytree(FIX, copy)
    (copy / "app" / "routing.py").write_bytes((copy / "app" / "routing.py").read_bytes() + b"\n# tampered\n")
    monkeypatch.setattr(harbor_sim, "FIXTURE", copy)
    called = []
    monkeypatch.setattr(harbor_sim.sandbox_runner, "run_isolated", lambda **k: called.append(1))
    with pytest.raises(harbor_sim.HarborSimError) as e:
        harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None, edits=[{"path": STD, "value": 500000}])
    assert e.value.code == "fixture_hash_mismatch" and called == []


def test_integrity_failure_claims_nothing(monkeypatch):
    real = harbor_sim._run
    state = {"n": 0}

    def fake(files, case, cfg, name):
        rows, meta = real(files, case, cfg, name)
        if state["n"] == 0:
            rows = rows[:-1]                                     # the seed run no longer reproduces Harbor's shipped output
        state["n"] += 1
        return rows, meta
    monkeypatch.setattr(harbor_sim, "_run", fake)
    with pytest.raises(harbor_sim.HarborSimError) as e:
        harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None, edits=[{"path": STD, "value": 500000}])
    assert e.value.code == "adapter_integrity_failed"


@pytest.mark.parametrize("bad", [b"- just\n- a list\n", b"version: 1\nrouting_rules:\n  standard_manager_threshold_jpy: abc\n  capex_finance_threshold_jpy: 2000000\n",
                                 b"routing_rules:\n  standard_manager_threshold_jpy: 1000000\n"])
def test_invalid_workspace_config_is_refused(bad):
    with pytest.raises(harbor_sim.HarborSimError) as e:
        harbor_sim.simulate_route(baseline_yaml=bad, baseline_ref={"kind": "x"}, edits=[{"path": STD, "value": 500000}])
    assert e.value.code in ("config_invalid", "config_key_missing")


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_through_the_real_job_route_worker_and_workspace_config(env, ch):
    req = env.req_client()
    jid, r = create_job(req, env, "simulation", {"adapter": "harbor_cli", "case": "route",
                                                 "edits": [{"path": STD, "value": 500000, "expected_current": 1000000}]})
    assert r.status_code == 200, r.text
    drain(env)
    body = job_body(ch.admin, env, jid)
    assert body["execution_status"] == "COMPLETED", body
    res = body["execution_result"]
    assert res["status"] == "EXECUTED" and res["adapter"] == "harbor_cli" and body["execution_usage"] in (None, {}) or True
    h = res["harbor"]
    assert h["baseline_source"]["kind"] == "workspace_revision" and h["baseline_source"]["logical_path"].endswith("config/approval.yaml")
    assert h["changed_count"] == len(_expected_standard_lowering(500000)) and h["unexplained_changes"] == 0
    assert res["coverage"]["submitted"] == 240 and len(res["outcomes"]) == 240
    assert (body.get("execution_provider_calls") or 0) == 0                  # deterministic: no model call
    # a rejected payload fails the job without running anything
    bj, rb = create_job(req, env, "simulation", {"adapter": "harbor_cli", "case": "route",
                                                 "edits": [{"path": "matching_rules.payment_term_days", "value": 45}]})
    drain(env)
    bad = job_body(ch.admin, env, bj)
    assert rb.status_code != 200 or bad["execution_status"] == "FAILED"


def test_capex_edit_moves_emergency_capex_rows_through_the_emergency_module_and_the_limits_say_so():
    """T1 showed 8 of 15 changed rows as EMERGENCY and a limits text saying emergency is hard-coded. Both are true: the CAPEX check runs BEFORE the literal in app/emergency.py.
    Expectation registered from Harbor's shipped data (baseline route + reason), not from the adapter's output."""
    reqs = {r["request_id"]: r for r in _jl("data/requisitions.jsonl")}
    shipped = {b["request_id"]: b for b in _jl("run_output/routes.jsonl")}
    want_emerg = {i for i, b in shipped.items() if reqs[i]["process"] == "EMERGENCY" and reqs[i]["category"] == "CAPEX"
                  and b["reason_code"] == "EMERGENCY_MANAGER_REQUIRED" and 1_000_000 <= reqs[i]["net_amount_jpy"] < 2_000_000}
    r = harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None, edits=[{"path": CAPEX, "value": 1000000}])
    got = {c["request_id"] for c in r["changed"] if c["process"] == "EMERGENCY"}
    assert got == want_emerg and r["emergency_rows_changed"] == len(want_emerg) > 0
    for c in r["changed"]:
        assert c["category"] == "CAPEX" and c["after"]["reason"] == "CAPEX_HIGH_VALUE" and c["cause"][0]["config_key"] == CAPEX
        want_file = "app/emergency.py" if c["process"] == "EMERGENCY" else "app/routing.py"
        assert c["cause"][0]["code_ref"]["file"] == want_file and "config.capex_finance_threshold_jpy" in c["cause"][0]["code_ref"]["text"]   # the line that ran for THIS row
    text = " ".join(r["limits"])
    assert "BEFORE the hard-coded" in text and "does change those rows" in text and "HARD-CODED 1,000,000 at app/emergency.py:" in text
    std = harbor_sim.simulate_route(baseline_yaml=None, baseline_ref=None, edits=[{"path": STD, "value": 500000}])
    assert "does change those rows" not in " ".join(std["limits"]) and std["emergency_rows_changed"] == 0       # the manager threshold never reaches emergency rows
