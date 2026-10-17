"""Fact correction flow — plan Step 6.8, kept structurally separate from
the feedback loop. This is the easiest place to accidentally violate Fact
Freeze: a "fix" applied through the meaning-feedback path would edit IR
in place instead of minting a new IR version and re-running G1.
"""

from __future__ import annotations

from dataclasses import dataclass


class FactCorrectionRequiredError(Exception):
    """Raised the instant a feedback loop's scope would touch IR — the
    caller must stop and route through `request_fact_correction`, never
    proceed as if it were a normal Loop 2 KM regeneration.
    """


@dataclass(frozen=True)
class FactCorrectionRequest:
    gap_id: str
    ir_entity_id: str
    reason: str
    requires_new_ir_version: bool = True
    requires_g1_rerun: bool = True


def is_fact_error_gap(gap: dict) -> bool:
    return gap.get("category") == "fact_error"


def route_gap(gap: dict) -> str:
    """Fact-error gaps exit the meaning-feedback loop entirely — they
    never get treated as a Loop 1/2/3 change.
    """
    return "fact_correction_flow" if is_fact_error_gap(gap) else "feedback_loop"


def request_fact_correction(gap: dict) -> FactCorrectionRequest:
    if not is_fact_error_gap(gap):
        raise ValueError("request_fact_correction called on a non-fact_error gap")
    return FactCorrectionRequest(
        gap_id=gap["id"],
        ir_entity_id=gap["related"][0] if gap.get("related") else "unknown",
        reason=gap.get("question", ""),
    )


def guard_loop_never_touches_ir(scope_layers: dict[str, str]) -> None:
    """`scope_layers`: {record_id: layer}. Call this before executing any
    Loop 1/2/3 step — a feedback loop reaching an IR-layer node must halt
    and become a FactCorrectionRequest instead of silently proceeding.
    """
    ir_touched = [rid for rid, layer in scope_layers.items() if layer == "ir"]
    if ir_touched:
        raise FactCorrectionRequiredError(
            f"feedback loop scope includes IR-layer record(s) {ir_touched} — "
            f"halt and route through fact correction, do not edit IR in place"
        )
