"""Thin Harbor page: ONE requisition through Harbor's own routing code under two configurations (pinned public copy, resource-limited runner, no model, no write)."""
from __future__ import annotations

import json

import pytest

from infra import v2_harbor_try as H
from infra.worker import harbor_sim

SEED = (harbor_sim.FIXTURE / "config" / "approval.yaml").read_bytes()
LOWER = SEED.replace(b"standard_manager_threshold_jpy: 1000000", b"standard_manager_threshold_jpy: 500000")


def req(process="STANDARD", category="OPEX", amount=600000, **kw):
    base = {"request_id": "REQ-TRY-1", "requester_id": "EMP-001", "supplier_id": "SUP-002", "process": process, "category": category, "net_amount_jpy": amount,
            "currency": "JPY", "requested_at": "2026-09-01T09:01:00", "quantity": 10, "item_description": "Trial item"}
    base.update(kw)
    return base


def test_01_a_config_change_changes_the_standard_route_and_not_the_emergency_one():
    assert LOWER != SEED
    std = H.try_requisition(req("STANDARD"), SEED, LOWER)
    assert std["before"]["decision"]["route"] == "AUTO" and std["after"]["decision"]["route"] == "NEEDS_MANAGER" and "route" in std["changed"]
    emg = H.try_requisition(req("EMERGENCY"), SEED, LOWER)            # emergency.py has its own literal 1_000_000 and does not read the threshold
    assert emg["before"]["decision"]["route"] == emg["after"]["decision"]["route"] == "AUTO" and "route" not in emg["changed"]
    big = H.try_requisition(req("EMERGENCY", amount=1200000), SEED, LOWER)
    assert big["before"]["decision"]["route"] == big["after"]["decision"]["route"] == "NEEDS_MANAGER"
    assert std["before"]["run"]["input_hash_unchanged"] is True


def test_02_the_shipped_baseline_agrees_with_harbor_for_a_public_requisition():
    reqs = [json.loads(x) for x in (harbor_sim.FIXTURE / "data/requisitions.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    shipped = {json.loads(x)["request_id"]: json.loads(x) for x in (harbor_sim.FIXTURE / "run_output/routes.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}
    r = next(x for x in reqs if x["process"] == "STANDARD")
    got = H.try_requisition({k: r[k] for k in H.FIELDS}, SEED, SEED)["before"]["decision"]
    assert got["route"] == shipped[r["request_id"]]["route"] and got["reason_code"] == shipped[r["request_id"]]["reason_code"]


@pytest.mark.parametrize("patch", [{"process": "RUSH"}, {"net_amount_jpy": "600000"}, {"net_amount_jpy": True}, {"request_id": "a b"}, {"currency": "jpy"},
                                   {"requested_at": "yesterday"}, {"item_description": ""}, {"extra": 1}])
def test_03_bad_input_is_refused_before_anything_runs(patch):
    with pytest.raises(harbor_sim.HarborSimError):
        H.try_requisition({**req(), **patch}, SEED, SEED)


def test_04_a_bad_config_is_refused():
    with pytest.raises(harbor_sim.HarborSimError):
        H.try_requisition(req(), b"routing_rules: [1]\n", SEED)


def test_05_http_route_needs_a_member_and_runs_without_a_model(env):
    from tests.pitch_brief.conftest import import_harbor
    admin = env.admin_client()
    arts, rev = import_harbor(env, admin)
    cfgs = admin.get("/v2/harbor/configs", params={"workspace_id": env.ws})
    assert cfgs.status_code == 200, cfgs.text
    j = cfgs.json()
    assert j["code"]["claim"].startswith("pinned public Harbor code") and set(j["code"]["files"]) == set(H.CODE_FILES)
    body = {"workspace_id": env.ws, "requisition": req("STANDARD", amount=1200000), "before_revision_id": rev, "after_revision_id": rev}
    r = admin.post("/v2/harbor/try", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["before"]["decision"]["route"] == "NEEDS_MANAGER" and out["changed"] == {} and out["input"]["net_amount_jpy"] == 1200000
    assert out["revisions"]["before"]["standard_manager_threshold_jpy"] == 1000000 and len(out["revisions"]["after"]["config_sha256"]) == 64
    assert admin.post("/v2/harbor/try", json={**body, "requisition": req(process="RUSH")}).status_code == 400
    assert admin.post("/v2/harbor/try", json={**body, "before_revision_id": "nope"}).status_code in (400, 404)
    anon = env.anon_client() if hasattr(env, "anon_client") else None
    if anon is not None:
        assert anon.post("/v2/harbor/try", json=body).status_code in (401, 403)
