"""Source bindings stored as a JSON locator keep their file (business_draft.binding_location), and a declared source_root maps imported paths to the repository root
(code_patch.resolve_file). Live finding SUR03-V2-A10 (2026-10-23): every code binding lost its file and the proposal-only coder was offered nothing."""
import json

from core.change_intelligence.v2 import business_draft as bd
from core.change_intelligence.v2 import code_patch

ARTS = {"art-1": "fixtures/harbor_public/app/emergency.py", "art-2": "fixtures/harbor_public/config/approval.yaml"}


def test_a_json_code_span_takes_its_path_from_the_bound_artifact():
    ref = json.dumps({"line_end": 48, "line_start": 8, "qualname": "route_emergency", "type": "code_span"}, sort_keys=True)
    assert bd.binding_location({"artifact_id": "art-1", "evidence_ref": ref}, ARTS) == ("fixtures/harbor_public/app/emergency.py", "route_emergency")


def test_a_line_span_has_its_file_but_no_label():
    ref = json.dumps({"line_end": 13, "line_start": 12, "type": "line_span"})
    assert bd.binding_location({"artifact_id": "art-1", "evidence_ref": ref}, ARTS) == ("fixtures/harbor_public/app/emergency.py", None)


def test_the_path_hash_label_form_is_unchanged():
    b = {"artifact_id": "art-2", "evidence_ref": "fixtures/harbor_public/config/approval.yaml#routing_rules.standard_manager_threshold_jpy"}
    assert bd.binding_location(b, ARTS) == ("fixtures/harbor_public/config/approval.yaml", "routing_rules.standard_manager_threshold_jpy")
    assert bd.binding_location({"artifact_id": "art-2", "evidence_ref": "doc.md"}, ARTS) == ("doc.md", None)


def test_a_json_locator_of_an_artifact_not_in_the_revision_stays_unresolved():
    ref = json.dumps({"qualname": "f", "type": "code_span"})
    path, label = bd.binding_location({"artifact_id": "gone", "evidence_ref": ref}, ARTS)
    assert label == "f" and bd._resolve_artifact("gone", path, ARTS) == (None, False)


def test_file_actions_bind_a_code_span_rule_to_its_function():
    ref = json.dumps({"line_end": 9, "line_start": 1, "qualname": "route_emergency", "type": "code_span"}, sort_keys=True)
    rules = {"r1": {"rule_id": "r1", "title": "emergency threshold", "source_bindings": [{"artifact_id": "art-1", "evidence_ref": ref}]}}
    obs = [{"artifact_id": "art-1", "locator": {"qualname": "route_emergency"}, "raw_value": "def route_emergency(r):\n    return r.amount >= 1000000\n"}]
    ops = [{"op_id": "op-1", "rule_id": "r1", "kind": "set_value", "before": {"value": 1000000}, "desired": {"value": 1200000, "unit": "JPY"}}]
    items = bd.file_actions({"affected_rules": []}, ops, rules, ARTS, obs)
    assert len(items) == 1
    it = items[0]
    assert (it["path"], it["locator"], it["kind"], it["resolution"], it["reason"]) == ("fixtures/harbor_public/app/emergency.py", "route_emergency", "code", "label",
                                                                                      "code_recommendation_only")
    assert "1000000" in it["stored_text"]


def test_source_root_maps_an_imported_path_to_the_repository_root():
    files = ["app/emergency.py", "config/approval.yaml"]
    assert code_patch.resolve_file("fixtures/harbor_public/app/emergency.py", files) == (None, "file_not_in_repository")
    assert code_patch.resolve_file("fixtures/harbor_public/app/emergency.py", files, "fixtures/harbor_public") == ("app/emergency.py", "")
    assert code_patch.resolve_file("fixtures/harbor_public/app/emergency.py", files, "fixtures/harbor_public/") == ("app/emergency.py", "")
    assert code_patch.resolve_file("other/app/emergency.py", files, "fixtures/harbor_public") == (None, "file_not_in_repository")
    assert code_patch.resolve_file("fixtures/harbor_public/../../etc/passwd", files, "fixtures/harbor_public") == (None, "path_outside_allowlist")
