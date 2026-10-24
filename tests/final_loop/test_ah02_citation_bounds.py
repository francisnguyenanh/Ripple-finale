"""AH-02: citation -> source resolves a bounded, de-duplicated page of bindings; the work (transactions) depends on the page, not on how many bindings the cited
rules carry; the shown sources and their order equal the first page of the previous behaviour (minus duplicates); `sources_total` / `has_more` count the
authorized bindings only; a JSON code locator keeps its file. Throwaway PostgreSQL, synthetic model, the real route."""
from __future__ import annotations

import json

import pytest

from infra.adapters import v2_request_threads as rt
from infra.adapters.v2_postgres import PostgresV2Store
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_citation_source import asked, cite_impact_and_state, source
from tests.final_loop.test_request_assistant_jobs import ra_env, use  # noqa: F401  (ra_env is a fixture)


def _real_binding(q):
    head = q.env.store.head(q.env.tenant, q.env.ws)
    view = q.env.store.source_revision_view(q.env.tenant, q.env.ws, head.source_revision_id)
    a = next(x for x in view["artifacts"] if str(x.get("logical_path", "")).endswith((".yaml", ".yml")))
    return head.source_revision_id, a["artifact_id"], a.get("logical_path")


@pytest.fixture
def cited(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    use(q, cite_impact_and_state)
    tid, mid, refs = asked(q, approver, cid, "key-ah02-0001")
    return q, approver, tid, mid, refs


def test_work_depends_on_the_page_not_on_the_number_of_bindings(cited, monkeypatch):
    q, approver, tid, mid, refs = cited
    rev, art, path = _real_binding(q)
    counts = {}
    original_tx = PostgresV2Store._tx

    def counting_tx(self):
        counts["tx"] = counts.get("tx", 0) + 1
        return original_tx(self)

    for n in (1, 10, 100, 1000):
        rule = {"rule_id": "rule-synthetic", "source_bindings": [{"artifact_id": art, "revision_id": rev, "evidence_ref": f"{path}#k{i}"} for i in range(n)]}
        monkeypatch.setattr(rt, "_rules_for_fact", lambda *a, **k: [rule])
        monkeypatch.setattr(PostgresV2Store, "_tx", counting_tx)
        counts["tx"] = 0
        r = source(q, approver, tid, mid, refs["impact"])
        monkeypatch.setattr(PostgresV2Store, "_tx", original_tx)
        body = r.json()
        assert r.status_code == 200 and body["available"] is True, body
        assert len(body["sources"]) == min(n, rt.CITATION_PAGE) and body["sources_total"] == n and body["has_more"] is (n > rt.CITATION_PAGE)
        assert [s["locator"] for s in body["sources"]] == [f"k{i}" for i in range(min(n, rt.CITATION_PAGE))]     # the same first page, same order
        counts[n] = counts["tx"]
    assert counts[1000] == counts[100] == counts[10]                                            # bounded by the page, independent of the binding count
    assert counts[10] - counts[1] <= 12                                                          # page work: the per-source excerpt reads only


def test_duplicates_are_counted_once_and_a_json_code_locator_keeps_its_file(cited, monkeypatch):
    q, approver, tid, mid, refs = cited
    rev, art, path = _real_binding(q)
    code_loc = json.dumps({"line_end": 9, "line_start": 1, "qualname": "route_request", "type": "code_span"}, sort_keys=True)
    rule = {"rule_id": "rule-synthetic", "source_bindings": [{"artifact_id": art, "revision_id": rev, "evidence_ref": f"{path}#a"},
                                                             {"artifact_id": art, "revision_id": rev, "evidence_ref": f"{path}#a"},
                                                             {"artifact_id": art, "revision_id": rev, "evidence_ref": code_loc}]}
    monkeypatch.setattr(rt, "_rules_for_fact", lambda *a, **k: [rule, rule])
    body = source(q, approver, tid, mid, refs["impact"]).json()
    assert body["sources_total"] == 2 and [s["locator"] for s in body["sources"]] == ["a", "route_request"]
    assert body["sources"][1]["logical_path"] == path                                             # the file, not the locator text
    assert "code_span" not in json.dumps(body)
