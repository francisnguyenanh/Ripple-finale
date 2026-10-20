"""Local rehearsal (tools/judge_local/sim_final.py): every SIMULATED decline is schema-shaped for its role and is reached from that role's own prompt template or rubric,
so the rehearsal screens show labelled declines instead of failing. Pure; no DB, no provider."""
import json
import pathlib

from tools.judge_local import sim_final as sf

CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config"


def _schema(name, key):
    d = json.loads((CONFIG / f"{name}.json").read_text(encoding="utf-8"))
    return d[{"prompt_template": "output_schema", "candidate_prompt_template": "candidate_output_schema", "unbound_prompt_template": "unbound_output_schema"}[key]], d[key]


def test_each_extension_decline_matches_its_schema_and_is_dispatched_from_its_template():
    client = sf.SimFinal("gemini-3.5-flash-lite", fallback=None)
    for (name, key), decline in sf.EXTRA.items():
        schema, template = _schema(name, key)
        assert set(decline) == set(schema["required"]), (name, key)
        verdict = "status" if "status" in schema["properties"] else "outcome"                 # the request assistant names its verdict `outcome`
        assert decline[verdict] in schema["properties"][verdict]["enum"], (name, key)
        out = json.loads(client.models.generate_content(contents=template.replace("{", "(").replace("}", ")")).text)
        assert out == decline, (name, key)
    markers = list(client.markers.values())
    assert len(markers) == len(set(markers)) and all(markers)


def test_simulation_roles_are_told_apart_by_their_rubric_and_match_their_schemas():
    d = json.loads((CONFIG / "ma_simulation.json").read_text(encoding="utf-8"))
    client = sf.SimFinal("gemini-3.5-flash-lite", fallback=None)
    for role, rubric in d["rubrics"].items():
        schema = d["output_schemas"][d["role_kinds"][role]]
        out = json.loads(client.models.generate_content(contents="header\n" + rubric).text)
        assert set(out) == set(schema["required"]) and out["status"] in schema["properties"]["status"]["enum"], role
