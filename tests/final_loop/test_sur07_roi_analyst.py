"""SUR-07 ROI analyst over a SYNTHETIC replay dataset, LOCAL: pure checks plus the real route, worker, budget and ledger on two accepted Harbor revisions (the manager threshold
moved by a TARGET rule) with a SYNTHETIC model (stub through the production pinned-call path). Covers statements whose numbers must be cited metric values, refused
ROI / saving / forecast wording, questions on declared assumptions only, sensitivity scenarios recomputed by the replay engine (never by the model), real data refused before
any job, records never echoed, idempotent requests and visibility. 0 real provider calls; model quality is NOT measured here; no real data was used."""
from __future__ import annotations

import copy
import re

import pytest

from core.change_intelligence.v2 import roi_analyst as ra
from core.change_intelligence.v2 import roi_replay
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401  (qa_env is a fixture)
from tests.final_loop.test_syn01_code import login
from tests.pitch_brief.conftest import drain

RECORDS = [{"id": f"s{i}", "time": "2026-03-01T00:00:00Z", "amount_jpy": a, "currency": "JPY", "category": "OFFICE", "process": "STANDARD", "supplier_risk": "LOW"}
           for i, a in enumerate([900_000, 1_200_000, 1_400_000, 1_600_000, 2_500_000])]
ASSUMPTIONS = {"minutes_per_approval": 20, "minutes_per_approval_range": [10, 30]}
LIMITS = {"max_statements": 3, "max_questions": 2, "max_scenarios": 2, "max_text_chars": 300}


def test_metric_table_and_checks_keep_only_cited_values_and_refuse_claims():
    rep = {"changes": {"count": 2, "value_jpy": "2600000", "approvals_delta": -2}, "effort": {"delta_hours": "-0.67", "basis": "x"}, "statement": "x", "report_hash": "h"}
    table = ra.metric_table(rep)
    assert table == {"changes.count": "2", "changes.value_jpy": "2600000", "changes.approvals_delta": "-2", "effort.delta_hours": "-0.67"}
    model = ra.validate_model_object({"status": "memo", "notes": [], "statements": [
        {"statement": "Two requests (2) change route, worth 2,600,000 JPY.", "metric_keys": ["changes.count", "changes.value_jpy"]},
        {"statement": "About 0.67 fewer handling hours.", "metric_keys": ["effort.delta_hours"]},
        {"statement": "This saves 2,600,000 JPY.", "metric_keys": ["changes.value_jpy"]},
        {"statement": "Seven requests change.", "metric_keys": ["changes.unknown"]},
        {"statement": "It moves 3 requests.", "metric_keys": ["changes.count"]}],
        "assumption_questions": [{"question": "Is 20 minutes per approval realistic?", "assumption": "minutes_per_approval"}, {"question": "x?", "assumption": "fte"}],
        "scenarios": [{"assumption": "minutes_per_approval", "value": 30, "rationale": "upper end"}, {"assumption": "hourly_cost", "value": 5000, "rationale": "x"},
                      {"assumption": "minutes_per_approval", "value": -1, "rationale": "x"}]}, LIMITS)
    out = ra.check(model, table, ["minutes_per_approval"], LIMITS)
    assert [k["statement"][:9] for k in out["kept"]] == ["Two reque", "About 0.6"]
    assert [r["reason"] for r in out["rejected"]] == ["roi_saving_or_forecast_claim", "metric_not_in_report", "figure_not_a_cited_metric:3", "assumption_not_declared",
                                                      "assumption_not_declared", "value_not_positive_number"]
    assert out["questions"][0]["assumption"] == "minutes_per_approval" and out["scenarios"] == [{"assumption": "minutes_per_approval", "value": "30", "rationale": "upper end", "origin": "model"}]


@pytest.fixture
def roi_env(qa_env, monkeypatch, tmp_path):                  # noqa: F811
    import os
    from infra.adapters.v2_business import business_view, commit_snapshot
    from tests.final_loop.test_kno05_accept import accept
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("role_model_ids:\n", "role_model_ids:\n  roi.analyst: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_roi.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    q = qa_env
    acc = business_view(q.env.store, q.env.tenant, q.env.ws)["accepted"]
    head = q.env.store.head(q.env.tenant, q.env.ws)
    target = copy.deepcopy(next(r for r in acc["rules"] if r["rule_id"] == "rule-standard-manager-review"))
    target.update(rule_id="rule-standard-manager-review-target", authority="TARGET", status="CONFIRMED", condition=dict(target["condition"], value=1500000),
                  provenance=target["provenance"] + [{"kind": "human_decision", "decision_id": "decision-roi-analyst"}], implementation_status="pending")
    commit_snapshot(q.env.store, q.env.tenant, q.env.ws, snapshot_id="snap-roi-analyst-2", actor_id=q.env.admin, lifecycle_state="PENDING_HUMAN", rules=acc["rules"] + [target],
                    relations=acc["relations"], gaps=acc["gaps"], decisions=acc["decisions"] + [{"decision_id": "decision-roi-analyst", "kind": "set_value", "quote": "Manager approval from 1,500,000 JPY.",
                                                                                                    "body": {"rule_id": "rule-standard-manager-review"}}],
                    expected_version=head.version, expected_business_revision=1, source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id,
                    decision_epoch=head.decision_epoch, idempotency_key="roi-analyst-2")
    assert accept(q.admin, q.env, acknowledged_open_gaps=True).status_code == 200
    return q


def analyst(prompt):
    metrics = dict(re.findall(r"- (\S+) = (\S+)", prompt.split("<metrics>\n")[1].split("</metrics>")[0]))
    assert "SYNTHETIC" in prompt and "2026-03-01" not in prompt and "amount_jpy" not in prompt                          # the records themselves are never sent, only the metric table
    assert "The declared assumptions are exactly these keys: minutes_per_approval, minutes_per_approval_range." in prompt and "hourly_cost" not in prompt   # v2
    return {"status": "memo", "notes": ["The dataset is small and covers one month only.", "Only 12 records were valid."],
            "statements": [{"statement": f"{metrics['changes.count']} requests leave manager review in the target version.", "metric_keys": ["changes.count"]},
                           {"statement": "The company saves money every month.", "metric_keys": ["changes.count"]},
                           {"statement": "About 12 hours are handled differently.", "metric_keys": ["effort.delta_hours"]}],
            "assumption_questions": [{"question": "Was 20 minutes per approval measured or estimated?", "assumption": "minutes_per_approval"}],
            "scenarios": [{"assumption": "minutes_per_approval", "value": 30, "rationale": "the upper end of the declared range"},
                          {"assumption": "hourly_cost", "value": 6000, "rationale": "not declared"}]}


def post(q, client, **body):
    base = {"workspace_id": q.env.ws, "records": RECORDS, "baseline_revision": 1, "target_revision": 2, "assumptions": ASSUMPTIONS, "label": "SYNTHETIC cohort A", "synthetic": True}
    return client.post("/v2/business/roi/analyst", json={**base, **body})


def test_analyst_memo_cites_engine_metrics_and_scenarios_are_computed_by_the_engine(roi_env):
    q = roi_env
    model = use(q, analyst)
    r = post(q, q.admin)
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    drain(q.env)
    got = q.admin.get(f"/v2/business/roi/analyst/{jid}", params={"workspace_id": q.env.ws}).json()
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, jid)
    assert got["status"] == "done", {k: rec.get(k) for k in ("status", "execution_status", "execution_reason", "attempts")}
    assert got["synthetic"] is True and got["label"].startswith("Analyst memo over a replay of a SYNTHETIC dataset") and "records" not in got
    assert [s["metric_keys"] for s in got["statements"]] == [["changes.count"]] and got["statements"][0]["values"] == {"changes.count": "2"}
    assert sorted(x["reason"] for x in got["rejected"]) == ["assumption_not_declared", "figure_not_a_cited_metric:12", "note_has_number", "roi_saving_or_forecast_claim"]
    assert got["notes"] == [{"note": "The dataset is small and covers one month only.", "origin": "model"}]
    sc = got["scenarios"]
    assert len(sc) == 1 and sc[0]["value"] == "30" and sc[0]["engine"]["computed_by"] == "roi_replay engine"
    from infra.adapters.v2_business import business_view, read_snapshot, _accepted_by_revision
    with read_snapshot(q.env.store) as cur:
        b, t = (_accepted_by_revision(cur, q.env.tenant, q.env.ws, n) for n in (1, 2))
    direct = roi_replay.replay(RECORDS, {"label": "business revision 1", "rules": b["rules"], "gaps": b["gaps"]}, {"label": "business revision 2", "rules": t["rules"], "gaps": t["gaps"]},
                               assumptions={"minutes_per_approval": 30}, dataset_label="SYNTHETIC cohort A")
    assert sc[0]["engine"]["metrics"]["effort.delta_hours"] == ra.metric_table(direct)["effort.delta_hours"] and sc[0]["engine"]["report_hash"] == direct["report_hash"]
    assert got["metrics"]["changes.count"] == "2" and got["ai_outcome"]["contribution"] == "retained"
    again = post(q, q.admin)
    drain(q.env)
    assert again.json()["job_id"] == jid and model.calls == 1


def test_real_data_is_refused_before_any_job_and_visibility_holds(roi_env):
    q = roi_env
    model = use(q, analyst)
    assert post(q, q.admin, synthetic=False).json()["code"] == "real_data_not_granted"
    assert post(q, q.admin, label="Q3 purchase orders").json()["code"] == "real_data_not_granted"
    assert post(q, q.admin, target_revision=9).json()["code"] == "unknown_reference"
    assert post(q, q.admin, records=[]).status_code == 400 and post(q, q.admin, attempt=4).status_code == 400
    assert model.calls == 0
    jid = post(q, q.admin).json()["job_id"]
    drain(q.env)
    viewer = login(q, f"viewer-roi-{q.env.nonce}", ("viewer",))
    assert post(q, viewer).status_code == 403
    assert viewer.get(f"/v2/business/roi/analyst/{jid}", params={"workspace_id": q.env.ws}).status_code == 404       # not the asker, no knowledge.confirm
    use(q, lambda p: {"status": "memo", "statements": "x", "assumption_questions": [], "scenarios": [], "notes": []})
    bad = post(q, q.admin, attempt=2).json()["job_id"]
    drain(q.env)
    assert q.admin.get(f"/v2/business/roi/analyst/{bad}", params={"workspace_id": q.env.ws}).json()["reason_code"] == "schema_violation"
