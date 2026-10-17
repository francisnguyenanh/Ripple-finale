# P8 FROZEN CONTRACTS

Frozen at G8, before Phase 9 began. Later phases **consume** these
definitions; they do not re-derive them, and no model re-infers them.

Enforced by `tests/contract/test_p8_frozen_contracts.py`. Changing a
definition here means changing that test in the same commit, deliberately.

Inherits `docs/contracts/P6_FROZEN.md` and `docs/contracts/P7_FROZEN.md` in
full. Where they overlap, P6 governs the analysis, P7 governs preparing and
applying a change, and this one governs what is true afterwards.

---

## 1. The one rule everything else follows from

**Verification inspects the artifact. It does not read any declaration.**

Not the ChangeSet's state, not the approval record's say-so, not the apply
result, not the planner's claim. Each of those is a statement that something
*was meant to* happen. Only the artifact says what is.

The distinction is invisible until an apply half-succeeds. Then "the
ChangeSet says APPLIED" and "the spreadsheet still says the old number" are
both true, and a verifier that read the first reports success on a system
that is inconsistent. A test asserts `verify()` never touches
`changeset.state`.

## 2. Verification result schema

```text
changeset_id      which proposal this is about
snapshot_before   the state it was computed against
snapshot_after    the state being verified
alignment         a P6 AlignmentState -- the same four, not a new vocabulary
edits             one EditOutcome per approved structured edit
unexpected        artifacts that moved with nothing approving it
dimensions        the six, each OK / DRIFT / UNKNOWN
unresolved        everything still open, in words a person reads
blocked_by        which of the five conditions forbid VERIFIED
reason            why this verdict and not another
```

`ALIGNED` with a non-empty `unresolved` or `blocked_by` **cannot be
constructed**. A clean verdict carrying open problems is the one result
nobody would check.

`passed` is true only for `ALIGNED`. Nothing else may reach VERIFIED.

### Edit outcomes

```text
APPLIED     the artifact holds the approved value
MISSING     it still holds the old one -- the change did not land
DIVERGED    it holds neither; no ordering explains it
UNREADABLE  present, but no reader could parse it
```

`MISSING` is **never** a pass, however green everything around it looks.

`UNREADABLE` is deliberately not folded into `MISSING`. "The old value is
still there" and "nobody can tell what is there" send a person to different
places, and only the first says what to do. It maps to `UNKNOWN` rather than
`DRIFT`, because nothing was observed to disagree — and it blocks a pass
either way, which is the part that must not depend on getting the nuance
right.

### The six dimensions

```text
workflow  software  data  documents  tests  evidence
```

A dimension nobody measured is `UNKNOWN`, never `OK`.

## 3. Snapshot relationship

```text
snapshot_id          content address; the state IS the id
parent_snapshot_id   the chain, deterministic
diff_from(other)     deterministic, sorted, byte-comparison
```

Verification answers five questions and each comes from snapshots alone:

```text
what was expected to change?      the approved structured edits
what actually changed?            snapshot_after.diff_from(before)
what did NOT change that should?  MISSING
what changed that should not?     UNEXPECTED
what changed afterwards?          REVERSE_DRIFT
```

Verifying a snapshot against itself is refused: there is nothing to verify,
and reporting a pass would be reporting on a change that never happened.

## 4. Lineage and version semantics — D4, closed

**Owner:** a session's `VerifiedLineage`. One counter, derived from the entry
list rather than stored beside it, because a stored counter and a list can
disagree.

**Incremented by exactly one event:** a verification that PASSED, on a
snapshot not already verified.

```text
apply once        -> new snapshot, version UNCHANGED
verify, passed    -> version + 1
verify again      -> version UNCHANGED (idempotent on snapshot_id)
verify, failed    -> version UNCHANGED, attempt kept with its reason
failed apply      -> no snapshot, nothing recorded
reverse drift     -> snapshot chain advances, version does NOT
```

Incrementing on apply would advance the version for a change nobody
confirmed landed — the declared-rather-than-actual defect this phase exists
to remove.

**Parent/child:** each entry names its snapshot and that snapshot's parent.
An entry whose parent is not the current verified head raises
`LineageMismatch` rather than being appended; a lineage with a gap cannot
answer what the verified state was before it.

**Verification refers to a snapshot by `snapshot_id`, never by version.**
The id is the state; a version needs the table to resolve.

**Failed attempts are kept.** "Tried three times, failed the same way" is
what a person needs before approving a fourth.

### What D4 does not close

`core/kernel/lineage.py`'s `DEFAULT_REF_VERSION` is still `"1"` and still
nothing increments it. It describes a **ref to a document-pipeline
artifact**; this describes **how far a session's verified state has
advanced**. Nothing in this phase reads it, and that module argues correctly
that giving it a value before it has a reader would enlarge the problem.
`test_no_consumer_reads_the_ref_version` is untouched.

Two counters for two different things are not competing counters. One
counter forced to mean both would be.

## 5. Intended-change verification

Every approved `StructuredEdit` is checked against the artifact by its own
locator. The comparison is three-way — approved value, old value, anything
else — so "did not land" is distinguished from "landed as something nobody
approved".

A missing intended change is **not** verified, and the finding names the
artifact, the path, what was expected and what was found.

## 6. Unexpected-change detection

An artifact that moved during the apply with no approved edit naming it is
`UNEXPECTED`, and it is **never absorbed** into the approved ChangeSet.

**Relatedness is membership of the approved set, not resemblance.** A
verifier deciding relatedness by similarity would absorb an out-of-band edit
the moment it touched the same file, which is the one thing it must not do.

## 7. Reverse drift

An artifact the change *does* cover, holding a value that is neither the old
nor the approved one, is `REVERSE_DRIFT`: somebody edited it outside the
approval path.

```text
approved  500000
applied   500000
external  250000     -> REVERSE_DRIFT, alignment CONFLICT
```

It is **never reinterpreted as an approved change**. The finding states that
no ChangeSet authorised the difference, and it becomes a new reconciliation
event for a person.

## 8. No auto-repair

Nothing in `verification.py` writes. A test walks its syntax tree and fails
on any `write`, `clear`, `write_text` or `write_bytes` call.

A verifier that quietly fixed what it found would make the drift it exists
to surface invisible — and the next time somebody edited a value out of
band, the system would tidy it away and report that everything is fine.

`next_actions()` returns **at least two options** per finding, deliberately.
Returning one would be a recommendation, and whether an out-of-band edit is
acceptable is a business decision wearing a technical costume.

## 9. The regression guard — five conditions

Each alone prevents VERIFIED:

```text
tests_failing               a test asserts the old behaviour and is now red
evidence_stale              a quoted line no longer matches its artifact
intent_conflict_open        what was asked and what is stated still disagree
artifact_stale              a required artifact still holds the old value
approval_evidence_missing   no valid approval, or one for another version
```

The fifth closes a subtle hole: a ChangeSet can be applied through a
legitimate path and then have its approval record lost or corrupted.
Verification **re-checks** rather than trusting recorded state.

All applicable blockers are reported together. A guard that stops at the
first hides the other four.

## 10. State transitions and events

The P7 machine is unchanged. `APPLIED → VERIFIED` remains the only edge into
VERIFIED, and `mark_verified` still refuses without a report (I14).

A failed verification **does not** transition to VERIFIED. It is itself a
report, carrying `passed = False`, and the caller reads that.

**Six loop-triggering events**, classified by `x-loop-triggering` in each
event's JSON Schema and read from `core/schema/event_registry.py`. No module
restates the list; a test fails if any of the nine names appears as a
literal in `events.py`.

```text
LOOP      ArtifactChanged  HumanFeedbackAdded  IntentAccepted
          ChangeApproved   ChangeApplied       VerificationCompleted
INTERNAL  ShadowChangeCreated  ImpactCalculated  ChangeProposed
```

Subscribing the loop to an internal event **raises**. Those three are
produced inside a turn already in progress, and subscribing to one creates a
cycle that no unit test reveals and a demo does.

**Idempotency is by `dedup_key`, not `event_id`.** An id makes a redelivery
unique; a key makes it the same fact, and "has this been handled" cannot be
answered by something designed to differ every time. A duplicate is reported
as a duplicate — "nothing happened because it was a redelivery" and "nothing
happened because no handler cared" are different facts.

## 11. Scoped propagation

`artifact -> records -> knowledge -> chunks`, each hop an edge the graph
already holds. Where the graph has no edge, the artifact is reported
`unreachable` rather than connected by guesswork, so the gap stays visible.

Every missing input is a **note**, never a smaller answer. A scope that is
small because nothing was supplied looks identical to one that is small
because little is affected, and those are opposite facts.

Changing one artifact must reprocess substantially fewer than all of them.
If the scope equals the corpus, "scoped" is decoration.

## 12. I8 — the loop cannot reach the Fact layer

`guard_loop_never_touches_ir` is checked **before any work**, so a scope
reaching the Fact layer is never partly processed and then halted. It
becomes a `FactCorrectionRequest` demanding a new version and a fresh gate;
the Fact is never edited in place.

A gap categorised `fact_error` leaves the loop entirely.

## 13. Human authority

P8 may verify, report, and prepare the next reconciliation proposal.

**P8 may not decide whether a newly discovered drift is acceptable.** That is
a HumanDecision. Both loop directions end at a report; neither ends at a
repair.

## 14. Determinism

For identical snapshot N, snapshot N+1, ChangeSet and verification inputs,
the result is identical. No `datetime.now()`, no generated ids, no implicit
actor defaults, no environment-dependent behaviour, and no model-generated
verdict.

A model may summarise evidence. **The verification result itself is
deterministic**, and a test asserts no proposer or model is reachable from
`verification.py`.

## 15. No second semantics

Rule identity, semantic extraction, DIRECT/INDIRECT/UNKNOWN and contradiction
detection all come from P6 through the objects passed in. `orchestrator.py`
coordinates and holds no verdict: a test fails if it names `AlignmentState`,
a risk threshold, or a tier.

Two implementations of one business rule disagree eventually, and the
disagreement surfaces as a verdict nobody can explain.

## 16. H1 — resolved

The `history` contract is a **dated, attributed changelog**, not repository
provenance. Checked against all three consumers, not assumed. No git
integration exists, and a test fails if one appears.

Full reasoning and evidence: `docs/verification/h1_and_wiring_decisions.md`.

## 17. P8-T03 wiring ownership

```text
PAID        fact_correction  reindex          -- tests green
WIRED, but their tests ask for the DOCUMENT pipeline:
            loop_controller  impact_graph     -- tests stay red
UNTOUCHED, no dependency proven:
            core/kernel/changeset.py          -- test stays red
```

The ledger in `tests/wiring/test_change_loop_wired.py` is unedited. No test
deleted, no ownership moved. Remaining `wiring_debt` failures: **3**.

Classification and reasoning: `docs/verification/h1_and_wiring_decisions.md`.

## 18. Benchmark lineage

Unchanged. No Eval or Stress dataset regenerated at this gate. The 24
hand-written scenarios remain an independent answer-key layer.
