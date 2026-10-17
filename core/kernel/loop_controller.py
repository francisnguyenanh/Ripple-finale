"""Loop Controller, reduced to what runs.

This module once held the whole loop tier: `execute_loop1/2/3`, a
`run_loop_with_convergence_guard`, and `core/agent/{loop,convergence}.py`
alongside it. Measured 2026-09-14: zero production imports of any of it.
The pipeline runs S1-S12 once; the thing that actually iterates is the
clarification question round, which has its own state in `rounds.jsonl`
and never went through here.

So the executors are gone, together with `profile.convergence` -- five
settings an operator could change with no effect, one of them documented
as a hard floor that was never checked. Deleting them is not a decision
that iteration is unwanted; it is a refusal to keep shipping a tier that
exists only in the configuration file. Designing what "one iteration"
means in today's architecture is a separate piece of work.

What remains is what `core/kernel/feedback.py` actually calls: a SCRIPT
that classifies a change and proposes Loop 1/2/3 for a human gate (G5) to
approve or correct. Classification stays out of the gate so the mechanism
is testable without a human in the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

LoopKind = Literal["loop1_inplace", "loop2_regenerate", "loop3_rule_change"]


@dataclass(frozen=True)
class ChangeClassification:
    loop: LoopKind
    reason: str


def classify_change(is_wording_only: bool, requires_km_regeneration: bool, is_new_ruleset: bool) -> ChangeClassification:
    """Script proposal — G5 may override this, but this function itself
    never asks a human anything.
    """
    if is_new_ruleset:
        return ChangeClassification("loop3_rule_change", "change affects the ruleset store, not one record")
    if requires_km_regeneration:
        return ChangeClassification("loop2_regenerate", "meaning changed — KM must be re-derived in scope")
    if is_wording_only:
        return ChangeClassification("loop1_inplace", "wording-only edit, traceability preserved, no agent call")
    return ChangeClassification("loop2_regenerate", "default to the safer, scoped re-generation path")
