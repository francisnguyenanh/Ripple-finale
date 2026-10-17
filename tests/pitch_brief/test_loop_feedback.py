"""B3: after apply -> verification, rebuild, re-extraction (real provider-call jobs, stub provider), loop status that is
never COMPLETE while residual sources remain, and a query on the NEW revision with new citations."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from infra.worker import extraction, gap_answer
from tests.pitch_brief.chain import APPROVAL, build
from tests.pitch_brief.conftest import EXTRACT_MODEL, GAP_MODEL, create_job, drain, job_body
from tests.pitch_brief.test_change_draft import _res
from tests.pitch_brief.test_draft_to_proposal import _grant, _to_approval, chain_to_proposal


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _usage():
    return SimpleNamespace(prompt_token_count=40, candidates_token_count=30, thoughts_token_count=None,
                           total_token_count=70)


class PostApplyExtraction:
    """Stub provider for the re-analysis jobs: reads the NEW text in the prompt (as a model would)."""
    def __init__(self):
        self.calls = 0
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        cands = []
        for line in kw["contents"].splitlines():
            if "routing_rules.standard_manager_threshold_jpy:" in line:
                val = int(line.rsplit(":", 1)[1].strip())
                cands.append({"subject": "Manager approval", "predicate": "standard_manager_approval_threshold",
                              "value": val, "unit": "JPY", "modality": "must", "scope": {"order_type": "standard"},
                              "exceptions": [], "outcome": None,
                              "quotes": [f"routing_rules.standard_manager_threshold_jpy: {val}"], "rationale": "stub"})
                break
        return SimpleNamespace(text=json.dumps({"candidates": cands}), model_version=EXTRACT_MODEL,
                               candidates=[SimpleNamespace(finish_reason="STOP")], usage_metadata=_usage())


class GapStub:
    def __init__(self, quote, answer):
        self.calls, self.quote, self.answer = 0, quote, answer
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        obj = {"status": "answered", "answer": self.answer, "states_absence": False,
               "evidence": [{"artifact_path": APPROVAL, "quote": self.quote}], "question_for_human": None,
               "scope_examined": [APPROVAL]}
        return SimpleNamespace(text=json.dumps(obj), model_version=GAP_MODEL,
                               candidates=[SimpleNamespace(finish_reason="STOP")], usage_metadata=_usage())


def _apply_chain(env, ch, monkeypatch, drain_after=True, post=None):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, review, aj, appr = _to_approval(env, ch, c)
    approver = _grant(env, "change_approver")
    rd = approver.post(f"/v2/jobs/{aj}/decision", json={"workspace_id": env.ws, "action": "approve", "reason": "ok",
                                                       "expected_approval_digest": appr["approval_digest"]})
    assert rd.status_code == 200, rd.text
    post = post or PostApplyExtraction()
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: post)
    apj, r = create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    assert r.status_code == 200, r.text
    if drain_after:
        drain(env)
        drain(env)
    return c, apj, post


def _status(env, ch, c, apj):
    return ch.admin.get("/v2/change/loop-status", params={"workspace_id": env.ws, "draft_job_id": c.draft,
                                                         "apply_job_id": apj})


def test_loop_status_is_partial_never_complete_and_lists_residual_sources(env, ch, monkeypatch):
    c, apj, post = _apply_chain(env, ch, monkeypatch)
    r = _status(env, ch, c, apj)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["stages"]["verification"] == "PASSED" and s["stages"]["knowledge_rebuild"] == "COMPLETED"
    ra = s["stages"]["reanalysis"]
    assert ra["total"] >= 1 and ra["completed"] == ra["total"]
    assert post.calls == ra["total"]                                   # each re-extraction is a real provider call
    assert s["status"] == "PARTIAL" and s["supported_scope_applied"]
    assert any(x["current_state"] == "still_old_value" for x in s["residual_counterevidence"]), s
    assert any(x.startswith("residual_sources") for x in s["reasons"])


def test_loop_status_rejects_non_apply_and_unknown_jobs(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    assert _status(env, ch, c, c.proposal).status_code == 404
    r = ch.admin.get("/v2/change/loop-status", params={"workspace_id": env.ws, "draft_job_id": "nope",
                                                      "apply_job_id": "nope"})
    assert r.status_code == 404


def test_query_after_update_cites_new_revision_and_old_claim_is_downgraded(env, ch, monkeypatch):
    c, apj, post = _apply_chain(env, ch, monkeypatch)
    head = env.store.head(env.tenant, env.ws).source_revision_id
    view = env.store.source_revision_view(env.tenant, env.ws, head)
    aid = next(a["artifact_id"] for a in view["artifacts"] if a["logical_path"] == APPROVAL)

    def ask(stub, qid):
        monkeypatch.setattr(gap_answer, "_client_for_model", lambda m: stub)
        jid, r = create_job(c.req, env, "gap.answer", {
            "question": "What is the Manager approval limit for standard orders?", "question_id": qid,
            "artifact_ids": [aid]})
        assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text        # admitted by policy: the requester's own AI permission
        drain(env)
        return _res(job_body(ch.admin, env, jid))

    new = ask(GapStub("routing_rules.standard_manager_threshold_jpy: 500000", "500000"), "q-new")
    assert new["status"] == "answered" and "500000" in new["answer"]
    assert new["evidence_spans"][0]["source_revision_id"] == head          # cited from the NEW revision
    old = ask(GapStub("routing_rules.standard_manager_threshold_jpy: 1000000", "1000000"), "q-old")
    assert old["status"] == "abstained" and old["downgraded_reason"]       # the old claim is not in the new source


def test_reanalysis_progresses_under_concurrency_limit_2_without_loss_or_duplicate_calls(env, ch, monkeypatch):
    """Candidate config has max_concurrent_jobs_per_workspace=2. Step the worker one claim at a time after apply and
    watch the loop status: never COMPLETE early, every re-extraction job runs exactly once, none lost."""
    from infra.worker.job_worker import claim_and_admit_one
    from tests.pitch_brief.conftest import PITCH_SCOPE
    c, apj, post = _apply_chain(env, ch, monkeypatch, drain_after=False)
    seen_status, steps = [], 0
    while steps < 200:
        claim = claim_and_admit_one(env.store, env.tenant, env.ws, "w")
        if claim is None:
            break
        steps += 1
        r = _status(env, ch, c, apj)
        if r.status_code == 200:
            seen_status.append((r.json()["status"], r.json()["stages"]["reanalysis"]))
    final = _status(env, ch, c, apj).json()
    ra = final["stages"]["reanalysis"]
    assert ra == {"completed": len(PITCH_SCOPE), "total": len(PITCH_SCOPE)}      # all six, none lost
    assert post.calls == len(PITCH_SCOPE)                                        # exactly one provider call each
    assert all(s != "COMPLETE" for s, _ in seen_status) and final["status"] == "PARTIAL"
    done_counts = [r_["completed"] for _, r_ in seen_status]
    assert done_counts == sorted(done_counts)                                   # progress is monotonic
    jobs = env.store.jobs_by_prefix(env.tenant, env.ws, f"reanalysis-apply-{apj}-")
    assert len({j["job_id"] for j in jobs}) == len(jobs) == len(PITCH_SCOPE)
    assert all(j["execution_status"] == "COMPLETED" for j in jobs)
    # nothing left to claim: a further drain neither calls the provider nor changes the totals
    drain(env)
    assert post.calls == len(PITCH_SCOPE)


def test_loop_status_reports_supported_edit_separately_and_a_text_number_is_not_alignment(env, ch, monkeypatch):
    c, apj, post = _apply_chain(env, ch, monkeypatch)
    s = _status(env, ch, c, apj).json()
    assert s["supported_edit_status"] == "VERIFIED" and s["request_status"] == "PARTIAL" and s["lineage"] in ("ok", "unproven")
    # a number found inside prose/code is never alignment: such a residual is reported as unresolved, not as "at_new_value"
    prose = [x for x in s["residual_counterevidence"] if x["draft_status"] in ("MANUAL_CHANGE", "AMBIGUITY", "UNSUPPORTED")]
    assert all(x["current_state"] != "at_new_value" or x["draft_status"] == "UNADDRESSED" for x in prose) or not prose


def test_lineage_of_an_apply_must_name_the_same_draft():
    from infra.worker import change_loop as cl

    class Store:
        def __init__(self, named):
            self.rec = {"apply": {"payload": {"approval_job_id": "ap"}}, "ap": {"payload": {"proposal_job_id": "pr"}},
                        "pr": {"payload": {"draft_job_id": named} if named else {}}}

        def job_record(self, tenant, ws, job_id):
            return self.rec[job_id]
    apply_ = {"payload": {"approval_job_id": "ap"}}
    assert cl._lineage(Store("d1"), "t", "w", "d1", apply_) == "ok"
    assert cl._lineage(Store("d2"), "t", "w", "d1", apply_) == "mismatch"
    assert cl._lineage(Store(None), "t", "w", "d1", apply_) == "unproven"


class ConflictStub:
    """The model abstains because two sources disagree and quotes the value each source states, plus one invented quote."""
    def __init__(self, quotes):
        self.calls, self.quotes, self.prompts = 0, quotes, []
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        self.prompts.append(kw["contents"])
        obj = {"status": "abstained", "answer": None, "states_absence": False,
               "evidence": [{"artifact_path": p, "quote": q} for p, q in self.quotes],
               "question_for_human": "The sources disagree on the manager limit; which one is current?",
               "scope_examined": sorted({p for p, _ in self.quotes})}
        return SimpleNamespace(text=json.dumps(obj), model_version=GAP_MODEL,
                               candidates=[SimpleNamespace(finish_reason="STOP")], usage_metadata=_usage())


def test_abstention_over_conflicting_sources_keeps_only_server_checked_quotes(env, ch, monkeypatch):
    """Gap found live 2026-10-08: an abstention listed the conflicting values in prose with no citation. The prompt now asks for one
    quote per source; the server keeps ONLY quotes it can resolve in the pinned revision (grounding unchanged: a made-up quote is dropped)."""
    c, apj, post = _apply_chain(env, ch, monkeypatch)
    head = env.store.head(env.tenant, env.ws).source_revision_id
    view = env.store.source_revision_view(env.tenant, env.ws, head)
    aid = next(a["artifact_id"] for a in view["artifacts"] if a["logical_path"] == APPROVAL)
    stub = ConflictStub([(APPROVAL, "routing_rules.standard_manager_threshold_jpy: 500000"),
                         (APPROVAL, "routing_rules.standard_manager_threshold_jpy: 123456")])
    monkeypatch.setattr(gap_answer, "_client_for_model", lambda m: stub)
    jid, r = create_job(c.req, env, "gap.answer", {"question": "What is the Manager approval limit for standard orders?",
                                                   "question_id": "q-conflict", "artifact_ids": [aid]})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    drain(env)
    res = _res(job_body(ch.admin, env, jid))
    assert "conflict or differ" in stub.prompts[0] and "one verbatim quote for each source" in stub.prompts[0]
    assert res["status"] == "abstained" and res["answer"] is None and res["question_for_human"]
    assert [s["quote"] for s in res["evidence_spans"]] == ["routing_rules.standard_manager_threshold_jpy: 500000"]
    assert res["evidence_spans"][0]["source_revision_id"] == head
    assert [x["reason"] for x in res["rejected_spans"]] == ["quote_not_found"]


def test_explorer_tells_unchanged_files_from_changed_files_after_an_apply(env, ch, monkeypatch):
    """Found 2026-10-08: after an apply every rule not re-read was shown "from an older revision", although 12 of 13 files had not changed
    and their output was still valid. A rule is out of date only when ITS FILE changed after it was read.
    State built like the live TARGETED re-read: only the changed file has a new extraction job, the others keep their earlier one."""
    from infra import v2_graph
    c, apj, post = _apply_chain(env, ch, monkeypatch)
    head = env.store.head(env.tenant, env.ws).source_revision_id
    view = env.store.source_revision_view(env.tenant, env.ws, head)
    aid = next(a["artifact_id"] for a in view["artifacts"] if a["logical_path"] == APPROVAL)
    with env.store._tx() as cur:                 # disposable test database: drop the re-reads of the UNCHANGED files
        cur.execute("""SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'
            AND job_id LIKE 'reanalysis-apply-%%' AND payload->>'artifact_id' <> %s""", (env.tenant, env.ws, aid))
        gone = [r[0] for r in cur.fetchall()]
        assert gone, "the FULL re-read of this fixture must include unchanged files"
        for t in ("v2_job_cost_ledger", "v2_job_attempts", "v2_jobs"):
            cur.execute(f"DELETE FROM {t} WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)", (env.tenant, env.ws, gone))
        cur.execute("""DELETE FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED' AND payload->>'job_id' = ANY(%s)""",
                    (env.tenant, env.ws, gone))
    g = v2_graph.build_graph(env.store, env.tenant, env.ws, with_evidence=False)
    cands = [n for n in g["nodes"] if n["type"] == "candidate"]
    mine = [n for n in cands if n["detail"].get("logical_path") == APPROVAL]
    others = [n for n in cands if n["detail"].get("logical_path") != APPROVAL]
    assert mine and others
    assert all(not n["stale"] and not n["file_unchanged_since_read"] for n in mine)             # re-read on the new revision
    assert all(not n["stale"] and n["state"] != "stale" and n["file_unchanged_since_read"] and n["read_at_earlier_revision"] for n in others)
    cov = g["coverage"]
    assert cov["stale_candidates"] == 0 and cov["unchanged_since_read_candidates"] == len(others)


def test_a_changed_file_that_was_not_re_read_is_out_of_date(env, ch, monkeypatch):
    """The other half: the file DID change and its re-read is missing -> its rules are out of date (stale), and the unchanged ones are still valid."""
    from infra import v2_graph
    c, apj, post = _apply_chain(env, ch, monkeypatch)
    head = env.store.head(env.tenant, env.ws).source_revision_id
    view = env.store.source_revision_view(env.tenant, env.ws, head)
    aid = next(a["artifact_id"] for a in view["artifacts"] if a["logical_path"] == APPROVAL)
    with env.store._tx() as cur:
        cur.execute("""SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'
            AND job_id LIKE 'reanalysis-apply-%%' AND payload->>'artifact_id' = %s""", (env.tenant, env.ws, aid))
        gone = [r[0] for r in cur.fetchall()]
        assert gone
        for t in ("v2_job_cost_ledger", "v2_job_attempts", "v2_jobs"):
            cur.execute(f"DELETE FROM {t} WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)", (env.tenant, env.ws, gone))
        cur.execute("""DELETE FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED' AND payload->>'job_id' = ANY(%s)""",
                    (env.tenant, env.ws, gone))
    g = v2_graph.build_graph(env.store, env.tenant, env.ws, with_evidence=False)
    mine = [n for n in g["nodes"] if n["type"] == "candidate" and n["detail"].get("logical_path") == APPROVAL]
    assert mine and all(n["stale"] and n["state"] == "stale" and not n["file_unchanged_since_read"] for n in mine)
    assert g["coverage"]["stale_candidates"] == len(mine)
