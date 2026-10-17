# -*- coding: utf-8 -*-
"""Only actions that impact evidence justifies. I16.

Seven kinds of action, because a rule that lives in seven kinds of place has
to be changed in seven kinds of place:

    CODE  DOC  WORKFLOW  DATA  TEST  API  COMMUNICATION

The last one is the one a purely technical product forgets. Lowering an
approval threshold does not only change software -- it changes what a group
of managers is expected to do on Monday. If the manager guide still states
the old figure and nobody told them, the change has not landed, whatever the
config file says.

**Every action carries `necessity`, and necessity must be earned:**

    required     direct impact evidence; without it the system is inconsistent
    recommended  worth doing for consistency, not load-bearing
    optional     could be done
    unsupported  not enough information to propose -- a person must decide

A planner that marks everything `required` has told the reviewer nothing,
and `unnecessary-change rate` at P9 measures exactly that. So `required` is
gated in code: an action with no impact evidence cannot be built as
`required`, it is refused.

**The planner is deliberately capable of proposing nothing.** Scenario H
(the rule already says what was asked for) yields zero actions. A planner
that always finds seven things to do does not understand the question, and
the cheapest way to look thorough is to be wrong in that direction.

**Detecting that a change is needed is separate from generating it.** Where
the generation half is out of scope -- a code patch, a new test -- the action
is still listed, as `required`, to be done by a person. Cutting the ability
to WRITE a patch must never quietly cut the ability to NOTICE one is needed;
only the second is the product's claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.impact import DIRECT, INDIRECT, UNKNOWN

#: The seven kinds. Ordered as a reviewer reads them: where the rule runs,
#: then where it is described, then who has to be told.
ACTION_KINDS = ("WORKFLOW", "DATA", "CODE", "TEST", "API", "DOC",
                "COMMUNICATION")

#: Ordered by how much attention each deserves, strongest first.
NECESSITY = ("required", "recommended", "optional", "unsupported")

REQUIRED, RECOMMENDED, OPTIONAL, UNSUPPORTED = NECESSITY

#: Who carries out each kind once approved. `system` actions are applied
#: through the single apply path (see apply_gate); `human` actions are listed,
#: tracked and verified, but performed by a person.
PERFORMER = {
    "WORKFLOW": "system",
    "DATA": "system",
    "DOC": "system",
    "CODE": "human",
    "TEST": "human",
    "API": "human",
    "COMMUNICATION": "human",
}

#: Which impact kinds justify which action kind. Read as: "an action of this
#: kind is warranted when the impact set contains a node of one of these
#: kinds". Nothing else may create an action -- this table is the whole of
#: I16, in one place where a test can read it.
_JUSTIFIED_BY = {
    "WORKFLOW": ("workflow_config",),
    "DATA": ("spreadsheet",),
    "CODE": ("software",),
    "TEST": ("test",),
    "API": ("api",),
    "DOC": ("document", "business_rule", "process", "audit_control"),
    "COMMUNICATION": ("role", "notification"),
}


class UnjustifiedAction(Exception):
    """An action nothing in the impact set supports. I16."""


@dataclass(frozen=True)
class Action:
    """One thing that has to happen, and the evidence that says so."""

    kind: str
    target: str
    description: str
    necessity: str
    performer: str
    impact_evidence: tuple = ()
    edits: tuple = ()
    reason: str = ""
    generated: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise UnjustifiedAction(f"{self.kind!r} is not one of {ACTION_KINDS}")
        if self.necessity not in NECESSITY:
            raise UnjustifiedAction(
                f"{self.necessity!r} is not one of {NECESSITY}")
        if self.necessity == REQUIRED and not self.impact_evidence:
            raise UnjustifiedAction(
                f"{self.kind} on {self.target} is marked required with no "
                f"impact evidence; 'required' is a claim about consequence, "
                f"and a planner that marks everything required has told the "
                f"reviewer nothing (I16)")
        if not self.reason:
            raise UnjustifiedAction(
                f"{self.kind} on {self.target} carries no reason")

    @property
    def is_proposal_only(self) -> bool:
        """Listed and approved, carried out by a person."""
        return self.performer == "human"


@dataclass
class ActionPlan:
    target: str
    actions: tuple[Action, ...] = ()
    notes: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.actions

    def of_necessity(self, necessity: str) -> tuple:
        return tuple(a for a in self.actions if a.necessity == necessity)

    def of_kind(self, kind: str) -> tuple:
        return tuple(a for a in self.actions if a.kind == kind)

    @property
    def kinds(self) -> tuple:
        return tuple(sorted({a.kind for a in self.actions}))

    @property
    def grouped(self) -> tuple:
        """`(necessity, actions)` strongest first. The review screen's order.

        Grouping by necessity rather than by kind is the point: a reviewer
        needs to see what is load-bearing before what is tidy-up, and a list
        sorted by artifact type buries one inside the other.
        """
        out = []
        for necessity in NECESSITY:
            actions = self.of_necessity(necessity)
            if actions:
                out.append((necessity, actions))
        return tuple(out)

    def needs_human_work(self) -> tuple:
        return tuple(a for a in self.actions if a.is_proposal_only)


# --- building ----------------------------------------------------------------


def _necessity_for(node, *, introduced_inconsistency: bool) -> str:
    """Necessity follows the tier, with one override.

    DIRECT      required     the system reads this value; leaving it is
                             a contradiction, not an inconsistency of style
    INDIRECT    recommended  it describes or checks the rule
    UNKNOWN     unsupported  not enough evidence to say -- a person decides

    The override: a route the simulation caught deciding a request
    differently is `required` whatever its tier, because that is not a
    prediction about consistency, it is an observation of a contradiction
    that will happen.
    """
    if introduced_inconsistency:
        return REQUIRED
    return {DIRECT: REQUIRED, INDIRECT: RECOMMENDED,
            UNKNOWN: UNSUPPORTED}[node.tier]


def _kind_for(node) -> str:
    for action_kind, impact_kinds in _JUSTIFIED_BY.items():
        if node.kind in impact_kinds:
            return action_kind
    return "DOC"


def plan(*, target, impact=None, introduced=(), inherited=(),
         current_value=None, proposed_value=None, edits_by_artifact=None,
         stale_tests_by_artifact=None,
         is_noop: bool = False, knowledge_only: bool = False) -> ActionPlan:
    """The actions this change requires, and nothing else.

    `introduced` and `inherited` are taken SEPARATELY and used differently
    (P6 freeze, item 2). An introduced contradiction promotes its artifact
    to `required`, because the proposal creates it. An inherited one is
    recorded as a note and never described as caused by this change.
    """
    plan_ = ActionPlan(target=target)

    if is_noop:
        # Scenario H. The rule already says what was asked for.
        plan_.notes.append(
            f"{target} already holds the requested value; no action is "
            f"required, and proposing one would be a change made for the "
            f"sake of having made one")
        return plan_

    #: module path -> the case ids that showed it at fault. Keyed by module,
    #: because the finding is about the route, not the request.
    #:
    #: Both routes appear in a disagreement, and only ONE of them is wrong.
    #: The one at fault is the route whose answer did NOT move when the
    #: value moved -- that is what "does not read the changed value" means.
    #: The other route disagrees precisely because it is behaving
    #: correctly, and naming it would put a required code change on a file
    #: that needs none. The same false-positive that made everything DIRECT,
    #: in a new place.
    inconsistent_modules: dict = {}
    for case in introduced:
        before = {getattr(o, "handler", ""): getattr(o, "outcome", None)
                  for o in getattr(case, "current", ()) if not getattr(o, "error", "")}
        for outcome in getattr(case, "shadow", ()):
            handler = getattr(outcome, "handler", "")
            if not handler or getattr(outcome, "error", ""):
                continue
            if handler in before and before[handler] != outcome.outcome:
                continue  # this route followed the change; it is not at fault
            inconsistent_modules.setdefault(
                handler.rsplit(".", 1)[0], set()).add(
                    getattr(case, "case_id", ""))
    inconsistent_artifacts = set(inconsistent_modules)

    actions: list[Action] = []
    seen: set = set()
    matched_modules: set = set()
    for node in getattr(impact, "nodes", ()):
        if node.kind == "business_rule":
            # The rule itself is the SUBJECT of the change, not a thing to
            # edit. Emitting an action for it put a line on the review
            # screen that named no artifact and asked for no work, which
            # makes the required list longer without making it truer.
            continue
        kind = _kind_for(node)

        if knowledge_only and kind not in ("DOC", "COMMUNICATION"):
            # Scenario E: the system behaves correctly; what was wrong was
            # the description of it. Touching code or workflow here would
            # be changing a working system to match a corrected sentence.
            continue

        key = (kind, node.identifier)
        if key in seen:
            continue
        seen.add(key)

        matching = [module for module in inconsistent_artifacts
                    if node.identifier.replace("/", ".").removesuffix(".py")
                    .endswith(module.rsplit(".", 1)[-1])]
        matched_modules.update(matching)
        introduced_here = bool(matching)
        necessity = _necessity_for(
            node, introduced_inconsistency=introduced_here)

        evidence = (node.evidence,) if node.evidence else ()
        if necessity == REQUIRED and not evidence:
            # Earned, not assumed: without a quotable line this drops a
            # step rather than making an unbacked claim.
            necessity = RECOMMENDED

        edits = tuple((edits_by_artifact or {}).get(node.identifier, ()))

        # A TEST action naming only the file sends somebody to read four
        # hundred lines. Where the stale assertions were located, the
        # action names them and their evidence is the quoted line.
        stale = tuple((stale_tests_by_artifact or {}).get(node.identifier, ()))
        if stale:
            evidence = tuple(item.evidence for item in stale)
        actions.append(Action(
            kind=kind,
            target=node.identifier,
            description=(
                _describe(kind, node, current_value, proposed_value,
                          introduced_here)
                + (f" -- {len(stale)} assertion(s) go red: "
                   + ", ".join(item.function for item in stale)
                   if stale else "")),
            necessity=necessity,
            performer=PERFORMER[kind],
            impact_evidence=evidence,
            edits=edits,
            reason=node.reason,
            generated=bool(edits)))

    # A route the simulation caught deciding a request differently may have
    # NO node in the impact set at all -- impact finds the artifacts that
    # STATE the rule, and this one is a route that ignores it, which is
    # exactly why it is a problem. Without this the planner would run the
    # simulation, receive the finding, and propose only the configuration
    # change: an integration failure, not a feature gap, and the one the
    # golden case exists to catch.
    if not knowledge_only:
        for module in sorted(set(inconsistent_modules) - matched_modules):
            identifier = module.replace(".", "/") + ".py"
            cases = sorted(c for c in inconsistent_modules[module] if c)
            # De-duplicated by construction (a set), and capped: three case
            # ids show the reader it is real; two hundred is a wall.
            actions.append(Action(
                kind="CODE", target=identifier,
                description=(
                    f"{identifier} decides these requests without reading "
                    f"the changed value; it needs a CODE change, not a "
                    f"configuration change"),
                necessity=REQUIRED, performer=PERFORMER["CODE"],
                impact_evidence=tuple(
                    f"simulation case {case}" for case in cases[:3]),
                reason=(
                    f"the simulation found {len(cases)} request(s) this "
                    f"route would decide differently from the route that "
                    f"reads the value"),
                generated=False))

    actions.sort(key=lambda a: (NECESSITY.index(a.necessity),
                                ACTION_KINDS.index(a.kind), a.target))
    plan_.actions = tuple(actions)

    if inherited:
        plan_.notes.append(
            f"{len(inherited)} contradiction(s) already existed before this "
            f"change and are NOT caused by it; they are reported so nobody "
            f"attributes them to this proposal")

    unsupported = plan_.of_necessity(UNSUPPORTED)
    if unsupported:
        plan_.notes.append(
            f"{len(unsupported)} item(s) could not be placed from the "
            f"evidence available; they are listed for a person to decide, "
            f"not silently dropped")

    return plan_


def _describe(kind, node, current_value, proposed_value, introduced) -> str:
    if introduced:
        return (f"{node.identifier} decides this request without reading the "
                f"changed value; it needs a CODE change, not a configuration "
                f"change")
    return {
        "WORKFLOW": f"set the value in {node.identifier} to {proposed_value}",
        "DATA": f"update the cell in {node.identifier} to {proposed_value}",
        # `software` reaching here is an artifact that CARRIES the value --
        # a config module, a constants file. The route that ignores the
        # value is handled separately and gets its own sentence, because
        # telling a configuration file to "read from configuration" is
        # advice nobody can act on.
        "CODE": f"update the value stated in {node.identifier} to "
                f"{proposed_value}",
        "TEST": f"{node.identifier} asserts {current_value}; update it to "
                f"{proposed_value}",
        "API": f"{node.identifier} publishes this value in a contract",
        "DOC": f"{node.identifier} states {current_value}; re-render it from "
               f"the Knowledge Model",
        "COMMUNICATION": f"notify {node.identifier}: the value they act on "
                         f"changes from {current_value} to {proposed_value}",
    }[kind]


def communication_actions(plan_: ActionPlan) -> tuple:
    """Who has to be TOLD. Kept addressable because it is easy to drop."""
    return plan_.of_kind("COMMUNICATION")


def render(plan_: ActionPlan) -> str:
    if plan_.is_empty:
        out = ["PROPOSED ACTIONS   none"]
        return "\n".join(out + [f"  {note}" for note in plan_.notes])
    out = [f"PROPOSED ACTIONS   {len(plan_.actions)} for {plan_.target}"]
    for necessity, actions in plan_.grouped:
        out.append("")
        out.append(f"  {necessity.upper()}")
        for action in actions:
            performed = "" if action.performer == "system" else "   [by a person]"
            out.append(f"    {action.kind:<14} {action.target}{performed}")
            out.append(f"      {action.description}")
            for evidence in action.impact_evidence:
                out.append(f"      evidence: {evidence}")
    for note in plan_.notes:
        out.append(f"  note: {note}")
    return "\n".join(out)


__all__ = [
    "ACTION_KINDS",
    "Action",
    "ActionPlan",
    "NECESSITY",
    "OPTIONAL",
    "PERFORMER",
    "RECOMMENDED",
    "REQUIRED",
    "UNSUPPORTED",
    "UnjustifiedAction",
    "communication_actions",
    "plan",
    "render",
]
