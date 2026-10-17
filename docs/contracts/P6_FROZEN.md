# P6 FROZEN CONTRACTS

Frozen at G6 PASS, before Phase 7 began. Phase 7 and later phases **consume**
these definitions; they do not re-derive them, and no LLM re-infers them.

Enforced by `tests/contract/test_p6_frozen_contracts.py`. Changing a
definition here means changing that test in the same commit, deliberately.

---

## 1. What the impact metrics are, and are not

```text
recall 1.00 · precision 1.00 · unnecessary 0.00
```

measured against **the current seed corpus and the hand-written answer key
IMPACT-001**, on 2026-09 lineage.

This is **not** proof of general correctness. It is the statement that on a
body of data whose right answer was written down by hand before the analyser
ran, the analyser agreed with it. A different corpus can produce a different
score, and that is the point of keeping the key.

The 24 hand-written scenarios in `reference_system/scenarios/corpus.yaml`
remain an **independent answer-key layer**: written from the rules, not from
the code, and never regenerated from engine output. Regenerating them from
the engine would turn the key into a copy of the thing it checks.

**Benchmark lineage:** seed as of commit `8ec4475`; key authored by hand;
neither regenerated since. Golden data may be regenerated only once the
P4–P6 contracts below are confirmed — not merely because a gate passed.

---

## 2. Introduced vs inherited contradictions

Two separate, independently named members of `SimulationResult`:

```text
new_inconsistencies()        disagreements the proposed change CREATES
inherited_inconsistencies()  disagreements that were already there
```

A consumer reads both. It never reads their union, and never computes one by
subtracting the other. **A phase downstream may not describe an inherited
contradiction as caused by the proposed change.**

## 3. DIRECT / INDIRECT / UNKNOWN

```text
DIRECT     the artifact CARRIES the value or behaviour the system actually
           uses for the affected decision
INDIRECT   the artifact mentions, documents, tests or depends on the rule
UNKNOWN    evidence is insufficient to place it
```

Merely mentioning the same rule, number, wording or identifier is **not**
DIRECT. A policy document may be impacted and require updating without being
the execution point; saying otherwise tells a reader nine things are direct
and teaches them to stop believing the word.

UNKNOWN stays a valid result. Forcing every node into DIRECT or INDIRECT
would mean inventing a placement the evidence does not support.

## 4. Risk

Deterministic and evidence-based. Every dimension reports its level, its
detail, and whether it was `measured`.

```text
unmeasured dimension  ->  raises the FLOOR to MEDIUM, contributes 0 points
```

A missing measurement may never produce a high score. A false alarm teaches
people to ignore the label exactly as a false reassurance does.

A risk level is **not** a recommendation. Risk explains: known dimensions,
unknown dimensions, the evidence, and the deterministic reason. The human
decision remains authoritative, and no downstream phase may convert a level
into an automatic approve or reject.

## 5. Handler collision

Handlers are discovered by **shape** — `(request, config) -> outcome` — never
by module name. A collision is reported only when all three hold:

```text
1. a valid common input exists
2. BOTH routes accept it (neither raises, neither declines)
3. their outcomes are materially different and deterministic
```

Two routes that simply do not apply to the same input are not a collision.

## 6. Hard-coding guard

AST-based, never plain-text grep. It detects **executable literals** and
permits documentation that explains a number conceptually. A text grep flags
a docstring explaining where a figure comes from — the opposite of the defect
the guard exists to catch.

## 7. P7 entry contract

Phase 7 consumes, and may not re-infer with a model:

```text
alignment state           introduced contradictions
impacted artifacts        inherited contradictions
DIRECT/INDIRECT/UNKNOWN   deterministic risk
evidence references       simulation results
workload / impact data
```

Gemini may explain, summarise or draft prose. **The deterministic P6 output
is authoritative** wherever the two differ.

## 8. Human authority

```text
Analyze -> Prepare ChangeSet -> Human Review -> Approve/Reject -> Verify
```

P7 prepares. It never silently applies.
