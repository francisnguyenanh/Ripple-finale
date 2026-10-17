# P9 FROZEN CONTRACTS

Frozen at G9, before Phase 10 began. Enforced by
`tests/contract/test_p9_frozen_contracts.py`.

Inherits `P6_FROZEN.md`, `P7_FROZEN.md` and `P8_FROZEN.md` in full. Those
govern analysis, preparing and applying a change, and what is true
afterwards. This one governs **what a stranger on the internet may do**.

---

## 1. PUBLIC_SURFACE

`core/change_intelligence/public_surface.py::PUBLIC_SURFACE` is the single
authoritative list. **18 operations**, each with a security classification.

**Nothing is reachable that is not declared.** The router dispatches *from*
this table, so a function with no entry cannot be called however it is
written or named. `Api.__post_init__` refuses to start if the handler table
and the surface disagree in **either** direction: an extra handler would be
reachable and undeclared, a missing one would be a promise the service
cannot keep.

### Classifications — a closed set of four

```text
READ           returns state; changes nothing
SESSION_WRITE  changes state inside ONE session's overlay
DECISION       records a human decision, with actor and timestamp
CONTROLLED     a deliberately narrow mutation with its own whitelist
```

There is no `ADMIN`, `INTERNAL`, `DEBUG` or `UNSAFE`. An operation that
would need one does not belong on a public surface.

**Every writing operation states the boundary its effect stops at.**
Construction fails without it: a write with no stated boundary is a write
whose boundary nobody checked.

### The operations

```text
READ           health · state.observe · findings.list · evidence.show
               impact.show · changeset.review · lineage.show
               evaluation.report · intent.parse
SESSION_WRITE  session.create · session.reset · whatif.simulate
               intent.create · changeset.prepare · changeset.apply
               verification.run
DECISION       changeset.decide
CONTROLLED     scenario.external_edit
```

Absent by design, and each checked by a test rather than promised here:
arbitrary filesystem write, repository operation, shell execution, code
execution, dynamic import, infrastructure mutation, arbitrary path access,
artifact upload, object deserialisation, unrestricted network access.

`intent.parse` now stores a versioned, grounded candidate in the session.
It is SESSION_WRITE; confirmation by candidate id and hash creates the Intent.
The authenticated browser BFF lives in `infra/judge.py` and exposes only this
surface, behind a signed cookie. Per ADR-004 (the single-service deployment decision)
it is the single deployed service and calls `app.main.handle_rpc` in-process;
the raw backend `/rpc` route is never mounted publicly. Cloud credentials never
reach browser code. It introduces no additional business operations.

## 2. External-edit whitelist — derived, never maintained

The whitelist is **not a second list**. It lives inside the single
`CONTROLLED` operation as its `mutations`, and
`external_edit_whitelist()` returns exactly that tuple. There is nothing to
keep in sync, because there is only one thing.

A test asserts no other module declares a whitelist of its own, and that
exactly one operation is `CONTROLLED` — two would be two lists that drift.

A `Mutation` names an artifact and a locator **exactly**: no globs, no
prefixes, no path resolution. A pattern is a promise about strings that
somebody eventually finds a way around.

### The seven refusals, in order

```text
1. path traversal, or any name that is not spelled as the whitelist spells it
2. artifact not whitelisted
3. locator not whitelisted for that artifact
4. value fails the declared type or bounds  (and is NEVER coerced)
5. artifact not present in this session
6. expected_current does not match what is there  (stale)
7. the value is already what was asked for -> reported, not written
```

Values are never coerced. `"300000"` is refused rather than converted,
because the value a person sees in the request must be the value that lands.

## 3. Session isolation

Every session has its own overlay, lineage and transient work. Asserted from
**outside**, through the public API only — the same path a judge uses, not
an internal helper that might isolate differently.

**A handler cannot reach the registry.** It receives a `JudgeSession`, which
holds no reference to any other session and no route back to the collection.
Isolation therefore does not rest on every handler author remembering.

Ids minted in a session carry its id as a prefix, so a cross-session
reference is refused **by inspection** rather than by a lookup that misses —
a lookup that misses looks the same as an object that was deleted.

Session ids are **sequential, not random**: replay must reproduce a scenario,
and a random id would make two runs differ in a field that means nothing.

## 4. Reset semantics

```text
Reset DOES        clear this session's overlay
                  drop this session's transient work
                  rebuild this session's lineage to version 0
Reset DOES NOT    touch the baseline
                  touch any other session
                  delete a directory or any file
                  remove the session itself
```

Implemented as "clear this overlay", because the baseline is immutable and
is therefore already the known-good state. There is nothing to restore
*from* elsewhere.

The lineage is rebuilt rather than kept: it describes how far a verified
state advanced, and after a reset that state is the baseline again. Keeping
the entries would claim the session had confirmed a state it no longer
holds.

Reset is idempotent, and a session remains usable afterwards.

## 5. Replay semantics

Replay consumes a baseline, a recorded sequence of **public** calls, and a
frozen configuration, and reproduces the deterministic result exactly.

```text
MUST match    detection · impact · risk · ChangeSet structure
              verification state · snapshot ids · lineage
MAY differ    model-generated explanation text, where the architecture
              already permits it
```

The excluded-field set is asserted **empty** for everything in the first
list.

**Replay is not the production path.** The public judge runs live. A judge
who discovers the answers were cached is right to discount the whole AI
claim, and a test asserts no module under `app/` refers to replay at all.

Replay is for CI, regression, benchmark, video reproducibility and failure
analysis.

**The scenario script uses only public operations.** A runner with a private
path is the fake demo path the next gate forbids, and it is invisible until
somebody deviates from the script.

## 6. Evaluation semantics

A `Metric` cannot be constructed without **all four**: numerator,
denominator, answer key, scope. `scope` must be one of the declared values;
free text is how a seed-local figure becomes "system accuracy".

```text
value of 0/0   ->  None, never 1.0 and never 0.0
rendered()     ->  prints the scope and the key beside the figure
Report         ->  states that no figure is a claim about systems in general
```

Zero of zero is an absence, not a perfect score. Reporting a number for it
would put a figure on a slide that no data supports.

**Negative metrics sit in the same table as positive ones**: correct
abstention and unnecessary-change rate. Any system finds something if nobody
counts what it finds that is not there.

`unnecessary-change rate` is phrased so that **low is good and the numerator
counts mistakes** — a metric rewarding more proposals is one a planner learns
to game.

`build_report` fetches nothing. Observations and keys are both passed in, so
it cannot fetch a key from the system it is scoring.

**No model scores anything.** Gemini may extract, explain, summarise and
interpret intent. Identity, state, impact, risk, verification and benchmark
scoring are deterministic and authoritative.

## 7. Public failure behaviour

Everything fails closed. Every refusal carries a **code** distinct from its
message, so a caller can act on it rather than retry blindly.

```text
unknown_operation  404   the surface is a closed list
missing_session    400   there is no implicit current session
unknown_session    404   and it is NOT created helpfully
malformed_request  400   a missing field is a refusal, never a default
cross_session      403   refused by inspection of the id
artifact_not_whitelisted / locator_not_whitelisted   403
invalid_value      400   never coerced
stale_artifact     409
unsupported_artifact_family  400
approval_required  403
unattributed_decision / unknown_decision   400
internal_error     500   and states that nothing was changed
```

An unforeseen exception is caught, reported as a refusal naming its type,
and changes nothing. A refused request leaves the session byte-identical.

## 8. Security boundary

Audited by **inventory**, not only by endpoint tests — the thing that
actually goes wrong is an internal helper quietly becoming reachable.

A test walks the syntax tree of every module under `app/` and fails on:

```text
imports    subprocess os shutil pickle marshal socket urllib requests
           importlib ctypes
calls      system popen spawn fork exec eval compile __import__
           import_module loads load unlink remove rmtree mkdir chmod
           rename urlopen connect
also       yaml.load (only safe_load), open(), any deserialisation
```

`inventory()` returns the surface as data, so a review can read, diff and
commit it rather than run a test suite to learn what is exposed.

## 9. Cloud Run assumptions

`Api` holds no state of its own, so two containers behave identically given
one store. Session affinity is a performance optimisation and **never** a
correctness mechanism.

Migration-sensitive boundaries are named in
`docs/verification/cloud_readiness.md`: the session registry and the artifact
overlay are process-local today, both already sit behind protocols declared
in P1, and nothing in the public contract promises that a session survives a
restart or that two requests reach the same instance.

Tier 1 still uses SQLite when building a baseline offline. That is correct
and is stated, because "no SQLite" is true of the serving tier only.

**Nothing was provisioned and there is no live URL.** Compatible with the
architecture is a smaller claim than deployed.

## 10. Human approval boundary

Unchanged from P7 and P8. `changeset.apply` refuses anything not APPROVED,
`changeset.decide` refuses without an actor and a timestamp, and no public
operation decides whether a drift is acceptable.

The first screen carries **no alignment score and no ground-truth table**
(P6 freeze), and nothing on the public surface recommends a decision (P7
freeze). The moment the product advises, a person's approval stops meaning
they judged it.

## 11. Known debt — carried forward unchanged

```text
impact_graph              wired, test asks for the document pipeline
changeset (kernel)        untouched, no dependency proven
loop_controller           wired, test asks for the document pipeline
fact_correction           PAID at P8
reindex                   PAID at P8
H1                        resolved at P8 as domain audit lineage
```

P9 resolved none of these and claims none of them. The ledger in
`tests/wiring/test_change_loop_wired.py` remains unedited; remaining
`wiring_debt` failures: **3**.

## 12. Dataset

No Eval or Stress dataset regenerated. No seed tuned. The 24 hand-written
scenarios and IMPACT-001 remain independent answer-key layers.
