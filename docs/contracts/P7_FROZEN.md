# P7 FROZEN CONTRACTS

Frozen at G7 PASS, before Phase 8 began. Phase 8 and later phases **consume**
these definitions; they do not re-derive them, and no model re-infers them.

Enforced by `tests/contract/test_p7_frozen_contracts.py`. Changing a
definition here means changing that test in the same commit, deliberately.

Inherits `docs/contracts/P6_FROZEN.md` in full. Where the two overlap, P6's
wording governs the analysis and this one governs what is done with it.

---

## 1. ChangeSet schema

Eleven fields, in the order the review screen reads them:

```text
trigger             what started this
intent              what a person asked for, at which VERSION
base_snapshot       the world this was computed against
affected_nodes      what moves, tiered
affected_artifacts  the files
evidence            the quotes behind every claim
simulation_result   what running it showed
actions             what would be done, grouped by necessity
risk                the deterministic assessment
approval            who decided, when, on which version
verification        what was checked afterwards
```

Plus `state`, `session_id`, `created_by`, `created_at`, `history`.

`trigger` and `base_snapshot` are mandatory at construction — a proposal that
does not name the world it was computed against describes a change to nothing
in particular (I17).

`simulation_result` is what makes this more than a task list. Without it the
ChangeSet says "here are files to edit" and the reviewer takes that on trust.

### Replay

From `changeset_id`, this chain must come back, with no hole earlier than the
point the ChangeSet has reached:

```text
base_snapshot -> trigger -> intent -> impact -> simulation
  -> proposal -> approval -> execution -> verification
```

A hole raises `BrokenChain` naming the missing links. The question an audit
asks is not "what changed" — git answers that — but "why was this done, on
what evidence, and who agreed".

## 2. Identity

`changeset_id` = `CHG-` + SHA-256 over the **proposal parts only**:

```text
trigger  ·  intent id AND version  ·  base_snapshot
affected_artifacts  ·  actions with their structured edits
```

Deliberately **not** over the whole object, and never over mutable workflow
state: moving REVIEW → APPROVED must not change the version, or the
transition would invalidate the approval that caused it.

An intent revised to v2 is a different request and produces a different id.

## 3. State machine

```text
Analyze -> Prepare ChangeSet -> Human Review -> Approve / Reject -> Verify

PROPOSED -> REVIEW | REJECTED
REVIEW   -> APPROVED | REJECTED
APPROVED -> APPLYING | REJECTED
APPLYING -> APPLIED | REJECTED
APPLIED  -> VERIFIED            (the ONLY outgoing edge -- I14)
VERIFIED -> (terminal)
REJECTED -> (terminal)
```

`APPLIED` is deliberately not terminal. An apply that nothing checked cannot
reach a finished state. `mark_verified` refuses without a report.

Every move produces a `Transition` carrying actor, timestamp and reason
(I12). A move with no actor raises `MissingActorError`, separately from an
illegal move raising `TransitionError` — a design mistake and a missing
signature are different problems.

## 4. Approval invariants

```text
ApprovalRecord { actor  timestamp  changeset_id  proposal_version  decision  reason }
```

All six mandatory; absent any one, construction is refused. `decision` is one
of `approve`, `reject`, `request_changes`, and only the first authorises
anything.

`proposal_version` is read **at the moment of the decision** and never
refreshed.

State and record must agree: an approval record on a ChangeSet that is not in
state APPROVED is refused. A disagreement about authorisation is resolved by
refusing, not by believing the record.

**Every refusal raises.** No falsy return, no logged warning, no
apply-and-mark. A boundary whose failure mode is a log line has already been
crossed by the time anybody reads the log.

## 5. Stale approval

```text
approve v1  ->  edit the proposal  ->  version changes  ->  apply refuses
```

Falls out of §2 and needs no bookkeeping. The alternative — a rule saying
"remember to invalidate approvals when the proposal is edited" — is a rule
somebody eventually forgets.

The refusal names **both** versions, so the reviewer can see what moved.

## 6. Snapshot rule

A proposal computed against snapshot S may only be applied while the session
is still at S. Otherwise the impact set and the simulation describe a world
that has moved, and the change is refused as `StaleDiff`.

Each `StructuredEdit` carries `expected_current` and is checked independently
at apply time. A value that is not what the edit believed is stale: **not
applied blind, and not silently reconciled**.

Applying part of an approved change is refused outright — it is worse than
applying none of it.

## 7. Apply boundary

**Exactly one function writes**: `apply_gate.apply_changeset`. Enforced by an
AST walk over the whole package, not by convention. The permitted-writer list
has one entry, and a second test asserts it has one entry, because the
cheapest way to satisfy the first is to add a name to it.

Three checks run, and all of them run **before any write** (asserted by line
number):

```text
check_approved  -> ApprovalRequired
check_version   -> StaleApproval
check_snapshot  -> StaleDiff
```

`apply_changeset` takes no `force`, `skip_approval`, `unsafe`, `bypass`,
`ignore_approval` or auto-confirm argument. A parameter that turns the gate
off is the gate being off.

Apply writes to the **session overlay**, never the baseline. The baseline hash
is identical afterwards, another session still reads the old value, and reset
still works (I17, I18). Consequence: apply and reverse-drift share the write
mechanism, and the only difference is that apply came through this gate —
which is what P8 detects.

Every check in `apply_changeset` must have a corresponding mutation in
`tests/mutation/test_approval_mutation.py`. Adding a gate without testing its
absence is itself a failure.

## 8. Action performer semantics

Seven kinds: `WORKFLOW  DATA  CODE  TEST  API  DOC  COMMUNICATION`.

```text
system   WORKFLOW  DATA  DOC          applied through the single apply path
human    CODE  TEST  API  COMMUNICATION   listed, approved, tracked, verified
                                          -- and NOT written
```

The apply result states explicitly which approved actions it did not carry
out. Silence there reads as "all of it was done".

`COMMUNICATION` stays. Lowering an approval threshold changes what a group of
managers does on Monday, not only what a config file says.

### Necessity

```text
required     direct impact evidence; without it the system is inconsistent
recommended  worth doing for consistency, not load-bearing
optional     could be done
unsupported  not enough information to propose -- a person decides
```

`required` is gated **in code**: an `Action` with no `impact_evidence` cannot
be constructed as `required`. A DIRECT node with no quotable line drops a step
rather than making the claim anyway.

A plan whose actions all carry one necessity is a failure: the field then
carries no information and a reviewer reads none of them.

The planner may propose **nothing**. Scenario H (the rule already says what
was asked for) yields zero actions *and a reason* — an empty list and an empty
list with a reason are different answers. Scenario E (knowledge-only
correction) yields no CODE, WORKFLOW or DATA action.

## 9. Detection is not generation. Proposal is not execution.

```text
detection   which code must change, which tests go red   -- IN SCOPE, KEPT
generation  writing the patch, writing the test          -- OUT OF SCOPE
```

Cutting the ability to **write** a patch must never quietly cut the ability to
**notice** one is needed. Only the second is the product's claim. Both appear
in the ChangeSet as `required` actions with `performer: human`.

Stale tests are detected per FUNCTION, from the affected range
`[min(current, proposed), max(current, proposed))` computed from the change
itself — never from a marker, a name convention, or a maintained list. A test
asserting the boundary value is **not** flagged: it is about to become
correct.

Detection reads the syntax tree, never the raw text. A regex matches the
number in a comment explaining why a test exists, which is the opposite of a
stale assertion.

`proposal != execution`: P6 answers "what if" and returns a `ProposalRequest`,
not a ChangeSet. P7 prepares a ChangeSet and never applies one.

## 10. Introduced vs inherited contradictions

Inherited from `P6_FROZEN.md` §2, with the P7 rules that follow from it:

- An **introduced** contradiction promotes its artifact to `required`. That is
  not a prediction about consistency; it is an observation of a contradiction
  that will happen.
- An **inherited** contradiction is recorded as a note, promotes nothing, and
  **may never be described as caused by this change**.
- The review screen labels the two separately, and both appear **above** the
  decision controls. A finding below the buttons is a finding somebody scrolls
  past.

### Which route is at fault

Both routes appear in a disagreement. **Only one is wrong: the route whose
answer did NOT move when the value moved.** The other disagrees precisely
because it read the change, and naming it would put a required code change on
a file that needs none.

A reviewer told two files are broken when one is will check neither.

### A route with no impact node

Impact analysis finds the artifacts that **state** the rule. A route that
**ignores** the rule has no such node — which is exactly why it is a problem.
When the simulation finds one, the ChangeSet must still represent it, with the
simulation cases as its evidence.

Without this the planner runs the simulation, receives the finding, and
proposes only the configuration change: an integration failure, not a feature
gap.

## 11. Actionability

Every action must identify a **concrete artifact and concrete work**. An
abstract rule node (`rule:<semantic_id>`) is the *subject* of the change, not
a thing to edit, and produces no action.

Non-actionable nodes remain available as **evidence and context**. They are not
removed from the impact set — they are not promoted into the action list.

An item naming no artifact and asking for no work makes the `required` list
longer without making it truer.

## 12. The screen never recommends

Risk explains: measured dimensions, unmeasured dimensions, evidence,
deterministic reason. It does not say "safe to approve", and no downstream
phase may convert a level into an automatic decision.

The moment the product advises, a signature stops meaning the person judged it
and starts meaning they agreed with it.

## 13. Benchmark lineage

Unchanged from the P6 freeze. No Eval or Stress dataset is regenerated at
this gate. The 24 hand-written scenarios remain an independent answer-key
layer.

## 14. Known wiring debt — not P7's to pay

`core/kernel/changeset.py` still has no pipeline caller. It is the document
pipeline's older object, **not** the ChangeSet defined here, and its wiring
remains P8-T03's, exactly as `tests/wiring/test_change_loop_wired.py`
declares.

The five `wiring_debt` failures are P0-declared and stay as they are. They may
shrink. They may not grow, and they may not be paid by deleting the test.
