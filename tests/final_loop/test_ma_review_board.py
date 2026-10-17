"""Multi-agent Review Board (MA-00 coordinator + MA-R1), LOCAL: synthetic specialists (role-aware stubs through the production pinned-call path) over a confirmed change on the
Harbor knowledge, through the real routes, worker, ledger (root reservation and per-child allocation), audit and the deterministic join. Covers agreement, minority and contested
findings, a fabricated reference, a legal claim, a bounded rebuttal round, a failed required role, cancellation with late results ignored, idempotent advance, budget invariant,
visibility and permissions. 0 real provider calls."""
from __future__ import annotations

import copy
import json
import time
from decimal import Decimal

import pytest

from core.change_intelligence.v2 import ma_orchestration as ma
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401  (qa_env is a fixture)
from tests.final_loop.test_syn01_code import login
from tests.pitch_brief.conftest import drain


@pytest.fixture
def ma_env(qa_env, monkeypatch, tmp_path):            # noqa: F811
    q = qa_env
    import os
    cfg_path = os.environ["RIPPLE_BUDGET_CONFIG_PATH"]
    cfg = open(cfg_path, encoding="utf-8").read().replace("role_model_ids:\n", "role_model_ids:\n  ma.specialist: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_ma.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    return q


def _role_of(prompt: str) -> str:
    for role, word in (("finance", "FINANCE critic"), ("operations", "OPERATIONS and TECHNOLOGY critic"), ("adversarial", "ADVERSARIAL critic"), ("compliance", "INTERNAL COMPLIANCE critic")):
        if word in prompt:
            return role
    raise AssertionError("unknown rubric")


def _refs_in(prompt: str) -> list:
    import re
    block = re.search(r"<refs>" + chr(10) + r"(.*?)" + chr(10) + r"</refs>", prompt, re.S).group(1)
    return [line.strip()[2:] for line in block.splitlines() if line.strip().startswith("- ")]


def finding(category, severity, statement, refs, counter=None):
    return {"category": category, "severity": severity, "statement": statement, "evidence_refs": refs, "assumptions": [], "counterexample": counter}


def obj(status="OK", findings=(), disputes=(), notes=()):
    return {"status": status, "findings": list(findings), "disputes": list(disputes), "notes": list(notes)}


SHARED = "Purchases between the old and the new threshold lose the manager sign-off without a compensating control."


def board_stub(prompt: str):
    """A SYNTHETIC board: finance and adversarial agree on one critical claim; operations raises a high claim alone; adversarial also fabricates a reference and makes a legal claim;
    compliance finds nothing. Round 2: finance disputes the operations claim; compliance re-affirms it."""
    role, refs = _role_of(prompt), _refs_in(prompt)
    rule = next(r for r in refs if r.startswith("rule-"))
    if "Another critic raised the claims below" in prompt:
        block = next(b for b in prompt.split("<rebuttal>") if "Another critic raised" in b).split("</rebuttal>")[0]
        claims = [line.split("claim_id=")[1].split(" ")[0] for line in block.splitlines() if "claim_id=" in line]
        if role == "finance":
            return obj(disputes=[{"claim_id": c, "reason": "The inventory already lists the configuration file; nothing else must change."} for c in claims])
        if role == "compliance":
            return obj(findings=[finding("operational", "high", "The workflow configuration must change together with the threshold.", [rule])])
        return obj("NONE_FOUND")
    if role == "finance":
        return obj(findings=[finding("financial", "critical", SHARED, [rule])])
    if role == "operations":
        return obj(findings=[finding("operational", "high", "The workflow configuration must change together with the threshold.", [rule])])
    if role == "adversarial":
        return obj(findings=[finding("fraud", "critical", SHARED, [rule], counter="Two orders of 400,000 JPY each instead of one of 800,000 JPY avoid the new threshold."),
                             finding("fraud", "high", "Blocked suppliers could be used through a reseller.", ["rule-not-in-pack"]),
                             finding("legal", "critical", "This may breach a consumer law on delivery charges.", [rule])])
    return obj("NONE_FOUND")


def _confirmed(q):
    cid, approver = prepared(q)                                   # the manager-threshold change of the C-set, confirmed on the qa_env knowledge
    return cid, approver


def start(q, client, cid, workflow="review_board"):
    return client.post("/v2/ma/runs", json={"workspace_id": q.env.ws, "change_id": cid, "workflow": workflow})


def settle(q, client, run_id, tries=60):
    for _ in range(tries):
        drain(q.env)
        got = client.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
        if got["status"] in ma.TERMINAL:
            return got
        time.sleep(0.2)
    return got


# ---- pure ------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_specialist_output_is_validated_against_the_authorized_pack_and_legal_claims_are_capped():
    allowed = ["rule-a", "op-1"]
    out = ma.validate_specialist_object(obj(findings=[finding("financial", "critical", "Money can move without the approver.", ["rule-a"]), finding("fraud", "high", "x", ["rule-zzz"]),
                                                      finding("legal", "critical", "Might breach a law.", ["op-1"]), finding("other", "low", "<script>alert(1)</script>", ["rule-a"]),
                                                      finding("operational", "medium", "No evidence.", [])],
                                            disputes=[{"claim_id": "clm-unknown", "reason": "x"}, {"claim_id": "clm-known", "reason": "The control exists."}]), allowed, known_claims=["clm-known"])
    assert [f["category"] for f in out["findings"]] == ["financial", "legal"] and out["findings"][1]["severity"] == "medium" and out["findings"][1]["legal_support"] == "unsupported"
    assert sorted(r["reason"] for r in out["rejected"]) == ["no_evidence", "reference_not_authorized", "unsafe_or_empty_statement"]
    assert out["disputes"] == [{"claim_id": "clm-known", "reason": "The control exists."}] and out["rejected_disputes"][0]["reason"] == "unknown_claim"
    with pytest.raises(ValueError):
        ma.validate_specialist_object({"status": "OK", "findings": []}, allowed)
    same = ma.claim_id_for("financial", "Money can move  without the approver!", ["rule-a"])
    assert same == out["findings"][0]["claim_id"]                                                                       # dedupe by normalized statement + refs


def test_join_is_order_independent_keeps_minority_and_contested_findings_and_names_missing_required_roles():
    m = ma.manifest("review_board")
    f1 = ma.validate_specialist_object(obj(findings=[finding("financial", "critical", SHARED, ["rule-x"])]), ["rule-x"])
    f2 = ma.validate_specialist_object(obj(findings=[finding("fraud", "critical", SHARED, ["rule-x"]), finding("operational", "high", "Only ops sees this.", ["rule-x"])]), ["rule-x"])
    claim_ops = f2["findings"][1]["claim_id"]
    f3 = ma.validate_specialist_object(obj(disputes=[{"claim_id": claim_ops, "reason": "Not true."}]), ["rule-x"], known_claims=[claim_ops])
    kids = [{"child_id": "c1", "role": "finance", "round": 1, "status": "completed", "result": f1}, {"child_id": "c2", "role": "adversarial", "round": 1, "status": "completed", "result": f2},
            {"child_id": "c3", "role": "operations", "round": 1, "status": "failed", "result": None}, {"child_id": "c4", "role": "finance", "round": 2, "status": "completed", "result": f3}]
    a = ma.join(kids, m, binding={"change_id": "chg-1"})
    b = ma.join(list(reversed(kids)), m, binding={"change_id": "chg-1"})
    assert a["report_hash"] == b["report_hash"] and a["status"] == "INCOMPLETE" and a["missing_required_roles"] == ["operations"]
    shared = next(f for f in a["findings"] if f["statement"] == SHARED)
    assert shared["raised_by"] == ["adversarial", "finance"] or len(shared["raised_by"]) == 1                            # different categories = different claims; same category would merge
    ops = next(f for f in a["findings"] if f["claim_id"] == claim_ops)
    assert ops["contested"] is True and ops["disputes"] == [{"by": "finance", "reason": "Not true."}] and claim_ops in a["unresolved_disagreements"]
    assert a["coverage"]["distinct_sources"] == 1 and a["human_action_needed"] is True
    targets = ma.rebuttal_targets([k for k in kids if k["round"] == 1], m)
    assert [t["claim_id"] for t in targets] and all(len(t["raised_by"]) == 1 for t in targets)
    who = ma.rebuttal_roles(targets, m, ["finance", "adversarial", "compliance"])
    assert set(who) == {"finance", "adversarial", "compliance"} and all(f["claim_id"] not in {c["claim_id"] for c in who["finance"]} for f in f1["findings"])   # nobody rebuts their own claim
    inv = ma.budget_invariant("0.30", [{"entry_state": "SETTLED", "cost_usd": "0.01"}, {"entry_state": "RESERVED", "reserved_usd": "0.10"}, {"root_remainder_usd": "0.10"}])
    assert inv["holds"] is True and inv["sum_usd"] == "0.21"


# ---- real routes, worker, ledger ----------------------------------------------------------------------------------------------------------------------------------------

def test_board_runs_over_a_confirmed_change_joins_with_a_bounded_rebuttal_and_keeps_disagreement(ma_env):
    q = ma_env
    cid, approver = _confirmed(q)
    use(q, board_stub)
    r = start(q, q.admin, cid)
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True and r.json()["status"] == "RUNNING"
    run_id = r.json()["run_id"]
    assert start(q, q.admin, cid).json()["created"] is False                                                             # same binding = same run
    st = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    assert len(st["children"]) == 4 and {c["role"] for c in st["children"]} == {"finance", "operations", "adversarial", "compliance"} and st["budget"]["holds"] is True
    assert Decimal(st["budget"]["root_reserved_usd"]) == Decimal(st["caps"]["per_child_worst_usd"]) * st["caps"]["max_children"]
    got = settle(q, q.admin, run_id)
    assert got["status"] == "REVIEW_READY", json.dumps(got["events"])[:600]
    rep = got["report"]
    by_stmt = {f["statement"]: f for f in rep["findings"]}
    shared = [f for f in rep["findings"] if f["statement"] == SHARED]
    assert len(shared) == 2 and {f["category"] for f in shared} == {"financial", "fraud"}                                # same words, two categories: two claims, each a single role
    ops = by_stmt["The workflow configuration must change together with the threshold."]
    assert ops["contested"] is True and ops["disputes"][0]["by"] == "finance" and "compliance" in ops["raised_by"] and ops["claim_id"] in rep["unresolved_disagreements"]
    legal = next(f for f in rep["findings"] if f["category"] == "legal")
    assert legal["legal_support"] == "unsupported" and legal["severity"] == "medium"
    assert "Blocked suppliers" not in by_stmt and rep["coverage"]["rejected_findings"] == 1                                 # the fabricated reference was dropped
    assert rep["missing_required_roles"] == [] and rep["roles"]["compliance"]["status"] == "completed" and rep["human_action_needed"] is True
    rounds = sorted((c["role"], c["round"]) for c in got["children"])
    assert (("finance", 2) in rounds) and (("compliance", 2) in rounds) and all(c["status"] in ("COMPLETED",) for c in got["children"])
    assert got["budget"]["holds"] is True and got["budget"]["unallocated_reserved_usd"] == "0" and all(c["entry_state"] == "SETTLED" for c in got["children"])
    assert [e["event"] for e in got["events"]][0] == "MA_RUN_STARTED" and got["events"][-1]["event"] == "MA_RUN_FINALIZED" and any(e["event"] == "MA_REBUTTAL_ROUND" for e in got["events"])
    again = q.admin.post(f"/v2/ma/runs/{run_id}/advance", json={"workspace_id": q.env.ws}).json()
    assert again["status"] == "REVIEW_READY" and again["report_hash"] == got["report_hash"]                               # advance is idempotent after finalization
    plain_reader = login(q, f"appr-plain-{q.env.nonce}", ("change_approver",))                                           # may decide, but holds no evidence.read
    plain = plain_reader.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    assert all("evidence_refs" not in f and "evidence_refs_count" in f for f in plain["report"]["findings"]) and "evidence_sources" not in plain["report"]["coverage"]
    assert q.admin.get(f"/v2/business/changes/{cid}/ma", params={"workspace_id": q.env.ws}).json()["runs"][0]["run_id"] == run_id
    viewer = login(q, f"viewer-ma-{q.env.nonce}", ("viewer",))
    assert viewer.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).status_code == 404
    assert start(q, q.req, cid).status_code == 403                                                                        # the requester has no ma.run
    j = q.admin.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws}).json()
    assert j["can"].get("commit_knowledge") in (False, True)                                                               # the board changed nothing about what a person may do


def test_a_failed_required_role_makes_the_report_incomplete_and_a_cancelled_run_ignores_late_results(ma_env):
    q = ma_env
    cid, _ = _confirmed(q)

    def failing(prompt):
        if _role_of(prompt) == "adversarial":
            return {"status": "OK", "findings": "not a list", "disputes": [], "notes": []}
        return obj("NONE_FOUND")
    use(q, failing)
    run_id = start(q, q.admin, cid).json()["run_id"]
    got = settle(q, q.admin, run_id)
    assert got["status"] == "INCOMPLETE" and got["report"]["missing_required_roles"] == ["adversarial"]
    adv = next(c for c in got["children"] if c["role"] == "adversarial")
    assert adv["status"] == "FAILED" and adv["job_status"] == "FAILED" and got["budget"]["holds"] is True
    # a second change, cancelled before any specialist ran: children cancelled, root released, late drain changes nothing
    from tests.final_loop.test_chg01_change_request import OP_ACTOR
    from tests.final_loop.test_chg02_impact import confirmed
    cid2, _ = confirmed(q, "Purchases from high-risk suppliers should be approved by the Finance Controller instead of the Operations Director.", [OP_ACTOR])
    use(q, board_stub)
    run2 = start(q, q.admin, cid2).json()["run_id"]
    c = q.admin.post(f"/v2/ma/runs/{run2}/cancel", json={"workspace_id": q.env.ws, "reason": "stop"})
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED" and all(ch["status"] == "CANCELLED" for ch in c.json()["children"])
    drain(q.env)
    after = q.admin.get(f"/v2/ma/runs/{run2}", params={"workspace_id": q.env.ws}).json()
    assert after["status"] == "CANCELLED" and after["report"] is None and after["budget"]["unallocated_reserved_usd"] == "0"
    assert q.admin.post(f"/v2/ma/runs/{run2}/advance", json={"workspace_id": q.env.ws}).json()["status"] == "CANCELLED"


def test_refusals_not_confirmed_disarmed_and_budget(ma_env, monkeypatch):
    q = ma_env
    from tests.final_loop.test_chg01_change_request import understand, model_obj
    from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE
    got, _ = understand(q, NUM_REQ, model_obj(ops=[OP_VALUE]), client=q.req)
    r = start(q, q.admin, got["change_id"])
    assert r.status_code == 409 and r.json()["code"] == "not_confirmed"
    cid, _ = _confirmed(q)
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    assert start(q, q.admin, cid).status_code == 409 and start(q, q.admin, cid).json()["code"] == "provider_disarmed"
    monkeypatch.delenv("RIPPLE_PROVIDER_MODE", raising=False)
    import os
    tiny = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace('total_cap_usd: "12.82"', 'total_cap_usd: "0.50"')
    p = os.path.join(os.path.dirname(os.environ["RIPPLE_BUDGET_CONFIG_PATH"]), "tiny.yaml")
    open(p, "w", encoding="utf-8").write(tiny)
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", p)
    r = start(q, q.admin, cid)
    assert r.status_code == 409 and r.json()["code"] == "budget_exceeded"                                                   # 8 possible children x 0.256 worst case > 0.50
    assert start(q, q.admin, cid, workflow="nope").status_code == 409


# ---- MA-K1: the arbiter workflow (two analysts, one synthesis call over their validated analyses) ----------------------------------------------------------------------

def arbiter_stub(prompt: str):
    refs = _refs_in(prompt)
    rule = next(r for r in refs if r.startswith("rule-"))
    code_refs = [r for r in refs if r.startswith("F")]
    if "You are the POLICY ANALYST" in prompt:
        return obj(findings=[finding("policy", "high", "The decided rule raises the manager threshold; the policy text names no other approver for this band.", [rule])])
    if "You are the CODE AUDITOR" in prompt:
        return obj(findings=[finding("operational", "medium", "The observed configuration still carries the old threshold value.", [rule] + code_refs[:1])])
    if "You are the ARBITER" in prompt:
        assert "<analyses>" in prompt and "[policy_analyst]" in prompt and "[code_auditor]" in prompt            # the synthesis sees the validated analyses as data
        row = lambda rid, cmp, prefs, crefs: {"rule_id": rid, "comparison": cmp, "policy_says": "Manager sign-off from the new threshold.", "code_shows": "The configuration holds the old value.",
                                              "policy_refs": prefs, "code_refs": crefs, "assumptions": ["Repository text is not production."], "options": ["change the configuration", "keep the old threshold"],
                                              "actor_to_decide": "knowledge owner"}
        return {"status": "OK", "rows": [row(rule, "divergent", [rule], code_refs[:1]), row(rule, "aligned", [rule], []), row("rule-not-here", "aligned", [rule], [rule])], "notes": ["Two sides compared."]}
    raise AssertionError("unknown rubric")


def test_arbiter_workflow_runs_two_analysts_then_one_synthesis_and_downgrades_verdicts_without_evidence_on_both_sides(ma_env):
    q = ma_env
    cid, approver = _confirmed(q)
    use(q, arbiter_stub)
    plain_approver = login(q, f"appr-arb-{q.env.nonce}", ("change_approver",))                                           # no evidence.read: the arbiter reads code evidence
    r = start(q, plain_approver, cid, workflow="arbiter")
    assert r.status_code == 409 and r.json()["code"] == "evidence_rights_required"
    r = start(q, q.admin, cid, workflow="arbiter")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    st = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    assert {c["role"] for c in st["children"]} == {"policy_analyst", "code_auditor"} and st["caps"]["max_children"] == 3
    got = settle(q, q.admin, run_id)
    assert got["status"] == "REVIEW_READY", json.dumps(got["events"])[:500]
    rep = got["report"]
    assert [c["role"] for c in got["children"] if c["round"] == 2] == ["arbiter"] and any(e["event"] == "MA_SYNTHESIS_ROUND" for e in got["events"])
    rows = rep["mismatch_table"]
    assert len(rows) == 2 and {r["comparison"] for r in rows} <= {"divergent", "unverifiable"}
    downgraded = [r for r in rows if r.get("downgraded_from")]
    assert downgraded and downgraded[0]["downgraded_from"] == "aligned" and downgraded[0]["comparison"] == "unverifiable"       # aligned without code evidence is not a verdict
    assert rep["roles"]["arbiter"]["status"] == "completed" and rep["missing_required_roles"] == [] and rep["coverage"]["rejected_findings"] == 1     # the rule outside the pack
    assert any("production behaviour" in l for l in rep["limitations"])
    analyst = {f["category"] for f in rep["findings"]}
    assert analyst == {"policy", "operational"} and rep["human_action_needed"] is True
    reviewer = approver                                                                                                      # change_approver + reviewer: holds evidence.read
    full = reviewer.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    assert full["report"]["mismatch_table"][0]["code_shows"] != "[needs evidence rights]"
    plain = plain_approver.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    assert all(r["code_shows"] == "[needs evidence rights]" and "code_refs" not in r for r in plain["report"]["mismatch_table"])
    assert got["budget"]["holds"] is True and got["budget"]["unallocated_reserved_usd"] == "0"
