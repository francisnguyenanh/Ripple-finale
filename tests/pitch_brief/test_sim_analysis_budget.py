"""R3 ceiling: the reservation of `sim.analysis` covers the worst case of 2 jobs / 4 provider calls (2 attempts each), is released or
settled through the real ledger, and a third job is refused while two reservations are pending. Real routes, worker and Postgres
ledger; the provider is a STUB (this proves the accounting path, not model quality or real token counts)."""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from infra.adapters.v2_postgres import PROJECT_SPEND_SQL, project_spend_params
from infra.worker import explain
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import REAL_BUDGET_PATH, create_job, drain, job_body
from tests.pitch_brief.test_sim_analysis import STD, _get, _obj, _post

PER_JOB = Decimal("0.25632")         # 2 attempts x (24000 in x 0.0015/1k + 12288 out+thinking x 0.0075/1k = 0.12816)
TWO_JOBS = Decimal("0.51264")


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _spent(env) -> Decimal:
    with env.store._tx() as cur:
        cur.execute(PROJECT_SPEND_SQL, project_spend_params(env.tenant, env.ws))
        return Decimal(str(cur.fetchone()[0]))


def _cap_config(tmp_path, monkeypatch, total, concurrency=None):
    text = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("  intent.explain: gemini-3.5-flash-lite\n",
                                                                "  intent.explain: gemini-3.5-flash-lite\n  simulation.analyze: gemini-3.8-flash\n")
    text = text.replace('total_cap_usd: "12.82"', f'total_cap_usd: "{total}"')
    assert f'total_cap_usd: "{total}"' in text, "the cap replacement must take effect"
    if concurrency is not None:                       # the real config limits a workspace to 2 queued/running jobs; None keeps it
        assert "max_concurrent_jobs_per_workspace: 2" in text
        text = text.replace("max_concurrent_jobs_per_workspace: 2", f"max_concurrent_jobs_per_workspace: {concurrency}")
    p = tmp_path / "cap.yaml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(p))


class FlakyThenOk:
    """First call of every job fails (a retryable provider error), the second answers: 2 attempts per job."""
    def __init__(self, thought=2500):
        self.calls, self.per_job, self.thought = 0, {}, thought
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        facts = json.loads(kw["contents"].split("<facts>\n", 1)[1].rsplit("\n</facts>", 1)[0])
        key = facts["facts_origin"]["simulation_job_id"]
        self.per_job[key] = self.per_job.get(key, 0) + 1
        if self.per_job[key] == 1:
            raise RuntimeError("transient provider failure")
        refs = list(facts["case_refs"])
        obj = _obj(changed={"changed_count": facts["changed_count_total"], "direction": facts["simulation"]["direction"], "case_refs": refs[:2]},
                   interpretation=[{"statement": "Orders just above the new limit now need a manager.", "case_refs": refs[:2]}])
        return SimpleNamespace(text=json.dumps(obj), model_version="gemini-3.8-flash", candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=4000, candidates_token_count=900,
                                                              thoughts_token_count=self.thought, total_token_count=4000 + 900 + self.thought))


def _simulation(env, ch, value):
    jid, r = create_job(env.req_client(), env, "simulation", {"adapter": "harbor_cli", "case": "route",
                                                              "edits": [{"path": STD, "value": value, "expected_current": 1000000}]})
    assert r.status_code == 200, r.text
    drain(env)
    return jid


def _ledger_rows(env):
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_job_cost_ledger WHERE tenant_id=%s", (env.tenant,))
        return cur.fetchone()[0]


def test_a_third_job_at_the_concurrency_limit_is_refused_before_any_reservation_or_provider_call(env, ch, tmp_path, monkeypatch):
    """Contract (infra/adapters/v2_postgres.py create path): the per-workspace concurrency check runs BEFORE the budget reservation. With the real
    limit (2) and two jobs queued/running, a third gets `concurrency_limit`, even though the budget would also refuse it; nothing is reserved,
    charged or sent to the provider."""
    sims = [_simulation(env, ch, v) for v in (500000, 600000, 700000)]
    base = _spent(env)
    _cap_config(tmp_path, monkeypatch, base + TWO_JOBS)                       # budget room for exactly two jobs, concurrency stays at the real 2
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    stub = FlakyThenOk()
    monkeypatch.setattr(explain, "_client_for_model", lambda m: stub)
    req = env.req_client()
    _post(req, env, sims[0]), _post(req, env, sims[1])
    spent, rows = _spent(env), _ledger_rows(env)
    refused = req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[2]})
    assert refused.status_code == 409 and refused.json()["code"] == "concurrency_limit", refused.text
    assert _spent(env) == spent and _ledger_rows(env) == rows, "a refused request leaves no reservation, charge or phantom ledger row"
    assert stub.calls == 0, "no provider call was made by the refusal"


def test_two_jobs_four_calls_fit_the_reservation_and_a_third_is_refused_until_they_settle(env, ch, tmp_path, monkeypatch):
    """Budget path: the concurrency allowance is raised so only the BUDGET can refuse (the concurrency path has its own test above)."""
    sims = [_simulation(env, ch, v) for v in (500000, 600000, 700000)]
    base = _spent(env)
    _cap_config(tmp_path, monkeypatch, base + TWO_JOBS, concurrency=10)       # room for EXACTLY two worst-case jobs; a free concurrency slot remains
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    stub = FlakyThenOk()
    monkeypatch.setattr(explain, "_client_for_model", lambda m: stub)
    req = env.req_client()
    a, b = _post(req, env, sims[0]), _post(req, env, sims[1])
    assert _spent(env) - base == TWO_JOBS, "two pending reservations are counted at the worst case"
    refused = req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[2]})
    assert refused.status_code == 409 and refused.json()["code"] == "budget_exceeded", refused.text
    assert _spent(env) - base == TWO_JOBS, "a refused reservation leaves no phantom row"
    assert stub.calls == 0, "the refusal made no provider call"
    for made in (a, b):                                                         # admitted by policy at creation: the reservation was taken then
        assert made["status"] == "running"
    import time
    for _ in range(60):
        drain(env)
        if all(_get(req, env, s)["status"] == "done" for s in sims[:2]):
            break
        time.sleep(1)
    assert all(_get(req, env, s)["status"] == "done" for s in sims[:2])
    assert stub.calls == 4, "2 jobs x 2 attempts = 4 provider calls"
    with env.store._tx() as cur:
        cur.execute("SELECT job_id, entry_state, cost_usd, reserved_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND job_id = ANY(%s)",
                    (env.tenant, [a["job_id"], b["job_id"]]))
        rows = cur.fetchall()
    assert len(rows) == 2
    for _job, state, cost, reserved in rows:
        assert state == "SETTLED" and Decimal(str(reserved)) == PER_JOB
        assert Decimal("0") < Decimal(str(cost)) <= PER_JOB, "actual (failed attempt priced at its worst case + the real call) stays inside the reservation"
    settled = _spent(env) - base
    assert settled <= TWO_JOBS and settled < TWO_JOBS, "the unused part of the reservation is released"
    # settlement released the unused part of each reservation: with a cap that holds the REAL spend plus one more worst-case job, the third is admitted
    # (still behind its own approval, no call yet). With the old cap it stays refused: the failed attempt is now booked at the flash worst case.
    again = req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[2]})
    assert again.status_code == 409 and again.json()["code"] == "budget_exceeded"
    _cap_config(tmp_path, monkeypatch, _spent(env) + PER_JOB, concurrency=10)
    assert _post(req, env, sims[2])["status"] == "running"


def test_thinking_is_priced_once_as_output_and_a_response_inside_the_ceiling_never_exceeds_the_reservation(env, ch, tmp_path, monkeypatch):
    """Provider accounting: output tokens and thought tokens are two disjoint counts, both billed at the output rate (never counted twice).
    Even a call at the full ceilings (24000 in, 12288 output+thinking) costs exactly the per-attempt worst case."""
    sim = _simulation(env, ch, 500000)
    _cap_config(tmp_path, monkeypatch, "100.00")
    from infra.worker import job_quota
    config = job_quota.load_worker_budget_config()
    in_price, out_price = Decimal("0.0015"), Decimal("0.0075")
    ceiling_call = Decimal(24000) / 1000 * in_price + Decimal(12288) / 1000 * out_price
    assert ceiling_call == Decimal("0.12816")
    reservation, _cap = job_quota.compute_budget_reservation(config)
    assert reservation == PER_JOB == ceiling_call * config.max_provider_calls_per_job * (config.max_retries + 1)
    one = job_quota.actual_call_cost_usd(config, model_id="gemini-3.8-flash", input_tokens=4000, output_tokens=900 + 2500)
    assert one == Decimal(4000) / 1000 * in_price + Decimal(3400) / 1000 * out_price      # thinking added ONCE
    assert config.max_output_tokens == 12288 and config.max_input_tokens_per_call == 24000


def _ledger(env, job_id):
    with env.store._tx() as cur:
        cur.execute("SELECT entry_state, cost_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND job_id=%s", (env.tenant, job_id))
        state, cost = cur.fetchone()
    return state, Decimal(str(cost))


def test_a_result_rejected_after_the_provider_answered_settles_the_reported_usage_priced_on_the_pinned_flash_model(env, ch, tmp_path, monkeypatch):
    """Found by the live run: such an attempt was booked at the DEFAULT (lite) worst case. It must be the usage the provider reported, at the
    flash rates of the pinned role."""
    from tests.pitch_brief.test_sim_analysis import Reader
    sim = _simulation(env, ch, 500000)
    _cap_config(tmp_path, monkeypatch, "100.00")
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    bad = Reader(mutate=lambda o, f: {**o, "gaps": [{"statement": "Fine.", "basis": "something_else"}]})
    monkeypatch.setattr(explain, "_client_for_model", lambda m: bad)
    made = _post(env.req_client(), env, sim)
    assert made["status"] == "running"
    import time
    for _ in range(40):
        drain(env)
        if _get(env.req_client(), env, sim)["status"] == "failed":
            break
        time.sleep(1)
    assert bad.calls == 1
    state, cost = _ledger(env, made["job_id"])
    assert state == "SETTLED" and cost == Decimal(900) / 1000 * Decimal("0.0015") + Decimal(300) / 1000 * Decimal("0.0075")


def test_a_call_that_fails_without_usage_is_booked_at_the_flash_worst_case_for_each_attempt(env, ch, tmp_path, monkeypatch):
    sim = _simulation(env, ch, 500000)
    _cap_config(tmp_path, monkeypatch, "100.00")
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)

    class AlwaysDown:
        calls = 0

        def __init__(self):
            self.models = SimpleNamespace(generate_content=self._gen)

        def _gen(self, **kw):
            AlwaysDown.calls += 1
            raise RuntimeError("provider down")
    monkeypatch.setattr(explain, "_client_for_model", lambda m: AlwaysDown())
    made = _post(env.req_client(), env, sim)
    assert made["status"] == "running"
    import time
    for _ in range(60):
        drain(env)
        if _get(env.req_client(), env, sim)["status"] == "failed":
            break
        time.sleep(1)
    assert AlwaysDown.calls == 2, "1 call per attempt, 2 attempts"
    assert _ledger(env, made["job_id"]) == ("SETTLED", PER_JOB), "2 x 0.12816: the flash worst case per attempt, never the lite default"
