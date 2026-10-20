"""SUR-06 risk / trade-off memo of a confirmed change and SUR-08 synthesis memo of a review-board report, LOCAL: SYNTHETIC memo writers (stubs through the production
pinned-call path) over the Harbor knowledge, through the real routes, worker, budget reservation, ledger and the review-board coordinator. Covers kept / refused statements,
the severity floor, minority findings listed by the server, idempotent requests (no second call), a moved board report, malformed output, disarmed provider, visibility and
permissions. 0 real provider calls; model quality is NOT measured here."""
from __future__ import annotations

import re

import pytest

from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_ma_review_board import _confirmed, board_stub, ma_env, settle, start  # noqa: F401  (ma_env is a fixture)
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401  (qa_env is a fixture)
from tests.final_loop.test_syn01_code import login
from tests.pitch_brief.conftest import drain

RISK_WORDS, SYNTH_WORDS = "understand the business risk of a change", "read the report of several specialist reviewers"


@pytest.fixture
def memo_env(ma_env, monkeypatch, tmp_path):                 # noqa: F811
    import os
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace(
        "role_model_ids:\n", "role_model_ids:\n  change.risk_memo: gemini-3.8-flash\n  ma.synthesis_memo: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_memo.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    return ma_env


def facts(prompt: str) -> list:
    """(ref, kind, must_address, text) of every fact line the server put in the prompt."""
    block = prompt.split("<facts>")[1].split("</facts>")[0]
    out = []
    for line in block.splitlines():
        m = re.match(r"- ref=(\S+) \[([^\],]+)([^\]]*)\]: (.*)", line.strip())
        if m:
            out.append((m.group(1), m.group(2), "must be addressed" in m.group(3), m.group(4)))
    return out


def stmt(kind, text, refs, materiality="medium"):
    return {"kind": kind, "statement": text, "refs": refs, "materiality": materiality, "rationale": "The cited facts show it."}


def memo_writer(prompt: str):
    """Board specialists answer as in the board test; the risk memo cites the HIGH rule with a LOW materiality (floor), adds an invented saving (refused) and leaves the
    other must-address items to the server; the synthesis memo states the shared claim, drops every minority finding and claims an approval (refused)."""
    if RISK_WORDS in prompt:
        f = facts(prompt)
        rules = [r for r, k, must, _t in f if k == "affected_rule"]
        high = next((r for r, k, must, _t in f if k == "affected_rule" and must), rules[0])
        op = next(r for r, k, _m, _t in f if k == "operation")
        return {"status": "memo", "open_questions": ["Which control covers purchases between the old and the new threshold?"],
                "statements": [stmt("risk", "Purchases in the band between the two thresholds lose a review step.", [op, high], "low"),
                               stmt("trade_off", "Raising the threshold saves 3,500,000 JPY of review effort per year.", [op])]}
    if SYNTH_WORDS in prompt:
        f = facts(prompt)
        agreed = next(r for r, k, must, t in f if k == "finding" and "financial finding" in t)
        return {"status": "memo", "open_questions": [],
                "statements": [stmt("agreement", "Finance sees the band between the thresholds as the main exposure.", [agreed], "low"),
                               stmt("blocker", "The board has approved the change.", [agreed], "high")]}
    return board_stub(prompt)


def post_risk(q, client, cid, **body):
    return client.post(f"/v2/business/changes/{cid}/impact/risk-memo", json={"workspace_id": q.env.ws, **body})


def get_risk(q, client, cid, **params):
    return client.get(f"/v2/business/changes/{cid}/impact/risk-memo", params={"workspace_id": q.env.ws, **params})


def test_risk_memo_keeps_grounded_statements_applies_the_floor_lists_unaddressed_items_and_never_calls_twice(memo_env):
    q = memo_env
    cid, approver = _confirmed(q)
    model = use(q, memo_writer)
    assert get_risk(q, q.admin, cid).json()["status"] == "not_requested"                                               # a read never calls a model
    r = post_risk(q, q.admin, cid)
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("running", "waiting_for_approval") and model.calls == 0
    drain(q.env)
    got = get_risk(q, q.admin, cid).json()
    assert got["status"] == "done", got
    memo = got["memo"]
    assert memo["advisory_only"] is True and memo["label"].startswith("Advisory memo")
    assert [s["kind"] for s in memo["statements"]] == ["risk"]
    kept = memo["statements"][0]
    floor = kept["materiality"]
    assert kept["materiality_proposed"] == "low" and (kept["floor_applied"] is (floor != "low"))
    assert [x["reason"] for x in memo["rejected"]] == ["figure_not_in_cited_facts:3500000"]                           # the invented saving is refused
    assert memo["open_questions"] == ["Which control covers purchases between the old and the new threshold?"]
    assert all(n["origin"] == "server" for n in memo["not_addressed"]) and memo["complete"] is (not memo["not_addressed"])
    assert got["ai_outcome"]["contribution"] == "retained" and got["model"]["pinned"] == "gemini-3.8-flash"
    calls = model.calls
    assert calls == 1
    again = post_risk(q, q.admin, cid)
    assert again.json()["job_id"] == r.json()["job_id"] and again.json()["status"] == "done"
    drain(q.env)
    assert model.calls == calls                                                                                       # the same request again makes no new call
    # the memo changed nothing: the impact report's severities are the algorithm's
    impact = q.admin.get(f"/v2/business/changes/{cid}/impact", params={"workspace_id": q.env.ws, "mode": "technical"}).json()
    assert "risk_memo" not in impact


def test_risk_memo_over_a_board_run_and_synthesis_memo_list_every_minority_finding(memo_env):
    q = memo_env
    cid, approver = _confirmed(q)
    model = use(q, memo_writer)
    run_id = start(q, q.admin, cid).json()["run_id"]
    board = settle(q, q.admin, run_id)
    assert board["status"] == "REVIEW_READY"
    contested = [f["claim_id"] for f in board["report"]["findings"] if f["resolution"] in ("unresolved", "upheld_by_raiser_disputed_by_others")]
    assert contested
    r = post_risk(q, q.admin, cid, board_run_id=run_id)
    assert r.status_code == 200, r.text
    drain(q.env)
    with_board = get_risk(q, q.admin, cid, board_run_id=run_id).json()
    assert with_board["status"] == "done" and with_board["binding"]["board_run_id"] == run_id and with_board["binding"]["board_report_hash"] == board["report_hash"]
    missing = {n["ref"] for n in with_board["memo"]["not_addressed"]}
    assert set(contested) <= missing                                                                                  # minority findings stay visible
    assert get_risk(q, q.admin, cid).json()["status"] == "not_requested"                                               # the memo without a board is a different request
    s = q.admin.post(f"/v2/ma/runs/{run_id}/synthesis-memo", json={"workspace_id": q.env.ws})
    assert s.status_code == 200, s.text
    drain(q.env)
    syn = q.admin.get(f"/v2/ma/runs/{run_id}/synthesis-memo", params={"workspace_id": q.env.ws}).json()
    assert syn["status"] == "done", syn
    m = syn["memo"]
    assert [x["kind"] for x in m["statements"]] == ["agreement"] and m["statements"][0]["floor_applied"] is True        # the financial finding is critical: low is raised
    assert [x["reason"] for x in m["rejected"]] == ["status_claim_not_in_facts"]
    reasons = {n["ref"]: n["reason"] for n in m["not_addressed"]}
    assert all(reasons[c] == "minority or unresolved finding" for c in contested) and m["complete"] is False
    # the board report is unchanged by either memo
    assert q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()["report_hash"] == board["report_hash"]
    # visibility: a viewer sees neither; a decider without ai.use may read but not request
    viewer = login(q, f"viewer-memo-{q.env.nonce}", ("viewer",))
    assert get_risk(q, viewer, cid).status_code == 404 and viewer.get(f"/v2/ma/runs/{run_id}/synthesis-memo", params={"workspace_id": q.env.ws}).status_code == 404
    plain = login(q, f"appr-memo-{q.env.nonce}", ("change_approver",))
    assert get_risk(q, plain, cid, board_run_id=run_id).json()["status"] == "done"
    from infra.adapters.v2_policy import permissions_for
    if "ai.use" not in permissions_for(("change_approver",)):
        assert post_risk(q, plain, cid, attempt=2).status_code == 403
    assert model.calls >= 2


def test_refusals_moved_board_report_malformed_output_disarmed_and_unknown_run(memo_env, monkeypatch):
    q = memo_env
    from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE, model_obj, understand
    got, _ = understand(q, NUM_REQ, model_obj(ops=[OP_VALUE]), client=q.req)
    assert post_risk(q, q.admin, got["change_id"]).json().get("code") == "not_confirmed"
    cid, _ = _confirmed(q)
    use(q, memo_writer)
    assert post_risk(q, q.admin, cid, board_run_id="mar-unknown").status_code == 404
    assert post_risk(q, q.admin, cid, attempt=4).status_code == 400
    run_id = start(q, q.admin, cid).json()["run_id"]
    assert q.admin.post(f"/v2/ma/runs/{run_id}/synthesis-memo", json={"workspace_id": q.env.ws}).json()["code"] == "board_not_finished"   # still running
    board = settle(q, q.admin, run_id)
    # a memo requested, then the report moves before the worker runs: the worker refuses, it never re-reads silently
    s = q.admin.post(f"/v2/ma/runs/{run_id}/synthesis-memo", json={"workspace_id": q.env.ws}).json()
    store = q.env.store
    with store._tx() as cur:
        cur.execute("UPDATE v2_ma_runs SET report_hash=%s WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s", ("0" * 64, q.env.tenant, q.env.ws, run_id))
    drain(q.env)
    rec = store.job_record(q.env.tenant, q.env.ws, s["job_id"])
    assert rec["execution_status"] == "FAILED" and rec["execution_reason"].startswith("board_report_moved")
    with store._tx() as cur:
        cur.execute("UPDATE v2_ma_runs SET report_hash=%s WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s", (board["report_hash"], q.env.tenant, q.env.ws, run_id))
    # malformed memo output: the job fails with its reason, shown as such (never read as "no risk")
    use(q, lambda p: {"status": "memo", "statements": "not a list", "open_questions": []} if RISK_WORDS in p else board_stub(p))
    post_risk(q, q.admin, cid, attempt=2)
    drain(q.env)
    bad = get_risk(q, q.admin, cid).json()
    assert bad["status"] == "failed" and bad["reason_code"] == "schema_violation" and "Nothing is concluded" in bad["reason"]
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    r = post_risk(q, q.admin, cid, attempt=3)
    assert r.status_code == 409 and r.json()["code"] == "provider_disarmed"
