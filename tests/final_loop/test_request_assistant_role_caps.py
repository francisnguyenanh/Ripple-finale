"""R23 production configuration: the request assistant's own output caps (role_max_output_tokens 4,096 reserved / role_generation_max_output_tokens 2,048 sent)
next to the global caps of every other role (12,288 here), through the real routes, admission, worker, provider seam (SYNTHETIC stub recording each call's
config and location), cost ledger and read-back on the throwaway PostgreSQL. Expected amounts are computed by hand from the fixture prices (24,000 input
tokens; 3.8 Flash global 0.0015 / 0.0075 and `us` 0.00165 / 0.00825 per 1k; stub usage 900 / 150), never by the product's own pricing functions:
  answer call ceiling   24 x 0.0015  + 4.096 x 0.0075  = 0.06672       stub usage, global  0.9 x 0.0015  + 0.15 x 0.0075  = 0.002475
  recovery ceiling (us) 24 x 0.00165 + 4.096 x 0.00825 = 0.073392      stub usage, us      0.9 x 0.00165 + 0.15 x 0.00825 = 0.0027225
Covered: success (one call, 2,048 sent), timeout -> recovery at `us` (both 2,048), no usage on either call (each at its own role ceiling), a late answer (its
usage booked, the recovery published), a worker reclaimed during the recovery (nothing booked twice), a budget too small refused before any call, and a
business question (another role) still sent the global cap. Not AI-quality evidence."""
from __future__ import annotations

import pathlib
from decimal import Decimal

from infra.worker import explain
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import _ClockedModel, _state_answer, ask, booked, prepared, ra_env  # noqa: F401
from tests.final_loop.test_request_assistant_recovery import _RateLimited, _ServerError, _Scripted, _frozen, _run_loc, _us
from tests.pitch_brief.conftest import drain

ANSWER_CEILING, RECOVERY_CEILING = Decimal("0.06672"), Decimal("0.073392")
USAGE_GLOBAL, USAGE_US = Decimal("0.002475"), Decimal("0.0027225")


def _role_caps(q, tmp_path, deadline=45):
    """The `us` recovery budget plus the request assistant's own caps; the global 12,288 ceiling (no global generation cap) stays for every other role."""
    _us(q, tmp_path, deadline=deadline)
    path = pathlib.Path(__import__("os").environ["RIPPLE_BUDGET_CONFIG_PATH"])
    cfg = path.read_text(encoding="utf-8")
    assert "max_output_tokens: 12288" in cfg and "generation_max_output_tokens" not in cfg
    path.write_text(cfg + "\nrole_max_output_tokens:\n  request.assistant: 4096\n  request.assistant.recover: 4096\n"
                          "role_generation_max_output_tokens:\n  request.assistant: 2048\n  request.assistant.recover: 2048\n", encoding="utf-8")


def _sent(model):
    return [c.get("max_output_tokens") for c in model.configs]


def test_role_caps_a_success_sends_2048_makes_one_call_and_books_its_usage(ra_env, tmp_path):
    q = ra_env
    _role_caps(q, tmp_path)
    _frozen(q)
    model = _Scripted([_state_answer])
    tid, m, asked = _run_loc(q, model, "key-rcap-0001")
    assert m["state"] == "answered" and model.calls == 1 and asked == [None] and _sent(model) == [2048]
    [b] = booked(q, tid)
    assert b["attempts"][0][2] == USAGE_GLOBAL and b["ledger"] == [USAGE_GLOBAL]


def test_role_caps_a_cut_answer_call_is_booked_at_its_4096_ceiling_and_the_us_recovery_at_its_usage(ra_env, tmp_path):
    q = ra_env
    _role_caps(q, tmp_path)
    _frozen(q)
    model = _Scripted([TimeoutError("read timed out"), _state_answer])
    tid, m, asked = _run_loc(q, model, "key-rcap-0002")
    assert m["state"] == "answered" and model.calls == 2 and asked == [None, "us"] and _sent(model) == [2048, 2048]
    [b] = booked(q, tid)
    assert b["attempts"][0][1] == 2 and b["attempts"][0][2] == ANSWER_CEILING + USAGE_US and b["ledger"] == [ANSWER_CEILING + USAGE_US]


def test_role_caps_no_usage_on_either_call_books_each_at_its_own_role_ceiling(ra_env, tmp_path):
    q = ra_env
    _role_caps(q, tmp_path)
    _frozen(q)
    model = _Scripted([_RateLimited("resource exhausted"), _ServerError("still unavailable")])
    tid, m, asked = _run_loc(q, model, "key-rcap-0003")
    assert m["state"] == "unavailable" and asked == [None, "us"] and model.calls == 2
    [b] = booked(q, tid)
    assert b["status"] == "FAILED" and b["attempts"][0][4] == "estimate_no_usage"
    assert b["attempts"][0][2] == ANSWER_CEILING + RECOVERY_CEILING == Decimal("0.140112") and b["ledger"] == [Decimal("0.140112")]


def test_role_caps_a_late_answer_is_not_used_its_usage_is_booked_and_the_recovery_is_published(ra_env, tmp_path):
    q = ra_env
    _role_caps(q, tmp_path, deadline=6)
    _frozen(q, primary_deadline_s=0.5, min_left_s=1)
    model = _Scripted([("late", 1.2), _state_answer])
    tid, m, asked = _run_loc(q, model, "key-rcap-0004")
    assert m["state"] == "answered" and model.calls == 2 and asked == [None, "us"]
    [b] = booked(q, tid)
    assert b["attempts"][0][1] == 2 and b["attempts"][0][2] == USAGE_GLOBAL + USAGE_US


def test_role_caps_a_worker_reclaimed_during_the_recovery_books_nothing_twice(ra_env, tmp_path):
    q = ra_env
    _role_caps(q, tmp_path)
    _frozen(q)
    taken = []

    def other_worker(prompt):
        if model.calls < 1:
            return
        from infra.worker.job_worker import claim_and_admit_one
        with q.env.store._tx() as cur:
            cur.execute("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'", (q.env.tenant, q.env.ws))
        while (c := claim_and_admit_one(q.env.store, q.env.tenant, q.env.ws, "worker-b")) is not None:
            taken.append((c.kind, (c.payload or {}).get("job_id"), c.attempts))
    model = _Scripted([TimeoutError("read timed out"), _state_answer], during=other_worker)
    tid, m, asked = _run_loc(q, model, "key-rcap-0005")
    jid = booked(q, tid)[0]["job_id"]
    assert any(j == jid and n == 2 for _, j, n in taken), taken
    [b] = booked(q, tid)
    assert model.calls == 2 and b["status"] == "FAILED" and b["ledger"] == [sum(a[2] for a in b["attempts"])]
    drain(q.env)
    assert model.calls == 2 and booked(q, tid)[0]["attempts"] == b["attempts"]


def test_role_caps_a_budget_below_the_reservation_is_refused_before_any_call(ra_env, tmp_path):
    q = ra_env
    _role_caps(q, tmp_path)
    cid, approver = prepared(q)                                                            # the request is prepared under the normal budget first
    path = pathlib.Path(__import__("os").environ["RIPPLE_BUDGET_CONFIG_PATH"])
    cfg = path.read_text(encoding="utf-8")
    assert cfg.count('per_job_cap_usd: "1.20"') == 1
    path.write_text(cfg.replace('per_job_cap_usd: "1.20"', 'per_job_cap_usd: "0.20"'), encoding="utf-8")   # below 2 calls x the worst call (0.12816 at 12,288)
    model = _Scripted([_state_answer])
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m, location=None: model)
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        before = cur.fetchone()[0]
    r = approver.post(f"/v2/requests/business_change/{cid}/assistant/threads", json={"workspace_id": q.env.ws})   # admission may refuse here already
    if r.status_code == 200:
        r = ask(q, approver, r.json()["thread_id"], "What stage is this at?", "key-rcap-0006", 0)
    assert r.status_code == 409 and r.json()["code"] == "budget_exceeded" and model.calls == 0
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        assert cur.fetchone()[0] == before                                                 # nothing reserved or booked for the refused question


def test_role_caps_another_role_keeps_the_global_cap_and_its_reservation(ra_env, tmp_path):
    from infra.worker import job_quota
    from tests.final_loop.test_qry03_qa import obj, run
    q = ra_env
    _role_caps(q, tmp_path)
    config = job_quota.load_worker_budget_config()
    assert job_quota.compute_budget_reservation(config)[0] == Decimal("0.25632")          # 2 calls x (24 x 0.0015 + 12.288 x 0.0075): unchanged by the assistant's caps
    model = _ClockedModel(lambda p: obj("ABSTAINED", "", [], "No rule about pets was provided."))
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m, location=None: model)
    a = run(q, q.admin, "What is the policy on pets in the office?")["answer"]
    assert a["status"] == "ABSTAINED" and model.calls == 1 and model.configs[0]["max_output_tokens"] == 12288   # knowledge.qa: the global cap, as before
