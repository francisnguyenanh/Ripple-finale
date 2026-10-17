# Python adapter — traps

What this adapter deliberately does not handle, and why. Written because a
stated limit is engineering, an
unstated one is a surprise waiting for whoever trusts the output.

---

## Not handled

| Construct | Consequence | Why not |
|---|---|---|
| `exec` / `eval` | A threshold built at runtime is invisible | Nothing short of running the code finds it, and the adapter is a static reader by design |
| Dynamic `importlib` | `DEPENDS_ON` misses the edge | Same |
| Metaclasses, `__getattr__` | A config read through a proxy is missed | The reference application uses neither; a real codebase that does needs a note in its profile |
| Decorators that rewrite a signature | `accepts_config` can be wrong | `functools.wraps` preserves the name, not the parameter list |
| Two levels of aliasing | `a = config.x; b = a; if amount >= b` reads as a plain variable | One level is tracked. Two is rare enough that guessing would cost more precision than it buys recall |
| Class attributes holding policy | `self.threshold = 1_000_000` in `__init__` is not a comparison, so it is not reported as a threshold | The literal IS captured as part of the class body; linking it to a later comparison needs dataflow this adapter does not do |
| Non-integer thresholds | `amount >= 1_000_000.0` is skipped | The domain is JPY, which has no minor unit. A float threshold here would itself be a defect |

## Judgement calls, stated rather than hidden

**`_MIN_INTERESTING_LITERAL = 1000`.** Below this, a bare integer in a
comparison is far more often an index, a retry count or a timeout than a
business threshold. The cost of the cut-off is a missed threshold under
¥1,000; the cost of not having it is every `len(x) > 3` in the codebase
arriving as a candidate business rule, which would bury the real ones.

**`_CONFIG_PARAMS = {config, cfg, settings, policy}`.** The adapter
recognises injected configuration by parameter NAME. That is only sound
because ADR-002 C-1 requires the reference application to take its config
that way. Pointed at an arbitrary codebase, this is the first assumption
that breaks, and it should be a profile setting before that happens.

## What it does do, and what depends on it

Two records carry the weight of WOW #2:

```text
hardcoded_threshold   from_config: false   a number that came from the source
config_threshold      from_config: true    a comparison against the policy
```

Plus one summary per function that accepts a config:

```text
reads_config     it asked the policy
forwards_config  it handed the policy to someone else -- not a defect
ignores_config   it took a policy and neither read nor passed it   <- the defect
```

The third line is `emergency_purchase.evaluate_emergency`. Distinguishing
it from the second is what stops the finding being buried among every
dispatcher in the application.
