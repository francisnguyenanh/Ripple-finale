"""SUR-07 contract version 2: the output schema of a job restricts question / scenario keys to the declared assumptions, and a note (a doubt that is not about a
declared assumption) is kept only as plain words without numbers, claims or markup. Pure functions; the prompt itself is asserted in the DB test's stub provider."""
from core.change_intelligence.v2 import roi_analyst as ra
from infra.worker.roi_analyst import load_config

LIMITS = {"max_statements": 6, "max_questions": 4, "max_scenarios": 3, "max_text_chars": 500, "max_notes": 2, "max_records": 2000}


def test_the_job_schema_names_only_the_declared_keys_and_leaves_the_frozen_schema_untouched():
    cfg, _ = load_config()
    assert cfg["version"] == 2 and "{declared_keys}" in cfg["prompt_template"]
    s = ra.schema_for(cfg["output_schema"], ["minutes_per_approval", "minutes_per_approval_range"])
    assert s["properties"]["assumption_questions"]["items"]["properties"]["assumption"]["enum"] == ["minutes_per_approval", "minutes_per_approval_range"]
    assert s["properties"]["scenarios"]["items"]["properties"]["assumption"]["enum"] == ["minutes_per_approval"]          # hourly_cost not declared here
    with_cost = ra.schema_for(cfg["output_schema"], ["hourly_cost", "minutes_per_approval", "minutes_per_approval_range"])
    assert with_cost["properties"]["scenarios"]["items"]["properties"]["assumption"]["enum"] == ["minutes_per_approval", "hourly_cost"]
    assert "enum" not in cfg["output_schema"]["properties"]["assumption_questions"]["items"]["properties"]["assumption"]          # the frozen file is not mutated


def test_notes_are_kept_only_as_plain_words_and_the_old_checks_are_unchanged():
    model = ra.validate_model_object({"status": "memo", "statements": [], "scenarios": [],
                                      "assumption_questions": [{"question": "Was the time per approval measured?", "assumption": "minutes_per_approval"},
                                                               {"question": "Is the data volume enough?", "assumption": "data_volume"}],
                                      "notes": ["The replay covers one month only.", "Only 30 records exist.", "We will certainly save time.", "See **this**.",
                                                "No supplier field is present.", "A third plain note."]}, LIMITS)
    out = ra.check(model, {}, ["minutes_per_approval", "minutes_per_approval_range"], LIMITS)
    assert [n["note"] for n in out["notes"]] == ["The replay covers one month only.", "No supplier field is present."]
    assert [(r["kind"], r["reason"]) for r in out["rejected"]] == [("question", "assumption_not_declared"), ("note", "note_has_number"),
                                                                   ("note", "note_not_acceptable"), ("note", "note_not_acceptable"), ("note", "over_note_cap")]
    assert [q["assumption"] for q in out["questions"]] == ["minutes_per_approval"]
