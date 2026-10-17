# -*- coding: utf-8 -*-
"""Run the corpus twice, for real, and let the disagreement surface.

    current = run(corpus, baseline_config)
    shadow  = run(corpus, shadow_config)

Both halves EXECUTE. This is the decision that matters here: a simulation that only reads code
cannot discover that one handler ignores configuration, and calling static
analysis "simulation" would be claiming something the product does not do.

The reference application is Python with explicit dependency injection, so both states run in one process with no module reloading and no
import-order games. A service takes `config` as an argument; nothing reads a
global.

**Handlers are discovered by SHAPE, never by name.** A handler is any
callable taking `(request, config)` and returning an outcome. This is a hard test constraint: the string "emergency_purchase" may not appear
in an assertion. If a test has to name the suspect module, the product is
staging a discovery rather than making one -- and on a real codebase nobody
knows the name in advance, which is the entire point.

So the inconsistency this module finds is stated structurally:

    two handlers, one input, two different answers

and it is found by comparing return values, not by reading source.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

#: The signature a handler must have to be discovered. Two parameters, and
#: the second one named `config` -- the injection point.
_HANDLER_PARAMS = ("request", "config")


@dataclass(frozen=True)
class Handler:
    """One executable path through the business rules."""

    name: str
    call: object

    def __call__(self, request, config):
        return self.call(request, config)


@dataclass(frozen=True)
class Outcome:
    """What one handler decided about one case, in one world."""

    case_id: str
    handler: str
    outcome: str
    error: str = ""


@dataclass(frozen=True)
class CaseResult:
    """One case, in both worlds, through every handler that accepted it.

    Two different questions are asked of this object and they must not be
    confused, because they give different numbers on the same data:

        changed        did the outcome this request ACTUALLY gets move?
        disagreeing    do two paths that can both decide it disagree?

    The first is the operational number a manager reads ("this many
    requests would be handled differently"). The second is a defect report
    ("two routes give one request two answers"). Counting every path in the
    first would inflate it with requests whose real-world handling never
    changes, and counting only the effective path in the second would hide
    the contradiction entirely.
    """

    case_id: str
    current: tuple[Outcome, ...]
    shadow: tuple[Outcome, ...]
    #: Which handler's answer is the one that really happens. Supplied by
    #: the caller: which route a request travels is a fact about the
    #: enterprise, not something simulation can derive.
    effective_handler: str = ""

    @property
    def changed(self) -> bool:
        """Did the outcome this request actually receives move?"""
        return self._effective(self.current) != self._effective(self.shadow)

    def _effective(self, outcomes):
        answers = {o.handler: o.outcome for o in outcomes if not o.error}
        if self.effective_handler:
            for handler, outcome in answers.items():
                if handler.endswith(self.effective_handler):
                    return outcome
        # With no effective handler named, every path counts. Falling back
        # to "any difference" is the safer direction: it over-reports a
        # change rather than silently missing one.
        return tuple(sorted(answers.items()))

    @staticmethod
    def _by_handler(outcomes) -> dict:
        return {o.handler: o.outcome for o in outcomes}

    def disagreeing_handlers(self, world: str = "shadow") -> tuple:
        """Handlers that answered differently FROM EACH OTHER, in one world.

        This is the inconsistency detector, and it never mentions a module.
        Two paths that both accept a request and return different decisions
        are a contradiction in the business rules, whatever they are called.
        """
        outcomes = self.shadow if world == "shadow" else self.current
        answers = {o.handler: o.outcome for o in outcomes if not o.error}
        if len(set(answers.values())) <= 1:
            return ()
        return tuple(sorted(answers))


@dataclass
class SimulationResult:
    cases: list = field(default_factory=list)
    handlers: tuple = ()

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def changed(self) -> list:
        return [c for c in self.cases if c.changed]

    @property
    def unchanged(self) -> list:
        return [c for c in self.cases if not c.changed]

    def inconsistencies(self) -> list:
        """Cases where two handlers disagree in the SHADOW world.

        Reported per case rather than counted: a count tells a reader how
        big the change is, and this tells them something is wrong.
        """
        return [c for c in self.cases if c.disagreeing_handlers("shadow")]

    def new_inconsistencies(self) -> list:
        """Disagreements the shadow INTRODUCED.

        The distinction matters: a contradiction that already existed is a
        finding about today, and one the proposed change creates is a
        consequence of the proposal. Reporting them together would blame
        the change for a problem it inherited.
        """
        return [c for c in self.cases
                if c.disagreeing_handlers("shadow")
                and not c.disagreeing_handlers("current")]

    def inherited_inconsistencies(self) -> list:
        """Disagreements that were ALREADY there before the change.

        Named and returned separately, rather than left as the difference
        between two other lists, because a consumer that has to subtract
        one list from another to find them will eventually not bother --
        and will then present a pre-existing defect as something the
        proposal caused. FROZEN CONTRACT (P6 freeze, item 2): a downstream
        phase consumes `new_inconsistencies` and this, never their union.
        """
        return [c for c in self.cases if c.disagreeing_handlers("current")]


def discover_handlers(*modules) -> tuple:
    """Every callable in these modules shaped like a decision path.

    By signature, sorted by name for determinism. Nothing here knows what
    the application does, which is why it keeps working when somebody adds
    a seventh handler nobody told the product about.
    """
    found = []
    for module in modules:
        for name, obj in sorted(vars(module).items()):
            if name.startswith("_") or not callable(obj):
                continue
            if getattr(obj, "__module__", None) != module.__name__:
                continue
            try:
                params = list(inspect.signature(obj).parameters)
            except (TypeError, ValueError):
                continue
            if tuple(params[:2]) == _HANDLER_PARAMS and len(params) == 2:
                found.append(Handler(name=f"{module.__name__}.{name}", call=obj))
    return tuple(sorted(found, key=lambda h: h.name))


def _accepts(handler, request, config) -> Outcome | None:
    """Run one handler, treating a refusal as a non-answer rather than a crash.

    A handler that raises on a request it does not serve has not made a
    decision about it, and recording "error" as though it were an outcome
    would turn every unrelated path into a spurious disagreement.
    """
    try:
        result = handler(request, config)
    except Exception as exc:  # noqa: BLE001 -- a path that refuses is not a verdict
        return Outcome(case_id="", handler=handler.name, outcome="",
                       error=f"{type(exc).__name__}: {exc}")
    if result is None:
        return None
    outcome = getattr(result, "outcome", result)
    return Outcome(case_id="", handler=handler.name, outcome=str(outcome))


def run(corpus, *, handlers, current_config, shadow_config,
        build_request, applies_to=None, effective_handler=None
        ) -> SimulationResult:
    """Execute every case against every handler, in both worlds.

    `applies_to` decides which handlers see which case -- the emergency
    path is only meaningful for an emergency request. `effective_handler`
    says which of them actually decides. Both are supplied by the caller
    rather than inferred, because "which route does this request travel"
    is a fact about the enterprise and not about simulation.

    A request is deliberately run through EVERY path that could accept it,
    not only the one that does. That is what makes the golden finding
    visible: the emergency route and the standard route both answer, and
    after the change they answer differently.
    """
    handlers = tuple(handlers)
    result = SimulationResult(handlers=tuple(h.name for h in handlers))

    for case in corpus:
        request = build_request(case)
        case_id = str(case.get("historical_case_id") or case.get("id") or len(result.cases))

        current, shadow = [], []
        for handler in handlers:
            if applies_to is not None and not applies_to(handler, case):
                continue
            for config, bucket in ((current_config, current), (shadow_config, shadow)):
                outcome = _accepts(handler, request, config)
                if outcome is None:
                    continue
                bucket.append(Outcome(case_id=case_id, handler=outcome.handler,
                                      outcome=outcome.outcome, error=outcome.error))

        result.cases.append(CaseResult(
            case_id=case_id,
            current=tuple(current),
            shadow=tuple(shadow),
            effective_handler=(effective_handler(case)
                               if callable(effective_handler)
                               else (effective_handler or ""))))

    return result


__all__ = [
    "CaseResult",
    "Handler",
    "Outcome",
    "SimulationResult",
    "discover_handlers",
    "run",
]
