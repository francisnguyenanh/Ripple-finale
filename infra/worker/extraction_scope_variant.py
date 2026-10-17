"""OPT-IN scope-variant extraction prompt, as a SUBCLASS in its own module.

`infra/worker/extraction.py` is deliberately NOT edited (it is pinned, and a w1_prod guard asserts it is byte-identical
to the reviewed baseline), and `job_worker.REGISTRY` still registers the plain `ExtractRulesExecutor`. This module is not
imported by any production path, so default behaviour and the pin stay byte-identical. Enabling the variant is an
INTENTIONAL change: register `ScopeVariantExtractRulesExecutor` in `job_worker` (and add this file to the pin closure),
then re-pin and re-measure with `tests/ai10_dev_wp3/measure_dev.py` before relying on it.

The only difference is one paragraph spliced into the prompt before the `<source>` block (never inside the untrusted
source): ask for an explicit, canonical scope object (`applies_to` / dimension key), explicit "all" when the text states a
universal scope, and keep "omit if not stated" (never guess). Result parsing/validation is inherited unchanged.
"""
from __future__ import annotations

import dataclasses
import math

from infra.worker.execution_types import ExecutionRequest, NonRetryableExecutionError
from infra.worker.extraction import ExtractRulesExecutor

SCOPE_VARIANT_ADDENDUM = (
    " SCOPE FORMAT: when the text states to whom/what a rule applies, give \"scope\" as "
    "an object whose key is \"applies_to\" (or the dimension, such as \"tier\", \"region\", "
    "\"channel\") and whose value is a short canonical noun phrase copied or minimally "
    "normalised from the text (for example \"premium customers\", \"EU\"). If the text "
    "states the rule applies to everyone/all cases, say so explicitly with the value "
    "\"all\"; never turn a restricted scope into \"all\" or the reverse. If the text does "
    "not state a scope, still omit or null it -- never guess or invent one.")

_SOURCE_OPEN = "\n<source>\n"


class ScopeVariantExtractRulesExecutor(ExtractRulesExecutor):
    # a subclass that changes the prompt MUST change its executor_id (recorded in the result / provenance)
    executor_id = "extraction.rule_candidates.scope_v1"
    prompt_variant = "scope_variant_v1"

    def prepare(self, ctx) -> ExecutionRequest:
        request = super().prepare(ctx)
        prompt = request.prompt
        # the first delimiter is the real one: role text precedes it and the source after it is delimiter-escaped
        idx = prompt.index(_SOURCE_OPEN)
        prompt = prompt[:idx] + SCOPE_VARIANT_ADDENDUM + prompt[idx:]
        if math.ceil(len(prompt.encode("utf-8")) / 2) > ctx.config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "source prompt exceeds input ceiling")
        return dataclasses.replace(request, prompt=prompt)
