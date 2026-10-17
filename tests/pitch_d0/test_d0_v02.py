"""D0 v0.2 tests: reuse of product deterministic code. Synthetic fixtures only."""
import pathlib
import subprocess
import sys

from core.change_intelligence.v2.source_ingest_adapters import parse_yaml
from pitch.d0 import extract_from_observations


def run(content):
    res = parse_yaml("a.yaml", content)
    return extract_from_observations(res.observations, artifact_id="a1", source_revision_id="r1", blob_hash="h")


def test_v02_value_parsing_via_product_parser():
    y = (b"a:\n  max_fee: '1,500 USD'\n  min_stay: '>= 3'\n  cap_ratio: '12 %'\n  limit_x: 'abc1'\n"
         b"b:\n  timeout: '30 minutes'\n  floor_amount: '\xe2\x89\xa5 10'\n")
    c = {(x["subject"], x["predicate"]): x for x in run(y)["candidates"]}
    assert c[("a", "max_fee")]["value"] == 1500 and c[("a", "max_fee")]["unit"] == "USD"
    assert c[("a", "min_stay")]["value"] == ">= 3" and "operator >=" in c[("a", "min_stay")]["rationale"]
    assert c[("a", "cap_ratio")]["unit"] == "%"
    assert ("a", "limit_x") not in c  # ambiguous text is not a number
    assert c[("b", "timeout")]["value"] == 30 and c[("b", "timeout")]["unit"] == "minutes"
    assert "operator \u2265" in c[("b", "floor_amount")]["rationale"]


def test_value_text_parity_with_production():
    from infra.worker.extraction import _value_text as prod
    from pitch.d0.extractor import _value_text as mine
    for v in (None, "x", 3, 2.5, True, [1, "a"], {"b": 1, "a": 2}):
        assert prod(v) == mine(v)


def test_d0_import_is_provider_free_in_fresh_process():
    code = ("import sys, pitch.d0\n"
            "bad=[m for m in sys.modules if m.split('.')[0] in ('ai_module','requests','httpx','psycopg','psycopg2')"
            " or m.startswith(('google.genai','google.generativeai','google.auth','google.api_core'))"
            " or m in ('infra.worker.live_analysis','infra.worker.extraction')]\n"
            "assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=str(pathlib.Path(__file__).resolve().parents[2]))
