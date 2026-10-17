# -*- coding: utf-8 -*-
"""The one list of what a stranger on the internet may do.

Every externally reachable operation is declared here, with a security
classification, and **nothing is reachable that is not declared**. That is
the difference between a surface and a set of routes: a route exists because
somebody wrote a function, and a surface exists because somebody decided.

The failure this design prevents is specific and common: an internal helper
acquires a route for one debugging session, the route outlives the session,
and six months later it is a public operation nobody chose. Here, the router
dispatches *from* this table, so a function with no entry cannot be called
however many decorators it grows.

**The external-edit whitelist is not a second list.** It lives inside the
one operation that performs a mutation, so "the whitelist" and "the surface"
cannot drift apart -- there is nothing to synchronise. A test asserts no
other module declares a whitelist of its own.

## The classifications

    READ           returns state; changes nothing
    SESSION_WRITE  changes state inside ONE session's overlay; never the
                   baseline, never another session, never the filesystem
    DECISION       records a human decision, with actor and timestamp
    CONTROLLED     a deliberately narrow mutation with its own whitelist
                   -- exactly one operation has this, and it is the
                   external-edit demo

There is no fourth classification, and in particular there is no
`ADMIN`, `INTERNAL` or `DEBUG`. An operation that would need one does not
belong on a public surface at all.

## What is deliberately absent

    arbitrary filesystem write     arbitrary repository operation
    shell execution                arbitrary code execution
    dynamic import                 infrastructure mutation
    arbitrary path access          artifact upload
    arbitrary deserialization      unrestricted network access

Each of those is checked by a test that walks the syntax tree of every
handler this table names, not by a promise in this docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The four. A fifth would be a decision to widen what a stranger may do,
#: and it should look like one.
READ = "READ"
SESSION_WRITE = "SESSION_WRITE"
DECISION = "DECISION"
CONTROLLED = "CONTROLLED"
CLASSIFICATIONS = (READ, SESSION_WRITE, DECISION, CONTROLLED)

#: Classifications that change state. Every one of them must name the
#: boundary it is confined to -- a write with no stated boundary is a write
#: whose boundary nobody checked.
WRITING = (SESSION_WRITE, DECISION, CONTROLLED)


class NotPublic(Exception):
    """Something tried to reach an operation that is not on the surface."""


class SurfaceViolation(Exception):
    """An operation declared in a way the surface does not permit."""


@dataclass(frozen=True)
class Mutation:
    """One thing the controlled-edit operation may change.

    A mutation is a *typed* change to a *named* location, not a payload.
    `artifact` and `locator` are matched exactly -- no globs, no prefixes,
    no path resolution -- because a pattern is a promise about strings that
    somebody will later find a way around.
    """

    artifact: str
    locator: str
    value_type: str
    minimum: object = None
    maximum: object = None
    reason: str = ""

    def accepts(self, value) -> tuple:
        """`(ok, why_not)`. Deterministic, and it never coerces.

        Coercing "500000" to an integer would mean the value a person sees
        in the request is not the value that lands, which is precisely the
        confusion the external-edit demo exists to detect.
        """
        expected = {"integer": int, "string": str}[self.value_type]
        if isinstance(value, bool) or not isinstance(value, expected):
            return False, (f"{self.artifact}:{self.locator} takes a "
                           f"{self.value_type}, and this is a "
                           f"{type(value).__name__}")
        if self.minimum is not None and value < self.minimum:
            return False, (f"{value} is below the minimum {self.minimum} for "
                           f"{self.artifact}:{self.locator}")
        if self.maximum is not None and value > self.maximum:
            return False, (f"{value} is above the maximum {self.maximum} for "
                           f"{self.artifact}:{self.locator}")
        return True, ""


@dataclass(frozen=True)
class Operation:
    """One thing the public may do, and the boundary it is confined to."""

    name: str
    classification: str
    summary: str
    #: Where its effect stops. Mandatory for anything that writes.
    boundary: str = ""
    #: Only the CONTROLLED operation has these. This IS the whitelist.
    mutations: tuple = ()

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise SurfaceViolation(
                f"{self.name}: {self.classification!r} is not one of "
                f"{CLASSIFICATIONS}")
        if self.classification in WRITING and not self.boundary:
            raise SurfaceViolation(
                f"{self.name} writes and does not state where its effect "
                f"stops; a write with no stated boundary is a write whose "
                f"boundary nobody checked")
        if self.mutations and self.classification != CONTROLLED:
            raise SurfaceViolation(
                f"{self.name} carries a mutation whitelist but is not "
                f"CONTROLLED; a whitelist outside the controlled operation "
                f"is a second list that will drift")
        if self.classification == CONTROLLED and not self.mutations:
            raise SurfaceViolation(
                f"{self.name} is CONTROLLED with an empty whitelist, which "
                f"permits nothing and says nothing -- state the allowed "
                f"mutations or drop the operation")

    @property
    def writes(self) -> bool:
        return self.classification in WRITING

    def mutation_for(self, artifact: str, locator: str) -> Mutation | None:
        for mutation in self.mutations:
            if mutation.artifact == artifact and mutation.locator == locator:
                return mutation
        return None


#: ---------------------------------------------------------------------------
#: THE SURFACE. Adding a row is a decision about what a stranger may do.
#: ---------------------------------------------------------------------------
PUBLIC_SURFACE: tuple = (
    Operation(
        name="health",
        classification=READ,
        summary="is the service up"),
    Operation(
        name="session.create",
        classification=SESSION_WRITE,
        summary="start an isolated session from the immutable baseline",
        boundary="creates one new session; touches no existing session"),
    Operation(
        name="session.reset",
        classification=SESSION_WRITE,
        summary="return this session to the baseline",
        boundary="this session's overlay and transient state only"),
    Operation(
        name="state.observe",
        classification=READ,
        summary="what the reference operation currently says, per artifact"),
    Operation(
        name="findings.list",
        classification=READ,
        summary="conflicts, gaps and unknowns discovered in this session"),
    Operation(
        name="evidence.show",
        classification=READ,
        summary="the quoted lines behind one finding"),
    Operation(
        name="whatif.simulate",
        classification=SESSION_WRITE,
        summary="ask what a proposed value would change",
        boundary="a shadow inside this session; the baseline is untouched"),
    Operation(
        name="impact.show",
        classification=READ,
        summary="what a proposed change would reach, tiered"),
    Operation(
        name="intent.parse",
        classification=SESSION_WRITE,
        summary="parse natural language into an intent candidate",
        boundary="stores one candidate in this session; creates no intent"),
    Operation(
        name="intent.create",
        classification=SESSION_WRITE,
        summary="record what a person says they want",
        boundary="this session's intent ledger"),
    Operation(
        name="changeset.prepare",
        classification=SESSION_WRITE,
        summary="build the coordinated change for review",
        boundary="this session; prepares only, applies nothing"),
    Operation(
        name="changeset.review",
        classification=READ,
        summary="the eight review blocks for one prepared ChangeSet"),
    Operation(
        name="changeset.decide",
        classification=DECISION,
        summary="approve, reject or request changes, with a reason",
        boundary="records one human decision against one proposal version"),
    Operation(
        name="changeset.apply",
        classification=SESSION_WRITE,
        summary="write an APPROVED ChangeSet into this session's overlay",
        boundary="this session's overlay; refuses without a valid approval"),
    Operation(
        name="verification.run",
        classification=SESSION_WRITE,
        summary="check what the artifacts actually say now",
        boundary="reads artifacts and records a result; repairs nothing"),
    Operation(
        name="lineage.show",
        classification=READ,
        summary="how far this session's verified state has advanced"),
    Operation(
        name="evaluation.report",
        classification=READ,
        summary="the measured metrics, with their answer key and scope"),
    Operation(
        #: The one exception, and the reason it is narrow.
        #:
        #: The demo has to answer "what happens when reality changes
        #: outside Ripple?". That question is answered completely by one
        #: whitelisted cell moving. A free upload endpoint would add no
        #: demonstrative value and the entire attack surface this module
        #: exists to remove.
        name="scenario.external_edit",
        classification=CONTROLLED,
        summary="simulate somebody editing a value outside the approval path",
        boundary="one whitelisted location in this session's overlay",
        mutations=(
            Mutation(
                artifact="data/approval_matrix.xlsx",
                locator="Thresholds!C2",
                value_type="integer",
                minimum=1,
                maximum=100_000_000,
                reason="the approval threshold a buyer reads from the "
                       "spreadsheet; moving it out of band is the drift the "
                       "demo detects"),
            Mutation(
                artifact="workflow/approval_routes.yaml",
                locator="approval_routes[3].condition.value",
                value_type="integer",
                minimum=1,
                maximum=100_000_000,
                reason="the same rule where the system actually reads it"),
        )),
)

_BY_NAME = {operation.name: operation for operation in PUBLIC_SURFACE}


# --- reading the surface -----------------------------------------------------


def names() -> tuple:
    return tuple(sorted(_BY_NAME))


def get(name: str) -> Operation:
    """The operation, or refuse. Never returns a default."""
    if name not in _BY_NAME:
        raise NotPublic(
            f"{name!r} is not a public operation. The surface is a closed "
            f"list, so an internal helper cannot become public by acquiring "
            f"a route")
    return _BY_NAME[name]


def is_public(name: str) -> bool:
    return name in _BY_NAME


def writing_operations() -> tuple:
    return tuple(o for o in PUBLIC_SURFACE if o.writes)


def read_operations() -> tuple:
    return tuple(o for o in PUBLIC_SURFACE if not o.writes)


def controlled_operation() -> Operation:
    """The single CONTROLLED operation. More than one would be a finding."""
    found = [o for o in PUBLIC_SURFACE if o.classification == CONTROLLED]
    if len(found) != 1:
        raise SurfaceViolation(
            f"{len(found)} controlled operations; the whitelist has to live "
            f"in exactly one place or it is two lists")
    return found[0]


def external_edit_whitelist() -> tuple:
    """DERIVED, never maintained. There is nothing here to keep in sync."""
    return controlled_operation().mutations


def whitelisted_artifacts() -> tuple:
    return tuple(sorted({m.artifact for m in external_edit_whitelist()}))


def whitelisted_locators(artifact: str) -> tuple:
    return tuple(sorted(m.locator for m in external_edit_whitelist()
                        if m.artifact == artifact))


def describe() -> str:
    """The inventory, as a person would audit it. Deterministic."""
    out = [f"PUBLIC SURFACE   {len(PUBLIC_SURFACE)} operations"]
    for classification in CLASSIFICATIONS:
        group = [o for o in PUBLIC_SURFACE
                 if o.classification == classification]
        if not group:
            continue
        out.append("")
        out.append(f"  {classification}")
        for operation in sorted(group, key=lambda o: o.name):
            out.append(f"    {operation.name:<26} {operation.summary}")
            if operation.boundary:
                out.append(f"        stops at: {operation.boundary}")
            for mutation in operation.mutations:
                out.append(f"        allows:   {mutation.artifact} "
                           f"{mutation.locator} ({mutation.value_type})")
    return "\n".join(out)


__all__ = [
    "CLASSIFICATIONS",
    "CONTROLLED",
    "DECISION",
    "Mutation",
    "NotPublic",
    "Operation",
    "PUBLIC_SURFACE",
    "READ",
    "SESSION_WRITE",
    "SurfaceViolation",
    "WRITING",
    "controlled_operation",
    "describe",
    "external_edit_whitelist",
    "get",
    "is_public",
    "names",
    "read_operations",
    "whitelisted_artifacts",
    "whitelisted_locators",
    "writing_operations",
]
