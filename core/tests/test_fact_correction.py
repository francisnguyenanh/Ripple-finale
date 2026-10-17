import pytest

from core.kernel.fact_correction import (
    FactCorrectionRequiredError,
    guard_loop_never_touches_ir,
    request_fact_correction,
    route_gap,
)


def test_fact_error_gap_routed_to_fact_correction_flow():
    gap = {"id": "GAP-1", "category": "fact_error", "related": ["PGM:A"], "question": "field length wrong?"}
    assert route_gap(gap) == "fact_correction_flow"


def test_ambiguity_gap_routed_to_feedback_loop():
    gap = {"id": "GAP-2", "category": "ambiguity"}
    assert route_gap(gap) == "feedback_loop"


def test_loop_touching_ir_halts_with_fact_correction_request():
    scope_layers = {"BR-1": "km", "PGM:A": "ir"}
    with pytest.raises(FactCorrectionRequiredError):
        guard_loop_never_touches_ir(scope_layers)


def test_loop_touching_only_km_and_doc_passes():
    guard_loop_never_touches_ir({"BR-1": "km", "L3-04": "doc"})  # must not raise


def test_fact_correction_request_requires_new_ir_version_and_g1_rerun():
    gap = {"id": "GAP-1", "category": "fact_error", "related": ["PGM:A"], "question": "field length wrong?"}
    req = request_fact_correction(gap)
    assert req.requires_new_ir_version is True
    assert req.requires_g1_rerun is True
    assert req.ir_entity_id == "PGM:A"
