# -*- coding: utf-8 -*-
"""Business Q&A over a snapshot-pinned context pack: prompt, claim validation and the server's final status (pure, no I/O).

The model phrases and cites; the server decides what stands. Every claim must cite ids that exist in the pack the asker was allowed to see, its numbers must come from
the cited items or from the question, and its basis (decided by a person versus what the sources say) must match the cited authority. A model never chooses between
statements that disagree, a thin or insufficient pack never yields ANSWERED, and an empty result is never presented as proof that no rule exists.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2.business_index import rule_is_gate

STATUSES = ("ANSWERED", "PARTIAL", "NEEDS_CLARIFICATION", "ABSTAINED")

# ---- applicability of a claim to the case the question describes (live finding 2026-10-17: a model applied thresholds of other purchase types) ------------------------
# The question is read for typed facts (one amount in JPY, CAPEX/OPEX, standard/emergency, supplier risk). When it describes a concrete case (an amount), every kept claim
# that cites a rule is checked against that rule's scope, condition and exceptions with the MON-POL evaluator: `applies`, `conditional` (a fact the asker could add would
# decide it), `not_applicable` (dropped: the rule does not reach this case) or `not_evaluated` (the claim rests on a passage or a decision, not on a structured rule).
# A scope key whose allowed values cover every value any rule of the pack names is treated as universal. A pinned default (config `fact_defaults`) may stand in for a
# missing fact; it is then disclosed as an assumption, never silent.

_AMOUNT_A = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(million|thousand|m\b|k\b)?\s*(?:yen|jpy|\u5186)", re.I)
_AMOUNT_B = re.compile(r"(?:jpy|\u00a5|\uffe5)\s*(\d[\d,]*(?:\.\d+)?)\s*(million|thousand|m\b|k\b)?", re.I)
_FOREIGN_AMOUNT = re.compile(r"(?:\d[\d,]*(?:\.\d+)?\s*(?:million|thousand|m\b|k\b)?\s*(?:usd|us dollars?|dollars?|eur|euros?|gbp|pounds?|cny|rmb|yuan)\b)|(?:[$\u20ac\u00a3]\s*\d)|(?:\b(?:usd|eur|gbp)\s*\d)", re.I)
FOREIGN_QUESTION = "What is the amount in JPY? The rules are written in JPY and an amount in another currency is never converted."
_MULT = {"million": 1_000_000, "m": 1_000_000, "thousand": 1_000, "k": 1_000, None: 1, "": 1}
FACT_WORDS = {
    "request.category": ((r"\bcapex\b|capital (?:equipment|expenditure|asset|acquisition)|long-term asset", "CAPEX"), (r"\bopex\b|operational expenditure|operating expense", "OPEX")),
    "process": ((r"\bemergency\b", "EMERGENCY"), (r"\bstandard\b", "STANDARD")),
    "supplier.risk": ((r"risk register|high[- ]risk|\brisky\b", "HIGH"),),
    "supplier.status": ((r"\bblocked\b", "BLOCKED"),),
    "purchase.category": ((r"licen[cs]e renewal|renew\w*\s+(?:of\s+)?(?:the\s+|our\s+|an?\s+|its\s+)?(?:existing\s+)?software\s+licen[cs]e", "SOFTWARE_LICENCE"),),   # a NEW subscription is not a renewal
}
FACT_QUESTIONS = {"process": "Is this a standard purchase or an emergency purchase?", "request.category": "Is this a capital purchase (CAPEX) or an operating expense?",
                  "supplier.risk": "Is the supplier on the risk register (high risk)?", "supplier.status": "Is the supplier blocked?",
                  "purchase.category": "Is this the renewal of an existing software licence, or a new purchase such as a new subscription?"}
FACT_ASSUMPTION_WORDS = {"process": {"STANDARD": "a standard purchase", "EMERGENCY": "an emergency purchase"}}


def question_facts(question: str, defaults: Optional[Mapping] = None) -> dict:
    """-> {"facts": {...}, "assumed": {...}, "amounts": n}. Facts are read from the question text only; `defaults` (pinned in the config) fill a missing key and are
    reported in `assumed`. The amount is kept only when the question names exactly one amount."""
    text = question or ""
    amounts = set()
    for rx, order in ((_AMOUNT_A, (1, 2)), (_AMOUNT_B, (1, 2))):
        for m in rx.finditer(text):
            try:
                n = float(m.group(order[0]).replace(",", "")) * _MULT[(m.group(order[1]) or "").lower() or None]
            except (ValueError, KeyError):
                continue
            amounts.add(int(n) if float(n).is_integer() else n)
    facts: dict = {}
    if len(amounts) == 1:
        facts["request.net_amount_jpy"] = next(iter(amounts))
    low = text.lower()
    for key, patterns in FACT_WORDS.items():
        for rx, value in patterns:
            if re.search(rx, low):
                facts[key] = value
                break
    assumed = {}
    for key, value in (defaults or {}).items():
        if key not in facts and isinstance(value, str) and value:
            facts[key] = value
            assumed[key] = value
    return {"facts": facts, "assumed": assumed, "amounts": len(amounts), "foreign_amount": bool(_FOREIGN_AMOUNT.search(text))}


def scope_universe(items: Iterable[Mapping]) -> dict:
    out: dict = {}
    for it in items:
        if it.get("kind") == "rule":
            for key, allowed in (it.get("scope") or {}).items():
                if isinstance(allowed, list):
                    out.setdefault(key, set()).update(str(a).strip().lower() for a in allowed)
    return out


def _scope_with_universe(rule: Mapping, facts: Mapping, universe: Mapping):
    from core.change_intelligence.v2.policy_decision import _scope_applies
    scoped, why = _scope_applies(rule, facts)
    if scoped is None:
        left = []
        for reason in why:
            key = reason.split(":", 1)[1]
            allowed = {str(a).strip().lower() for a in (rule.get("scope") or {}).get(key, [])}
            if not (len(universe.get(key, set())) >= 2 and allowed >= universe[key]):       # the rule names every value the pack knows: the fact does not matter
                left.append(reason)
        return (True, []) if not left else (None, left)
    return scoped, why


AMOUNT_FACT = "request.net_amount_jpy"
_NOT_AN_AMOUNT_OF_THE_CASE = re.compile(r"difference|tolerance|delta|variance|fee|charge|gross|tax|freight|shipping", re.I)
_AMOUNT_NAME = re.compile(r"(^|[._])(amount|value|price|total|cost|sum|spend)(_jpy)?$", re.I)


def _amount_leaves(cond) -> list:
    """(field, unit) of every order comparison in a condition tree."""
    if not isinstance(cond, Mapping):
        return []
    if "field" in cond:
        return [(str(cond["field"]), cond.get("unit"))] if cond.get("op") in ("gt", "gte", "lt", "lte") else []
    out: list = []
    for key in ("all", "any"):
        for c in cond.get(key, []):
            out += _amount_leaves(c)
    if "not" in cond:
        out += _amount_leaves(cond["not"])
    return out


def amount_field_stands_for_the_case(field: str, unit) -> bool:
    """Whether a rule's compared field is the amount of the case: a `..._jpy` field, or an amount-named field (`purchase_request.amount`) the rule compares in JPY
    (the unit is on the leaf, from the source). Never a difference, tolerance, charge, gross, tax or freight field, never a field in another currency, never a field
    without a known currency (prose field names are not read as the case amount on the name alone: that stays a question)."""
    if _NOT_AN_AMOUNT_OF_THE_CASE.search(field):
        return False
    if field.endswith("_jpy"):
        return True
    return bool(_AMOUNT_NAME.search(field)) and isinstance(unit, str) and unit.strip().upper() == "JPY"


def _fields_in(cond) -> set:
    if not isinstance(cond, Mapping):
        return set()
    if "field" in cond:
        return {str(cond["field"])}
    out: set = set()
    for key in ("all", "any"):
        for c in cond.get(key, []):
            out |= _fields_in(c)
    if "not" in cond:
        out |= _fields_in(cond["not"])
    return out


def case_facts_for_rule(rule: Mapping, facts: Mapping) -> dict:
    """The one amount the question states is the amount of the case: it also answers any other JPY amount field the rule compares (order net goods, travel expense ...),
    never a field that measures a difference, tolerance or charge. Live eval finding 2026-10-17: freight rules compare `order.net_goods_jpy`, not `request.net_amount_jpy`."""
    out = dict(facts)
    if AMOUNT_FACT in facts:
        leaves = _amount_leaves(rule.get("condition")) + [l for e in rule.get("exceptions") or [] if isinstance(e, Mapping) for l in _amount_leaves(e.get("condition"))]
        for f, unit in leaves:
            if f not in out and amount_field_stands_for_the_case(f, unit):
                out[f] = facts[AMOUNT_FACT]
    return out


def rule_applicability(rule: Mapping, facts: Mapping, universe: Mapping, *, alias_amount: bool = True) -> dict:
    """{"status": applies|conditional|not_applicable|not_evaluated, "missing": [fact keys], "why": [...]} for one structured rule.
    `alias_amount=False` evaluates only the facts the question states (no case amount standing in for another JPY field): used for rules nobody cited."""
    from core.change_intelligence.v2.policy_decision import evaluate_condition
    if rule.get("condition") is None:
        return {"status": "not_evaluated", "missing": [], "why": ["rule_condition_unknown"]}
    facts = case_facts_for_rule(rule, facts) if alias_amount else dict(facts)
    scoped, why = _scope_with_universe(rule, facts, universe)
    if scoped is False:
        return {"status": "not_applicable", "missing": [], "why": ["scope_excludes_this_case"]}
    held, cwhy = evaluate_condition(rule["condition"], facts)
    if held is False:
        return {"status": "not_applicable", "missing": [], "why": ["condition_does_not_hold"]}
    missing = sorted({r.split(":", 1)[1] for r in why + cwhy if ":" in r})
    undecided_exception = False
    for e in rule.get("exceptions") or []:
        cond = e.get("condition") if isinstance(e, Mapping) else None
        if cond is None:
            continue
        eh, ewhy = evaluate_condition(cond, facts)
        if eh is True:
            return {"status": "not_applicable", "missing": [], "why": ["exception_applies:" + str(e.get("text"))[:80]]}
        if eh is None:
            undecided_exception = True
            missing = sorted(set(missing) | {r.split(":", 1)[1] for r in ewhy if ":" in r})
    if held is True and scoped is True and not undecided_exception:
        return {"status": "applies", "missing": [], "why": []}
    return {"status": "conditional", "missing": missing, "why": why + cwhy}


def claim_applicability(refs: Iterable[str], by_id: Mapping, facts: Mapping, universe: Mapping) -> dict:
    """Aggregate over the rules a claim cites: applies > conditional > not_applicable (all cited rules out of reach) > not_evaluated (no structured rule cited)."""
    results = [rule_applicability(by_id[r], facts, universe) for r in refs if r in by_id and by_id[r].get("kind") == "rule"]
    if not results:
        return {"status": "not_evaluated", "missing": [], "why": ["no_structured_rule_cited"]}
    for status in ("applies", "conditional"):
        hits = [x for x in results if x["status"] == status]
        if hits:
            return {"status": status, "missing": sorted({m for x in hits for m in x["missing"]}), "why": [w for x in hits for w in x["why"]]}
    if all(x["status"] == "not_applicable" for x in results):
        return {"status": "not_applicable", "missing": [], "why": [w for x in results for w in x["why"]]}
    return {"status": "not_evaluated", "missing": [], "why": [w for x in results for w in x["why"]]}



MAX_SERVER_CLAIMS = 3
_IMPLEMENTED_STATE = re.compile(r"\b(?:currently|right now|today|in (?:the )?(?:system|code|configuration|config|files?)|implemented|configured|in production|in force in)\b", re.I)


def _decided_by(pack: Mapping, pair: set, kept: list) -> Optional[str]:
    """The decided TARGET rule that settles this disagreement, when a kept claim states it as decided: an item with TARGET authority, CONFIRMED, backed by a
    human decision, whose versions include one side of the pair, cited with basis target/decision. None otherwise (the disagreement stays open for the answer)."""
    for it in pack["items"]:
        ids = {it["item_id"]} | {m["item_id"] for m in it.get("versions", [])}
        if it.get("kind") == "rule" and it.get("authority") == "TARGET" and it.get("status") == "CONFIRMED" and it.get("decision_ids") and ids & pair:
            if any(c["basis"] in ("target", "decision") and set(c["refs"]) & ids for c in kept):
                return it["item_id"]
    return None


def _implementation_pending(pack: Mapping, item_id: str) -> bool:
    return any(it["item_id"] == item_id and it.get("implementation_status") in ("pending", "partial") for it in pack["items"])
SETTLED_RULE_STATES = frozenset({"VALIDATED", "CONFIRMED"})      # DRAFT / PENDING_HUMAN are not confirmed data: never a server claim


def _settled_uncited_gates(pack: Mapping, cited: set) -> list:
    """Approval gates of the pack no kept claim cites that are settled data: VALIDATED or CONFIRMED, no unknown field (such as an unknown unit), no open question about
    them, and on neither side of a disagreement — except the decided TARGET rule of a disagreement a person settled (`question_status` RESOLVED on the counter-evidence,
    2026-10-22): the decision made it the business rule, so it is as settled as any other; the losing side stays left to the conflict disclosure."""
    conflicting = [c for c in pack["counterevidence"] if c["kind"] == "conflicting_rule"]
    settled = {r for c in conflicting if c.get("question_status") == "RESOLVED" for r in (c["rule_id"], c["with"])}
    open_conflict = {r for c in conflicting if c.get("question_status") != "RESOLVED" for r in (c["rule_id"], c["with"])}
    questioned = {r for c in pack["counterevidence"] if c["kind"] == "open_question" for r in c.get("affected_rule_ids", [])}
    out = []
    for it in pack["items"]:
        ids = {it["item_id"]} | {m["item_id"] for m in it.get("versions", [])}
        if it.get("kind") != "rule" or not _is_gate(it) or ids & cited or ids & open_conflict or ids & questioned:
            continue
        if it.get("status") not in SETTLED_RULE_STATES or it.get("unknown_fields"):
            continue
        decided = it.get("authority") == "TARGET" and it.get("status") == "CONFIRMED" and bool(it.get("decision_ids"))
        if ids & settled and not decided:
            continue
        out.append(it)
    return out


def uncited_applicable_gates(pack: Mapping, cited: set, qf: Mapping) -> list:
    """Approval gates of the pack that reach the stated case although no kept claim cites them (omission the server can see). Strict: every fact the rule needs is stated
    in the question (no pinned default, no amount standing in for another field), the question names no amount in another currency, the rule is settled (VALIDATED or
    CONFIRMED, no unknown field such as an unknown unit, no open question about it) and is on neither side of an undecided disagreement (left to the conflict disclosure)."""
    if qf.get("foreign_amount"):
        return []
    stated = {k: v for k, v in qf["facts"].items() if k not in qf.get("assumed", {})}
    universe = scope_universe(pack["items"])
    out = [it for it in _settled_uncited_gates(pack, cited) if rule_applicability(it, stated, universe, alias_amount=False)["status"] == "applies"]
    return out[:MAX_SERVER_CLAIMS]


def uncited_conditional_gates(pack: Mapping, cited: set, qf: Mapping) -> list:
    """Approval gates nobody cited that a fact the question does not state would decide (2026-10-22, JOURNEY-V4-J3 A4: a 4,000,000 JPY purchase whose category is not
    stated may also need Finance under the CAPEX rule). Only facts the question reader knows how to read (`FACT_QUESTIONS`) are asked for: the case amount stands
    for the rule's JPY amount field here (a question about an amount the asker stated would be wrong) and a pinned default counts as read, so a rule that only
    needs the assumed process is not asked about. Only gates keyed to an amount are considered: the amount is what the asker stated, so a rule in that band that
    lacks one readable fact is about this case; a gate with no amount comparison (supplier risk alone) is not asked about on every question. Never a claim: the
    server asks, it does not answer. -> [{"rule_id", "title", "missing"}]"""
    if qf.get("foreign_amount"):
        return []
    universe = scope_universe(pack["items"])
    out = []
    for it in _settled_uncited_gates(pack, cited):
        if not _amount_leaves(it.get("condition")):
            continue                                                                       # a gate not keyed to an amount (supplier risk, process) is not about the stated case
        got = rule_applicability(it, qf["facts"], universe)
        missing = [k for k in got["missing"] if k in FACT_QUESTIONS]
        if got["status"] == "conditional" and missing and len(missing) == len(got["missing"]):
            out.append({"rule_id": it["item_id"], "title": _safe_text(it.get("title") or it.get("text"), 300) or it["item_id"], "missing": missing})
    return out[:MAX_SERVER_CLAIMS]


def _server_claim(it: Mapping) -> dict:
    basis = "target" if it.get("authority") == "TARGET" else "observed"        # the authority of the version this view serves, not of any version behind it
    title = _safe_text(it.get("title") or it.get("text"), 300) or it["item_id"]
    return {"text": f"The case in the question also meets the recorded condition of this rule: {title.rstrip('.')}.", "refs": [it["item_id"]], "basis": basis,
            "authority_labels": ["what the sources say" if basis == "observed" else "decided by a person"], "uses_evidence": False, "applicability": "applies",
            "conditional_on": [], "assumptions": [], "origin": "server_applicability"}


BASES = ("target", "observed", "decision")
MODEL_KEYS = {"status", "answer", "claims", "uncertainty", "clarifying_questions"}
RETRIEVAL_NOTE = "The search can miss relevant rules, so an empty or short list is not proof that no rule exists."
ABSTAIN_TEXT = "The business knowledge available for this question does not answer it."
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_ID = re.compile(r"\b(?:rule|decision|gap|edge|concept|role|snap|obs)-[0-9a-z_-]{3,}\b|\b[0-9a-f]{32,}\b")
_APPROVAL = re.compile(r"\b(?:approv\w*|authori[sz]\w*|sign(?:s|ed)?[- ]?off)\b", re.I)
_GATE_KINDS = {"review_required"}                    # outcome kinds that are an approval gate (the manager / Finance / Director rules); a periodic review is not


def _is_gate(it: Mapping) -> bool:
    """An approval gate: the modelled kind, or what the rule's own outcome and actor say (`business_index.rule_is_gate`; extracted rules carry no modelled kind)."""
    return (it.get("outcome") or {}).get("kind") in _GATE_KINDS or bool(it.get("gate")) or rule_is_gate(it)


def _span(ref) -> Optional[tuple]:
    """(line_start, line_end) of a line-span locator given as a dict or as its JSON text; None for any other locator."""
    loc = ref
    if isinstance(ref, str):
        try:
            loc = json.loads(ref)
        except ValueError:
            return None
    if not isinstance(loc, Mapping) or loc.get("type") != "line_span":
        return None
    try:
        return int(loc["line_start"]), int(loc["line_end"])
    except (KeyError, TypeError, ValueError):
        return None


def rule_bound_to_passages(refs: Iterable[str], obs_loc: Mapping, items: Iterable[Mapping]) -> Optional[str]:
    """The ONE structured rule bound (artifact + overlapping line span, from the stored source bindings) to the passages a claim cites, when the claim cites no rule
    itself; None when no rule or several rules are bound there (nothing is guessed). Live finding JOURNEY-V4 / V5 J3 A4a: the model cited the policy passage instead
    of the rule read from it, so the claim could not be checked against the case; the binding is stored provenance, not a match on words."""
    spans = [obs_loc[r] for r in refs if r in obs_loc]
    if not spans:
        return None
    found = set()
    for it in items:
        if it.get("kind") != "rule":
            continue
        for b in it.get("bindings") or []:
            bs = _span(b.get("evidence_ref"))
            if bs is None:
                continue
            for art, (lo, hi) in spans:
                if b.get("artifact_id") == art and bs[0] <= hi and lo <= bs[1]:
                    found.add(it["item_id"])
    return next(iter(found)) if len(found) == 1 else None
_UNSAFE = re.compile(r"<[^>]*>|\]\(|https?://|www\.|`|javascript:|\x00", re.I)


def numbers_in(value) -> set:
    out = set()
    for m in _NUM.findall(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _safe_text(text, limit) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = re.sub(r"\s+", " ", text).strip()
    if not t or len(t) > limit or _UNSAFE.search(t) or _ID.search(t):
        return None
    return t


# ---- the prompt ---------------------------------------------------------------------------------------------------------------------

def item_authorities(item: Mapping) -> set:
    auths = {item.get("authority")} | {m.get("authority") for m in item.get("versions", [])}
    if item.get("kind") == "decision":
        auths.add("TARGET")
    return {a for a in auths if a}


def readable_ids(pack: Mapping, can_read_evidence: bool) -> set:
    ids = set()
    for it in pack["items"]:
        ids.add(it["item_id"])
        ids.update(m["item_id"] for m in it.get("versions", []))
    ids.update(c["rule_id"] for c in pack["counterevidence"] if c["kind"] == "conflicting_rule")
    if can_read_evidence:
        ids.update(o["observation_id"] for o in pack["observations"].get("results", []))
    return ids


def build_prompt(frozen: Mapping, question: str, pack: Mapping, *, previous: Optional[Mapping] = None, can_read_evidence: bool = False) -> tuple:
    """-> (prompt, shown) where `shown` says what was left out because of the prompt cap. Item text is business wording; source text only with evidence rights."""
    limit = frozen["limits"]["max_prompt_chars"]
    lines, used, left_out = [], 0, 0
    for it in pack["items"]:
        auth = "/".join(sorted(item_authorities(it)))
        extra = ""
        if it["kind"] == "rule":
            extra = f" implementation={it.get('implementation_status')}"
        line = f"- id={it['item_id']} kind={it['kind']} authority={auth} status={it.get('status')}{extra}\n  text: {it['text']}"
        if used + len(line) > limit:
            left_out += 1
            continue
        lines.append(line)
        used += len(line)
    if can_read_evidence:
        for o in pack["observations"].get("results", [])[:5]:
            line = f"- id={o['observation_id']} kind=source authority=OBSERVED\n  text: {json.dumps(o['raw_value'], ensure_ascii=False)[:400]}"
            if used + len(line) <= limit:
                lines.append(line)
                used += len(line)
    disagree = [f"- {c['title']} (id={c['rule_id']}) disagrees with id={c['with']}" for c in pack["counterevidence"] if c["kind"] == "conflicting_rule"]
    open_q = [f"- {c['question']}" for c in pack["counterevidence"] if c["kind"] == "open_question"]
    prev = ""
    if previous:
        prev = f"<previous>\nquestion: {previous['question']}\nanswer: {previous['answer']}\n</previous>\n"
    prompt = (frozen["prompt_template"].replace("{question}", question).replace("{previous}", prev).replace("{knowledge}", "\n".join(lines) or "(no items)")
              .replace("{disagreements}", "\n".join(disagree) or "(none)").replace("{open_questions}", "\n".join(open_q) or "(none)"))
    return prompt, {"items_left_out": left_out}


# ---- validation ---------------------------------------------------------------------------------------------------------------------

def validate_model_object(obj, frozen: Mapping) -> dict:
    """Shape only: keys, types, enums, sizes. Raises ValueError(reason); claims are judged one by one later."""
    lim = frozen["limits"]
    if not isinstance(obj, dict) or set(obj) != MODEL_KEYS:
        raise ValueError("keys")
    if obj["status"] not in STATUSES or not isinstance(obj["answer"], str) or not isinstance(obj["uncertainty"], str):
        raise ValueError("status_or_text")
    if not isinstance(obj["claims"], list) or len(obj["claims"]) > lim["max_claims"]:
        raise ValueError("claims")
    if not isinstance(obj["clarifying_questions"], list) or len(obj["clarifying_questions"]) > lim["max_questions"] or not all(isinstance(q, str) for q in obj["clarifying_questions"]):
        raise ValueError("clarifying_questions")
    for c in obj["claims"]:
        if not isinstance(c, dict) or set(c) != {"text", "refs", "basis"} or not isinstance(c["text"], str) or not isinstance(c["refs"], list) \
                or not all(isinstance(r, str) for r in c["refs"]) or c["basis"] not in BASES:
            raise ValueError("claim_shape")
    return obj


def judge_claims(claims: Iterable[Mapping], pack: Mapping, question: str, frozen: Mapping, *, can_read_evidence: bool, facts: Optional[Mapping] = None) -> tuple:
    """-> (kept, dropped). A claim is kept only when its text is safe, it cites readable ids, its numbers come from the cited items or the question, its basis matches and,
    when the question describes a concrete case, the cited rule reaches that case (`applies` / `conditional` / `not_evaluated`; `not_applicable` is dropped)."""
    lim = frozen["limits"]
    qf = facts if facts is not None else question_facts(question, frozen.get("fact_defaults"))
    concrete = "request.net_amount_jpy" in qf["facts"]
    universe = scope_universe(pack["items"])
    readable = readable_ids(pack, can_read_evidence)
    by_id = {}
    for it in pack["items"]:
        by_id[it["item_id"]] = it
        for m in it.get("versions", []):
            by_id.setdefault(m["item_id"], dict(it, authority=m.get("authority"), versions=[]))
    for c in pack["counterevidence"]:                 # a rule that disagrees with a hit is citable too (its text and authority came from the stored rule)
        if c["kind"] == "conflicting_rule":
            by_id.setdefault(c["rule_id"], {"item_id": c["rule_id"], "kind": "rule", "text": c["text"], "authority": c["authority"], "versions": []})
    obs_text = {o["observation_id"]: json.dumps(o["raw_value"], ensure_ascii=False) for o in pack["observations"].get("results", [])} if can_read_evidence else {}
    obs_loc = {o["observation_id"]: (o.get("artifact_id"), _span(o.get("locator"))) for o in pack["observations"].get("results", []) if _span(o.get("locator"))} if can_read_evidence else {}
    # a person's decision is cited through the rule it decided: the stored provenance (TARGET rule -> decision_ids) links them, nothing is inferred
    decided_rule = {}
    for it in pack["items"]:
        if it.get("kind") == "rule" and it.get("authority") == "TARGET":
            for d in it.get("decision_ids") or []:
                decided_rule.setdefault(d, []).append(it["item_id"])
    kept, dropped = [], []
    for c in claims:
        text = _safe_text(c["text"], lim["max_claim_chars"])
        refs = list(dict.fromkeys(c["refs"]))
        from_decision = [r for d in refs for r in decided_rule.get(d, []) if r not in refs and r in readable]
        refs = refs + list(dict.fromkeys(from_decision))
        from_passage = None
        if refs and not any(r in by_id and by_id[r].get("kind") == "rule" for r in refs):
            from_passage = rule_bound_to_passages(refs, obs_loc, pack["items"])              # the rule read from the cited passage: stored binding, same lines
            if from_passage and from_passage in readable:
                refs = refs + [from_passage]
        if text is None:
            dropped.append({"reason": "unsafe_or_empty_text", "text": str(c["text"])[:80]})
        elif not refs:
            dropped.append({"reason": "no_reference", "text": text[:80]})
        elif any(r not in readable for r in refs):
            dropped.append({"reason": "reference_not_in_pack", "text": text[:80]})
        else:
            cited_text = " ".join([by_id[r]["text"] for r in refs if r in by_id] + [obs_text[r] for r in refs if r in obs_text])
            allowed = numbers_in(cited_text) | numbers_in(question) | set(map(float, range(0, 11)))
            auths = set()
            for r in refs:
                if r in by_id:
                    auths |= item_authorities(by_id[r])
                elif r in obs_text:
                    auths.add("OBSERVED")
            need = {"target": {"TARGET"}, "decision": {"TARGET"}, "observed": {"OBSERVED"}}[c["basis"]]
            if numbers_in(text) - allowed:
                dropped.append({"reason": "number_not_in_cited_items", "text": text[:80]})
            elif not (auths & need):
                dropped.append({"reason": "basis_does_not_match_cited_authority", "text": text[:80]})
            elif _APPROVAL.search(text) and not (_APPROVAL.search(cited_text) or any(_is_gate(by_id.get(r) or {}) for r in refs)):
                dropped.append({"reason": "claim_overstates_outcome", "text": text[:80]})     # "reviewed by" is not "approved by": an approval the cited items never state
            else:
                claim = {"text": text, "refs": refs, "basis": c["basis"], "authority_labels": ["what the sources say" if c["basis"] == "observed" else "decided by a person"],
                         "uses_evidence": any(r in obs_text for r in refs), "applicability": "not_checked", "conditional_on": [], "assumptions": []}
                if from_decision:
                    claim["refs_from_decision"] = list(dict.fromkeys(from_decision))      # added by the server from the decision the claim cites
                if from_passage and from_passage in refs:
                    claim["refs_from_passage"] = [from_passage]                           # added by the server from the binding of the cited passage
                if concrete:
                    app = claim_applicability(refs, by_id, qf["facts"], universe)
                    if app["status"] == "not_applicable":
                        dropped.append({"reason": "claim_not_applicable", "text": text[:80], "detail": app["why"][:4], "refs": refs, "basis": c["basis"]})
                        continue
                    claim["applicability"], claim["conditional_on"] = app["status"], app["missing"]
                    if app["status"] == "applies" and qf["assumed"]:
                        strict = claim_applicability(refs, by_id, {k: v for k, v in qf["facts"].items() if k not in qf["assumed"]}, universe)
                        if strict["status"] != "applies":                       # the default was needed to reach the case: disclosed as an assumption
                            claim["assumptions"] = sorted(f"{k}={v}" for k, v in qf["assumed"].items() if k in strict["missing"])
                kept.append(claim)
    return kept, dropped


def finalize(model: Mapping, kept: list, dropped: list, pack: Mapping, frozen: Mapping, facts: Optional[Mapping] = None) -> dict:
    """The server's status, answer text and disclosures. Never stronger than the evidence allows. For a concrete case (an amount in the question) the applicability of
    the kept claims decides: no claim reaches the case but a fact could -> NEEDS_CLARIFICATION with the facts to add; only unevaluable passages -> PARTIAL; some claims
    conditional or assumed -> PARTIAL / disclosed; a claim that does not reach the case was already dropped."""
    lim = frozen["limits"]
    status = model["status"]
    reasons = []
    qf = facts if facts is not None else question_facts(pack.get("query") or "", frozen.get("fact_defaults"))
    concrete = "request.net_amount_jpy" in qf["facts"]
    applicability_questions: list = []
    conditional = [c for c in kept if c.get("applicability") == "conditional"]
    unevaluated = [c for c in kept if c.get("applicability") == "not_evaluated"]
    applying = [c for c in kept if c.get("applicability") == "applies"]
    assumed = sorted({a for c in kept for a in c.get("assumptions", [])})
    misapplied = [d for d in dropped if d["reason"] == "claim_not_applicable"]
    cited = {r for c in kept for r in c["refs"]}
    for it in pack["items"]:
        if it["item_id"] in cited:
            cited.update(m["item_id"] for m in it.get("versions", []))
    conflicts = []
    view = pack.get("view") or (pack.get("pin") or {}).get("view") or "target"
    for c in pack["counterevidence"]:
        if c["kind"] == "conflicting_rule" and ({c["rule_id"], c["with"]} & cited):
            decided = _decided_by(pack, {c["rule_id"], c["with"]}, kept) if view == "target" else None
            conflicts.append({"rules": sorted([c["rule_id"], c["with"]]), "title": c["title"], "one_side_cited_only": not ({c["rule_id"], c["with"]} <= cited),
                              "decided_by": decided})
    questions = [q for q in (_safe_text(q, 200) for q in model["clarifying_questions"]) if q][: lim["max_questions"]]
    added = []
    if concrete and kept and (pack["items"] or pack["observations"].get("results")):
        # omission the server can see: an approval gate that reaches the stated case is added as a server claim and the answer is never complete without it
        added = [_server_claim(it) for it in uncited_applicable_gates(pack, cited, qf)][: max(0, lim["max_claims"] - len(kept))]
        kept, applying = kept + added, applying + added
    undecided_gates = uncited_conditional_gates(pack, cited | {r for c in added for r in c["refs"]}, qf) if concrete and kept and (pack["items"] or pack["observations"].get("results")) else []
    if not kept:
        if status == "NEEDS_CLARIFICATION" and questions:
            reasons.append("needs_clarification")
        else:
            status = "ABSTAINED"
            reasons.append("no_valid_claim" if model["claims"] else "no_claim")
            questions = []
    else:
        if status in ("ABSTAINED", "NEEDS_CLARIFICATION"):
            status = "PARTIAL"                       # the model abstained but cited valid evidence: keep what is supported, say it is partial
            reasons.append("model_abstained_with_supported_claims")
        # a history claim about the losing side of a decided disagreement that does not reach the case is not a gap in the answer (H-QA-06, option B)
        losing = {r for c in conflicts if c["decided_by"] for r in c["rules"]} - {m["item_id"] for it in pack["items"] if it["item_id"] in {c["decided_by"] for c in conflicts} for m in [it, *it.get("versions", [])]}
        material = [d for d in dropped if not (d["reason"] == "claim_not_applicable" and d.get("basis") == "observed" and d.get("refs") and set(d["refs"]) <= losing)]
        if material:
            status = "PARTIAL" if status == "ANSWERED" else status
            reasons.append("some_claims_dropped")
        elif dropped:
            reasons.append("history_claims_not_applicable")
        if conflicts:
            # owner decision H-QA-06 (option B, 2026-10-18): in the accepted-target view a disagreement a valid decision settled, answered with the decided rule,
            # is disclosed but does not by itself make the answer partial; an undecided one, the observed view, or an answer that does not state the decision does
            open_ = [c for c in conflicts if not c["decided_by"]]
            if open_:
                status = "PARTIAL" if status == "ANSWERED" else status
                reasons.append("sources_disagree")
            else:
                reasons.append("sources_disagree_decided")
                pending_decided = [c["decided_by"] for c in conflicts if _implementation_pending(pack, c["decided_by"])]
                if pending_decided and _IMPLEMENTED_STATE.search(pack.get("query") or ""):
                    status = "PARTIAL" if status == "ANSWERED" else status         # the question asks what is in force in the files/code, which still show the old rule
                    reasons.append("implementation_pending")
        if undecided_gates:
            # a gate the model left out that a fact the asker can add would decide: the answer is not complete and asks for that fact (never a claim, never a guess)
            status = "PARTIAL" if status == "ANSWERED" else status
            reasons.append("conditional_rule_not_cited")
            for key in sorted({m for g in undecided_gates for m in g["missing"]}):
                if FACT_QUESTIONS[key] not in applicability_questions:
                    applicability_questions.append(FACT_QUESTIONS[key])
        if pack.get("sufficiency") == "insufficient":
            status = "PARTIAL" if status == "ANSWERED" else status
            reasons.append("evidence_judged_insufficient")
        if added:
            status = "PARTIAL" if status == "ANSWERED" else status
            reasons.append("applicable_rule_not_cited")
        if not pack["items"] and not pack["observations"].get("results"):
            status = "ABSTAINED"
            reasons.append("empty_pack")
            kept, questions = [], []
        if concrete and kept:
            if not applying and conditional:
                status = "NEEDS_CLARIFICATION"
                reasons.append("applicability_undecided")
                for key in sorted({m for c in conditional for m in c["conditional_on"]}):
                    applicability_questions.append(FACT_QUESTIONS.get(key, f"What is the {key.replace('.', ' ').replace('_', ' ')} of this case?"))
            elif not applying and unevaluated:
                status = "PARTIAL"
                reasons.append("applicability_not_evaluated")
            elif conditional or unevaluated:
                status = "PARTIAL" if status == "ANSWERED" else status
                reasons.append("some_claims_conditional")
                for key in sorted({m for c in conditional for m in c["conditional_on"]}):   # the answer names what is missing to settle the rest (owner decision Q10, option B)
                    applicability_questions.append(FACT_QUESTIONS.get(key, f"What is the {key.replace('.', ' ').replace('_', ' ')} of this case?"))
            if assumed:
                reasons.append("assumed_defaults")
                # owner decision G2-QA-10 (option B, 2026-10-18): a pinned default is a technical reading aid, not a fact of the case; when a claim needed it to reach the
                # case, the missing fact can change the result, so the answer is at most PARTIAL and asks for it. A fact the question states (including a hypothetical the
                # asker sets, "if it were a standard purchase") is not assumed and is unaffected; no authoritative business default is declared in the knowledge model yet.
                status = "PARTIAL" if status == "ANSWERED" else status
                reasons.append("assumed_default_needs_fact")
                for key in sorted({a.split("=")[0] for a in assumed}):
                    q = FACT_QUESTIONS.get(key, f"What is the {key.replace('.', ' ').replace('_', ' ')} of this case?")
                    if q not in applicability_questions:
                        applicability_questions.append(q)
    if misapplied:
        reasons.append("claims_not_applicable_dropped")
    if qf.get("foreign_amount") and not qf.get("amounts") and status in ("ANSWERED", "PARTIAL"):
        # contract (units): an amount in another currency is never converted to JPY; without a JPY amount no case can be decided (live finding 2026-10-17)
        status = "NEEDS_CLARIFICATION"
        reasons.append("amount_not_in_jpy")
        applicability_questions.insert(0, FOREIGN_QUESTION)
    if applicability_questions:
        questions = (applicability_questions + [q for q in questions if q not in applicability_questions])[: lim["max_questions"]]
    uncertainty = _safe_text(model["uncertainty"], lim["max_uncertainty_chars"]) or ""
    if assumed:
        words = [FACT_ASSUMPTION_WORDS.get(a.split("=")[0], {}).get(a.split("=")[1], a) for a in assumed]
        uncertainty = (uncertainty + " " if uncertainty else "") + "Assumed: " + ", ".join(words) + "."
    if status == "ABSTAINED":
        answer = ABSTAIN_TEXT + (" " + uncertainty if uncertainty else "")
    else:
        proposed = _safe_text(model["answer"], lim["max_answer_chars"])
        cited_numbers = numbers_in(" ".join(c["text"] for c in kept))
        ok = proposed is not None and not (numbers_in(proposed) - (cited_numbers | numbers_in(pack["query"]) | set(map(float, range(0, 11)))))
        if concrete and (conditional or unevaluated or misapplied or assumed):
            ok = False                                            # the model's free text may assert what the rules do not reach: the claims speak, each with its condition
        answer = proposed if ok and status != "NEEDS_CLARIFICATION" else (" ".join(_claim_sentence(c) for c in kept) if kept else "More detail is needed to answer.")
        if answer == proposed and added:
            answer = (answer + " " + " ".join(c["text"] for c in added)).strip()        # the model's text did not name these rules: the server's sentences follow it
        if status != "NEEDS_CLARIFICATION" and undecided_gates:
            answer = (answer + " " + " ".join(f"Whether this rule also applies depends on a fact the question does not state: {g['title'].rstrip('.')}." for g in undecided_gates)).strip()
        if not ok and kept:
            reasons.append("answer_text_replaced_by_claims")
    pending = sorted({it["item_id"] for it in pack["items"] if it["item_id"] in cited and it.get("authority") == "TARGET" and it.get("implementation_status") in ("pending", "partial")})
    return {"status": status, "answer": answer, "claims": kept, "dropped": dropped, "conflicts": conflicts, "uncertainty": uncertainty, "clarifying_questions": questions,
            "reasons": reasons, "retrieval_note": RETRIEVAL_NOTE, "case_facts": {"facts": qf["facts"], "assumed": qf["assumed"], "concrete": concrete},
            "undecided_gates": undecided_gates,
            "implementation_note": "The files and code still show the old rule for: " + ", ".join(pending) + "." if pending else None}


def _claim_sentence(c: Mapping) -> str:
    """A kept claim as the reader should hear it: conditional claims carry their open condition, assumed ones their assumption."""
    text = c["text"]
    if c.get("applicability") == "conditional" and c.get("conditional_on"):
        asks = "; ".join(FACT_QUESTIONS.get(k, k).rstrip("?").lower() for k in c["conditional_on"])
        return f"Only if it is decided whether {asks}: {text}" if asks else text
    if c.get("applicability") == "not_evaluated":
        return f"{text} (whether this applies to the case in the question could not be checked against a structured rule.)"
    if c.get("assumptions"):
        words = [FACT_ASSUMPTION_WORDS.get(a.split("=")[0], {}).get(a.split("=")[1], a) for a in c["assumptions"]]
        return f"{text} (assuming {', '.join(words)}.)"
    return text


# ---- reading an answer --------------------------------------------------------------------------------------------------------------

WITHHELD = "[withheld: this part rests on source text that needs the evidence.read permission]"


def render(result: Mapping, *, mode: str, can_read_evidence: bool, titles: Mapping, newer_knowledge: bool) -> dict:
    """The answer as one reader may see it. Business: plain claims and titles. Technical: ids, pin, validation; source references only with evidence rights.
    A claim that rests on source text is withheld from a reader without evidence rights, and so is the model's free-text answer if any claim was withheld."""
    claims, withheld = [], 0
    for c in result["claims"]:
        if c["uses_evidence"] and not can_read_evidence:
            withheld += 1
            claims.append({"text": WITHHELD, "basis": c["basis"], "authority_labels": c["authority_labels"], "sources": [], "origin": c.get("origin", "model")})
        else:
            claims.append({"text": c["text"], "basis": c["basis"], "authority_labels": c["authority_labels"], "sources": [titles.get(r, "a source passage") for r in c["refs"]],
                           "applicability": c.get("applicability", "not_checked"), "conditional_on": list(c.get("conditional_on", [])), "assumptions": list(c.get("assumptions", [])),
                           "origin": c.get("origin", "model")})                    # model = written by the model and validated; server_applicability = derived by the server's rule check
            if mode == "technical":
                claims[-1]["refs"] = list(c["refs"])
                if c.get("refs_from_decision"):
                    claims[-1]["refs_from_decision"] = list(c["refs_from_decision"])
    answer = result["answer"] if not withheld else ("Part of this answer rests on source text that needs the evidence.read permission. " + " ".join(c["text"] for c in claims if c["text"] != WITHHELD)).strip()
    out = {"status": result["status"], "answer": answer, "claims": claims, "conflicts": [{"titles": [titles.get(r, r) for r in c["rules"]], "one_side_cited_only": c["one_side_cited_only"], "decided": bool(c.get("decided_by")),
                          "decided_title": titles.get(c["decided_by"]) if c.get("decided_by") else None} for c in result["conflicts"]],
           "uncertainty": result["uncertainty"], "clarifying_questions": result["clarifying_questions"], "retrieval_note": result["retrieval_note"],
           "implementation_note": result["implementation_note"], "view": result["view"], "snapshot_state": result["pack"]["pin"].get("snapshot_state"),
           "newer_knowledge_exists": newer_knowledge, "withheld_claims": withheld, "synthetic_note": None,
           "retrieval_state": result.get("retrieval_state") or {"status": "unknown", "reason": None}}
    if mode == "technical":
        out["technical"] = {"pin": result["pack"]["pin"], "reasons": result["reasons"], "dropped_claims": result["dropped"], "case_facts": result.get("case_facts"), "retrieval": result["pack"]["retrieval"], "semantic": result["pack"]["semantic"],
                            "sufficiency": result["pack"]["sufficiency"], "coverage": result["pack"]["coverage"], "index": result["pack"]["index"],
                            "pack_items": result["pack"]["items"] if can_read_evidence else [{k: v for k, v in i.items() if k not in ("evidence_refs",)} for i in result["pack"]["items"]],
                            "config_sha256": result["config_sha256"], "model": result.get("model"), "evidence_access_when_asked": result["evidence_access"]}
    return out
