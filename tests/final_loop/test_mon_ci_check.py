"""MON-CI (advisory half, local): the business CI drift check over a FIXTURE repository created by the test (Harbor approval.yaml), the accepted fixture policy and a protected
mapping. Pure report logic plus the real CLI (git show, YAML, JSON report, exit codes). No provider, no network, no required-check integration (that completion gate is real
and stays OPEN)."""
from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys

import pytest

from core.change_intelligence.v2 import business_ci as ci
from tests.pitch_brief.conftest import HARBOR_INPUT

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "final_knowledge_loop"
RULES = json.loads((FIX / "expected_knowledge.json").read_text(encoding="utf-8"))["observed_rules"]
POLICY = {"snapshot_id": "accepted-fixture", "business_revision": 1, "rules": RULES}
YAML = (HARBOR_INPUT / "config" / "approval.yaml").read_text(encoding="utf-8")


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def mapping_for(policy, repo_id="fixture"):
    return {"profile": ci.PROFILE, "repo_id": repo_id, "policy_hash": ci.policy_hash(policy), "files": {"approval.yaml": "config/approval.yaml"}}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "config").mkdir(parents=True)
    (r / "config" / "approval.yaml").write_text(YAML, encoding="utf-8")
    _git(r, "init", "-q")
    _git(r, "-c", "user.name=f", "-c", "user.email=f@localhost", "add", "-A")
    _git(r, "-c", "user.name=f", "-c", "user.email=f@localhost", "commit", "-q", "-m", "base")
    return r


def commit(r, text, path="config/approval.yaml", msg="head"):
    p = r / path
    if text is None:
        _git(r, "rm", "-q", path)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        _git(r, "-c", "user.name=f", "-c", "user.email=f@localhost", "add", "-A")
    _git(r, "-c", "user.name=f", "-c", "user.email=f@localhost", "commit", "-q", "-am", msg)
    return _git(r, "rev-parse", "HEAD")


def cli(*args):
    return subprocess.run([sys.executable, "-m", "tools.business_ci.check", *args], cwd=ROOT, capture_output=True, text=True)


# ---- pure report logic ----------------------------------------------------------------------------------------------------------------------------------------------------

def _values(mapping_of_values):
    return lambda path, key: mapping_of_values.get((path, key), ci.UNREADABLE)


STD = ("config/approval.yaml", "routing_rules.standard_manager_threshold_jpy")
CAPEX = ("config/approval.yaml", "routing_rules.capex_finance_threshold_jpy")


def test_plan_sees_only_single_numeric_rules_with_a_yaml_binding_and_names_the_rest():
    plan = ci.plan_checks(POLICY, mapping_for(POLICY))
    checked = {c["rule_id"]: c for c in plan["checks"]}
    assert checked["rule-standard-manager-review"]["expected"] == 1000000 and checked["rule-standard-manager-review"]["key"] == "routing_rules.standard_manager_threshold_jpy"
    assert checked["rule-capex-finance-config"]["expected"] == 2000000
    assert {u["rule_id"] for u in plan["unmapped"]} >= {"rule-capex-finance-policy"} and plan["unsupported"] == []
    forgot = ci.plan_checks(POLICY, {**mapping_for(POLICY), "files": {}})
    assert forgot["checks"] == [] and {u["reason"] for u in forgot["unsupported"]} == {"file_not_in_mapping"}      # a forgotten mapping file blocks, never silent


def test_aligned_drift_no_impact_and_unsupported_are_told_apart_and_pre_existing_drift_is_still_drift():
    head_ok = {STD: 1000000, CAPEX: 2000000}
    rep = ci.run(POLICY, mapping_for(POLICY), repo_id="fixture", base_sha="a" * 40, head_sha="b" * 40, read_base=_values(head_ok), read_head=_values(head_ok), changed_paths=["README.md"])
    assert rep["conclusion"] == "aligned" and rep["would_block"] is False and {r["status"] for r in rep["results"]} == {"no_impact"} and rep["coverage"]["complete"] is True
    drift = {**head_ok, STD: 1500000}
    rep = ci.run(POLICY, mapping_for(POLICY), repo_id="fixture", base_sha="a" * 40, head_sha="c" * 40, read_base=_values(head_ok), read_head=_values(drift), changed_paths=["config/approval.yaml"])
    d = next(r for r in rep["results"] if r["rule_id"] == "rule-standard-manager-review")
    assert rep["conclusion"] == "drift" and rep["would_block"] is True and d["status"] == "drift" and d["introduced_by_diff"] is True and d["head"] == 1500000
    assert next(r for r in rep["results"] if r["rule_id"] == "rule-capex-finance-config")["status"] == "aligned"      # touched file, same value
    rep = ci.run(POLICY, mapping_for(POLICY), repo_id="fixture", base_sha="c" * 40, head_sha="d" * 40, read_base=_values(drift), read_head=_values(drift), changed_paths=[])
    d = next(r for r in rep["results"] if r["rule_id"] == "rule-standard-manager-review")
    assert d["status"] == "drift" and d["introduced_by_diff"] is False                                                   # already wrong before the diff: still drift
    gone = {STD: 1000000}
    rep = ci.run(POLICY, mapping_for(POLICY), repo_id="fixture", base_sha="a" * 40, head_sha="e" * 40, read_base=_values(head_ok), read_head=_values(gone), changed_paths=["config/approval.yaml"])
    assert rep["conclusion"] == "incomplete" and rep["would_block"] is True and next(r for r in rep["results"] if r["rule_id"] == "rule-capex-finance-config")["reason"] == "head_location_unreadable"
    wordy = {**head_ok, CAPEX: "two million"}
    rep = ci.run(POLICY, mapping_for(POLICY), repo_id="fixture", base_sha="a" * 40, head_sha="f" * 40, read_base=_values(head_ok), read_head=_values(wordy), changed_paths=["config/approval.yaml"])
    assert next(r for r in rep["results"] if r["rule_id"] == "rule-capex-finance-config")["reason"] == "not_numeric" and rep["conclusion"] == "incomplete"


def test_a_decided_target_rule_is_the_yardstick_and_the_moved_policy_or_wrong_repository_evaluates_nothing():
    target = copy.deepcopy(next(r for r in RULES if r["rule_id"] == "rule-standard-manager-review"))
    target.update(rule_id="rule-standard-manager-review-target", authority="TARGET", condition=dict(target["condition"], value=1500000))
    policy2 = {"snapshot_id": "accepted-2", "business_revision": 2, "rules": RULES + [target]}
    head = {STD: 1500000, CAPEX: 2000000}
    rep = ci.run(policy2, mapping_for(policy2), repo_id="fixture", base_sha="a" * 40, head_sha="b" * 40, read_base=_values({}), read_head=_values(head), changed_paths=["config/approval.yaml"])
    assert rep["conclusion"] == "aligned" and {r["rule_id"] for r in rep["results"]} == {"rule-standard-manager-review-target", "rule-capex-finance-config"}
    moved = ci.run(policy2, mapping_for(POLICY), repo_id="fixture", base_sha="a" * 40, head_sha="b" * 40, read_base=_values({}), read_head=_values(head), changed_paths=[])
    assert moved["conclusion"] == "incomplete" and moved["results"] == [] and moved["incomplete_reasons"][0].startswith("policy_moved") and moved["would_block"] is True
    wrong = ci.run(policy2, mapping_for(policy2, repo_id="other"), repo_id="fixture", base_sha="a" * 40, head_sha="b" * 40, read_base=_values({}), read_head=_values(head), changed_paths=[])
    assert wrong["conclusion"] == "incomplete" and wrong["incomplete_reasons"][0].startswith("wrong_repository")


def test_a_stored_report_is_verified_by_hash_and_a_moved_head_makes_it_stale():
    head = {STD: 1000000, CAPEX: 2000000}
    rep = ci.run(POLICY, mapping_for(POLICY), repo_id="fixture", base_sha="a" * 40, head_sha="b" * 40, read_base=_values(head), read_head=_values(head), changed_paths=[])
    assert ci.verify(rep, current_head_sha="b" * 40) == []
    assert ci.verify(rep, current_head_sha="c" * 40) == ["stale_head: the repository head moved after this check"]
    forged = copy.deepcopy(rep)
    forged["results"][0]["status"] = "aligned"
    forged["conclusion"] = "aligned"
    assert "report_hash_mismatch" in ci.verify(forged)


# ---- the real command line over a fixture repository -------------------------------------------------------------------------------------------------------------------------

def test_cli_reports_aligned_then_drift_over_a_fixture_repository_and_verifies_the_stored_report(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD")
    policy_file, mapping_file, out = tmp_path / "policy.json", tmp_path / "mapping.json", tmp_path / "report.json"
    policy_file.write_text(json.dumps(POLICY), encoding="utf-8")
    mapping_file.write_text(json.dumps(mapping_for(POLICY)), encoding="utf-8")
    head = commit(repo, "# docs only" + chr(10), path="README.md")
    r = cli("--repo", str(repo), "--repo-id", "fixture", "--base", base, "--head", head, "--policy", str(policy_file), "--mapping", str(mapping_file), "--out", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["conclusion"] == "aligned" and {x["status"] for x in rep["results"]} == {"no_impact"} and rep["head_sha"] == head and "ADVISORY aligned" in r.stdout
    assert cli("verify", "--report", str(out), "--repo", str(repo)).returncode == 0
    drifted = commit(repo, YAML.replace("standard_manager_threshold_jpy: 1000000", "standard_manager_threshold_jpy: 1500000"))
    assert cli("verify", "--report", str(out), "--repo", str(repo)).returncode == 3                                       # head moved after the check: stale
    r = cli("--repo", str(repo), "--repo-id", "fixture", "--base", base, "--head", drifted, "--policy", str(policy_file), "--mapping", str(mapping_file), "--out", str(out))
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert r.returncode == 2 and rep["conclusion"] == "drift" and next(x for x in rep["results"] if x["rule_id"] == "rule-standard-manager-review")["introduced_by_diff"] is True
    gone = commit(repo, None)
    r = cli("--repo", str(repo), "--repo-id", "fixture", "--base", base, "--head", gone, "--policy", str(policy_file), "--mapping", str(mapping_file), "--out", str(out))
    assert r.returncode == 3 and json.loads(out.read_text(encoding="utf-8"))["conclusion"] == "incomplete"               # the mapped file was deleted: never compliant
    forged = json.loads(out.read_text(encoding="utf-8"))
    forged["conclusion"] = "aligned"
    out.write_text(json.dumps(forged), encoding="utf-8")
    v = cli("verify", "--report", str(out))
    assert v.returncode == 3 and "report_hash_mismatch" in v.stdout
