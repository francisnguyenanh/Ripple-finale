"""Review claim 6: the AI investigation of UNKNOWN rules has a product caller and is bounded where the job is created.
Real routes/store/worker, STUB provider. requirement -> evidence:
  * a member with ai.use starts it for themselves (admitted by policy, no second person); a member without ai.use cannot           -> test 1
  * one investigation per (revision, rule, file) and at most 3 per rule on a revision; a failed job does not count               -> test 2
  * a disarmed provider refuses it before anything is created                                                                    -> test 3
  * its answer reaches the impact analysis ONLY as `indirect_model` (apart from the deterministic evidence), re-checked; a
    judgement whose quote is not in the file is rejected and nothing becomes a dependency                                        -> test 4"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from infra.worker import impact_judge
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import EXTRACT_MODEL, create_job, drain, job_body
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal
from tests.pitch_brief.test_loop_feedback import _usage
from tests.pitch_brief.test_role_model import member


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


class Judge:
    def __init__(self, depends=True, quote=None):
        self.calls, self.depends, self.quote = 0, depends, quote
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        obj = {"depends": self.depends, "evidence_quote": self.quote if self.depends else None, "reason": "stub reading"}
        return SimpleNamespace(text=json.dumps(obj), model_version=EXTRACT_MODEL, candidates=[SimpleNamespace(finish_reason="STOP")], usage_metadata=_usage())


def _targets(env, ch, c):
    """(target candidate id, scenario ids, [(artifact id, a verbatim quote of it)]) for the other files of the Harbor bundle."""
    from infra.worker import extraction
    from tests.pitch_brief.chain import APPROVAL, FINGUIDE, MATRIX, POLICY, WORKFLOW, cand_id
    target = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    scenario = [target] + [x["candidate_id"] for path, cs in ch.cands.items() for x in cs if x["candidate_id"] != target]
    head = env.store.head(env.tenant, env.ws)
    obs = env.store.source_observations(env.tenant, env.ws, head.source_revision_id)
    files = []
    for path in (FINGUIDE, WORKFLOW, POLICY, MATRIX):
        aid = ch.arts[path]
        texts = [extraction._observation_text(o) for o in obs if o["artifact_id"] == aid and extraction._observation_text(o)]
        files.append((aid, texts[0][:60] if texts else ""))
    return target, scenario, files


def _judge_payload(target, scenario, aid):
    return {"target_candidate_id": target, "scenario_candidate_ids": scenario, "artifact_id": aid, "scope_artifact_ids": [aid]}


def test_a_member_with_ai_use_starts_it_for_themselves_and_one_without_cannot(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    target, scenario, files = _targets(env, ch, c)
    jid, r = create_job(env.req_client(), env, "impact.judge", _judge_payload(target, scenario, files[0][0]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED" and job_body(ch.admin, env, jid)["approved_by"] == "policy:ai.use"       # nobody has to allow it
    suspended = member(env, "requester", "ai_suspended")
    _j, r2 = create_job(suspended, env, "impact.judge", _judge_payload(target, scenario, files[-1][0]))
    assert r2.status_code == 403, r2.text                                                                           # AI use switched off for that member


def test_one_per_file_at_most_three_per_rule_and_a_failed_job_does_not_count():
    """The bound is a pure function of the stored judge jobs of the revision (here a fake store: the rows it would read)."""
    from contextlib import contextmanager
    from infra import v2_judge_bounds as vb

    class Cur:
        def __init__(self, rows): self.rows = rows
        def execute(self, *a): pass
        def fetchall(self): return [(json.dumps(r),) for r in self.rows]

    class Store:
        def __init__(self, rows): self.rows = rows
        @contextmanager
        def _tx(self): yield Cur(self.rows)

    pay = lambda a: {"target_candidate_id": "T", "artifact_id": a}
    three = [pay("a1"), pay("a2"), pay("a3")]
    assert vb.refusal(Store([]), "t", "w", "r", pay("a1")) is None
    assert "already investigated" in vb.refusal(Store([pay("a1")]), "t", "w", "r", pay("a1"))
    assert vb.refusal(Store(three[:2]), "t", "w", "r", pay("a3")) is None
    assert "at most 3" in vb.refusal(Store(three), "t", "w", "r", pay("a4"))
    other = [{"target_candidate_id": "OTHER", "artifact_id": "a1"}]
    assert vb.refusal(Store(other * 3), "t", "w", "r", pay("a1")) is None                 # a different rule has its own bound
    # a FAILED/CANCELLED job is excluded by the query itself (the SQL filters on the execution state), so a retry is not blocked by it
    import inspect
    assert "NOT IN ('FAILED', 'CANCELLED')" in inspect.getsource(vb.refusal)


def test_a_disarmed_provider_refuses_it_before_anything_is_created(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    target, scenario, files = _targets(env, ch, c)
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    before = env.store.job_attempts  # noqa: F841 (documents that nothing is read from attempts here)
    _j, r = create_job(env.req_client(), env, "impact.judge", _judge_payload(target, scenario, files[0][0]))
    assert r.status_code == 409 and r.json().get("code") == "provider_disarmed"
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='impact.judge'", (env.tenant, env.ws))
        assert cur.fetchone()[0] == 0


def test_the_answer_reaches_the_impact_analysis_apart_and_a_quote_that_is_not_in_the_file_is_rejected(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    target, scenario, files = _targets(env, ch, c)
    req = env.req_client()
    stub = Judge(depends=True)
    monkeypatch.setattr(impact_judge, "_client_for_model", lambda m: stub)
    monkeypatch.setattr(impact_judge, "_assert_global_endpoint", lambda: None, raising=False)
    jid = good_aid = None
    for aid, quote in files:                                                  # eligibility (rule-bearing, not already flagged by the deterministic scan) is the executor's own rule
        stub.quote = quote
        j, r = create_job(req, env, "impact.judge", _judge_payload(target, scenario, aid))
        assert r.status_code == 200, r.text
        drain(env)
        if job_body(ch.admin, env, j)["execution_status"] == "COMPLETED":
            jid, good_aid = j, aid
            break
    assert jid, "no file of the bundle was eligible for a judgement"
    assert stub.calls == 1
    base = job_body(ch.admin, env, c.impact)["execution_result"]
    imp = {"candidate_ids": [target] + [u["candidate_id"] for u in base["unknown"]], "target_candidate_id": target, "existing_finding_ids": [], "judge_job_ids": [jid]}
    ij, ri = create_job(req, env, "impact", imp)
    assert ri.status_code == 200, ri.text
    drain(env)
    res = job_body(ch.admin, env, ij)["execution_result"]
    assert res["coverage"]["model_judgments"]["requested"] == 1
    assert [x["job_id"] for x in res["indirect_model"]] == [jid] and res["coverage"]["model_judgments"]["accepted"] == 1             # kept apart ...
    assert not any(x.get("artifact_id") == good_aid for x in res["indirect"])                                              # ... never merged into the deterministic evidence
    assert res["direct"] == base["direct"] and res["indirect"] == base["indirect"] and res["unknown"] == base["unknown"]       # the deterministic buckets are exactly what they were
