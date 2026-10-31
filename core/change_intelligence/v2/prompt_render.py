"""Single-pass prompt rendering (AI-01 step 2, 2026-10-31).

A prompt template carries `{name}` placeholders; the values rendered into them are a person's words or a source's text next to the server's own
sections. Substituting the placeholders one after another (`template.replace(a, x).replace(b, y)`) expands a placeholder literal that a person
typed inside an EARLIER value when a LATER substitution runs over it. `render_once` substitutes every placeholder of the template in ONE pass,
reading each value as data: a `{knowledge}` typed in a question stays the two characters the person typed.

Equivalence with the historical chain (the replay proof of the plan): for every input whose values carry no placeholder token of a later
substitution, the two renderings are byte-identical; the stored evidence prompts of the recorded runs were rendered from such inputs
(`tests/contract/test_prompt_render_single_pass.py` holds the proof on the builders' own fixtures).
"""
from __future__ import annotations

import re
from typing import Mapping

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_once(template: str, values: Mapping[str, object]) -> str:
    """Every `{name}` of `template` whose name is in `values` becomes `str(values[name])`, in one pass; a `{name}` absent from `values` stays as written
    (as the chain left it); placeholder literals inside the values are never expanded."""
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def render_sequentially(template: str, pairs) -> str:
    """The historical chain, kept for the equivalence proof only (never used by a builder)."""
    out = template
    for key, value in pairs:
        out = out.replace("{" + key + "}", str(value))
    return out
