"""Pitch comparator preservation: REAL ExtractRulesExecutor (stub provider, model-shaped payloads) over the REAL import
path (prepare/commit/ingest worker) and the real store; result then round-tripped through JSONB (as persisted in
v2_jobs.execution_result) and fed read-only to pitch.eval.scorer._pair. SYNTHETIC fixtures, not the Pitch set."""
from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest

DSN = os.environ.get("PITCH_CMP_TEST_DSN", "postgresql://postgres:pw@127.0.0.1:55821/t")

DOC = b"""# Synthetic policy

Lodging per night is limited to at most 9000 JPY.

Reviewers per request must be at least 2 people.

Fee is below 300 JPY per item.

Weight is up to 40 kg.

Quota exceeds 70 units.

Stock is no less than 15 units.

Batch size is 25 units.

Retention is about 30 days.

Cap is 500 USD per case.

Limit is 80 percent.
"""
Q = {  # fixture -> verbatim quote
    "upper": "Lodging per night is limited to at most 9000 JPY.", "lower": "Reviewers per request must be at least 2 people.",
    "lt": "Fee is below 300 JPY per item.", "le": "Weight is up to 40 kg.", "gt": "Quota exceeds 70 units.",
    "ge": "Stock is no less than 15 units.", "plain": "Batch size is 25 units.", "ambig": "Retention is about 30 days.",
    "conflict": "Cap is 500 USD per case.", "invalid": "Limit is 80 percent."}


def cand(key, subject, value, unit, **extra):
    c = {"subject": subject, "predicate": "limit", "value": value, "unit": unit, "modality": "must",
         "scope": {"section": "policy"}, "exceptions": [], "outcome": None, "quotes": [Q[key]], "rationale": "r"}
    c.update(extra)
    return c


# key -> (candidate, gold comparator or None for plain, expected operator in result or None, rejected?)
CASES = {
    "upper": (cand("upper", "lodging", 9000, "JPY", operator="<="), "<=", "<=", False),
    "lower": (cand("lower", "reviewers", 2, "people", operator=">="), ">=", ">=", False),
    "lt": (cand("lt", "fee", 300, "JPY", operator="<"), "<", "<", False),
    "le": (cand("le", "weight", 40, "kg", operator="<="), "<=", "<=", False),
    "gt": (cand("gt", "quota", 70, "units", operator=">"), ">", ">", False),
    "ge": (cand("ge", "stock", 15, "units", operator=">="), ">=", ">=", False),
    "plain": (cand("plain", "batch", 25, "units"), None, None, False),
    "ambig": (cand("ambig", "retention", 30, "days", operator=None), None, None, False),
    "conflict": (cand("conflict", "cap", ">= 500", "USD", operator="<="), "<=", None, True),
    "invalid": (cand("invalid", "limit", 80, "percent", operator="at most"), "<=", None, True),
}


def _table(row):
    out = os.environ.get("PITCH_CMP_TABLE")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    try:
        with psycopg2.connect(DSN, connect_timeout=2):
            pass
    except psycopg2.OperationalError:
        pytest.skip("needs a disposable PostgreSQL; set PITCH_CMP_TEST_DSN")
    from fastapi.testclient import TestClient
    from infra import judge, v2_source_routes
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store
    from infra.worker.job_worker import claim_and_admit_one

    class Blobs:
        def __init__(self):
            self.d = {}

        def put_immutable(self, digest, content):
            self.d.setdefault(digest, content)

        def read(self, digest):
            return self.d[digest]

    cfgp = tmp_path_factory.mktemp("cfg") / "b.yaml"
    cfgp.write_text("default_model_id: m\nmodel_prices:\n  m:\n    input_usd_per_1k: '0.01'\n    output_usd_per_1k: '0.01'\n"
                    "max_output_tokens: 2048\nmax_input_tokens_per_call: 8192\nmax_retries: 1\nmax_provider_calls_per_job: 1\n"
                    "per_job_cap_usd: '1.00'\ntotal_cap_usd: '50.00'\nfail_closed_on_missing_usage: true\n"
                    "latency_slo_p50_seconds: 5\nlatency_slo_p95_seconds: 20\n", encoding="utf-8")
    saved = {k: os.environ.get(k) for k in ("RIPPLE_V2_DSN", "RIPPLE_V2_BLOB_BUCKET", "RIPPLE_BUDGET_CONFIG_PATH")}
    os.environ.update({"RIPPLE_V2_DSN": DSN, "RIPPLE_V2_BLOB_BUCKET": "b", "RIPPLE_BUDGET_CONFIG_PATH": str(cfgp)})
    blobs = Blobs()
    orig = v2_source_routes._blob_port
    v2_source_routes._blob_port = lambda: blobs
    n = uuid4().hex[:10]
    tenant, project, ws, acc, cred = f"t-{n}", f"p-{n}", f"w-{n}", f"a-{n}", f"c-{n}"
    store = PostgresV2Store(DSN, blobs)
    store.migrate()
    store.create_workspace(tenant, project, ws)
    ids = PostgresIdentityStore(DSN)
    ids.migrate()
    ids.bootstrap_admin(tenant, project, ws, acc, cred)
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": acc, "credential": cred}).status_code == 200
    r = c.post("/v2/sources/imports", json={"workspace_id": ws, "idempotency_key": uuid4().hex, "files": [
        {"path": "policy.md", "content_b64": base64.b64encode(DOC).decode()}]})
    assert r.status_code == 200, r.text
    imp = r.json()["import_id"]
    r = c.post(f"/v2/sources/imports/{imp}/commit", json={"workspace_id": ws, "expected_version": 0, "acknowledged_skips": []})
    assert r.status_code == 200, r.text
    while claim_and_admit_one(store, tenant, ws, "w"):
        pass
    rev = store.head(tenant, ws).source_revision_id
    view = store.source_revision_view(tenant, ws, rev)
    art = next(a for a in view["artifacts"] if a["logical_path"] == "policy.md")
    yield SimpleNamespace(store=store, tenant=tenant, ws=ws, rev=rev, art=art["artifact_id"],
                          role=art.get("source_role") or "unknown")
    v2_source_routes._blob_port = orig
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _cfg(ceiling):
    return SimpleNamespace(default_model_id="m", model_prices={"m": object()}, max_input_tokens_per_call=ceiling,
                           max_output_tokens=2048, role_model_ids={}, request_timeout_seconds=None)


def _ctx(env, ceiling):
    from infra.worker.execution_types import ExecutionContext
    return ExecutionContext(store=env.store, config=_cfg(ceiling), request_context={}, job={
        "tenant_id": env.tenant, "workspace_id": env.ws, "pinned_source_revision_id": env.rev,
        "payload": {"artifact_id": env.art, "source_role": env.role}})


def run_executor(env, candidates, ceiling=8192):
    from infra.worker import extraction as ex
    from infra.worker.execution_types import ProviderCallBudget
    payload = {"candidates": candidates}
    stub = SimpleNamespace(models=SimpleNamespace(generate_content=lambda model, contents, config: SimpleNamespace(
        model_version="m", candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata={"prompt_token_count": 10, "candidates_token_count": 10, "total_token_count": 20},
        text=json.dumps(payload))))
    orig = ex._client_for_model
    ex._client_for_model = lambda _m: stub
    try:
        ctx = _ctx(env, ceiling)
        e = ex.ExtractRulesExecutor()
        return e.execute(ctx, e.prepare(ctx), ProviderCallBudget(1)).result
    finally:
        ex._client_for_model = orig


def stored(result):
    """Round trip through JSONB exactly as v2_jobs.execution_result persists the result dict."""
    with psycopg2.connect(DSN) as cn, cn.cursor() as cur:
        cur.execute("SELECT %s::jsonb", (json.dumps(result),))
        v = cur.fetchone()[0]
    return v if isinstance(v, dict) else json.loads(v)


def score(c, gold_cmp, subject):
    from pitch.eval import scorer as S
    g = {"subject": subject, "predicate": "limit", "comparator": gold_cmp, "value": c["value"], "unit": c["unit"], "scope": None}
    rank, reasons, _ = S._pair(g, c, False)
    return rank, reasons


@pytest.mark.parametrize("key", list(CASES))
def test_case(env, key):
    c, gold_cmp, expect_op, rejected = CASES[key]
    res = run_executor(env, [c])
    if rejected:
        assert res["candidates"] == [], res
        assert len(res["rejected"]) == 1 and res["rejected"][0]["reason"].startswith("schema_invalid"), res["rejected"]
        _table({"case": key, "rejected": res["rejected"]})
        return
    st = stored(res)
    assert len(st["candidates"]) == 1
    sc = st["candidates"][0]
    assert sc["operator"] == expect_op
    # scorer sees the stored/executor dict exactly as emitted (read-only): it reads cand['comparator'], not 'operator'
    rank_raw, reasons_raw = score(sc, gold_cmp, c["subject"])
    adapted = {**sc, "comparator": sc["operator"]}      # PROPOSED adapter mapping (test-only; scorer untouched)
    rank_ad, reasons_ad = score(adapted, gold_cmp, c["subject"])
    if gold_cmp in ("<", "<=", ">", ">="):
        assert "comparator_missing" in reasons_raw                      # INTERFACE GAP
        assert rank_ad == 3 and reasons_ad == []                        # strict only after the mapping
    else:
        assert "comparator_missing" not in reasons_raw and "comparator_missing" not in reasons_ad
    _table({"case": key, "executor_operator": sc["operator"], "value": sc["value"],
            "scorer_raw": [rank_raw, reasons_raw], "scorer_with_adapter": [rank_ad, reasons_ad]})


def test_value_text_operator_agreeing_is_accepted(env):
    res = run_executor(env, [cand("conflict", "cap", ">= 500", "USD", operator=">=")])
    assert res["candidates"][0]["operator"] == ">=" and res["candidates"][0]["value"] == ">= 500"


def test_value_text_operator_only_unchanged(env):
    res = run_executor(env, [cand("conflict", "cap", ">= 500", "USD")])
    assert res["candidates"][0]["operator"] is None and res["candidates"][0]["value"] == ">= 500"


def test_non_string_operator_rejected(env):
    res = run_executor(env, [cand("plain", "batch", 25, "units", operator=["<="])])
    assert res["candidates"] == [] and res["rejected"][0]["reason"].startswith("schema_invalid")


def test_merge_conflict_and_missing_scope_keeps_operator(env):
    a = cand("upper", "lodging", 9000, "JPY", operator="<=")
    b = cand("upper", "lodging", 9000, "JPY", operator=">=")
    res = run_executor(env, [a, b])
    assert [c["operator"] for c in res["candidates"]] == ["<=", ">="]      # two different statements: both kept
    assert res["rejected"] == [] and "dropped_for_conflict" not in res
    d = cand("lt", "fee", 300, "JPY", operator="<")
    d["scope"] = None
    res = run_executor(env, [d])
    assert res["candidates"][0]["applicability"] == "UNKNOWN" and res["candidates"][0]["operator"] == "<"


def test_prompt_guard_margin(env):
    from infra.worker import extraction as ex
    req = ex.ExtractRulesExecutor().prepare(_ctx(env, 24000))
    bound = ex._input_token_bound(req.prompt, ex._REQUEST_SCHEMA)
    assert bound <= 24000
    _table({"guard": {"bound": bound, "ceiling": 24000, "margin": 24000 - bound,
                      "prompt_bytes": len(req.prompt.encode())}})


def test_two_rules_with_one_subject_predicate_scope_both_survive(env):
    """Compatible branches (amount <= 0 invalid, amount >= N needs a manager) used to collapse into one; both stay now."""
    a = cand("upper", "lodging", 9000, "JPY", operator="<=")
    b = cand("lower", "lodging", 2, "JPY", operator=">=")
    res = run_executor(env, [a, b])
    assert [(c["operator"], c["value"]) for c in res["candidates"]] == [("<=", 9000), (">=", 2)]
    assert res["rejected"] == []


def test_same_operator_different_values_are_two_candidates(env):
    a = cand("upper", "lodging", 9000, "JPY", operator="<=")
    b = cand("lower", "lodging", 500, "JPY", operator="<=")
    res = run_executor(env, [a, b])
    assert sorted(c["value"] for c in res["candidates"]) == [500, 9000]


def test_identical_statement_twice_is_one_candidate_with_both_quotes(env):
    a = cand("upper", "lodging", 9000, "JPY", operator="<=")
    b = cand("upper", "lodging", 9000.0, "JPY", operator="<=", quotes=[Q["upper"], Q["lower"]])
    res = run_executor(env, [a, b])
    assert len(res["candidates"]) == 1 and res["candidates"][0]["ancestry"]["merged_count"] == 2
    assert [e["quote"] for e in res["candidates"][0]["evidence"]] == [Q["upper"], Q["lower"]]


def test_unit_outcome_modality_and_exception_differences_stay_visible(env):
    base = cand("upper", "lodging", 9000, "JPY", operator="<=")
    variants = [dict(base, unit="USD"), dict(base, outcome="BLOCK"), dict(base, modality="may"), dict(base, exceptions=["staff"])]
    res = run_executor(env, [base] + variants)
    assert len(res["candidates"]) == 5


def test_bool_is_not_one_and_nested_list_scope_does_not_crash(env):
    a = cand("plain", "flag", True, None)
    b = cand("plain", "flag", 1, None)
    c = cand("plain", "flag", 1, None, scope={"section": ["a", {"b": [1, 2]}]})
    res = run_executor(env, [a, b, c])
    assert len(res["candidates"]) == 3 and res["rejected"] == []


def test_guards_survive_forged_quote_and_unsized_values(env):
    good = cand("upper", "lodging", 9000, "JPY", operator="<=")
    forged = cand("upper", "lodging", 1, "JPY", operator=">=", quotes=["this sentence is not in the source"])
    huge = cand("upper", "lodging", "x" * 5000, "JPY", operator="<=")
    res = run_executor(env, [good, forged, huge])
    assert any(r["reason"] == "no_verifiable_citation" for r in res["rejected"])
    assert [c["value"] for c in res["candidates"] if c["value"] == 9000] == [9000]


def test_signature_is_hashable_for_any_scope_and_unique_when_not_comparable():
    from infra.worker import extraction as ex
    kw = dict(subject="s", predicate="p", modality="must", operator=None, value=1, unit=None, exceptions=(), outcome=None)
    sig = ex._extraction_signature(scope={"a": [1, {"b": [2]}]}, unique="u1", **kw)
    assert hash(sig) is not None
    assert ex._extraction_signature(scope={"a": 1}, unique="u1", **dict(kw, value=float("nan"))) == ("unique", "u1")
    assert ex._extraction_signature(scope={"a": 1}, unique="u2", **dict(kw, value=True)) != ex._extraction_signature(scope={"a": 1}, unique="u3", **kw)


def test_a_code_condition_against_a_configuration_attribute_is_representable_and_the_code_role_asks_for_conditions(env):
    """Synthetic DEV fixture: the schema carries a comparison against a configuration attribute (operator, attribute name as the value, outcome, scope); the CODE role instruction asks
    for decision conditions and not only input validation."""
    c = cand("upper", "orders.total", "limits.max_total", "JPY", operator=">=", outcome="ESCALATE")
    res = run_executor(env, [c])
    got = res["candidates"][0]
    assert got["operator"] == ">=" and got["value"] == "limits.max_total" and got["outcome"] == "ESCALATE" and got["scope"] == {"section": "policy"}
    from infra.worker import extraction as ex
    text = ex._ROLE_INSTRUCTIONS["implementation"]
    assert "conditions that decide an outcome" in text and "not only input validation" in text and "configuration attribute" in text
    assert "Harbor" not in text and "threshold" not in text                       # generic wording: nothing taken from one example
