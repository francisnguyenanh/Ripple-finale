"""Budget semantics and the cap: what `max_provider_calls_per_job` / `max_retries` mean at run time, and a proof (real routes, real worker, stub provider only)
that reservation, retry and settlement never take the project past the approved cap. No provider is called.

The two loaders read one key differently (shared loader: total calls = max_retries + 1; worker contract: calls per ATTEMPT). The run-time contract is the
worker's; `infra/worker/budget_invariants.py` states it once and `docs/contracts/AI_CLOSED_LOOP_BUDGETS.md` section 3a documents it.
"""
from __future__ import annotations

import pathlib
import re
import time
from decimal import Decimal
from types import SimpleNamespace

import psycopg2
import pytest

from infra.adapters.v2_postgres import PROJECT_SPEND_SQL, project_spend_params
from infra.worker import budget_invariants as B
from infra.worker import extraction, job_quota
from infra.worker.budget_config import BudgetConfigError, load_budget_config
from infra.worker.job_worker import claim_and_admit_one
from tests.pitch_brief import conftest as C
from tests.pitch_brief.conftest import DSN, create_job, import_harbor, job_body

R = Decimal("0.25632")
REPO = pathlib.Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------------------------------------------------------ semantics (pure)
def test_01_the_committed_config_means_one_call_per_attempt_two_attempts_two_calls(monkeypatch):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    cfg = job_quota.load_worker_budget_config(str(C.REAL_BUDGET_PATH))
    assert (cfg.max_provider_calls_per_job, cfg.max_retries) == (1, 1)
    assert B.attempts(cfg) == 2 and B.max_total_provider_calls(cfg) == 2
    assert B.job_reservation_usd(cfg) == R == job_quota.compute_budget_reservation(cfg)[0]       # the amount held at creation
    assert B.problems(cfg) == []
    assert B.job_reservation_usd(cfg) <= cfg.per_job_cap_usd < cfg.total_cap_usd


def test_02_the_shared_loader_reading_is_the_one_call_spike_case_and_stays_rejected(monkeypatch):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    with pytest.raises(BudgetConfigError, match=r"max_retries \+ 1"):
        load_budget_config(str(C.REAL_BUDGET_PATH))               # its meaning: TOTAL calls; with a retry the Pitch config is not expressible in it
    # The same total (2) is what the worker contract derives, so both readings agree on the number of calls a job may make
    cfg = job_quota.load_worker_budget_config(str(C.REAL_BUDGET_PATH))
    assert B.max_total_provider_calls(cfg) == cfg.max_retries + 1


def test_03_every_runtime_caller_uses_the_worker_loader():
    offenders = []
    for base in ("infra", "tools", "core"):
        for p in (REPO / base).rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            if p.name in ("budget_config.py", "job_quota.py"):
                continue
            if re.search(r"(?<!worker_)load_budget_config\(", text) and "load_worker_budget_config as load_budget_config" not in text:
                offenders.append(str(p.relative_to(REPO)))
    assert offenders == [], f"runtime code that reads the key with the shared loader's meaning: {offenders}"


def test_04_invariants_reject_a_config_that_could_escape_the_ledger_or_the_caps(monkeypatch, tmp_path):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    text = C.REAL_BUDGET_PATH.read_text(encoding="utf-8")
    def cfg_with(old, new):
        p = tmp_path / ("c%d.yaml" % abs(hash(new)))
        p.write_text(text.replace(old, new), encoding="utf-8")
        return job_quota.load_worker_budget_config(str(p))
    assert any("per-job cap" in x for x in B.problems(cfg_with('per_job_cap_usd: "0.30"', 'per_job_cap_usd: "0.20"')))
    assert any("fail_closed" in x for x in B.problems(cfg_with("fail_closed_on_missing_usage: true", "fail_closed_on_missing_usage: false")))


# ------------------------------------------------------------------------------------------------------------------ the cap holds through reserve / retry / settle
class AlwaysDown:
    """A provider that always fails with a retryable error: every attempt makes ONE call and costs the fail-closed worst case."""
    def __init__(self):
        self.calls = 0
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        err = RuntimeError("503 unavailable")
        err.code = 503
        raise err


def _spend(env) -> Decimal:
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute(PROJECT_SPEND_SQL, project_spend_params(env.tenant, env.ws))
        return cur.fetchone()[0]


def _ledger(env, job_id):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT entry_state, cost_usd, reserved_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (env.tenant, env.ws, job_id))
        return cur.fetchone()


def _attempts(env, job_id):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT provider_calls, cost_usd, cost_estimate_usd FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s ORDER BY recorded_at", (env.tenant, env.ws, job_id))
        return cur.fetchall()


def _run_to_terminal(env, store, tries=6):
    for _ in range(tries):
        claim = claim_and_admit_one(store, env.tenant, env.ws, "w", lease_seconds=1)
        if claim is None:
            time.sleep(1.3)
            continue
        time.sleep(0.05)


def test_05_reservation_retry_and_settlement_stay_inside_the_cap(env, monkeypatch):
    monkeypatch.setenv("RIPPLE_RETRY_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("RIPPLE_RETRY_BACKOFF_TIMEOUT_SECONDS", "0")
    admin, req = env.admin_client(), env.req_client()
    arts, _ = import_harbor(env, admin)
    from tests.pitch_brief.conftest import drain
    drain(env)
    base = _spend(env)
    cap = base + Decimal("0.60")                               # room for two jobs (2 x 0.25632 = 0.51264) but not three
    monkeypatch.setenv("RIPPLE_BUDGET_TOTAL_CAP_USD", str(cap))
    down = AlwaysDown()
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: down)
    paths = sorted(arts)

    def new_job(path):
        jid, r = create_job(req, env, "extraction", {"artifact_id": arts[path], "source_role": "unknown"})
        return jid, r

    j1, r1 = new_job(paths[0])
    assert r1.status_code == 200, r1.text
    assert _ledger(env, j1)[0] == "RESERVED" and _ledger(env, j1)[1] == R         # reserved at creation: both attempts
    assert _spend(env) == base + R <= cap
    assert admin.post(f"/v2/jobs/{j1}/approve", json={"workspace_id": env.ws}).status_code == 200
    _run_to_terminal(env, env.store)
    body = job_body(admin, env, j1)
    assert body["execution_status"] == "FAILED", body["execution_status"]
    att = _attempts(env, j1)
    assert len(att) == 2 == B.attempts(job_quota.load_worker_budget_config())                  # max_retries + 1 attempts, no more
    assert all(a[0] == 1 for a in att) and sum(a[0] for a in att) == 2 == down.calls          # one call per attempt, two in all
    state, settled, _ = _ledger(env, j1)
    assert state == "SETTLED" and settled == sum(a[1] for a in att) <= R                       # settlement never above the reservation here
    assert _spend(env) <= cap

    j2, r2 = new_job(paths[1])
    assert r2.status_code == 200, r2.text
    assert _spend(env) == base + settled + R <= cap
    before = _spend(env)
    j3, r3 = new_job(paths[2])
    assert r3.status_code == 409 and "budget_exceeded" in r3.text                              # a third reservation does not fit
    assert _ledger(env, j3) is None and _spend(env) == before
    rr = req.post(f"/v2/jobs/{j1}/retry", json={"workspace_id": env.ws})
    assert rr.status_code in (403, 409), rr.text                                              # the requester may not; and even an allowed retry needs room
    # j1 used every approved provider attempt: the retry is refused before anything is reserved, whatever the cap (the approved attempts are not reset)
    rr = admin.post(f"/v2/jobs/{j1}/retry", json={"workspace_id": env.ws})
    assert rr.status_code == 409 and "provider_attempts_exhausted" in rr.text, rr.text
    assert job_body(admin, env, j1)["execution_status"] == "FAILED" and _spend(env) == before
    monkeypatch.setenv("RIPPLE_BUDGET_TOTAL_CAP_USD", str(cap + R))                            # the OPERATOR raises the cap (here, in the test only)
    rr = admin.post(f"/v2/jobs/{j1}/retry", json={"workspace_id": env.ws})
    assert rr.status_code == 409 and "provider_attempts_exhausted" in rr.text, rr.text         # room does not buy more attempts
    assert _ledger(env, j1)[0] == "SETTLED" and _spend(env) == before                          # nothing reserved, nothing booked


def test_06_a_reported_overrun_is_recorded_not_hidden_and_blocks_the_next_job(env, monkeypatch):
    """If a provider reported more tokens than the ceilings, the cost is booked as reported (never clamped) and the next admission sees it."""
    admin, req = env.admin_client(), env.req_client()
    arts, _ = import_harbor(env, admin)
    from tests.pitch_brief.conftest import drain
    drain(env)
    base = _spend(env)
    monkeypatch.setenv("RIPPLE_BUDGET_TOTAL_CAP_USD", str(base + Decimal("0.40")))

    class Greedy:
        def __init__(self):
            self.models = SimpleNamespace(generate_content=self._gen)

        def _gen(self, **kw):
            return SimpleNamespace(text='{"candidates": []}', model_version=C.EXTRACT_MODEL, candidates=[SimpleNamespace(finish_reason="STOP")],
                                   usage_metadata=SimpleNamespace(prompt_token_count=24000, candidates_token_count=40000, thoughts_token_count=None, total_token_count=64000))
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: Greedy())
    path = sorted(arts)[0]
    jid, r = create_job(req, env, "extraction", {"artifact_id": arts[path], "source_role": "unknown"})
    assert r.status_code == 200, r.text
    assert admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
    drain(env)
    att = _attempts(env, jid)
    assert att and att[0][1] > att[0][2], "the booked cost exceeds the per-call worst case: it is recorded as reported"
    assert _ledger(env, jid)[1] == sum(a[1] for a in att)                                     # settled at the real sum, not clamped to the reservation
    jid2, r2 = create_job(req, env, "extraction", {"artifact_id": arts[sorted(arts)[1]], "source_role": "unknown"})
    assert r2.status_code == 409 and "budget_exceeded" in r2.text                              # the next job sees the overrun and is refused
    over = [a for a in att if a[1] > a[2]]
    assert B.attempts_over_estimate(DSN, env.tenant, env.ws) == [(jid, len(over))]            # and it is listable for the live acceptance check


# ------------------------------------------------------------------------------------------------------------------ the re-extraction wave is under the same cap
@pytest.fixture
def ch(env, monkeypatch):
    from tests.pitch_brief.chain import build
    return build(env, monkeypatch)


def test_07_the_wave_reserves_like_every_other_job_and_a_capped_wave_never_reads_as_complete(env, ch, monkeypatch):
    from tests.pitch_brief.conftest import PITCH_SCOPE, drain
    from tests.pitch_brief.test_loop_feedback import _apply_chain, _status
    spent_before_apply = _spend(env)
    # room for the apply job's own hold plus 3.5 more jobs: the wave of 6 can reserve 3
    cap = spent_before_apply + R * Decimal("4.5")
    monkeypatch.setenv("RIPPLE_BUDGET_TOTAL_CAP_USD", str(cap))
    c, apj, post = _apply_chain(env, ch, monkeypatch, drain_after=False)
    for _ in range(60):
        if claim_and_admit_one(env.store, env.tenant, env.ws, "w") is None:
            break
        assert _spend(env) <= cap, "the project total went past the cap while the wave was scheduled and run"
        for j in env.store.jobs_by_prefix(env.tenant, env.ws, f"reanalysis-apply-{apj}-"):
            assert _ledger(env, j["job_id"]) is not None, "a wave job exists without a reservation: it could spend past the cap unchecked"
    jobs = env.store.jobs_by_prefix(env.tenant, env.ws, f"reanalysis-apply-{apj}-")
    rebuild = env.store.job_record(env.tenant, env.ws, f"rebuild-{apj}")["execution_result"]["reanalysis"]
    deferred = rebuild["budget_deferred_artifact_ids"]
    assert 0 < len(jobs) < len(PITCH_SCOPE) and deferred and rebuild["complete"] is False
    assert len(jobs) + len(deferred) == len(rebuild["artifact_ids"]) == len(PITCH_SCOPE)          # nothing dropped: scheduled or reported
    assert post.calls == len(jobs)                                                                # no call for a job that was not created
    assert all(j["execution_status"] == "COMPLETED" for j in jobs)
    st = _status(env, ch, c, apj).json()
    assert st["stages"]["reanalysis"] == {"completed": len(jobs), "total": len(PITCH_SCOPE), "deferred_for_budget": len(deferred)}
    assert st["status"] != "COMPLETE" and any(r.startswith("reanalysis_deferred_for_budget") for r in st["reasons"])
    assert _spend(env) <= cap
    assert B.attempts_over_estimate(DSN, env.tenant, env.ws) == []


def test_08_without_a_cap_problem_the_wave_is_unchanged(env, ch, monkeypatch):
    from tests.pitch_brief.conftest import PITCH_SCOPE
    from tests.pitch_brief.test_loop_feedback import _apply_chain, _status
    c, apj, post = _apply_chain(env, ch, monkeypatch, drain_after=True)
    jobs = env.store.jobs_by_prefix(env.tenant, env.ws, f"reanalysis-apply-{apj}-")
    assert len(jobs) == len(PITCH_SCOPE) and post.calls == len(PITCH_SCOPE)
    assert _status(env, ch, c, apj).json()["stages"]["reanalysis"] == {"completed": len(PITCH_SCOPE), "total": len(PITCH_SCOPE)}


def test_09_a_capped_wave_leaves_pending_stale_and_never_old_results_as_current(env, ch, monkeypatch):
    from infra.worker import gap_answer
    from tests.pitch_brief.chain import APPROVAL
    from tests.pitch_brief.conftest import PITCH_SCOPE, GAP_MODEL
    from tests.pitch_brief.test_change_draft import _res
    from tests.pitch_brief.test_loop_feedback import GapStub, _apply_chain, _status
    cap = _spend(env) + R * Decimal("4.5")
    monkeypatch.setenv("RIPPLE_BUDGET_TOTAL_CAP_USD", str(cap))
    c, apj, post = _apply_chain(env, ch, monkeypatch, drain_after=False)
    for _ in range(60):
        if claim_and_admit_one(env.store, env.tenant, env.ws, "w") is None:
            break
    head = env.store.head(env.tenant, env.ws).source_revision_id
    view = env.store.source_revision_view(env.tenant, env.ws, head)
    paths = {a["artifact_id"]: a["logical_path"] for a in view["artifacts"]}
    jobs = env.store.jobs_by_prefix(env.tenant, env.ws, f"reanalysis-apply-{apj}-")
    scheduled = {j["job_id"].removeprefix(f"reanalysis-apply-{apj}-") for j in jobs}
    rebuild = env.store.job_record(env.tenant, env.ws, f"rebuild-{apj}")["execution_result"]["reanalysis"]
    deferred = set(rebuild["budget_deferred_artifact_ids"])
    assert deferred and scheduled and scheduled | deferred == set(paths)
    approval = next(a for a, p in paths.items() if p == APPROVAL)
    assert approval in scheduled, "the CHANGED file is re-read first when the cap cannot hold the whole wave"
    # what a person sees on Sources: a file that was not re-read is out of date, never "analysed"
    life = ch.admin.get("/v2/sources/lifecycle", params={"workspace_id": env.ws}).json()
    state = {r["logical_path"]: r["extraction"]["state"] for r in life["artifacts"]}
    # (which files are deferred depends on random artifact ids: the unchanged files are ordered by id. calculations.py was never extracted by this chain, so if it is deferred it is
    # "not_analysed", not "stale"; either way it is never shown as analysed.)
    never_analysed = {"harbor_demo/app/calculations.py"}
    # Contract since c49e468: a deferred file whose bytes did NOT change keeps its earlier analysis, labelled as an earlier reading
    # (`file_unchanged_since_analysis`); only a CHANGED file that was not re-read is "stale". Here only approval.yaml changed and it is scheduled first.
    extr = {r["logical_path"]: r["extraction"] for r in life["artifacts"]}
    assert all(state[paths[a]] == ("not_analysed" if paths[a] in never_analysed else "analysed") for a in deferred) and all(state[paths[a]] == "analysed" for a in scheduled),         ({paths[a]: state[paths[a]] for a in deferred}, {paths[a]: state[paths[a]] for a in scheduled})
    assert all(extr[paths[a]]["file_unchanged_since_analysis"] is True for a in deferred if paths[a] not in never_analysed), "a deferred file is shown as an earlier reading of an unchanged file"
    assert all(extr[paths[a]]["on_current_revision"] is False for a in deferred), "a deferred file has no reading on the current revision"
    # what the Explorer shows: claims that came from a file that was not re-read are STALE, not current
    g = ch.admin.get("/v2/knowledge/graph", params={"workspace_id": env.ws}).json()
    cands = [n for n in g["nodes"] if n["type"] == "candidate"]
    deferred_paths = {paths[a] for a in deferred}
    from_deferred = [n for n in cands if n["detail"]["logical_path"] in deferred_paths]
    assert from_deferred and all(n["source_revision_id"] != head for n in from_deferred)          # claims of a file that was not re-read still point at the EARLIER revision
    assert all(n["state"] != "stale" for n in from_deferred), "the file did not change, so its earlier claims stay valid (labelled as earlier readings)"
    fresh = [n for n in cands if n["detail"]["logical_path"] not in deferred_paths and n["source_revision_id"] == head]
    assert fresh and all(n["state"] != "stale" for n in fresh)                                          # claims re-read on the new revision are current
    # the loop never reads complete and names the unfinished work
    st = _status(env, ch, c, apj).json()
    assert st["status"] != "COMPLETE" and st["stages"]["reanalysis"]["deferred_for_budget"] == len(deferred)
    # a query answers from the NEW revision only: the old value is not current, the new one is cited from the head
    def ask(stub, qid):
        monkeypatch.setattr(gap_answer, "_client_for_model", lambda m: stub)
        jid, r = create_job(c.req, env, "gap.answer", {"question": "What is the Manager approval limit for standard orders?", "question_id": qid, "artifact_ids": [approval]})
        assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
        from tests.pitch_brief.conftest import drain
        drain(env)
        return _res(job_body(ch.admin, env, jid))
    new = ask(GapStub("routing_rules.standard_manager_threshold_jpy: 500000", "500000"), "q-new")
    assert new["status"] == "answered" and new["evidence_spans"][0]["source_revision_id"] == head
    old = ask(GapStub("routing_rules.standard_manager_threshold_jpy: 1000000", "1000000"), "q-old")
    assert old["status"] == "abstained" and old["downgraded_reason"]
    assert _spend(env) <= cap
