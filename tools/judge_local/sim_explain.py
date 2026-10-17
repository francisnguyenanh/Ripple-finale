"""SIMULATED wording of verified facts for the local rehearsal (NOT a model, no network, no spend). It only rephrases numbers and names
that are already in the facts, so the server's check (every number must come from the facts) passes for the right reason."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

TAG = "[SIMULATED AI] "


def _num(v):
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return None


def wording(prompt: str) -> str:
    facts = json.loads(prompt.split("<facts>\n", 1)[1].split("\n</facts>", 1)[0])
    parts = []
    r = facts.get("request") or {}
    if r and _num(r.get("from")) and _num(r.get("to")):
        cur = f" {r['currency']}" if r.get("currency") else ""
        parts.append(f"The request changes {r.get('what') or 'a rule'} from {_num(r['from'])}{cur} to {_num(r['to'])}{cur}.")
    sim = facts.get("simulation")
    if isinstance(sim, dict) and sim.get("cases_simulated") is not None:
        parts.append(f"In the simulation, {sim.get('cases_that_change')} of {sim.get('cases_simulated')} sample cases would change their outcome.")
    elif isinstance(sim, dict):
        parts.append("No behaviour simulation backs this change, so its effect on real cases is not known.")
    rev = facts.get("review") or {}
    if rev.get("checks_total"):
        parts.append(f"{rev.get('checks_passed')} of {rev.get('checks_total')} checks passed.")
    if rev.get("checks_not_available"):
        parts.append(f"{len(rev['checks_not_available'])} check(s) could not run and stay unresolved.")
    n = facts.get("manual_items_left_for_people")
    if n:
        parts.append(f"{n} related item(s) are left for people to edit.")
    if facts.get("state"):
        parts.append(f"The change is currently {str(facts['state']).replace('_', ' ').lower()}; it is not finished until a person accepts the result.")
    if not parts:
        parts.append("The verified facts do not contain enough to explain.")
    parts.append(facts.get("next") or "A person decides next.")
    return TAG + " ".join(parts)


class SimWording:
    def __init__(self, model_id):
        self.model_id, self.calls = model_id, 0
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        text = re.sub(r"\s+", " ", wording(kw["contents"])).strip()[:690]
        return SimpleNamespace(text=json.dumps({"text": text}), model_version=self.model_id, candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=60, candidates_token_count=60, thoughts_token_count=None, total_token_count=120))
