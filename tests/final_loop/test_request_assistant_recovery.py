"""R19: the request assistant's one sequential recovery call (config version 22; `infra/worker/request_assistant.py::_answer`) through the real routes, admission,
worker, provider seam (SYNTHETIC stub, records every outbound prompt and call config), checks, cost ledger and read-back on the throwaway PostgreSQL.
Nothing here is AI-quality evidence. Covered: primary success (one call, stage timing recorded, next steps unchanged), primary timeout / late answer / provider
error / object outside the schema -> recovery on the same prompt with only the time left, recovery failure -> one failed attempt, too little time left -> no
recovery, a worker reclaimed during the recovery call -> nothing published or booked twice, a role without a pin -> no recovery. Every case: one attempt row,
one ledger row, at most two provider calls."""
from __future__ import annotations

import json
import pathlib

import pytest

from infra.worker import explain
from infra.worker import request_assistant as RQA
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import (_ClockedModel, _price, _state_answer, ask, booked, prepared, ra_env, settle,  # noqa: F401
                                                          thread)
from tests.pitch_brief.conftest import drain


class _ServerError(Exception):
    code = 503


class _RateLimited(Exception):
    code = 429


def _ceiling(q):
    from infra.worker import job_quota
    return job_quota.actual_call_cost_usd(job_quota.load_worker_budget_config(), model_id="gemini-3.8-flash", input_tokens=None, output_tokens=None)


def _budget(q, tmp_path, deadline=45, pin_recovery=True, calls=2):
    """The assistant test budget with retries off, the job deadline, and the recovery role pinned to the same stub model (or not)."""
    cfg = open(q.rqa_budget_path, encoding="utf-8").read()
    assert cfg.count("max_retries: 1\n") == 1
    cfg = cfg.replace("max_retries: 1\n", "max_retries: 0\n").replace("max_provider_calls_per_job: 2\n", f"max_provider_calls_per_job: {calls}\n")
    if pin_recovery:
        cfg = cfg.replace("role_model_ids:\n", "role_model_ids:\n  request.assistant.recover: gemini-3.8-flash\n", 1)
    cfg += f"\nrole_request_timeouts:\n  request.assistant: {deadline}\n  request.assistant.recover: {deadline}\n"
    path = tmp_path / f"budget_recovery_{deadline}_{pin_recovery}_{calls}.yaml"
    path.write_text(cfg, encoding="utf-8")
    q.monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))


def _frozen(q, primary_deadline_s=24, min_left_s=8):
    """The product config (version 22) with the recovery timings scaled down for a test; prompt, schema and limits as shipped."""
    frozen, sha = RQA.load_config()
    assert frozen["version"] in (22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32) and frozen["recovery"]["role"] == "request.assistant.recover" and frozen["recovery"]["min_left_s"] == 8
    frozen = json.loads(json.dumps(frozen))
    frozen["recovery"].update(primary_deadline_s=primary_deadline_s, min_left_s=min_left_s)
    q.monkeypatch.setattr(RQA, "load_config", lambda: (frozen, sha))


class _Scripted(_ClockedModel):
    """Call n behaves as `script[n]`: a callable (the answer), an exception instance (raised), or ("late", seconds) (answers after that delay)."""

    def __init__(self, script, during=None):
        super().__init__(_state_answer, during=during)
        self.script = script

    def _gen(self, **kw):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.delay = 0.0
        if isinstance(step, tuple) and step[0] == "hang":                                          # R21: the call stays open (no bytes), then the SDK times out
            import time
            self.calls += 1
            self.prompts.append(kw["contents"])
            self.configs.append(kw.get("config"))
            time.sleep(step[1])
            raise TimeoutError("stub: the SDK's own timeout, long after the deadline")
        if isinstance(step, BaseException):
            self.calls += 1
            self.prompts.append(kw["contents"])
            self.configs.append(kw.get("config"))
            raise step
        if isinstance(step, tuple):
            self.delay = step[1]
        self.fn = step if callable(step) else _state_answer
        out = super()._gen(**kw)
        out.usage_metadata.traffic_type = "ON_DEMAND"                                              # what the standard tier reports
        return out


def _run(q, model, question="What stage is this at?", key="key-r19-0000", cid_approver=None):
    cid, approver = cid_approver or prepared(q)                                              # prepared() installs its own stub: ours goes in after it
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], question, key, 0).status_code == 200
    return t["thread_id"], settle(q, approver, t["thread_id"])["messages"][0]


def _bad_shape(p):
    return {"outcome": "answered", "answer_blocks": [], "limitations": []}                      # answer/5 shape: outside the answer/6 schema


def test_r19_primary_success_is_one_call_with_stage_timing_and_the_reused_task_view(ra_env, tmp_path):
    q = ra_env
    _budget(q, tmp_path)
    _frozen(q)
    model = _Scripted([_state_answer])
    tid, m = _run(q, model, key="key-r19-0001")
    assert m["state"] == "answered", (booked(q, tid), model.calls)
    assert model.calls == 1 and model.configs[0]["http_options"]["timeout"] == 24_000                 # the primary's own deadline
    [b] = booked(q, tid)
    assert [(a[0], a[1]) for a in b["attempts"]] == [("COMPLETED", 1)] and b["ledger"] == [b["attempts"][0][2]] and b["attempts"][0][2] == _price(q, 900, 150)
    res = q.env.store.job_record(q.env.tenant, q.env.ws, b["job_id"])["execution_result"]
    t = res["timing"]
    assert set(t) >= {"prepare_s", "calls", "checks_s", "total_s"} and [c["role"] for c in t["calls"]] == ["request.assistant"] and t["total_s"] >= t["prepare_s"]
    assert "recovery" not in res and res["provider_calls"] == 1
    assert res["model"]["traffic_type"] == "ON_DEMAND" and "requested_tier" not in res["model"]           # R19: the served tier is recorded without a requested tier
    assert [a["label"] for a in m["answer"]["next_actions"]] == ["Decide the knowledge change (approve, reject or request changes)"]   # the same next steps as before


@pytest.mark.parametrize("first, reason", [(TimeoutError("read timed out"), "timeout"), (_ServerError("upstream unavailable"), "provider_failed"),
                                           (_RateLimited("resource exhausted"), "rate_limited"), (_bad_shape, "schema_violation")])
def test_r19_a_technical_failure_of_the_primary_is_recovered_once_on_the_same_prompt_and_booked_once(ra_env, tmp_path, first, reason):
    q = ra_env
    _budget(q, tmp_path)
    _frozen(q)
    model = _Scripted([first, _state_answer])
    tid, m = _run(q, model, key=f"key-r19-01-{reason}")
    assert m["state"] == "answered" and model.calls == 2 and model.prompts[0] == model.prompts[1]                       # same prompt, same pack
    assert model.configs[0]["http_options"]["timeout"] == 24_000 and 30_000 <= model.configs[1]["http_options"]["timeout"] <= 45_000   # only the time left
    assert [x["text"] for x in m["answer"]["blocks"] if x["kind"] != "authority"] == ["The request is at the decision step."]
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, booked(q, tid)[0]["job_id"])["execution_result"]
    assert rec["recovery"]["reason"] == reason and rec["model"]["answered_by"] == "recovery" and rec["provider_calls"] == 2 and rec["model"]["traffic_type"] == "ON_DEMAND"
    assert [c["role"] for c in rec["timing"]["calls"]] == ["request.assistant", "request.assistant.recover"]
    assert m["answer"]["parts"]["recovery"]["reason"] == reason
    if reason == "rate_limited":                                                                    # R19: the 429's typed safe diagnostics are kept with the recovery
        d = rec["recovery"]["primary_diagnostics"]
        assert d["status_code"] == 429 and d["error_class"] == "_RateLimited" and "message" in d or "message_length" in d
    [b] = booked(q, tid)
    outcome, calls, cost, estimate, basis = b["attempts"][0]
    assert len(b["attempts"]) == 1 and outcome == "COMPLETED" and calls == 2 and b["ledger"] == [cost]
    if reason == "schema_violation":                                                                # the refused first object reported its usage: both usages booked
        assert basis == "provider_usage" and cost == _price(q, 1800, 300)
    else:                                                                                           # R19: the failed call at its ceiling, the recovery at its usage
        assert basis == "estimate_partial_usage" and cost == _ceiling(q) + _price(q, 900, 150)          # never the recovery call at its ceiling too
    drain(q.env)
    assert model.calls == 2 and booked(q, tid)[0]["attempts"] == b["attempts"]                      # nothing runs again


def test_r19_a_late_primary_answer_is_not_used_its_usage_is_booked_and_the_recovery_answer_is_published(ra_env, tmp_path):
    q = ra_env
    _budget(q, tmp_path, deadline=6)
    _frozen(q, primary_deadline_s=0.5, min_left_s=1)
    model = _Scripted([("late", 1.2), _state_answer])
    tid, m = _run(q, model, key="key-r19-0002")
    assert m["state"] == "answered" and model.calls == 2 and model.configs[0]["http_options"]["timeout"] == 500
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, booked(q, tid)[0]["job_id"])["execution_result"]
    assert rec["recovery"]["reason"] == "timeout" and rec["recovery"]["primary_usage_reported"] is True
    [b] = booked(q, tid)
    outcome, calls, cost, estimate, basis = b["attempts"][0]
    assert calls == 2 and basis == "provider_usage" and cost == _price(q, 1800, 300) and b["ledger"] == [cost]   # the late answer's usage, booked once


def test_r19_a_failed_recovery_fails_the_job_once_and_too_little_time_left_makes_no_recovery(ra_env, tmp_path):
    q = ra_env
    _budget(q, tmp_path)
    _frozen(q)
    cid_a = prepared(q)
    model = _Scripted([TimeoutError("read timed out"), _ServerError("still unavailable")])
    tid, m = _run(q, model, key="key-r19-0003", cid_approver=cid_a)
    assert m["state"] == "unavailable" and m["answer"]["blocks"] == [] and model.calls == 2
    [b] = booked(q, tid)
    assert b["status"] == "FAILED" and len(b["attempts"]) == 1 and b["attempts"][0][1] == 2 and b["ledger"] == [b["attempts"][0][2]]
    assert b["attempts"][0][4] == "estimate_no_usage" and b["attempts"][0][2] == 2 * _ceiling(q)       # both calls at their own ceiling (same model and tier here)
    drain(q.env)
    assert model.calls == 2 and booked(q, tid)[0]["attempts"] == b["attempts"]
    _budget(q, tmp_path, deadline=3)
    _frozen(q, primary_deadline_s=2.5, min_left_s=1)
    model = _Scripted([("late", 2.8), _state_answer])
    tid, m = _run(q, model, key="key-r19-0004", cid_approver=cid_a)
    assert m["state"] == "unavailable" and m["answer"]["reason_code"] == "provider_timeout" and model.calls == 1   # the job's deadline left no room
    [b] = booked(q, tid)
    assert b["attempts"][0][1] == 1 and b["ledger"] == [b["attempts"][0][2]]


def test_r19_a_worker_reclaimed_during_the_recovery_call_publishes_nothing_twice(ra_env, tmp_path):
    q = ra_env
    _budget(q, tmp_path)
    _frozen(q)
    taken = []

    def other_worker(prompt):
        if model.calls < 1:                                                                         # only while the recovery call is open
            return
        from infra.worker.job_worker import claim_and_admit_one
        with q.env.store._tx() as cur:
            cur.execute("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'", (q.env.tenant, q.env.ws))
        while (c := claim_and_admit_one(q.env.store, q.env.tenant, q.env.ws, "worker-b")) is not None:
            taken.append((c.kind, (c.payload or {}).get("job_id"), c.attempts))
    model = _Scripted([TimeoutError("read timed out"), _state_answer], during=other_worker)
    tid, m = _run(q, model, key="key-r19-0005")
    jid = booked(q, tid)[0]["job_id"]
    assert any(j == jid and n == 2 for _, j, n in taken), taken
    assert model.calls == 2 and m["state"] == "unavailable" and m["answer"]["reason_code"] == "attempt_limit"
    [b] = booked(q, tid)
    usage = [a for a in b["attempts"] if a[0] == "USAGE_UNKNOWN"]
    late = [a for a in b["attempts"] if a[0] != "USAGE_UNKNOWN"]
    assert b["status"] == "FAILED" and len(usage) == 2 and all(a[2] == a[3] for a in usage) and b["ledger"] == [sum(a[2] for a in usage)]
    assert [(a[0], a[2], a[3]) for a in late] in ([], [("LATE_USAGE", 0, 0)]), late      # REL-02: the reclaimed worker's usage is noted once at cost 0, never charged
    drain(q.env)
    assert model.calls == 2 and booked(q, tid)[0]["attempts"] == b["attempts"]


def test_r19_without_a_pinned_recovery_role_the_primary_failure_fails_the_job_as_before(ra_env, tmp_path):
    q = ra_env
    _budget(q, tmp_path, pin_recovery=False)
    _frozen(q)
    model = _Scripted([TimeoutError("read timed out"), _state_answer])
    tid, m = _run(q, model, key="key-r19-0006")
    assert m["state"] == "unavailable" and model.calls == 1 and model.configs[0]["http_options"]["timeout"] == 45_000   # no recovery: the role's own deadline


def test_r20_a_failed_recovery_at_another_tier_is_booked_at_each_calls_own_ceiling(ra_env, tmp_path):
    """R20 (A157 H5-14): the answer call (standard) and the recovery call (priority) both rate limited -> the failed attempt is booked at the standard ceiling plus
    the PRIORITY ceiling, not twice the standard one."""
    q = ra_env
    _budget(q, tmp_path)
    path = pathlib.Path(__import__("os").environ["RIPPLE_BUDGET_CONFIG_PATH"])
    cfg = path.read_text(encoding="utf-8")
    assert "gemini-3.8-flash:" in cfg
    cfg += "\nrole_service_tiers:\n  request.assistant.recover: priority\n"
    cfg = cfg.replace("model_prices:\n", "model_prices:\n  gemini-3.8-flash@priority:\n    input_usd_per_1k: '0.00135'\n    output_usd_per_1k: '0.00675'\n", 1)
    path.write_text(cfg, encoding="utf-8")
    _frozen(q)
    model = _Scripted([_RateLimited("resource exhausted"), _RateLimited("resource exhausted")])
    tid, m = _run(q, model, key="key-r20-0001")
    assert m["state"] == "unavailable" and model.calls == 2
    from infra.worker import job_quota
    c = job_quota.load_worker_budget_config()
    std = job_quota.actual_call_cost_usd(c, model_id="gemini-3.8-flash", input_tokens=None, output_tokens=None)
    pri = job_quota.actual_call_cost_usd(c, model_id="gemini-3.8-flash@priority", input_tokens=None, output_tokens=None)
    [b] = booked(q, tid)
    assert b["status"] == "FAILED" and b["attempts"][0][1] == 2 and b["attempts"][0][2] == std + pri and b["ledger"] == [std + pri] and pri != std
    assert b["attempts"][0][4] == "estimate_no_usage"



def _diagnostics(q, tid):
    with q.env.store._tx() as cur:
        cur.execute("""SELECT a.request_context->'provider_diagnostics' FROM v2_job_attempts a JOIN v2_request_messages m
                       ON m.job_id = a.job_id AND m.tenant_id = a.tenant_id AND m.workspace_id = a.workspace_id WHERE m.thread_id = %s""", (tid,))
        return [r[0] for r in cur.fetchall()]


def test_r21_a_call_still_open_past_its_deadline_and_grace_is_abandoned_and_the_recovery_answers(ra_env, tmp_path):
    """R21 (A160 H3-01 / H4-01): the SDK's HTTP timeout is per phase, so a call can outlive its deadline; the wall-clock bound ends the wait at the deadline
    plus the grace, the recovery gets the time left, and the abandoned call is booked at its ceiling (no usage)."""
    from infra.worker.live_analysis import WALL_CLOCK_GRACE_S
    q = ra_env
    _budget(q, tmp_path, deadline=8)
    _frozen(q, primary_deadline_s=0.5, min_left_s=1)
    model = _Scripted([("hang", 4.0), _state_answer])
    tid, m = _run(q, model, key="key-r21-0001")
    assert m["state"] == "answered" and model.calls == 2
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, booked(q, tid)[0]["job_id"])["execution_result"]
    assert rec["recovery"]["reason"] == "timeout" and rec["recovery"]["primary_usage_reported"] is False
    first = rec["timing"]["calls"][0]
    assert first["outcome"] == "timeout" and 0.5 <= first["s"] < 0.5 + WALL_CLOCK_GRACE_S + 0.8      # ended by the wall-clock bound, not the 4 s hang
    [b] = booked(q, tid)
    assert b["attempts"][0][1] == 2 and b["attempts"][0][2] == _ceiling(q) + _price(q, 900, 150) and b["ledger"] == [b["attempts"][0][2]]
    assert rec["timing"]["total_s"] < 4.0                                                           # the job did not wait out the 4 s hang


def test_r21_a_failed_job_keeps_its_call_timeline_and_the_answer_calls_diagnostics(ra_env, tmp_path):
    q = ra_env
    _budget(q, tmp_path)
    _frozen(q)
    model = _Scripted([_RateLimited("resource exhausted"), TimeoutError("read timed out")])
    tid, m = _run(q, model, key="key-r21-0002")
    assert m["state"] == "unavailable" and model.calls == 2
    [d] = _diagnostics(q, tid)
    assert [c["role"] for c in d["rqa_calls"]] == ["request.assistant", "request.assistant.recover"]
    assert [c["outcome"] for c in d["rqa_calls"]] == ["rate_limited", "timeout"] and all(isinstance(c["s"], (int, float)) for c in d["rqa_calls"])
    assert d["rqa_primary_error"] == "ProviderRateLimitError" and d["rqa_primary_diagnostics"]["status_code"] == 429
    assert "prompt" not in json.dumps(d).lower()                                                      # safe metadata only


class _Closable(_Scripted):
    """R22: a stub whose open call ends only when the caller closes the client (as the httpx transport does), else it answers late."""

    def __init__(self, script):
        import threading
        super().__init__(script)
        self.closed, self.late = threading.Event(), []

    def close(self):
        self.closed.set()

    def _gen(self, **kw):
        step = self.script[min(self.calls, len(self.script) - 1)]
        if isinstance(step, tuple) and step[0] == "hang_until_closed":
            self.calls += 1
            self.prompts.append(kw["contents"])
            self.configs.append(kw.get("config"))
            self.closed.clear()
            if self.closed.wait(step[1]):
                raise ConnectionError("stub: the connection was closed by the caller")
            self.late.append("an answer after the bound")                                         # would only happen if the call were not closed
            return super()._gen(**{**kw})
        return super()._gen(**kw)


def test_r22_an_abandoned_call_is_closed_its_late_answer_never_arrives_and_nothing_is_called_or_booked_again(ra_env, tmp_path):
    """Failure injection through the real routes and worker: the answer call hangs past its deadline + grace; the worker closes it (the stub ends only on
    close), the recovery answers with the time left; afterwards no late answer, no third call, one attempt, one ledger row at ceiling + recovery usage."""
    import time
    q = ra_env
    _budget(q, tmp_path, deadline=8)
    _frozen(q, primary_deadline_s=0.5, min_left_s=1)
    model = _Closable([("hang_until_closed", 6.0), _state_answer])
    tid, m = _run(q, model, key="key-r22-0001")
    assert m["state"] == "answered" and model.calls == 2 and model.late == []
    [b] = booked(q, tid)
    assert b["attempts"][0][1] == 2 and b["attempts"][0][2] == _ceiling(q) + _price(q, 900, 150) and b["ledger"] == [b["attempts"][0][2]]
    time.sleep(1.0)
    drain(q.env)
    assert model.calls == 2 and model.late == [] and booked(q, tid) == [b]                       # nothing late, nothing again


def _us(q, tmp_path, deadline=45):
    """R21: the assistant budget with the recovery role on the `us` multi-region, priced at its own list price; the answer call stays global."""
    _budget(q, tmp_path, deadline=deadline)
    path = pathlib.Path(__import__("os").environ["RIPPLE_BUDGET_CONFIG_PATH"])
    cfg = path.read_text(encoding="utf-8")
    cfg = cfg.replace("model_prices:\n", "model_prices:\n  gemini-3.8-flash@us:\n    input_usd_per_1k: '0.00165'\n    output_usd_per_1k: '0.00825'\n", 1)
    cfg += "\nrole_locations:\n  request.assistant.recover: us\n"
    path.write_text(cfg, encoding="utf-8")


def _run_loc(q, model, key, question="What stage is this at?"):
    """`_run` with a client factory that records the location each call asked for (None = the account's global endpoint)."""
    asked = []
    cid, approver = prepared(q)

    def factory(m, location=None):
        asked.append(location)
        return model
    q.monkeypatch.setattr(explain, "_client_for_model", factory)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], question, key, 0).status_code == 200
    return t["thread_id"], settle(q, approver, t["thread_id"])["messages"][0], asked


def _usd(q, key, inp, out):
    from decimal import Decimal
    from infra.worker import job_quota
    c = job_quota.load_worker_budget_config()
    if inp is None:
        return job_quota.actual_call_cost_usd(c, model_id=key, input_tokens=None, output_tokens=None)
    p = c.model_prices[key]
    return (Decimal(inp) * Decimal(str(p.input_usd_per_1k)) + Decimal(out) * Decimal(str(p.output_usd_per_1k))) / Decimal(1000)


def test_r21_us_a_primary_success_stays_global_and_makes_one_call(ra_env, tmp_path):
    q = ra_env
    _us(q, tmp_path)
    _frozen(q)
    model = _Scripted([_state_answer])
    tid, m, asked = _run_loc(q, model, "key-r21us-0001")
    assert m["state"] == "answered" and model.calls == 1 and asked == [None]                     # the answer call never leaves the global endpoint
    [b] = booked(q, tid)
    assert b["attempts"][0][2] == _usd(q, "gemini-3.8-flash", 900, 150)


def test_r21_us_the_recovery_runs_at_us_and_is_booked_at_the_us_price(ra_env, tmp_path):
    q = ra_env
    _us(q, tmp_path)
    _frozen(q)
    model = _Scripted([TimeoutError("read timed out"), _state_answer])
    tid, m, asked = _run_loc(q, model, "key-r21us-0002")
    assert m["state"] == "answered" and model.calls == 2 and asked == [None, "us"]
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, booked(q, tid)[0]["job_id"])["execution_result"]
    assert rec["recovery"]["recovery_location"] == "us" and rec["recovery"]["reason"] == "timeout"
    [b] = booked(q, tid)
    assert b["attempts"][0][1] == 2 and b["attempts"][0][2] == _usd(q, "gemini-3.8-flash", None, None) + _usd(q, "gemini-3.8-flash@us", 900, 150)
    assert b["ledger"] == [b["attempts"][0][2]]


def test_r21_us_a_double_failure_is_booked_at_each_calls_own_ceiling(ra_env, tmp_path):
    q = ra_env
    _us(q, tmp_path)
    _frozen(q)
    model = _Scripted([_RateLimited("resource exhausted"), _ServerError("still unavailable")])
    tid, m, asked = _run_loc(q, model, "key-r21us-0003")
    assert m["state"] == "unavailable" and m["answer"]["blocks"] == [] and asked == [None, "us"]
    [b] = booked(q, tid)
    assert b["status"] == "FAILED" and b["attempts"][0][4] == "estimate_no_usage"
    assert b["attempts"][0][2] == _usd(q, "gemini-3.8-flash", None, None) + _usd(q, "gemini-3.8-flash@us", None, None) and b["ledger"] == [b["attempts"][0][2]]


def test_r21_us_a_refused_recovered_answer_is_still_booked_per_call(ra_env, tmp_path):
    q = ra_env
    _us(q, tmp_path)
    _frozen(q)
    model = _Scripted([TimeoutError("read timed out"), _bad_shape])
    tid, m, asked = _run_loc(q, model, "key-r21us-0004")
    assert m["state"] == "unavailable" and asked == [None, "us"] and model.calls == 2
    [b] = booked(q, tid)
    assert b["attempts"][0][2] == _usd(q, "gemini-3.8-flash", None, None) + _usd(q, "gemini-3.8-flash@us", 900, 150)   # the us call at the us price, not global


def test_r21_us_a_hanging_us_recovery_is_closed_nothing_late_nothing_booked_twice(ra_env, tmp_path):
    import time
    q = ra_env
    _us(q, tmp_path, deadline=6)
    _frozen(q, primary_deadline_s=0.5, min_left_s=1)
    model = _Closable([TimeoutError("read timed out"), ("hang_until_closed", 10.0)])
    tid, m, asked = _run_loc(q, model, "key-r21us-0005")
    assert m["state"] == "unavailable" and asked == [None, "us"] and model.calls == 2 and model.late == []
    [b] = booked(q, tid)
    assert b["attempts"][0][2] == _usd(q, "gemini-3.8-flash", None, None) + _usd(q, "gemini-3.8-flash@us", None, None)
    time.sleep(1.0)
    drain(q.env)
    assert model.calls == 2 and model.late == [] and booked(q, tid) == [b]


def _direct_us(q, tmp_path, deadline=45):
    """R23 (prepared, not active): the answer call on the `us` multi-region at its own list price; the recovery stays on the global endpoint."""
    _budget(q, tmp_path, deadline=deadline)
    path = pathlib.Path(__import__("os").environ["RIPPLE_BUDGET_CONFIG_PATH"])
    cfg = path.read_text(encoding="utf-8")
    cfg = cfg.replace("model_prices:\n", "model_prices:\n  gemini-3.8-flash@us:\n    input_usd_per_1k: '0.00165'\n    output_usd_per_1k: '0.00825'\n", 1)
    cfg += "\nrole_locations:\n  request.assistant: us\n"
    path.write_text(cfg, encoding="utf-8")


def test_r23_direct_us_an_answer_at_us_is_one_call_booked_at_the_us_price_not_the_global_one(ra_env, tmp_path):
    q = ra_env
    _direct_us(q, tmp_path)
    _frozen(q)
    model = _Scripted([_state_answer])
    tid, m, asked = _run_loc(q, model, "key-r23us-0001")
    assert m["state"] == "answered" and model.calls == 1 and asked == ["us"]
    assert q.env.store.job_record(q.env.tenant, q.env.ws, booked(q, tid)[0]["job_id"])["execution_result"]["model"]["location"] == "us"   # the harness pins it
    [b] = booked(q, tid)
    assert b["attempts"][0][2] == _usd(q, "gemini-3.8-flash@us", 900, 150) != _usd(q, "gemini-3.8-flash", 900, 150) and b["ledger"] == [b["attempts"][0][2]]


def test_r23_direct_us_a_cut_us_call_is_recovered_on_global_and_booked_at_the_us_ceiling_plus_the_global_usage(ra_env, tmp_path):
    q = ra_env
    _direct_us(q, tmp_path)
    _frozen(q)
    model = _Scripted([TimeoutError("read timed out"), _state_answer])
    tid, m, asked = _run_loc(q, model, "key-r23us-0002")
    assert m["state"] == "answered" and model.calls == 2 and asked == ["us", None]
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, booked(q, tid)[0]["job_id"])["execution_result"]
    assert rec["recovery"]["recovery_location"] == "global" and rec["recovery"]["reason"] == "timeout" and rec["model"]["location"] == "global"
    [b] = booked(q, tid)
    assert b["attempts"][0][2] == _usd(q, "gemini-3.8-flash@us", None, None) + _usd(q, "gemini-3.8-flash", 900, 150) and b["ledger"] == [b["attempts"][0][2]]


def test_r23_direct_us_a_double_failure_is_booked_at_each_calls_own_ceiling(ra_env, tmp_path):
    q = ra_env
    _direct_us(q, tmp_path)
    _frozen(q)
    model = _Scripted([_RateLimited("resource exhausted"), _ServerError("still unavailable")])
    tid, m, asked = _run_loc(q, model, "key-r23us-0003")
    assert m["state"] == "unavailable" and asked == ["us", None]
    [b] = booked(q, tid)
    assert b["status"] == "FAILED" and b["attempts"][0][2] == _usd(q, "gemini-3.8-flash@us", None, None) + _usd(q, "gemini-3.8-flash", None, None)


def test_r23_direct_us_a_cut_us_call_without_time_for_a_recovery_is_booked_at_the_us_ceiling(ra_env, tmp_path):
    q = ra_env
    _direct_us(q, tmp_path, deadline=10)
    _frozen(q, primary_deadline_s=24, min_left_s=30)                                         # the job's time left is always below the recovery's minimum
    model = _Scripted([_ServerError("upstream unavailable")])
    tid, m, asked = _run_loc(q, model, "key-r23us-0004")
    assert m["state"] == "unavailable" and model.calls == 1 and asked == ["us"]
    [b] = booked(q, tid)
    assert b["status"] == "FAILED" and b["attempts"][0][2] == _usd(q, "gemini-3.8-flash@us", None, None) and b["ledger"] == [b["attempts"][0][2]]


def test_r23_direct_us_a_us_answer_refused_by_shape_without_a_recovery_is_booked_at_its_us_usage(ra_env, tmp_path):
    q = ra_env
    _direct_us(q, tmp_path, deadline=10)
    _frozen(q, primary_deadline_s=24, min_left_s=30)
    model = _Scripted([_bad_shape])
    tid, m, asked = _run_loc(q, model, "key-r23us-0005")
    assert m["state"] == "unavailable" and model.calls == 1 and asked == ["us"]
    [b] = booked(q, tid)
    assert b["attempts"][0][2] == _usd(q, "gemini-3.8-flash@us", 900, 150)


def _caps(q, tmp_path, gen=2048, reserve=4096, deadline=45):
    """R23 (approved for evaluation 2026-10-29): the request-assistant budget with a lower generation cap sent to the provider and the reservation kept at 2 x that cap."""
    _budget(q, tmp_path, deadline=deadline)
    path = pathlib.Path(__import__("os").environ["RIPPLE_BUDGET_CONFIG_PATH"])
    cfg = path.read_text(encoding="utf-8")
    assert cfg.count("max_output_tokens: 12288\n") == 1 and "generation_max_output_tokens" not in cfg
    path.write_text(cfg.replace("max_output_tokens: 12288\n", f"max_output_tokens: {reserve}\ngeneration_max_output_tokens: {gen}\n"), encoding="utf-8")


class _Usage(_Scripted):
    """Call n answers with `finish[n]` and the usage `usage[n]` = (prompt, candidates, thoughts)."""

    def __init__(self, finish, usage):
        super().__init__([_state_answer])
        self.finish, self.usage = finish, usage

    def _gen(self, **kw):
        from types import SimpleNamespace
        k = self.calls
        self.calls += 1
        self.prompts.append(kw["contents"])
        self.configs.append(kw.get("config"))
        p, c, t = self.usage[k]
        return SimpleNamespace(text=json.dumps(_state_answer(kw["contents"])), model_version="gemini-3.8-flash", candidates=[SimpleNamespace(finish_reason=self.finish[k])],
                               usage_metadata=SimpleNamespace(prompt_token_count=p, candidates_token_count=c, thoughts_token_count=t, total_token_count=p + c + (t or 0),
                                                              traffic_type="ON_DEMAND"))


def _attempt_context(q, tid):
    jid = booked(q, tid)[0]["job_id"]
    with q.env.store._tx() as cur:
        cur.execute("SELECT request_context FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (q.env.tenant, q.env.ws, jid))
        rc = cur.fetchone()[0]
    return rc if isinstance(rc, dict) else json.loads(rc)


def test_r23_caps_an_answer_cut_at_the_lower_cap_is_refused_never_shown_and_booked_at_its_reported_usage(ra_env, tmp_path):
    q = ra_env
    _caps(q, tmp_path)
    _frozen(q)
    model = _Usage(["MAX_TOKENS"], [(900, 1900, 148)])
    tid, m = _run(q, model, key="key-r23cap-0001")
    assert model.configs[0]["max_output_tokens"] == 2048 and model.calls == 1                       # the lower cap is what the provider is sent; truncation is not recovered
    assert m["state"] == "unavailable" and m["answer"]["blocks"] == []                              # a cut answer is never shown as an answer
    [b] = booked(q, tid)
    assert b["status"] == "FAILED" and "truncat" in str(b["reason"]).lower() and b["attempts"][0][2] == _price(q, 900, 2048) and b["ledger"] == [b["attempts"][0][2]]


def test_r23_caps_output_above_the_request_cap_is_booked_as_reported_and_flagged(ra_env, tmp_path):
    q = ra_env
    _caps(q, tmp_path)
    _frozen(q)
    model = _Usage(["STOP"], [(900, 1500, 2500)])                                                  # an overshoot past 2048 (within the 2x reservation)
    tid, m = _run(q, model, key="key-r23cap-0002")
    assert m["state"] == "answered"
    [b] = booked(q, tid)
    assert b["attempts"][0][2] == _price(q, 900, 4000)                                              # booked as reported, never capped down
    rc = _attempt_context(q, tid)
    assert rc["generation_cap"] == 2048 and rc["output_plus_thinking"] == 4000 and rc["over_generation_cap"] is True


def test_r23_caps_a_cut_call_without_usage_is_booked_at_the_lower_ceiling_never_zero(ra_env, tmp_path):
    from decimal import Decimal
    q = ra_env
    _caps(q, tmp_path)
    _frozen(q)
    model = _Scripted([TimeoutError("read timed out"), _state_answer])
    tid, m = _run(q, model, key="key-r23cap-0003")
    assert m["state"] == "answered" and model.calls == 2
    [b] = booked(q, tid)
    from infra.worker import job_quota
    c = job_quota.load_worker_budget_config()
    pr = c.model_prices["gemini-3.8-flash"]
    assert c.max_output_tokens == 4096 and _ceiling(q) == (Decimal(c.max_input_tokens_per_call) * Decimal(str(pr.input_usd_per_1k)) + Decimal(4096) * Decimal(str(pr.output_usd_per_1k))) / 1000
    assert b["attempts"][0][2] == _ceiling(q) + _price(q, 900, 150)                                 # the cut call at the lower ceiling (output side 4096), never 0


def _aus_caps(q, tmp_path, deadline=45):
    """R23 package A-US + caps: answer call global, recovery at `us` (its own list price), generation cap 2048, reservation 4096."""
    _caps(q, tmp_path, deadline=deadline)
    path = pathlib.Path(__import__("os").environ["RIPPLE_BUDGET_CONFIG_PATH"])
    cfg = path.read_text(encoding="utf-8")
    cfg = cfg.replace("model_prices:\n", "model_prices:\n  gemini-3.8-flash@us:\n    input_usd_per_1k: '0.00165'\n    output_usd_per_1k: '0.00825'\n", 1)
    path.write_text(cfg + "\nrole_locations:\n  request.assistant.recover: us\n", encoding="utf-8")


def test_r23_aus_caps_both_calls_are_sent_the_2048_cap_and_a_cut_answer_call_is_booked_at_the_lower_global_ceiling_plus_the_us_usage(ra_env, tmp_path):
    q = ra_env
    _aus_caps(q, tmp_path)
    _frozen(q)
    model = _Scripted([TimeoutError("read timed out"), _state_answer])
    tid, m, asked = _run_loc(q, model, "key-r23aus-0001")
    assert m["state"] == "answered" and asked == [None, "us"] and [c["max_output_tokens"] for c in model.configs] == [2048, 2048]
    [b] = booked(q, tid)
    assert b["attempts"][0][2] == _usd(q, "gemini-3.8-flash", None, None) + _usd(q, "gemini-3.8-flash@us", 900, 150) and b["ledger"] == [b["attempts"][0][2]]
    from infra.worker import budget_invariants, job_quota
    c = job_quota.load_worker_budget_config()
    assert c.max_output_tokens == 4096 and c.generation_max_output_tokens == 2048 and budget_invariants.job_reservation_usd(c) == 2 * _usd(q, "gemini-3.8-flash@us", None, None)


def test_r23_aus_caps_a_late_answer_is_not_used_its_usage_is_booked_and_the_us_recovery_is_published(ra_env, tmp_path):
    q = ra_env
    _aus_caps(q, tmp_path, deadline=6)
    _frozen(q, primary_deadline_s=0.5, min_left_s=1)
    model = _Scripted([("late", 1.2), _state_answer])
    tid, m, asked = _run_loc(q, model, "key-r23aus-0002")
    assert m["state"] == "answered" and model.calls == 2 and asked == [None, "us"]
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, booked(q, tid)[0]["job_id"])["execution_result"]
    assert rec["recovery"]["primary_usage_reported"] is True and rec["recovery"]["recovery_location"] == "us" and rec["model"]["location"] == "us"
    [b] = booked(q, tid)
    assert b["attempts"][0][1] == 2 and b["attempts"][0][2] == _usd(q, "gemini-3.8-flash", 900, 150) + _usd(q, "gemini-3.8-flash@us", 900, 150)   # each call once, at its own price


def test_r23_aus_caps_a_truncated_answer_call_is_kept_as_unavailable_and_no_extra_call_is_made(ra_env, tmp_path):
    q = ra_env
    _aus_caps(q, tmp_path)
    _frozen(q)
    model = _Usage(["MAX_TOKENS"], [(900, 2000, 48)])
    tid, m, asked = _run_loc(q, model, "key-r23aus-0003")
    assert m["state"] == "unavailable" and m["answer"]["blocks"] == [] and model.calls == 1 and asked == [None]   # truncation is not a recovery reason (contract)
    [b] = booked(q, tid)
    assert b["status"] == "FAILED" and b["attempts"][0][2] == _usd(q, "gemini-3.8-flash", 900, 2048)
