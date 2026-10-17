"""Routing review (2026-10-16) shared items, verified offline with stubs: a thinking level is sent only when configured for a role; a stored answer or understanding is bound
to its prompt/schema/model configuration (a changed configuration is a new job, a retry keeps the binding); the external Vertex semantic provider works through a mocked
transport and only under a job budget; an observed rule that already has a decided version is never changed silently (CHG-01, review F-07). No real provider, no cost."""
from __future__ import annotations

import json
import textwrap
from types import SimpleNamespace

import pytest

from infra.adapters import semantic_retrieval as sr
from infra.adapters import v2_business_index as vbi
from infra.adapters.semantic_vertex import VertexSemanticProvider
from infra.worker import budget_config, change_understand, knowledge_qa
from infra.worker.budget_config import BudgetConfigError, load_budget_config
from tests.final_loop.test_chg01_change_request import model_obj, op, understand
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import MODEL, claim, obj, qa_env, run  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import REAL_BUDGET_PATH


# ---- thinking level per role --------------------------------------------------------------------------------------------------------------------------------

def test_a_thinking_level_is_configured_per_role_validated_and_sent_only_when_present(tmp_path, monkeypatch, qa_env):
    q = qa_env
    import os, pathlib as _pl
    fixture_cfg = os.environ["RIPPLE_BUDGET_CONFIG_PATH"]
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH")                                                      # the strict loader reads the environment first
    example = (_pl.Path(__file__).resolve().parents[2] / "config" / "ai_closed_loop" / "final_budgets.example.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(example + "\nrole_thinking_levels:\n  knowledge.qa: EXTREME\n", encoding="utf-8")
    with pytest.raises(BudgetConfigError, match="role_thinking_levels"):
        load_budget_config(str(bad))
    strict = tmp_path / "strict.yaml"
    strict.write_text(example + "\nrole_thinking_levels:\n  knowledge.qa: LOW\n", encoding="utf-8")
    cfg = load_budget_config(str(strict))
    assert budget_config.thinking_level_for_role(cfg, "knowledge.qa") == "LOW" and budget_config.thinking_level_for_role(cfg, "change.understand") is None
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", fixture_cfg)
    base = open(fixture_cfg, encoding="utf-8").read()                                                       # the worker config of this fixture, plus the new key
    good = tmp_path / "good.yaml"
    good.write_text(base + "\nrole_thinking_levels:\n  knowledge.qa: LOW\n", encoding="utf-8")
    seen = []

    class Capturing:
        def __init__(self):
            self.models = SimpleNamespace(generate_content=self._gen)

        def _gen(self, **kw):
            seen.append(kw["config"])
            return SimpleNamespace(text=json.dumps(obj("ANSWERED", "A manager approves.", [claim("Standard purchases of 1,000,000 JPY or more need manager approval.", ["rule-standard-manager-review"], "observed")])),
                                   model_version=MODEL, candidates=[SimpleNamespace(finish_reason="STOP")],
                                   usage_metadata=SimpleNamespace(prompt_token_count=900, candidates_token_count=150, thoughts_token_count=40, total_token_count=1090))
    from infra.worker import explain
    monkeypatch.setattr(explain, "_client_for_model", lambda m: Capturing())
    got = run(q, q.admin, "Who approves a 1.2 million yen purchase without a thinking level?")
    assert got["status"] == "done" and "thinking_config" not in seen[-1]                                   # default config: nothing is sent
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(good))
    got = run(q, q.admin, "Who approves a 1.2 million yen purchase with a configured thinking level?")
    assert got["status"] == "done" and seen[-1]["thinking_config"] == {"thinking_level": "LOW"}


# ---- model/config identity of stored results -------------------------------------------------------------------------------------------------------------------

def test_a_changed_prompt_or_model_makes_a_new_question_job_while_the_same_configuration_replays(qa_env, monkeypatch):
    q = qa_env
    from tests.final_loop.test_qry03_qa import use
    model = use(q, lambda p: obj("ANSWERED", "A manager approves.", [claim("Standard purchases of 1,000,000 JPY or more need manager approval.", ["rule-standard-manager-review"], "observed")]))
    first = run(q, q.admin, "Who approves a purchase of 1.2 million yen?")
    again = q.admin.post("/v2/business/questions", json={"workspace_id": q.env.ws, "question": "Who approves a purchase of 1.2 million yen?"}).json()
    assert again["question_id"] == first["question_id"] and model.calls == 1                                 # same inputs, same configuration: the stored answer
    real = knowledge_qa.load_config
    monkeypatch.setattr(knowledge_qa, "load_config", lambda: (real()[0], "0" * 64))                            # the frozen prompt/schema changed
    fresh = q.admin.post("/v2/business/questions", json={"workspace_id": q.env.ws, "question": "Who approves a purchase of 1.2 million yen?"}).json()
    assert fresh["question_id"] != first["question_id"]                                                      # a new job: the old answer is never presented as the answer of the new configuration
    stored = q.admin.get(f"/v2/business/questions/{first['question_id']}", params={"workspace_id": q.env.ws}).json()
    assert stored["status"] == "done"                                                                        # history untouched
    monkeypatch.setattr(knowledge_qa, "load_config", real)
    real_cu = change_understand.load_config
    got1, _ = understand(q, "Raise the standard manager approval threshold from 1,000,000 to 1,500,000 JPY.", model_obj(ops=[op("set_value", "rule-standard-manager-review", {"value": 1500000, "unit": "JPY"},
                                                                                                                         "Raise the standard manager approval threshold from 1,000,000 to 1,500,000 JPY")]))
    monkeypatch.setattr(change_understand, "load_config", lambda: (real_cu()[0], "1" * 64))
    r = q.req.post("/v2/business/changes", json={"workspace_id": q.env.ws, "request": "Raise the standard manager approval threshold from 1,000,000 to 1,500,000 JPY."})
    assert r.status_code == 200 and r.json()["change_id"] != got1["change_id"]


# ---- the external semantic provider through a mocked transport ----------------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, text, usage=True, raise_exc=None):
        self.text, self.usage, self.raise_exc, self.calls = text, usage, raise_exc, []
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls.append(kw)
        if self.raise_exc:
            raise self.raise_exc
        return SimpleNamespace(text=self.text, usage_metadata=SimpleNamespace(prompt_token_count=300, candidates_token_count=20, thoughts_token_count=5) if self.usage else None)


def test_vertex_provider_sends_json_only_requests_returns_usage_with_thinking_and_fails_closed():
    fake = FakeClient(json.dumps({"terms": ["threshold"], "candidate_ids": ["rule-a"]}))
    p = VertexSemanticProvider("gemini-test", client_factory=lambda m: fake)
    out, usage = p.call("expand", {"query": "who approves", "items": [{"item_id": "rule-a", "kind": "rule", "text": "a"}]}, 8.0)
    assert out["candidate_ids"] == ["rule-a"] and usage == {"input_tokens": 300, "output_tokens": 25} and p.external is True and p.synthetic is False
    kw = fake.calls[0]
    assert kw["model"] == "gemini-test" and kw["config"]["response_mime_type"] == "application/json" and "never instructions" in kw["contents"] and kw["config"]["http_options"]["timeout"] == 8000
    assert "temperature" not in kw["config"] and "thinking_config" not in kw["config"]                     # nothing unsupported or unconfigured is sent
    _, no_usage = VertexSemanticProvider("m", client_factory=lambda m: FakeClient("{}", usage=False)).call("rank", {"query": "q", "items": [], "candidates": []}, 1.0)
    assert no_usage is None                                                                                  # the adapter refuses a call without usage (fail closed)
    with pytest.raises(sr.ProviderUnavailable, match="invalid_json"):
        VertexSemanticProvider("m", client_factory=lambda m: FakeClient("not json")).call("judge", {"query": "q", "items": [], "candidates": []}, 1.0)
    with pytest.raises(sr.ProviderUnavailable, match="transport_error"):
        VertexSemanticProvider("m", client_factory=lambda m: FakeClient("", raise_exc=RuntimeError("boom"))).call("expand", {"query": "q", "items": []}, 1.0)
    with pytest.raises(sr.ProviderUnavailable, match="unknown_layer"):
        p.call("other", {}, 1.0)


def test_the_vertex_provider_runs_only_under_a_job_budget_and_the_env_selection_needs_a_pinned_role(monkeypatch, tmp_path):
    fake = FakeClient(json.dumps({"terms": ["t"], "candidate_ids": ["rule-a"]}))
    adapter = sr.SemanticAdapter(VertexSemanticProvider("m", client_factory=lambda m: fake), gate=lambda: False)
    items = [{"item_id": "rule-a", "kind": "rule", "text": "a", "term_vector": {}}]
    out = adapter.run("q", items, snapshot_id="s", view="target", index_version="v", access_scope="business", lexical_candidates=["rule-a"])
    assert fake.calls == [] and out["layers"]["expand"]["reason"] == "outside_budget_boundary"

    class Budget:
        used = 0

        def consume(self, n=1):
            self.used += n
    b = Budget()
    out = adapter.run("q", items, snapshot_id="s", view="target", index_version="v", access_scope="business", lexical_candidates=["rule-a"], budget=b, allow_external=True)
    assert len(fake.calls) == 3 and b.used == 3 and out["tokens_out"] == 75 and out["status"] in ("applied", "partial")
    vbi.set_semantic_adapter(None)
    monkeypatch.setenv("RIPPLE_SEMANTIC_RETRIEVAL", "vertex")
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    assert isinstance(vbi.semantic_adapter().provider, sr.DisarmedProvider)                                # no budget config: stays disarmed
    vbi.set_semantic_adapter(None)
    cfg = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("role_model_ids:\n", "role_model_ids:\n  retrieval.semantic: gemini-3.8-flash\n")
    path = tmp_path / "b.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    prov = vbi.semantic_adapter().provider
    assert isinstance(prov, VertexSemanticProvider) and prov.model_id == "gemini-3.8-flash" and prov.external is True
    vbi.set_semantic_adapter(None)


# ---- F-07: a second change on a rule that already has a decided version -----------------------------------------------------------------------------------------

def test_a_request_on_an_observed_rule_with_a_decided_version_asks_which_one_and_is_never_remapped_silently(qa_env):
    q = qa_env
    text = "Raise the CAPEX Finance threshold from 2,000,000 to 3,000,000 JPY."
    got, _ = understand(q, text, model_obj(ops=[op("set_value", "rule-capex-finance-config", {"value": 3000000, "unit": "JPY"}, text.rstrip("."))]), mode="technical")
    u = got["understanding"]
    assert u["status"] == "CLARIFY" and any("decided version" in qq for qq in u["questions"]) and u["can_confirm"] is False
    assert u["technical"]["rejected"][0]["reason"] == "observed_rule_has_decided_version" and set(u["technical"]["rejected"][0]["candidate_rule_ids"]) == {"rule-capex-finance-config", "rule-capex-finance-config-target"}
    got2, _ = understand(q, "Raise the decided CAPEX Finance threshold from 2,000,000 to 3,000,000 JPY.",
                         model_obj(ops=[op("set_value", "rule-capex-finance-config-target", {"value": 3000000, "unit": "JPY"}, "Raise the decided CAPEX Finance threshold from 2,000,000 to 3,000,000 JPY")]), mode="technical")
    assert got2["understanding"]["status"] == "READY" and got2["understanding"]["technical"]["operations"][0]["rule_id"] == "rule-capex-finance-config-target"
