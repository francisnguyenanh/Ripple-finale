"""Every pinned Final output schema (config/<role>.json) must convert into the provider's structured-output schema the way `call_pinned_json` sends it, so a pinned file
can never make the live call fail at the SDK boundary. Pure: SDK conversion only, no network, no provider."""
import json
import pathlib

import pytest

from infra.worker.explain import provider_schema

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROLES = ("knowledge_qa", "change_understand", "change_impact_deps", "change_draft_guidance", "knowledge_gap_proposals", "gap_answer_interpret")


@pytest.mark.parametrize("name", ROLES)
def test_each_pinned_output_schema_converts_for_the_provider(name):
    from google.genai import _transformers as tr
    schema = json.loads((ROOT / "config" / f"{name}.json").read_text(encoding="utf-8"))["output_schema"]
    converted = provider_schema(schema)
    out = tr.t_schema(None, converted)
    assert out is not None and set(schema["required"]) == set(converted["required"])      # the key contract is unchanged


def test_null_types_and_null_enum_members_become_nullable_and_unsupported_keywords_are_dropped():
    s = {"type": "object", "additionalProperties": False, "required": ["a", "b"],
         "properties": {"a": {"type": ["string", "null"], "maxLength": 5}, "b": {"type": "string", "enum": ["x", None]}, "c": {"type": "array", "items": {"type": ["integer", "null"]}}}}
    out = provider_schema(s)
    assert out["properties"]["a"] == {"type": "string", "nullable": True} and out["properties"]["b"] == {"type": "string", "nullable": True, "enum": ["x"]}
    assert out["properties"]["c"]["items"] == {"type": "integer", "nullable": True} and "additionalProperties" not in out and out["required"] == ["a", "b"]
