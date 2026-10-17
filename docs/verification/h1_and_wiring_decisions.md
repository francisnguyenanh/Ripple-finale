# H1 and the wiring debt — two decisions, with the evidence

Both were carried as open ambiguities into the verification phase. Both are resolved here
rather than built around, because an ambiguity you build on top of stops
being an ambiguity and becomes an assumption nobody remembers making.

---

# H1 — `history` is declared history, and that is the contract

**Decision: implement and verify the domain audit lineage. Do NOT build a
Git-backed provenance layer.**

## What the existing contract actually requires

Three consumers read the `history` family. Each was checked, not assumed:

| Consumer | What it needs | Git required? |
|---|---|---|
| `alignment._has_time_ordering` | an `observed_at` per representation, to tell DRIFT from CONFLICT | no — any dated record |
| `discovery_report.stale_from_history` | what a change **said about itself**: declared temporary, no follow-through | no — and git could not supply it |
| P8 verification | did this state arrive through an approved ChangeSet? | no — see below |

The second row is the interesting one. The single most valuable thing the
history adapter extracts is *"this was added in November 2020 marked
temporary, and there is no revert"*. That is a **claim a person recorded**,
not a fact a version-control system stores. A commit log says a line
changed; it does not say somebody intended to change it back. Excavating
git would give us less of what this feature needs, not more.

A grep across `core/` and `tests/` finds **no** dependency on `git log`,
GitPython, dulwich, or a git subprocess. Nothing in the frozen P4–P7
contracts mentions repository provenance. The contract is a **dated,
attributed changelog**, and `changelog.jsonl` satisfies it exactly.

## What P8 needs, and where it comes from

P8's provenance question is not "which commit changed this file". It is:

> did the current state of this artifact arrive through an approved
> ChangeSet, or did somebody write it?

That is answered by the session snapshot chain and `VerifiedLineage`, both
deterministic and both built this phase:

```text
snapshot_id          content-addressed; the state IS the id
parent_snapshot_id   the chain, deterministic
VerifiedLineage      which snapshots a verification passed on, and under
                     which ChangeSet
```

An artifact at a snapshot with no lineage entry is, precisely, a change
nobody confirmed — which is the reverse-drift finding. Git would add
nothing here and could not participate: the runtime writes to a session
overlay and never to the filesystem (I17, I18), so there is no repository
for it to observe.

## What this is not

This is **not** a claim that a production deployment would never want real
repository provenance. It is a statement that the contract in front of us
does not require it, that building it would be a separate platform project,
and that the honest place to say so is here and in the README's limitations
section rather than in a half-built integration.

`README §21` and `docs/verification/gate_G2.md` already state that `history`
is declared. That stays true and stays stated.

## Enforced by

`tests/contract/test_h1_history_provenance.py` — asserts no git dependency
exists, that the three consumers read dated records rather than commits, and
that the reverse-drift finding is derived from the lineage rather than from
any repository.

---

# The five wiring debts, classified

This part concerns the earlier document pipeline, which is not part of this submission; it is kept because the frozen contract (`docs/contracts/P8_FROZEN.md` §17) cites it.

The ledger in `tests/wiring/test_change_loop_wired.py` is **untouched**. No
test deleted, no ownership moved, no unrelated phase edited to force a
colour change. Two debts went green because the loop genuinely needed the
modules; three did not, and the reason is the same for all three.

| Module | Classification | Status |
|---|---|---|
| `fact_correction` | **required for P8** | **PAID** |
| `reindex` | **required for P8** | **PAID** |
| `loop_controller` | **required for P8**, wired — but not where the test looks | partially paid |
| `impact_graph` | required later | deferred, stated |
| `changeset` (kernel) | **intentionally deferred** | deferred, stated |

## Paid: `fact_correction` and `reindex`

Both are now imported and called by
`core/change_intelligence/orchestrator.py`, and both are load-bearing rather
than decorative:

- `guard_loop_never_touches_ir` is checked **before any work** in the
  reality direction. I8 is the claim that the loop cannot quietly rewrite
  what was observed, and it is now enforced on a real execution path, with
  tests that force a scope onto the Fact layer and require the halt.
- `find_chunks_to_reindex` scopes the reindex to the knowledge the change
  actually reached. P8-T02's whole claim is that work is proportional to
  the change.

Their two wiring tests are green. The ledger still lists them, because
removing an entry requires deleting its test and that was forbidden. The
ledger guard is unaffected and still passes.

## Partially paid: `loop_controller`

`classify_change` is called by the orchestrator on every turn of the reality
direction, so the module is **no longer library-only** — the thing the debt
was actually about. Its test asks specifically for an import in
`core/pipeline_stages.py`, which is the **document pipeline**, a different
execution path that has no reason to classify change-intelligence loops.

Making that test green would mean importing loop orchestration into an
unrelated pipeline to satisfy a string match. That is the speculative
architecture the phase brief forbids, so the test stays red and this
paragraph is the reason.

## Deferred: `impact_graph`

`impact_scope` is called by the orchestrator, so this module is also no
longer library-only. Its test asks for something more specific and
different: that `impact_scope` **drives regeneration** inside
`pipeline_stages.py`. That is the document pipeline deciding what to
rebuild — real work, genuinely belonging to the document pipeline, and
about that other pipeline. Not required for either loop direction.

## Intentionally deferred: `core/kernel/changeset.py`

The document pipeline's older changeset object. It is **not** the P7
`ChangeSet`, which is `core/change_intelligence/changeset.py` and is fully
wired and exercised. P8's verification path has no dependency on the kernel
object — stated as a dependency claim and checked, not assumed.

Touching it was explicitly out of scope absent a proven dependency. There
is none, so it was not touched.

## The honest summary

```text
5 declared at P0
2 paid       fact_correction, reindex        -- tests now green
2 no longer library-only but tests still red loop_controller, impact_graph
1 untouched, no dependency proven            changeset (kernel)

wiring_debt failures: 5 -> 3
```

Per the fallback that was agreed for this case ("wire 3 of 5 and declare the rest"), and per
its closing note: *if a module is still library-only at the end of the
phase, the README must say exactly that*. Two are no longer library-only
and their tests say so; two are wired but not where their tests look; one is
untouched. All five entries remain in the ledger.

---

# 2026-09-25 — the three remaining debts are PAID (owner chose real wiring)

The owner rejected declaring them as limitations and asked for the wiring
itself. It lives in the document pipeline, where the tests look, and it is
load-bearing: S9 (`stage_documents`) now decides which documents are
**regenerated** -- re-issued as new AI drafts whose human review no longer
holds -- instead of silently resetting every review on every run.

| Module | Where | What it decides |
|---|---|---|
| `impact_graph` | `pipeline_stages._regeneration_plan` | `impact_scope` over the KM lineage graph: a document citing any changed record or anything downstream of one is regenerated, even if its own text did not move |
| `loop_controller` | same | `classify_change`: loop3 (catalog changed) regenerates all; loop2 (KM changed) the graph's reach plus moved text; loop1 (wording only) moved text only |
| `changeset` (kernel) | same + S9 | each regeneration appends a `Changeset` (computed scope, trigger, real composer `AgentCallStats`) to `work/versions/changesets.jsonl`; `Baseline` decides the no-change fast path |

Supporting changes:
- Every document is still re-rendered (frontmatter carries run-global hashes
  the stale checks require); what the graph decides is review state.
- Composed sections reuse the last render's model output while their prompt is
  unchanged (`work/versions/composer_runs.jsonl`, `RecordingProvider` +
  `ReplayProvider`). Without it an LLM rewrite made every composed document
  "change" on every run.
- Bug fixed on the way: `_impact_of_km` (S8) built an invalid `Edge` whose
  TypeError was swallowed, so every record with dependents had no blast
  radius. S8 and S9 now share one lineage graph (`_km_lineage_graph`).

Owner sign-off (2026-09-25): review state now survives a re-run unless the
change reaches the document. Evidence: `core/tests/test_p8_regeneration.py`
(11 tests, incl. two mutations: stubbing `impact_scope` or forcing loop1 lets
a stale downstream approval survive). The ledger in
`tests/wiring/test_change_loop_wired.py` is untouched; all five wiring tests
are green.
