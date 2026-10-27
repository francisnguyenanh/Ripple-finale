"""HK-ROI: the ROI replay engine against an independently written oracle on frozen SYNTHETIC sets (tests/fixtures/roi_synthetic, README there).

Pure checks first (reproducible generation, oracle independence, development / acceptance leakage, case coverage, engine = oracle on every scenario), then the
acceptance split through the real route. Synthetic data: a simulation result, never an observed enterprise benefit; no provider is called."""
from __future__ import annotations

import ast
import hashlib
import json
import pathlib

import pytest

from core.change_intelligence.v2 import roi_replay as roi
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)

D = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "roi_synthetic"
CLASSES = {"normal", "boundary", "missing", "contradictory", "exception", "invalid", "outside_window"}


def _load(name):
    return json.loads((D / name).read_text(encoding="utf-8"))


def project(rep):
    """The report fields the oracle predicts, in the oracle's shape."""
    cov = rep["coverage"]
    return {"denominators": rep["denominators"], "baseline": rep["baseline"], "target": rep["target"], "changes": rep["changes"],
            "effort": {k: rep["effort"][k] for k in ("baseline_hours", "target_hours", "delta_hours", "delta_hours_range")},
            "unsupported_ids": sorted(u["id"] for u in rep["unsupported_rows"]), "unsupported_value_jpy": rep["unsupported_value_jpy"],
            "excluded": [[None if e["reason"] == "missing_id" else e["id"], e["reason"]] for e in rep["excluded"]],
            "coverage": {"baseline": cov["baseline"]["rules_evaluated"], "target": cov["target"]["rules_evaluated"],
                         "baseline_superseded": cov["baseline"]["rules_superseded_by_target"], "target_superseded": cov["target"]["rules_superseded_by_target"],
                         "baseline_not_evaluated": [[x["rule_id"], x["reason"]] for x in cov["baseline"]["rules_not_evaluated"]],
                         "target_not_evaluated": [[x["rule_id"], x["reason"]] for x in cov["target"]["rules_not_evaluated"]]},
            "cost": rep["cost"] if rep["cost"] == "not_estimated" else {"delta_cost_range": rep["cost"]["delta_cost_range"]}}


def _want(expected):
    return {k: v for k, v in expected.items() if k != "changed_ids"}


def test_the_sets_regenerate_byte_for_byte_from_the_seeds(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("roi_synthetic_generate", D / "generate.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    manifest = gen.main(tmp_path)
    assert manifest["sha256"] == _load("MANIFEST.json")["sha256"]
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((D / name).read_bytes()).hexdigest() == digest, name


def test_the_oracle_does_not_import_the_engine_or_the_product():
    tree = ast.parse((D / "oracle.py").read_text(encoding="utf-8"))
    imported = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imported <= {"__future__", "datetime", "decimal"}, imported


def test_development_and_acceptance_do_not_share_records_and_every_class_is_present():
    dev, acc = _load("dev.json"), _load("acceptance.json")
    assert dev["synthetic"] is True and acc["synthetic"] is True and dev["seed"] != acc["seed"]
    ids = lambda s: {str(r.get("id")) for r in s["records"] if r.get("id")}
    content = lambda s: {(r.get("time"), str(r.get("amount_jpy")), r.get("category"), r.get("process"), r.get("supplier_risk")) for r in s["records"]}
    assert not ids(dev) & ids(acc)
    assert not content(dev) & content(acc)
    for s in (dev, acc):
        assert set(s["cases"]) == CLASSES and len(s["cases"]) == len(s["records"])
    assert len(acc["records"]) >= 2000


@pytest.mark.parametrize("split", ["dev", "acceptance"])
@pytest.mark.parametrize("scenario", ["S1", "S2", "S3"])
def test_the_engine_reports_what_the_oracle_computed(split, scenario):
    sc, data = _load("scenarios.json"), _load(f"{split}.json")
    rep = roi.replay(data["records"], sc["baseline"], sc["targets"][scenario], assumptions=sc["assumptions"][scenario], window_from=sc["window"]["from"],
                     window_to=sc["window"]["to"], dataset_label=f"SYNTHETIC {split} {scenario}")
    got = json.loads(json.dumps(project(rep), default=str))
    want = _want(_load(f"expected_{split}.json")["scenarios"][scenario])
    for key in want:
        assert got[key] == want[key], key
    d = rep["denominators"]
    assert d["cohort"] == d["replayed"] + d["unsupported"] and d["records_in"] == d["cohort"] + len(rep["excluded"])
    assert rep["risk"] == {"payment_delay": "not_estimated", "loss": "not_estimated", "note": rep["risk"]["note"]}
    if scenario == "S3":                                                              # undecidable rows are listed and never become a saving
        assert d["unsupported"] > 0 and rep["changes"]["count"] == 0 and rep["changes"]["approvals_delta"] == 0


def test_the_acceptance_split_through_the_real_route_matches_the_oracle(qa_env):
    """S1 on the QRY-03 workspace: revision 1 = its accepted Harbor knowledge, revision 2 = the manager threshold moved to 1,500,000 JPY by a decision."""
    import copy
    from infra.adapters.v2_business import business_view, commit_snapshot
    from tests.final_loop.test_kno05_accept import accept
    q = qa_env
    sc, data = _load("scenarios.json"), _load("acceptance.json")
    acc = business_view(q.env.store, q.env.tenant, q.env.ws)["accepted"]
    head = q.env.store.head(q.env.tenant, q.env.ws)
    target = copy.deepcopy(next(r for r in acc["rules"] if r["rule_id"] == "rule-standard-manager-review"))
    target.update(rule_id="rule-standard-manager-review-target", authority="TARGET", status="CONFIRMED", condition=dict(target["condition"], value=1500000),
                  provenance=target["provenance"] + [{"kind": "human_decision", "decision_id": "decision-hk-roi"}], implementation_status="pending")
    commit_snapshot(q.env.store, q.env.tenant, q.env.ws, snapshot_id="snap-hk-roi-2", actor_id=q.env.admin, lifecycle_state="PENDING_HUMAN", rules=acc["rules"] + [target],
                    relations=acc["relations"], gaps=acc["gaps"],
                    decisions=acc["decisions"] + [{"decision_id": "decision-hk-roi", "kind": "set_value", "quote": "Manager approval from 1,500,000 JPY.",
                                                   "body": {"rule_id": "rule-standard-manager-review", "value": 1500000}}],
                    expected_version=head.version, expected_business_revision=1, source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id,
                    decision_epoch=head.decision_epoch, idempotency_key="hk-roi-2")
    assert accept(q.admin, q.env, acknowledged_open_gaps=True).status_code == 200
    r = q.admin.post("/v2/business/roi/replay", json={"workspace_id": q.env.ws, "records": data["records"], "baseline_revision": 1, "target_revision": 2,
                                                       "assumptions": sc["assumptions"]["S1"], "window_from": sc["window"]["from"], "window_to": sc["window"]["to"],
                                                       "label": "SYNTHETIC acceptance S1"})
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    want = _want(_load("expected_acceptance.json")["scenarios"]["S1"])
    got = json.loads(json.dumps(project(body), default=str))
    for key in ("denominators", "baseline", "target", "changes", "effort", "cost", "unsupported_ids", "excluded"):
        assert got[key] == want[key], key
    # The workspace's knowledge already carries a decided TARGET for the CAPEX-from-2,000,000 rule (QRY-03: the disagreement was decided for that rule, same
    # condition), so its rule ids differ from the Harbor fixture's while the rules that decide are the oracle's four (same roots, same conditions).
    root = lambda ids: sorted(i[: -len("-target")] if i.endswith("-target") else i for i in ids)
    assert root(got["coverage"]["target"]) == root(want["coverage"]["target"]) and root(got["coverage"]["baseline"]) == root(want["coverage"]["baseline"]), got["coverage"]
    decided = {r["rule_id"]: r for r in business_view(q.env.store, q.env.tenant, q.env.ws)["accepted"]["rules"]}
    capex = decided.get("rule-capex-finance-config-target") or decided["rule-capex-finance-config"]
    assert capex["condition"] == {"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"},
                                          {"field": "request.net_amount_jpy", "op": "gte", "value": 2000000, "unit": "JPY"}]}, capex["condition"]
    assert "records" not in body and body["pin"]["target"]["business_revision"] == 2
