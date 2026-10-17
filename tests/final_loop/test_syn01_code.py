"""SYN-01: code change recommendations for a confirmed business change, never an edit. Pure module + real routes over the Harbor code fixture (`app/calculations.py`
observed as functions by the Python parser). The understanding comes from the CHG-01 STUB; no model is used for the recommendation itself. Not human acceptance."""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import psycopg2
import pytest

from core.change_intelligence.v2 import code_recommendation as cr
from infra.worker import explain
from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE, model_obj, op, understand
from tests.final_loop.test_chg02_impact import confirmed
from tests.final_loop.test_chg02_impact import op as full_op
from tests.final_loop.test_chg03_draft import ARTS, OBS, VALUE, draft
from tests.final_loop.test_chg04_review import login
from tests.final_loop.test_kno01_business_store import bind, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_kno03_graph_gaps import seed
from tests.final_loop.test_kno05_accept import accept, answer
from tests.final_loop.test_qry02_retrieval import DISTRACTORS, KNOWLEDGE
from tests.final_loop.test_qry03_qa import DECISION_QUOTE, qa_env  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import REAL_BUDGET_PATH, drain

FREIGHT_REQ = "Make freight free only from 600,000 JPY instead of 500,000 JPY."
FREIGHT = {"rule_id": "rule-freight-free", "concept_ids": ["concept-freight"], "title": "Freight is free for goods of 500,000 JPY or more",
           "condition": {"field": "order.goods_net_jpy", "op": "gte", "value": 500000, "unit": "JPY"}, "actor_ref": None, "outcome": {"kind": "freight_waived"}, "exceptions": [],
           "scope": {"process": ["STANDARD"]}, "status": "VALIDATED", "authority": "OBSERVED",
           "provenance": [{"kind": "source_evidence", "artifact_id": "code-calc", "revision_id": "rev-1", "evidence_ref": "calculations.py#calculate_freight"}],
           "source_bindings": [{"artifact_id": "code-calc", "revision_id": "rev-1", "evidence_ref": "calculations.py#calculate_freight"}], "implementation_status": "verified"}
OP_FREIGHT = op("set_value", "rule-freight-free", {"value": 600000, "unit": "JPY"}, "Make freight free only from 600,000 JPY instead of 500,000 JPY")


@pytest.fixture
def code_env(env, ws, monkeypatch, tmp_path):                      # noqa: F811
    """The QRY-03 workspace plus one rule bound to a function of the imported Harbor code."""
    cfg = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("  intent.explain: gemini-3.5-flash-lite", "  intent.explain: gemini-3.5-flash-lite\n  knowledge.qa: gemini-3.8-flash\n  change.understand: gemini-3.8-flash\n  change.impact_deps: gemini-3.8-flash\n  change.draft_guidance: gemini-3.8-flash")
    path = tmp_path / "budget_code.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    seed(env, ws, rules=bind(KNOWLEDGE["observed_rules"] + DISTRACTORS + [copy.deepcopy(FREIGHT)], ws.source_revision_id))
    admin = env.admin_client()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    gap = next(g for g in admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"] if g["kind"] == "conflicting_facts")
    answer(admin, env, gap, {"choose_rule_id": "rule-capex-finance-config"}, DECISION_QUOTE)
    assert accept(admin, env, acknowledged_open_gaps=True).status_code == 200
    drain(env)
    return SimpleNamespace(env=env, admin=admin, req=env.req_client(), monkeypatch=monkeypatch)


def code(q, client, cid):
    return client.get(f"/v2/business/changes/{cid}/code", params={"workspace_id": q.env.ws})


def blob_hashes(q):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT logical_path, blob_hash FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s ORDER BY logical_path", (q.env.tenant, q.env.ws))
        return cur.fetchall()


# ---- pure ------------------------------------------------------------------------------------------------------------------------------------------------

def test_literals_are_found_by_numeric_value_in_any_spelling_and_pseudocode_never_holds_a_command():
    text = "def f(x):\n    if x >= 500_000:\n        return 0\n    return 2_000  # 500,000 is the limit\n"
    hits = cr.find_literals(text, 500000)
    assert [h["line"] for h in hits] == [2, 4] and hits[0]["literal"] == "500_000" and hits[1]["literal"] == "500,000"
    assert cr.find_literals(text, 2000) == [{"line": 4, "literal": "2_000", "text": "return 2_000  # 500,000 is the limit"}] and cr.find_literals(text, 7) == []
    assert "500,000" in text and cr._UNSAFE_LINE.search("    os.system('x')") and not cr._UNSAFE_LINE.search("THRESHOLD = 600000")


def test_a_supported_uncertain_and_unresolved_item_each_say_why_and_the_whole_is_bound_and_stale_aware():
    code_rule = copy.deepcopy(FREIGHT)
    rules = list({r["rule_id"]: r for r in KNOWLEDGE["observed_rules"]}.values()) + [code_rule]
    text = "def calculate_freight(goods_net_jpy: int) -> int:\n    if goods_net_jpy >= 500_000:\n        return 0\n    return 2_000\n"
    obs = OBS + [{"artifact_id": "a-calc", "kind": "code_block", "locator": {"type": "code_span", "qualname": "calculate_freight", "line_start": 19, "line_end": 27}, "raw_value": text}]
    arts = dict(ARTS, **{"a-calc": "harbor_demo/app/calculations.py"})
    freight_op = full_op("set_value", "rule-freight-free", {"value": 500000, "unit": "JPY"}, {"value": 600000, "unit": "JPY"})
    d = draft(freight_op, rules=rules, observations=obs, artifacts=arts)
    rec = cr.build_recommendations(d, [freight_op], head={"source_revision_id": "r", "snapshot_id": "s"})
    item = rec["items"][0]
    assert item["status"] == "SUPPORTED" and item["literals"][0]["literal"] == "500_000" and item["literals"][0]["line"] == 2 and "600000" in item["desired"]["pseudocode"]
    assert item["manual_only"] is True and item["risk"] == "MEDIUM" and item["observed_logic"].startswith("def calculate_freight") and item["acceptance_checks"][0].startswith("After the change, re-import")
    other = draft(freight_op, rules=rules, observations=[o for o in obs if o["artifact_id"] != "a-calc"] + [dict(obs[-1], raw_value="def calculate_freight(x):\n    return LIMITS.freight(x)\n")], artifacts=arts)
    unc = cr.build_recommendations(other, [freight_op], head={"source_revision_id": "r", "snapshot_id": "s"})["items"][0]
    assert unc["status"] == "UNCERTAIN" and "computed, imported or configured" in unc["why"] and unc["risk"] == "HIGH"
    missing = draft(freight_op, rules=rules, observations=obs, artifacts=dict(ARTS))
    unr = cr.build_recommendations(missing, [freight_op], head={"source_revision_id": "r", "snapshot_id": "s"})["items"][0]
    assert unr["status"] == "UNRESOLVED" and "no such file" in unr["why"]
    unitless = full_op("set_value", "rule-freight-free", {"value": 500000}, {"value": 600000, "unit": "JPY"})                   # the sources gave 500000 without a unit
    unk = cr.build_recommendations(draft(unitless, rules=rules, observations=obs, artifacts=arts), [unitless], head={"source_revision_id": "r", "snapshot_id": "s"})["items"][0]
    assert unk["status"] == "UNCERTAIN" and "without a unit" in unk["why"] and unk["risk"] == "HIGH"
    moved = cr.build_recommendations(d, [freight_op], head={"source_revision_id": "r2", "snapshot_id": "s"})
    assert moved["stale"] is True and "source files moved" in moved["stale_reasons"][0] and moved["recommendation_hash"] == rec["recommendation_hash"]      # the binding is the pin, staleness is a label
    plain = cr.render(rec, can_read_evidence=False)
    assert "observed_logic" not in str(plain["items"]) and "calculations.py" not in str(plain) and plain["counts"]["SUPPORTED"] == 1 and plain["apply"]["supported"] is False
    with pytest.raises(ValueError, match="unsafe_pseudocode"):
        cr.build_recommendations(d, [dict(freight_op, desired={"value": "1; rm -rf /", "unit": "JPY"})], head={"source_revision_id": "r", "snapshot_id": "s"})


# ---- real routes over the Harbor code fixture --------------------------------------------------------------------------------------------------------------

def test_a_developer_gets_a_supported_recommendation_for_the_observed_function_and_no_byte_of_code_changes(code_env):
    q = code_env
    hashes = blob_hashes(q)
    cid, model = confirmed(q, FREIGHT_REQ, [OP_FREIGHT])
    r = code(q, q.admin, cid)
    assert r.status_code == 200, r.text
    body = r.json()
    item = next(i for i in body["items"] if i["rule_id"] == "rule-freight-free")
    assert item["status"] == "SUPPORTED" and item["path"] == "calculations.py" and item["locator"] == "calculate_freight"
    assert {h["literal"] for h in item["literals"]} == {"500,000", "500_000"}                                  # the docstring and the comparison both hold the old value
    assert item["observed_logic"].startswith("def calculate_freight") and "600000" in item["desired"]["pseudocode"] and item["manual_only"] is True and body["apply"]["supported"] is False
    assert body["stale"] is False and body["bound_source_revision_id"] == body["current_source_revision_id"] and body["checklist"][0].startswith("F")
    plain = code(q, q.req, cid).json()                                                                       # requester: no evidence rights
    assert "calculations.py" not in str(plain) and plain["items"][0]["status"] == "SUPPORTED" and plain["withheld"]
    refused = q.admin.post(f"/v2/business/changes/{cid}/code/apply", json={"workspace_id": q.env.ws, "patch": "x"})
    assert refused.status_code == 409 and refused.json()["code"] == "code_apply_not_supported"
    assert blob_hashes(q) == hashes and model.calls == 1                                                      # no source byte changed; only the understanding asked a model
    assert code(q, login(q, f"viewer-c-{q.env.nonce}", ("viewer",)), cid).status_code == 404 and code(q, q.admin, "chg-nope").status_code == 404


def test_a_code_location_the_sources_do_not_have_is_unresolved_not_silently_skipped(qa_env):
    q = qa_env
    text = "Raise the CAPEX Finance threshold from 2,000,000 to 3,000,000 JPY."
    # qa_env decided the CAPEX conflict, so the rule has a decided (TARGET) version: a change names that version, otherwise the server asks which one is meant
    cid, _ = confirmed(q, text, [op("set_value", "rule-capex-finance-config-target", {"value": 3000000, "unit": "JPY"}, text.rstrip("."))])
    body = code(q, q.admin, cid).json()
    item = next(i for i in body["items"] if i["path"] == "routing.py")
    assert item["status"] == "UNRESOLVED" and body["counts"]["UNRESOLVED"] >= 1 and "no such file" in item["why"]
    got, _ = understand(q, "Please " + NUM_REQ[0].lower() + NUM_REQ[1:], model_obj(ops=[OP_VALUE]))
    assert code(q, q.req, got["change_id"]).status_code == 409                                                  # not confirmed: no recommendation
