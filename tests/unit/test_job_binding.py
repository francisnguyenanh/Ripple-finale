"""LHA-00 / LLM-00 (2026-10-18): an AI job id carries the role's frozen prompt/schema and pinned model; the same inputs under a changed configuration are a new job,
never a replay of an old result. Pure (no DB, no provider); the config loaders are replaced, nothing is written."""
import importlib

import pytest

from core.change_intelligence.v2 import business_mining as bm
from infra.worker import job_binding


@pytest.mark.parametrize("kind", sorted(job_binding.KINDS))
def test_the_fingerprint_follows_the_prompt_config_and_the_pinned_model(kind, monkeypatch, tmp_path):
    module = importlib.import_module(job_binding.KINDS[kind][0])
    base = job_binding.fingerprint(kind)
    assert job_binding.fingerprint(kind) == base                                                       # stable for the same configuration
    real = module.load_config
    monkeypatch.setattr(module, "load_config", lambda: (real()[0], "0" * 64))
    assert job_binding.fingerprint(kind) != base                                                       # another prompt/schema
    monkeypatch.setattr(module, "load_config", real)
    import os
    from infra.worker import budget_config
    if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        monkeypatch.setattr(budget_config, "model_for_role", lambda config, role: "another-model")
        assert job_binding.fingerprint(kind) != base                                                   # another pinned model


def test_job_ids_change_with_the_binding_and_keep_their_other_inputs(monkeypatch):
    from infra.worker import gap_answer_interpret as gai, gap_interview_question as giq, knowledge_gap_proposals as gp
    def ids():
        return (gai.job_id_for("s", "g", "The plant manager."), giq.job_id_for("sess", 1), gp.job_id_for("s", 1))
    before = ids()
    assert gai.job_id_for("s", "g", "  the PLANT manager. ") == before[0]                               # same words, same configuration: the stored reading
    assert bm.group_job_id("s", "g", 1, "b1") != bm.group_job_id("s", "g", 1, "b2") and bm.group_job_id("s", "g", 1, "b1") == bm.group_job_id("s", "g", 1, "b1")
    for mod in (gai, giq, gp):
        real = mod.load_config
        monkeypatch.setattr(mod, "load_config", lambda real=real: (real()[0], "f" * 64))
    assert all(a != b for a, b in zip(ids(), before))                                                  # a changed prompt is a new job for each kind
