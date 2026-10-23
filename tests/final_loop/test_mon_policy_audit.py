"""MON-POL (typed policy decision over the accepted knowledge) and MON-AUD (scoped audit package with manifest and standalone verifier), through the real routes and store.
No model: the evaluator is deterministic over the Final condition grammar; the package is a projection of stored rows. Pure cases first, then the routes on the QRY-03 workspace
(accepted revision 1 with a decided CAPEX rule and two open questions on the emergency rule).
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile

import pytest

from core.change_intelligence.v2 import policy_decision as pd
from tests.final_loop.test_chg02_impact import RULES
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry02_retrieval import DISTRACTORS, KNOWLEDGE
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)

PIN = {"snapshot_id": "s", "business_revision": 1}
# every field the fixture rules use; the three distractors whose outcome is "rejected_if_*" (an ambiguous, unclassified kind) are given facts that do NOT trigger them
STANDARD = {"process": "STANDARD", "request.net_amount_jpy": 1200000, "request.category": "OFFICE", "supplier.risk": "LOW", "order.net_goods_jpy": 1, "invoice.price_difference_jpy": 0,
            "invoice.age_days": 1, "supplier.status": "ACTIVE", "approver.is_requester": "true", "request.quantity": 0, "request.currency": "USD", "event.kind": "NONE", "expense.travel_jpy": 0,
            "purchase.category": "OFFICE", "supplier.is_new": "false", "request.budget_quarter": "PREVIOUS", "receipt.matched": "true"}


def test_conditions_evaluate_with_the_grammar_and_say_what_is_missing():
    cond = {"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "value": 2000000, "unit": "JPY"}]}
    assert pd.evaluate_condition(cond, {"request.category": "CAPEX", "request.net_amount_jpy": "2,500,000"}) == (True, [])
    assert pd.evaluate_condition(cond, {"request.category": "OFFICE"}) == (False, [])                                                    # one false child decides an `all`
    assert pd.evaluate_condition(cond, {"request.category": "CAPEX"}) == (None, ["fact_missing:request.net_amount_jpy"])
    assert pd.evaluate_condition({"not": {"field": "approver.is_requester", "op": "eq", "value": "true"}}, {"approver.is_requester": "false"}) == (True, [])
    assert pd.evaluate_condition({"field": "x", "op": "in", "value": ["A", "B"]}, {"x": "b"}) == (True, [])
    assert pd.evaluate_condition({"field": "x", "op": "gt", "value": 5}, {"x": "many"}) == (None, ["fact_not_comparable:x"])
    assert pd.evaluate_condition(None, {}) == (None, ["rule_condition_unknown"])


def test_decision_is_conservative_and_a_decided_target_replaces_the_observed_rule():
    rules = list(RULES.values())
    out = pd.decide(rules, [], dict(STANDARD, **{"request.net_amount_jpy": 1200000}), pin=PIN)
    assert out["decision"] == "REQUIRES_APPROVAL" and out["obligations"][0]["by"] == "role-manager" and out["obligations"][0]["rule_id"] == "rule-standard-manager-review"
    below = pd.decide(rules, [], dict(STANDARD, **{"request.net_amount_jpy": 500000}), pin=PIN)
    assert below["decision"] == "ALLOW" and below["obligations"] == []
    missing = pd.decide(rules, [], {"process": "STANDARD", "request.category": "OFFICE"}, pin=PIN)
    assert missing["decision"] == "INDETERMINATE" and any("fact_missing:request.net_amount_jpy" in r for r in missing["indeterminate_reasons"])
    target = dict(RULES["rule-capex-finance-config"], rule_id="rule-capex-finance-config-target", authority="TARGET", status="CONFIRMED",
                  condition={"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "value": 9000000, "unit": "JPY"}]},
                  provenance=[{"kind": "human_decision", "decision_id": "d1"}])
    decided = pd.decide(rules + [target], [], dict(STANDARD, **{"request.category": "CAPEX", "request.net_amount_jpy": 3000000}), pin=PIN)
    assert decided["superseded_observed_rules"] == ["rule-capex-finance-config"]
    assert all(r["rule_id"] != "rule-capex-finance-config" for r in decided["applicable_rules"]) and any(r["rule_id"] == "rule-capex-finance-config-target" and r["condition"] == "does_not_hold" for r in decided["applicable_rules"])
    gap = {"gap_id": "g", "status": "OPEN", "affected_rule_ids": ["rule-standard-manager-review"]}
    open_q = pd.decide(rules, [gap], dict(STANDARD), pin=PIN)
    assert open_q["decision"] == "INDETERMINATE" and any("open_question" in r for r in open_q["indeterminate_reasons"])
    emergency = pd.decide(rules, [], {"process": "EMERGENCY", "request.net_amount_jpy": 10, "request.category": "OFFICE", "supplier.risk": "LOW"}, pin=PIN)
    assert emergency["decision"] == "INDETERMINATE" and any("rule_condition_unknown" in r or "rule_fields_unknown" in r for r in emergency["indeterminate_reasons"])
    with pytest.raises(ValueError):
        pd.decide(rules, [], {}, pin=PIN)
    exc_rule = dict(RULES["rule-standard-manager-review"], exceptions=[{"exception_id": "e1", "text": "except during audit season", "condition": None}])
    unevaluable = pd.decide([exc_rule], [], dict(STANDARD), pin=PIN)
    assert unevaluable["decision"] == "INDETERMINATE" and "exception_without_condition" in unevaluable["indeterminate_reasons"][0]
    assert pd.classify_outcome("rejected") == "DENY" and pd.classify_outcome("review_required") == "REQUIRES_APPROVAL" and pd.classify_outcome("free_shipping") == "EFFECT" and pd.classify_outcome("unknown") == "UNCLASSIFIED"
    assert pd.classify_outcome("rejected_if_invalid") == "UNCLASSIFIED"                                                            # a conditional kind names a condition, not an effect


# ---- routes ------------------------------------------------------------------------------------------------------------------------------------------------------------------

def decide(q, client, facts, ws_id=None):
    return client.post("/v2/business/policy/decide", json={"workspace_id": ws_id or q.env.ws, "facts": facts})


def test_policy_decision_route_binds_to_the_accepted_version_redacts_ids_for_plain_readers_and_is_audited(qa_env):
    q = qa_env
    full = dict(STANDARD)
    r = decide(q, q.admin, full)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["decision"] == "REQUIRES_APPROVAL", out["indeterminate_reasons"]
    assert out["obligations"][0]["by"] == "role-manager" and out["pin"]["business_revision"] == 1 and out["facts_digest"] and out["decision_hash"]
    assert out["superseded_observed_rules"] == ["rule-capex-finance-config"]                                                               # the decided CAPEX target replaces the observed rule
    plain = decide(q, q.req, full).json()
    assert plain["decision"] == "REQUIRES_APPROVAL" and "rule_id" not in plain["applicable_rules"][0] and plain["superseded_observed_rules"] == 1 and plain["decision_hash"] == out["decision_hash"]
    blocked = decide(q, q.admin, dict(full, **{"supplier.status": "BLOCKED"})).json()
    assert blocked["decision"] == "DENY"                                                                                                   # the blocked-supplier rule forbids it
    ambiguous = decide(q, q.admin, dict(full, **{"request.quantity": 3})).json()
    assert ambiguous["decision"] == "INDETERMINATE" and any("outcome_kind_unclassified" in r for r in ambiguous["indeterminate_reasons"])  # a triggered rule with an ambiguous outcome kind decides nothing
    capex = decide(q, q.admin, dict(full, **{"request.category": "CAPEX", "request.net_amount_jpy": 2500000})).json()
    assert capex["decision"] == "REQUIRES_APPROVAL" and {o["by"] for o in capex["obligations"]} == {"role-manager", "role-finance"}
    assert decide(q, q.admin, {"process": "STANDARD"}).json()["decision"] == "INDETERMINATE"
    assert decide(q, q.admin, {"process": "EMERGENCY", "request.net_amount_jpy": 5}).json()["decision"] == "INDETERMINATE"                 # the emergency rule has unknown actor/condition
    assert decide(q, q.admin, {}).status_code == 400 and decide(q, q.admin, "x").status_code == 400
    assert decide(q, q.admin, full, ws_id="ws-not-mine").status_code in (403, 404)
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*), max(payload->>'decision') FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='POLICY_DECISION'", (q.env.tenant, q.env.ws))
        n, last = cur.fetchone()
    assert n >= 5 and last in ("REQUIRES_APPROVAL", "INDETERMINATE")
    ctx = q.admin.post("/v2/business/context", json={"workspace_id": q.env.ws, "query": "Who approves 1.2 million?"})
    assert ctx.status_code == 200 and "decision" not in ctx.json()                                                                          # the context route stays an evidence pack, no ALLOW


def test_audit_package_verifies_detects_tampering_and_redacts_for_plain_readers(qa_env, tmp_path):
    q = qa_env
    r = q.admin.post("/v2/business/audit/export", json={"workspace_id": q.env.ws})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/zip"), r.text[:200]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"index.md", "knowledge_versions.json", "decisions.json", "change_decisions.json", "code_decisions.json", "jobs.json", "audit_events.json", "coverage.json", "MANIFEST.json", "verify_audit_pack.py"} <= names
    manifest = json.loads(z.read("MANIFEST.json"))
    versions = json.loads(z.read("knowledge_versions.json"))
    decisions = json.loads(z.read("decisions.json"))
    coverage = json.loads(z.read("coverage.json"))
    assert versions[0]["business_revision"] == 1 and any(d["kind"] == "answer_gap" for d in decisions) and coverage["evidence_rights"] is True and coverage["redactions"] == []
    assert coverage["not_available"]["git_commit"] and "NOT a digital signature" in coverage["integrity"]
    pkg = tmp_path / "pack.zip"
    pkg.write_bytes(r.content)
    verifier = tmp_path / "verify_audit_pack.py"
    verifier.write_text(z.read("verify_audit_pack.py").decode("utf-8"), encoding="utf-8")
    ok = subprocess.run([sys.executable, str(verifier), str(pkg)], capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and ok.stdout.strip().startswith("OK"), ok.stdout + ok.stderr
    # tamper one byte of one file -> the verifier fails; an added file is reported as unlisted
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(pkg) as src, zipfile.ZipFile(tampered, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "decisions.json":
                data = data.replace(b'"answer_gap"', b'"answer_xxx"', 1)
            dst.writestr(item, data)
        dst.writestr("extra.txt", "not in the manifest")
    bad = subprocess.run([sys.executable, str(verifier), str(tampered)], capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1 and "hash differs: decisions.json" in bad.stdout and "unlisted file: extra.txt" in bad.stdout
    # a plain reader gets the package without evidence references; the export itself is audited
    plain = q.req.post("/v2/business/audit/export", json={"workspace_id": q.env.ws})
    assert plain.status_code == 200
    pc = json.loads(zipfile.ZipFile(io.BytesIO(plain.content)).read("coverage.json"))
    assert pc["evidence_rights"] is False and pc["redactions"]
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='AUDIT_PACK_EXPORTED'", (q.env.tenant, q.env.ws))
        assert cur.fetchone()[0] == 2
    assert q.admin.post("/v2/business/audit/export", json={"workspace_id": q.env.ws, "change_id": "chg-nope"}).status_code == 404
    assert q.admin.post("/v2/business/audit/export", json={"workspace_id": q.env.ws, "change_id": "not-a-change"}).status_code == 400
    again = q.admin.post("/v2/business/audit/export", json={"workspace_id": q.env.ws})
    m2 = json.loads(zipfile.ZipFile(io.BytesIO(again.content)).read("MANIFEST.json"))
    volatile = {"audit_events.json", "coverage.json", "index.md"}                                                                           # the audit log grows with each export, and these three describe it
    files1 = {f["file"]: f["sha256"] for f in manifest["files"] if f["file"] not in volatile}
    files2 = {f["file"]: f["sha256"] for f in m2["files"] if f["file"] not in volatile}
    assert files1 == files2                                                                                                                 # deterministic for unchanged rows


# ---- MON-ROI: one scoped compare request through the real route (engine evidence; real data and the data owner's acceptance stay OPEN) ----------------------------------------

def _roi(q, client, **body):
    return client.post("/v2/business/roi/replay", json={"workspace_id": q.env.ws, **body})


def test_roi_replay_route_compares_two_accepted_revisions_over_the_callers_records_and_is_audited(qa_env):
    import copy
    import psycopg2
    from infra.adapters.v2_business import business_view, commit_snapshot
    from tests.final_loop.test_kno05_accept import accept
    q = qa_env
    records = [{"id": f"r{i}", "time": "2026-03-01T00:00:00Z", "amount_jpy": a, "currency": "JPY", "category": "OFFICE", "process": "STANDARD", "supplier_risk": "LOW"}
               for i, a in enumerate([900_000, 1_200_000, 1_400_000, 1_600_000, 2_500_000])]
    assumptions = {"minutes_per_approval": 20, "minutes_per_approval_range": [10, 30]}
    same = _roi(q, q.admin, records=records, baseline_revision=1, target_revision=1, assumptions=assumptions, label="same")
    assert same.status_code == 200, same.text
    assert same.json()["changes"]["count"] == 0 and same.json()["denominators"]["cohort"] == 5 and same.json()["pin"]["baseline"]["business_revision"] == 1
    # a decided change becomes revision 2: the manager threshold moves from 1,000,000 to 1,500,000 JPY (a TARGET rule next to the OBSERVED one)
    acc = business_view(q.env.store, q.env.tenant, q.env.ws)["accepted"]
    head = q.env.store.head(q.env.tenant, q.env.ws)
    target = copy.deepcopy(next(r for r in acc["rules"] if r["rule_id"] == "rule-standard-manager-review"))
    target.update(rule_id="rule-standard-manager-review-target", authority="TARGET", status="CONFIRMED", condition=dict(target["condition"], value=1500000),
                  provenance=target["provenance"] + [{"kind": "human_decision", "decision_id": "decision-roi-test"}], implementation_status="pending")
    commit_snapshot(q.env.store, q.env.tenant, q.env.ws, snapshot_id="snap-roi-2", actor_id=q.env.admin, lifecycle_state="PENDING_HUMAN", rules=acc["rules"] + [target],
                    relations=acc["relations"], gaps=acc["gaps"], decisions=acc["decisions"] + [{"decision_id": "decision-roi-test", "kind": "set_value", "quote": "Manager approval from 1,500,000 JPY.", "body": {"rule_id": "rule-standard-manager-review"}}],
                    expected_version=head.version, expected_business_revision=1, source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id,
                    decision_epoch=head.decision_epoch, idempotency_key="roi-2")
    assert accept(q.admin, q.env, acknowledged_open_gaps=True).status_code == 200
    rep = _roi(q, q.admin, records=records, baseline_revision=1, target_revision=2, assumptions=assumptions, label="threshold")
    assert rep.status_code == 200, rep.text
    r = rep.json()
    assert r["changes"]["count"] == 2 and r["changes"]["value_jpy"] == "2600000" and r["changes"]["approvals_delta"] == -2 and r["effort"]["delta_hours"] == "-0.67" and r["cost"] == "not_estimated"
    assert r["pin"]["target"]["business_revision"] == 2 and r["denominators"]["cohort"] == 5 and r["denominators"]["replayed"] + r["denominators"]["unsupported"] == 5
    assert "records" not in r and r["dataset"]["hash"]                                                                             # the caller's rows are not echoed or stored
    with psycopg2.connect(q.env.dsn) as cx, cx.cursor() as cur:
        cur.execute("SELECT count(*), array_agg(payload->>'report_hash') FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='ROI_REPLAY_RUN'", (q.env.tenant, q.env.ws))
        n, hashes = cur.fetchone()
    assert n == 2 and r["report_hash"] in hashes                                                  # (was max(): the lexically larger hash, not the latest run; corrected 2026-10-22)
    # refusals: unknown revision, missing assumption, oversized/empty cohort, a viewer-less stranger
    assert _roi(q, q.admin, records=records, baseline_revision=1, target_revision=9, assumptions=assumptions).status_code == 409
    assert _roi(q, q.admin, records=records, baseline_revision=1, target_revision=2, assumptions={}).status_code == 400
    assert _roi(q, q.admin, records=[], baseline_revision=1, target_revision=2, assumptions=assumptions).status_code == 400
    assert _roi(q, q.admin, records=records, baseline_revision="1", target_revision=2, assumptions=assumptions).status_code == 400
    anonymous = q.env.admin_client()
    anonymous.cookies.clear()
    assert _roi(q, anonymous, records=records, baseline_revision=1, target_revision=2, assumptions=assumptions).status_code in (401, 403)
