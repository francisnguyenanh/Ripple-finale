# -*- coding: utf-8 -*-
"""The screen a person decides on. Eight blocks, in this order.

    WHY                why this was asked for, by whom, when
    CURRENT STATE      what the system says today, in every representation
    DESIRED INTENT     what the person wants
    EVIDENCE           the quote behind each claim
    IMPACT             what moves, DIRECT first
    SIMULATION         what running it showed, and what it contradicts
    PROPOSED ACTIONS   grouped by necessity, required first
    RISK               level, reasons, and what was not measured
                       [Approve]  [Reject]  [Request changes]

The order is the argument. A screen that opens with a diff asks the reader
to evaluate a change before telling them what problem it solves, and a
reader who does not know the problem can only check the diff for syntax.
WHY first means the reader arrives at the actions already able to judge
whether they are the right actions.

Three rules the layout enforces:

**Contradictions appear ABOVE the buttons.** A finding placed below the
decision controls is a finding somebody scrolls past. If this change makes
two routes answer one request differently, that belongs where the eye
already is.

**Introduced and inherited are separately labelled.** An inherited
contradiction is a fact about today, not a consequence of this proposal.
Presenting them together would have the reviewer reject a change for a
problem it did not cause (P6 freeze, item 2).

**Nothing on this screen recommends a decision.** Risk explains what is
known, what is unknown, and why. It does not say "safe to approve". The
moment the product says that, the person's signature stops meaning they
judged it and starts meaning they agreed with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence import action_plan as action_plan_mod
from core.change_intelligence.impact import DIRECT, INDIRECT, UNKNOWN

#: The eight, in order. A test asserts the rendered screen presents them in
#: exactly this sequence, because the sequence is the part that is easy to
#: rearrange for space and expensive to have rearranged.
BLOCKS = ("WHY", "CURRENT STATE", "DESIRED INTENT", "EVIDENCE", "IMPACT",
          "SIMULATION", "PROPOSED ACTIONS", "RISK")

#: The three things a reviewer may do. There is no fourth, and none of them
#: is a default.
CHOICES = ("Approve", "Reject", "Request changes")


@dataclass
class ReviewScreen:
    """A view model. Holds no logic and decides nothing."""

    changeset_id: str
    target: str
    why: str = ""
    requested_by: str = ""
    requested_at: str = ""
    current_state: tuple = ()
    desired_intent: str = ""
    evidence: tuple = ()
    impact: tuple = ()
    simulation: object = None
    introduced: tuple = ()
    inherited: tuple = ()
    actions: object = None
    risk: object = None
    notes: list = field(default_factory=list)

    @property
    def blocks(self) -> tuple:
        return BLOCKS

    @property
    def has_introduced_contradiction(self) -> bool:
        return bool(self.introduced)


def build_screen(changeset, *, why: str = "", current_state=(),
                 desired_intent: str = "") -> ReviewScreen:
    """Assemble the screen from a ChangeSet. Reads; never computes.

    Everything here already exists on the ChangeSet, deterministically
    produced by P6. This function's whole job is ordering and labelling --
    if it started deriving a figure, the screen and the ChangeSet could
    disagree, and the reviewer would be deciding on the one that is not
    audited.
    """
    impact = getattr(changeset.affected_nodes, "__iter__", None)
    nodes = tuple(changeset.affected_nodes) if impact else ()
    ordered = (tuple(n for n in nodes if n.tier == DIRECT)
               + tuple(n for n in nodes if n.tier == INDIRECT)
               + tuple(n for n in nodes if n.tier == UNKNOWN))

    intent = changeset.intent
    screen = ReviewScreen(
        changeset_id=changeset.changeset_id,
        target=getattr(intent, "target", "") or changeset.trigger,
        why=why or getattr(intent, "reason", "") or changeset.trigger,
        requested_by=getattr(intent, "requested_by", "") or changeset.created_by,
        requested_at=getattr(intent, "requested_at", "") or changeset.created_at,
        current_state=tuple(current_state),
        desired_intent=desired_intent or _desired(intent),
        evidence=tuple(changeset.evidence),
        impact=ordered,
        simulation=changeset.simulation_result,
        introduced=changeset.introduced_contradictions,
        inherited=changeset.inherited_contradictions,
        actions=changeset.actions,
        risk=changeset.risk)

    if not screen.evidence:
        screen.notes.append(
            "no evidence was attached; every claim on this screen should be "
            "traceable to a quoted line, and a reviewer cannot check one "
            "that is not")
    return screen


def _desired(intent) -> str:
    if intent is None:
        return ""
    target = getattr(intent, "target", "")
    desired = getattr(intent, "desired_value", None)
    if target and desired is not None:
        return f"{target} should be {desired}"
    return getattr(intent, "statement", "") or ""


# --- rendering ---------------------------------------------------------------


def _render_why(screen) -> list:
    who = screen.requested_by or "unattributed"
    when = f" on {screen.requested_at}" if screen.requested_at else ""
    return [f"  {screen.why}", f"  asked for by {who}{when}"]


def _render_current(screen) -> list:
    if not screen.current_state:
        return ["  (not supplied)"]
    return [f"  {artifact}: {value}" for artifact, value in screen.current_state]


def _render_evidence(screen) -> list:
    if not screen.evidence:
        return ["  (none attached)"]
    out = []
    for ref in screen.evidence:
        locator = f":{getattr(ref, 'locator', '')}" if getattr(ref, "locator", "") else ""
        out.append(f"  {getattr(ref, 'artifact', ref)}{locator}")
        quote = getattr(ref, "quote", "")
        if quote:
            out.append(f"      \"{quote}\"")
    return out


def _render_impact(screen) -> list:
    if not screen.impact:
        return ["  (not computed)"]
    out = []
    for node in screen.impact:
        out.append(f"  {node.tier:<9} {node.kind:<15} {node.identifier}")
        out.append(f"      {node.reason}")
    return out


def _render_simulation(screen) -> list:
    out = []
    result = screen.simulation
    if result is None:
        out.append("  (no simulation was run, so there are no operational figures)")
    else:
        out.append(f"  {result.total} scenarios simulated")
        out.append(f"  {len(result.changed)} would be handled differently")

    if screen.introduced:
        out.append("")
        out.append(f"  INTRODUCED BY THIS CHANGE  ({len(screen.introduced)})")
        for case in screen.introduced:
            answers = sorted({o.outcome for o in case.shadow if not o.error})
            out.append(f"    {case.case_id}: two routes return "
                       f"{', '.join(answers)}")
        out.append("    one of these routes does not read the changed value")

    if screen.inherited:
        out.append("")
        out.append(f"  ALREADY PRESENT, NOT CAUSED BY THIS CHANGE  "
                   f"({len(screen.inherited)})")
        for case in screen.inherited:
            out.append(f"    {case.case_id}")
    return out


def _render_risk(screen) -> list:
    risk = screen.risk
    if risk is None:
        return ["  (not assessed)"]
    out = [f"  {risk.level}"]
    for dimension in risk.dimensions:
        marker = "?" if not dimension.measured else " "
        out.append(f"   {marker} {dimension.name:<22} {dimension.level:<7} "
                   f"{dimension.detail}")
    if risk.floors:
        out.append("  not measured: " + "; ".join(risk.floors))
    return out


_RENDERERS = {
    "WHY": _render_why,
    "CURRENT STATE": _render_current,
    "DESIRED INTENT": lambda s: [f"  {s.desired_intent or '(not stated)'}"],
    "EVIDENCE": _render_evidence,
    "IMPACT": _render_impact,
    "SIMULATION": _render_simulation,
    "PROPOSED ACTIONS": lambda s: [
        "  " + line for line in
        action_plan_mod.render(s.actions).splitlines()[1:]
    ] if s.actions is not None else ["  (none proposed)"],
    "RISK": _render_risk,
}


def render(screen: ReviewScreen) -> str:
    """The whole screen. Deterministic from the same ChangeSet."""
    out = [f"REVIEW   {screen.changeset_id}   {screen.target}", ""]
    for block in BLOCKS:
        out.append(block)
        out.extend(_RENDERERS[block](screen))
        out.append("")

    for note in screen.notes:
        out.append(f"  note: {note}")
    if screen.notes:
        out.append("")

    out.append("   ".join(f"[{choice}]" for choice in CHOICES))
    return "\n".join(out)


def block_order(text: str) -> list:
    """The blocks as they actually appear in rendered text.

    Used by the test rather than trusting BLOCKS: the constant says what
    the order should be, and this says what a reader gets.
    """
    seen = []
    for line in text.splitlines():
        if line in BLOCKS and line not in seen:
            seen.append(line)
    return seen


__all__ = [
    "BLOCKS",
    "CHOICES",
    "ReviewScreen",
    "block_order",
    "build_screen",
    "render",
]
