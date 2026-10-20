# -*- coding: utf-8 -*-
"""Request assistant: the safe context pack of ONE request for ONE reader (RQA-01, contract REQUEST_TASKS_AND_ASSISTANT section 4). Pure: no I/O, no model.

The adapter collects facts ONLY through read services executed as the asking person (the task view, the family's own read shaping); this module turns them into bounded,
citable facts with stable ids, a coverage record and a manifest hash. Facts are data for the model, never instructions; an existing model output stays labelled as an
interpretation, never as a source or an authority. Nothing here decides who may act: obligations and allowed actions come from the deterministic task view.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

from core.change_intelligence.v2.serialize import canonical_hash

CONTEXT_VERSION = "request-assistant-context/2"                 # AIG-02: tiered, stable selection with honest overflow (contract section 6)
ANSWER_SCHEMA = "request-assistant-answer/3"                    # AIG-03 v5: + `cause` blocks and server-computed blocker coverage (answer/2: typed authority, AIG-01)
LOCALE = "en"                                                   # the only supported answer locale
TIERS = ("mandatory", "support", "history")
_KIND_RANK = {k: i for i, k in enumerate(("state", "request", "obligation", "waiting", "blocker", "action", "step", "axis", "rule_change", "open_question", "impact",
                                         "decision", "review_note", "interpretation"))}
MAX_PACK_CHARS = 12000                                          # the facts' text budget of one pack (the prompt has its own, larger ceiling)


class ContextOverflow(ValueError):
    """The facts that must be complete (mandatory tier) do not fit the pack, or one of them would be cut. Refused before any provider call; nothing is dropped silently."""
    def __init__(self, reason: str, detail: Mapping):
        super().__init__(reason)
        self.reason, self.detail = reason, dict(detail)
KINDS = ("state", "obligation", "waiting", "blocker", "action", "step", "axis", "request", "review_note", "rule_change", "open_question", "decision", "impact", "interpretation")
MAX_FACTS = 80
MAX_FACT_CHARS = 400
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def fact(kind: str, text, source: str, data: Mapping | None = None, display=None, *, tier: str = "support", key: str | None = None) -> dict | None:
    """`data` = typed values of the fact (who holds a step, whether it is the reader's, whether the reader may act); the model sees only `text`, the checks use `data`."""
    if kind not in KINDS or not isinstance(text, str):
        return None
    t = " ".join(_CTRL.sub(" ", text).split())
    if not t:
        return None
    return {"kind": kind, "text": t[:MAX_FACT_CHARS], "source": source, **({"truncated": True} if len(t) > MAX_FACT_CHARS else {}), **({"data": dict(data)} if data else {}),
            **({"display": " ".join(display.split())[:MAX_FACT_CHARS]} if display else {}), "tier": tier if tier in TIERS else "support", "key": str(key or f"{kind}:{t[:120]}")}


def pack(request_ref: Mapping, as_of: str, facts: Iterable, *, auth: Mapping, upstream: Mapping | None = None) -> dict:
    """Bounded, numbered facts (`F1`..) + coverage + manifest hash (AIG-02). Mandatory facts are all kept and must be complete, else `ContextOverflow` (before any provider
    call). Support, then history facts fill the remaining room in a stable order: tier, kind, then the fact's source key (history: the order the caller gave, newest
    first). Never database enumeration or read time. Ids are assigned on copies. `auth` is the reader's authorization fingerprint; `upstream` records caps applied
    before the pack (e.g. the impact list limit). The hash binds facts (with tier and key), coverage, version and authorization."""
    offered = [dict(f) for f in facts if f]
    tiers = {t: [f for f in offered if f.get("tier", "support") == t] for t in TIERS}
    order = lambda f: (_KIND_RANK.get(f["kind"], 99), f.get("key") or "", f["text"])         # noqa: E731
    mandatory = sorted(tiers["mandatory"], key=order)
    cut = [f["key"] for f in mandatory if f.get("truncated")]
    if cut:
        raise ContextOverflow("mandatory_fact_too_long", {"facts": cut[:5]})
    if len(mandatory) > MAX_FACTS or sum(len(f["text"]) for f in mandatory) > MAX_PACK_CHARS:
        raise ContextOverflow("mandatory_facts_exceed_bounds", {"mandatory": len(mandatory), "max_facts": MAX_FACTS})
    shown, chars = list(mandatory), sum(len(f["text"]) for f in mandatory)
    omitted = {t: 0 for t in TIERS}
    for t in ("support", "history"):
        pool = sorted(tiers[t], key=order) if t == "support" else tiers[t]                   # history keeps the caller's newest-first order (decision time, then id)
        for f in pool:
            if len(shown) < MAX_FACTS and chars + len(f["text"]) <= MAX_PACK_CHARS:
                shown.append(f)
                chars += len(f["text"])
            else:
                omitted[t] += 1
    seen, unique = set(), []
    for f in shown:                                                                         # one fact per source identity
        if f["key"] in seen:
            continue
        seen.add(f["key"])
        unique.append(f)
    for i, f in enumerate(unique, 1):
        f["id"] = f"F{i}"
    coverage = {"facts_total": len(offered), "facts_shown": len(unique), "truncated": any(omitted.values()) or any(f.get("truncated") for f in unique),
                "tiers": {t: {"offered": len(tiers[t]), "selected": sum(1 for f in unique if f.get("tier") == t), "omitted": omitted[t]} for t in TIERS},
                "upstream": dict(upstream or {})}
    body = {"version": CONTEXT_VERSION, "request_ref": dict(request_ref), "facts": unique, "coverage": coverage}
    return {**body, "as_of": as_of, "auth": dict(auth), "manifest_hash": canonical_hash({**body, "auth": dict(auth)})}


_STEP_WORD = {"done": "done", "current": "now", "waiting": "waiting", "blocked": "blocked", "failed": "failed", "not_applicable": "not needed", "partial": "partly done"}
_STATE_WORD = {"current": "now", "waiting": "waiting", "blocked": "blocked", "future": "later", "done": "done", "failed": "failed"}


def task_facts(tv: Mapping) -> list:
    """Facts from the reader's own task view (already computed under the reader's rights)."""
    M = {"tier": "mandatory"}
    out = [fact("state", f"Current stage: {tv['stage']['label']}.", "task_view", key="state", **M)]
    if str((tv.get("request_ref") or {}).get("id", "")).startswith("chat-"):
        out.append(fact("request", "The request is still a creation conversation; its text is not part of this context.", "task_view", key="request:title", **M))   # v1: creation chat never in the pack
    elif tv.get("title"):
        out.append(fact("request", f"The request: {tv['title']}", "task_view", key="request:title", **M))
    for o in tv.get("obligations") or []:
        who = "you" if o.get("obligation_me") else o.get("holder_label") or o.get("holder")
        req = "required" if o.get("required") else "optional"
        extra = f" (you may do it: {'yes' if o.get('eligible_me') else 'no'}{'; ' + o['reason'] if o.get('reason') else ''})"
        if o.get("obligation_me"):
            plain = "you are asked to do this now" if o.get("state") == "current" else f"yours, {_STATE_WORD.get(o.get('state'), o.get('state'))}"
        else:
            plain = f"held by {o.get('holder_label') or 'the role pool'}, {_STATE_WORD.get(o.get('state'), o.get('state'))}" + ("; you may also do it, but you are not asked to" if o.get("eligible_me") else "")
        out.append(fact("obligation", f"{o['label']} - {req}, held by {who}, state {o.get('state')}{extra}.", "task_view", display=f"{o['label']}{'' if o.get('required') else ' (optional)'}: {plain}.", key=f"obligation:{o.get('code')}", data=
                        {"holder": o.get("holder"), "holder_label": o.get("holder_label") or "", "you": bool(o.get("obligation_me")), "may": bool(o.get("eligible_me")),
                         "assigned": bool(o.get("assigned_me")), "required": bool(o.get("required")), "state": o.get("state"), "label": o["label"]}, **M))
    for w in tv.get("waiting_on") or []:
        out.append(fact("waiting", f"Waiting on {w.get('holder_label') or w.get('holder')}.", "task_view", {"holder": w.get("holder"), "holder_label": w.get("holder_label") or ""},
                        display=f"Waiting on {w.get('holder_label') or w.get('holder')}.", key=f"waiting:{w.get('code')}:{w.get('holder')}", **M))
    for b in tv.get("blockers") or []:
        out.append(fact("blocker", f"Blocker: {b.get('text')}", "task_view", {"code": b.get("code")}, key=f"blocker:{b.get('code')}", **M))
    for a in tv.get("permitted_actions") or []:
        state = ("allowed for you and it is the step you are asked to do now" if a.get("current") else "allowed for you, but it is not a step you are asked to do now")             if a.get("allowed") else "not allowed for you"
        out.append(fact("action", f"Action '{a.get('label')}': {state}{' - ' + a['reason'] if a.get('reason') else ''}.", "task_view",
                        {"action": a.get("action"), "allowed": bool(a.get("allowed")), "current": bool(a.get("current")), "label": a.get("label")}, key=f"action:{a.get('action')}", **M,
                        display=f"{a.get('label')}: {'your step now' if a.get('current') else 'you may do this' if a.get('allowed') else 'not available to you'}{' (' + a['reason'] + ')' if a.get('reason') and not a.get('allowed') else ''}."))
    steps = [f"{s['label']}: {s['state']}" for s in tv.get("stages") or [] if s.get("state") not in ("future",)]
    if steps:
        plain = [f"{x['label']}: {_STEP_WORD.get(x['state'], x['state'])}" for x in tv.get("stages") or [] if x.get("state") not in ("future",)]
        out.append(fact("step", "Steps so far: " + "; ".join(steps) + ".", "task_view", display="Steps so far: " + "; ".join(plain) + ".", key="steps", **M))
    for k, v in (tv.get("completion_axes") or {}).items():
        if isinstance(v, (str, bool)) and k != "statement":
            out.append(fact("axis", f"Completion axis {k}: {v}.", "task_view", key=f"axis:{k}", **M))
    return out


# ---- answering (RQA-03): prompt and the server's checks of the model's answer ----------------------------------------------------------------------------------
MODEL_KEYS = frozenset({"outcome", "answer_blocks", "limitations", "authority_assertions"})
REQUIRED_MODEL_KEYS = frozenset({"outcome", "answer_blocks", "limitations"})
ASSERTION_KEYS = frozenset({"subject", "fact_ref", "relation", "polarity", "scope"})
RELATIONS = ("holds", "must", "may", "waiting_on", "blocked")
OUTCOMES = ("answered", "partial", "insufficient_context", "out_of_scope")
BLOCK_KINDS = ("fact", "interpretation", "suggestion", "cause")
# a `cause` block says WHY the request is where it is, about the request (never who may / must act): it must cite a fact with business substance
CAUSE_FACTS = frozenset({"blocker", "decision", "rule_change", "impact", "request", "waiting", "review_note"})
MAX_UNEXPLAINED_SHOWN = 5
_NOT_PLAIN = re.compile(r"[<>`|]|https?://|www\.|\]\(|[\x00-\x08\x0b-\x1f\x7f]", re.I)
_NUM = re.compile(r"(?<![A-Za-z0-9_])\d+(?:[.,]\d+)*")
_NEGATED = re.compile(r"(?i)\b(not|cannot|can't|can not|no|unable|never|isn't|aren't|doesn't|don't|won't)\b")
_HOLDS = re.compile(r"(?i)\b(you hold|you are (required|asked|responsible|expected)|your (task|turn|obligation|step|job) (is|to)|it is (up to|on) you|held by you|you (must|need to) (decide|approve|confirm|commit|apply|answer|clarify))\b"
                    # possessive / assigning phrasings (class of the next-step live N02a, 2026-10-20): "the next step is yours", "it's your turn", "waiting on you",
                    # "you are the one / next", "you should ask for the draft"
                    r"|\b(is|are) yours\b|\byours to (do|take|decide)\b|\bit'?s your (turn|step|task|move|job)\b|\bwaiting (on|for) you\b|\byou are (the one|next|the holder)\b"
                    r"|\byou (should|need to|have to|must) (now )?(ask|request|provide|generate|run|prepare|state|respond|revise|review|set|draft|take|do)\b")
_NOT_HOLDS = re.compile(r"(?i)\b(you (do not|don't|no longer) hold|not (held by|your) |isn't yours|is not yours|not up to you|you are not (required|asked|responsible|expected)"
                        r"|not waiting (on|for) you|you (should not|need not|do not need to|don't need to|don't have to|do not have to))")
# ---- authority claims (evaluator / validator v2, 2026-10-20; class of A2 live P5) -----------------------------------------------------------------------------
# A block that says who holds a step, who is waited on, or whether the reader may act must agree with the TYPED values of the facts it cites (obligation: holder, you,
# may; waiting: holder; action: allowed). A valid reference alone is not enough. Refused blocks are listed; if nothing survives, the answer is `refused`, never success.
_ABLE = re.compile(r"(?i)\byou (can|may|are (allowed|able|permitted) to|could)\s+(now\s+)?(proceed (by|to)\s+)?(approv|decid|confirm|commit|apply|clarif|answer|review|reject|accept)")
_UNABLE = re.compile(r"(?i)\byou (cannot|can't|can not|may not|are not (allowed|able|permitted) to|could not)\s+(approv|decid|confirm|commit|apply|clarif|answer|review|reject|accept)|\byour role cannot\b")
_REQUIRED = re.compile(r"(?i)\byou (are|were) (required|obliged)\b|\byou (must|have to)\b")
CHECKS_VERSION = "request-assistant-checks/6"                   # checks/6: a contradicted assertion is answered by the server's corrected sentence
COMPATIBLE_CHECKS = frozenset({"request-assistant-checks/4", "request-assistant-checks/5", CHECKS_VERSION})   # authority already typed and server-rendered: shown as stored, no legacy filtering
_ROLE_WORDS = {"requester": ("requester", "person who asked"), "approver": ("approver",), "reviewer": ("reviewer",), "knowledge owner": ("knowledge owner",),
               "administrator": ("administrator", "admin"), "system": ("system", "the ai")}
_THIRD = re.compile(r"(?i)\b(?:the |an |a )?(requester|approver|reviewer|knowledge owner|administrator|admin|system)s?\b[^.;]{0,40}?\b(holds?|must|needs? to|has to|is (?:asked|responsible|expected|required) to|is the one|is to)\b")
_WAITING = re.compile(r"(?i)\bwaiting (?:on|for) (?:the |an |a )?(requester|approver|reviewer|knowledge owner|administrator|admin|system)\b")


def _role_of(word: str) -> str:
    w = word.lower()
    return next(k for k, v in _ROLE_WORDS.items() if any(w.startswith(x.split()[0]) for x in v) or w == k)


def _holder_names(f: Mapping) -> set:
    d = f.get("data") or {}
    label = re.sub(r"\(not [^)]*\)", " ", f"{d.get('holder_label', '')} {d.get('holder', '')}".lower())   # "an approver (not the person who asked)" names no requester
    out = {k for k, words in _ROLE_WORDS.items() if any(x in label for x in words)}
    if d.get("holder") in ("role_pool",) and "approver" in label:
        out.add("approver")
    return out


def authority_problem(text: str, cited: list, kind: str) -> str | None:
    obligations = [f for f in cited if f["kind"] == "obligation"]
    actions = [f for f in cited if f["kind"] == "action"]
    holders_cited = set().union(*[_holder_names(f) for f in cited if f["kind"] in ("obligation", "waiting")] or [set()])
    typed = [f for f in obligations + actions if f.get("data") is not None]
    if _HOLDS.search(text) and not _NOT_HOLDS.search(text):
        if typed:                                                                           # supported by an obligation that is the reader's, or an action that is their CURRENT step
            if not (any((f.get("data") or {}).get("you") for f in obligations) or any((f.get("data") or {}).get("current") for f in actions)):
                return "contradicts_cited_obligation"
        elif not any("held by you" in f["text"] for f in obligations):
            return "contradicts_cited_obligation"
    mine = [f for f in obligations if (f.get("data") or {}).get("you")]
    if _REQUIRED.search(text) and mine and all((f.get("data") or {}).get("required") is False for f in mine):
        return "contradicts_cited_obligation"                                               # an optional step presented as required
    if _UNABLE.search(text):
        if any((f.get("data") or {}).get("allowed") for f in actions) or any((f.get("data") or {}).get("you") and (f.get("data") or {}).get("may") for f in obligations):
            if not any((f.get("data") or {}).get("allowed") is False for f in actions):
                return "contradicts_cited_permission"
    elif _ABLE.search(text):
        if typed:
            if not (any((f.get("data") or {}).get("allowed") for f in actions) or any((f.get("data") or {}).get("may") for f in obligations)):
                return "contradicts_cited_permission"
        elif any(("not allowed for you" in f["text"] or "you may do it: no" in f["text"]) for f in obligations + actions):
            return "contradicts_cited_permission"
    if kind == "suggestion" and not _NEGATED.search(text) and any(("not allowed for you" in f["text"] or "you may do it: no" in f["text"]) for f in obligations + actions) \
            and not any((f.get("data") or {}).get("allowed") or (f.get("data") or {}).get("may") for f in obligations + actions):
        return "contradicts_cited_permission"
    for m in list(_THIRD.finditer(text)) + list(_WAITING.finditer(text)):
        role = _role_of(m.group(1))
        if role not in holders_cited:
            return "holder_not_grounded"
    return None


_ACTS = re.compile(r"(?i)\b(i|we)\s+(have\s+|has\s+|just\s+)?(approved|confirmed|applied|accepted|rejected|committed|recorded|submitted)\b")


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or not REQUIRED_MODEL_KEYS <= set(obj) <= MODEL_KEYS or obj["outcome"] not in OUTCOMES:
        raise ValueError("keys_or_outcome")
    obj = {**obj, "authority_assertions": obj.get("authority_assertions", [])}
    asr = obj["authority_assertions"]
    if not isinstance(asr, list) or len(asr) > int(limits.get("max_assertions", 4)):
        raise ValueError("authority_assertions")
    for a in asr:
        if not isinstance(a, dict) or set(a) != ASSERTION_KEYS or a["subject"] not in ("self", "holder") or a["relation"] not in RELATIONS \
                or not isinstance(a["polarity"], bool) or a["scope"] not in ("current", "later") or not isinstance(a["fact_ref"], str):
            raise ValueError("assertion")
    if not isinstance(obj["answer_blocks"], list) or len(obj["answer_blocks"]) > int(limits["max_blocks"]):
        raise ValueError("answer_blocks")
    for b in obj["answer_blocks"]:
        if not isinstance(b, dict) or set(b) != {"text", "fact_refs", "kind"} or not isinstance(b["text"], str) or b["kind"] not in BLOCK_KINDS \
                or not isinstance(b["fact_refs"], list) or not all(isinstance(r, str) for r in b["fact_refs"]):
            raise ValueError("block")
    if not isinstance(obj["limitations"], list) or not all(isinstance(x, str) for x in obj["limitations"]):
        raise ValueError("limitations")
    return obj


# AIG-01: authority is stated only through typed assertions; a free block that claims who holds, must, may or is waited on is refused (defence: the lexical checks below)
_FREE_AUTHORITY = re.compile(r"(?i)\b(you|your role)\s+(can|cannot|can't|may|could|must|should|need to|have to|hold|are (?:not )?(?:allowed|able|permitted|asked|required|expected|responsible))\b"
                             r"|\b(is|are) (?:not )?yours\b|\bit'?s your (?:turn|step|task|move|job)\b|\bheld by you\b|\bwaiting (?:on|for) you\b")
_BARE_YES_NO = re.compile(r"(?i)^\s*(yes|no)\b")


def free_text_authority(text: str) -> str | None:
    if _BARE_YES_NO.search(text):
        return "unbound_affirmation"
    if _FREE_AUTHORITY.search(text) or _HOLDS.search(text) or _NOT_HOLDS.search(text) or _ABLE.search(text) or _UNABLE.search(text) or _THIRD.search(text) or _WAITING.search(text):
        return "authority_in_free_text"
    return None


def _scope_ok(state, scope) -> bool:
    return state == "current" if scope == "current" else state == "future"


def assertion_truth(a: Mapping, f: Mapping) -> bool | None:
    """True / False when the assertion can be decided from the cited fact's typed values; None when the combination is not supported (rejected)."""
    d, rel, subj, scope = f.get("data") or {}, a["relation"], a["subject"], a["scope"]
    if f["kind"] == "obligation":
        if rel == "holds" and subj == "self":
            return bool(d.get("you")) and _scope_ok(d.get("state"), scope)
        if rel == "holds" and subj == "holder":
            return _scope_ok(d.get("state"), scope) if a["polarity"] else None                 # a holder always holds its own step: its negation is not a claim
        if rel == "must" and subj == "self":
            return bool(d.get("you")) and bool(d.get("required")) and _scope_ok(d.get("state"), scope)
        if rel == "may" and subj == "self":
            return bool(d.get("may")) and _scope_ok(d.get("state"), scope)
        if rel == "blocked" and scope == "current":
            return d.get("state") == "blocked"
        return None
    if f["kind"] == "action" and subj == "self" and scope == "current":
        if rel == "may":
            return bool(d.get("allowed"))
        if rel in ("holds", "must"):
            return bool(d.get("current"))
        return None
    if f["kind"] == "waiting" and rel == "waiting_on" and subj == "holder" and scope == "current" and a["polarity"]:
        return True
    if f["kind"] == "blocker" and rel == "blocked" and scope == "current" and a["polarity"]:
        return True
    return None


def render_assertion(a: Mapping, f: Mapping) -> str:
    """The decisive authority sentence, in controlled English, from a KEPT assertion and the canonical fact (the model cannot author it)."""
    d, rel, pos, later = f.get("data") or {}, a["relation"], a["polarity"], a["scope"] == "later"
    label, holder = d.get("label") or "this step", d.get("holder_label") or "its holder"
    if f["kind"] == "action":
        if rel == "may":
            return f"You may do this: {label}." if pos else f"You may not do this: {label}."
        return f"This is the step you are asked to do now: {label}." if pos else f"This is not a step you are asked to do now: {label}."
    if f["kind"] == "waiting":
        return f"The request is waiting on {holder}."
    if f["kind"] == "blocker":
        return "The request is blocked: " + f["text"].removeprefix("Blocker: ").rstrip(".") + "."
    if rel == "holds" and a["subject"] == "holder":
        return f"{holder[:1].upper() + holder[1:]} holds this step{' (later)' if later else ''}: {label}."
    if rel == "holds":
        return (f"This later step will be yours: {label}." if later else f"You hold the current step: {label}.") if pos else f"This step is not yours: {label} (held by {holder})."
    if rel == "must":
        return (f"You will be asked to do this later: {label}." if later else f"You are asked to do this now: {label}.") if pos else f"You are not asked to do this: {label}."
    if rel == "may":
        if not pos:
            return f"You may not do this: {label}."
        return f"You may do this: {label}." if d.get("you") else f"You may do this, but you are not asked to: {label}."
    return f"This step is blocked: {label}." if pos else f"This step is not blocked: {label}."


def corrected_assertion(a: Mapping, f: Mapping) -> dict | None:
    """What the cited fact decides about the same question when a CONTRADICTED assertion is refused, as a typed assertion the server renders; None when the fact
    does not decide it. Each candidate is re-checked by `assertion_truth`, so nothing is shown that the fact does not make true:
    - a step of someone else that the model gave to the reader -> "this step is not yours" (true whatever its stage);
    - same stage as asserted -> the opposite assertion;
    - a current claim about a blocked step -> "this step is blocked"; about a later step -> the same claim for later."""
    d, rel, subj, scope = f.get("data") or {}, a["relation"], a["subject"], a["scope"]
    cands = []
    if f["kind"] == "obligation":
        state = d.get("state")
        if subj == "self" and rel in ("holds", "must") and a["polarity"] and not d.get("you"):
            cands.append(dict(a, relation="holds", polarity=False))
        elif _scope_ok(state, scope):
            cands.append(dict(a, polarity=not a["polarity"]))
        elif scope == "current" and state == "blocked":
            cands.append(dict(a, subject="self", relation="blocked", polarity=True))
        elif scope == "current" and state == "future":
            cands.append(dict(a, scope="later"))
    elif f["kind"] == "action":
        cands.append(dict(a, polarity=not a["polarity"]))
    for c in cands:
        if assertion_truth(c, f) is c["polarity"]:
            return c
    return None


def check_assertions(assertions, facts: Mapping) -> tuple[list, list]:
    """Kept assertions are rendered by the server. A contradicted one stays refused (its reason is recorded); when the right answer to the same question is decided by
    the cited fact, the server's corrected sentence is shown instead, marked `corrected` (authority stays deterministic; the model chose only what was asked about)."""
    kept, rejected, seen = [], [], set()
    for i, a in enumerate(assertions or []):
        f = facts.get(a["fact_ref"])
        if f is None or f["kind"] not in ("obligation", "action", "waiting", "blocker"):
            rejected.append({"index": i, "kind": "authority", "reason": "assertion_unknown_fact"})
            continue
        truth = assertion_truth(a, f)
        if truth is None:
            rejected.append({"index": i, "kind": "authority", "reason": "assertion_not_supported"})
        elif truth != a["polarity"]:
            c = corrected_assertion(a, f)
            rejected.append({"index": i, "kind": "authority", "reason": "assertion_contradicts_fact", "corrected": c is not None})
            if c is not None and _akey(c) not in seen:
                seen.add(_akey(c))
                kept.append({"text": render_assertion(c, f), "fact_refs": [a["fact_ref"]], "kind": "authority", "assertion": c, "corrected": True,
                             "origin": "server_correction", "corrects": i})                  # the refused model assertion this sentence answers
        elif _akey(a) not in seen:
            seen.add(_akey(a))
            kept.append({"text": render_assertion(a, f), "fact_refs": [a["fact_ref"]], "kind": "authority", "assertion": dict(a)})
    return kept, rejected


def authority_accounting(model_assertions, rejected: list, kept: list) -> dict:
    """checks/6: the model's authority work and the server's corrections, counted apart. A contradicted assertion is a model error even when the server shows the
    right sentence for it; `shown_*` describe what the reader sees. Evaluations score the model from the `model_*` counts only."""
    refused = [r for r in rejected if r.get("kind") == "authority"]
    reasons = [r["reason"] for r in refused]
    shown = [b for b in kept if b.get("kind") == "authority"]
    return {"model_assertions": len(model_assertions or []), "model_kept": len(model_assertions or []) - len(refused),
            "model_contradicted": reasons.count("assertion_contradicts_fact"), "model_unsupported": reasons.count("assertion_not_supported"),
            "model_unknown_fact": reasons.count("assertion_unknown_fact"), "server_corrections": sum(1 for b in shown if b.get("corrected")),
            "shown_authority": len(shown), "shown_from_model": sum(1 for b in shown if not b.get("corrected"))}


def _akey(a: Mapping) -> tuple:
    return (a["fact_ref"], a["subject"], a["relation"], a["scope"], a["polarity"])


def _nums(text: str) -> set:
    return {n.replace(",", "") for n in _NUM.findall(text or "")}


def check_answer(model: Mapping, pack_: Mapping, limits: Mapping) -> dict:
    """-> {status, outcome, blocks, rejected, limitations}. A block is kept whole or refused with its reason, never edited. `status` is the server's verdict: `answered`,
    `partial` (some blocks refused or the model said partial), `insufficient_context`, `out_of_scope`, or `refused` (the model claimed an answer but nothing survived)."""
    facts = {f["id"]: f for f in pack_["facts"]}
    kept, rejected = [], []
    for i, b in enumerate(model["answer_blocks"]):
        text = " ".join(b["text"].split())
        refs = list(dict.fromkeys(b["fact_refs"]))
        cited = [facts[r] for r in refs if r in facts]
        reason = None
        if not text or len(text) > int(limits["max_block_chars"]) or _NOT_PLAIN.search(text):
            reason = "not_plain_text"
        elif any(r not in facts for r in refs):
            reason = "unknown_fact_ref"
        elif not refs and not (b["kind"] == "interpretation" and model["outcome"] in ("out_of_scope", "insufficient_context")):
            reason = "no_fact_cited"
        elif _ACTS.search(text):
            reason = "claims_an_action"                                                       # the assistant never acts
        elif _nums(text) - set().union(*[_nums(f["text"]) for f in cited] or [set()]):
            reason = "number_not_in_cited_facts"
        elif b["kind"] == "suggestion" and not any(f["kind"] in ("action", "obligation", "blocker") for f in cited):
            reason = "suggestion_without_action_fact"
        elif b["kind"] == "cause" and not any(f["kind"] in CAUSE_FACTS for f in cited):
            reason = "cause_without_cause_fact"
        else:                                                                               # a wrong claim keeps its specific reason; a right one is still refused in free text:
            reason = authority_problem(text, cited, b["kind"]) or free_text_authority(text)   # authority is stated only through typed assertions (AIG-01)
        if reason:
            rejected.append({"index": i, "kind": b["kind"], "reason": reason})
            continue
        kept.append({"text": text, "fact_refs": refs, "kind": b["kind"]})
    authority, a_rejected = check_assertions(model.get("authority_assertions") or [], facts)
    rejected += a_rejected
    accounting = authority_accounting(model.get("authority_assertions") or [], a_rejected, authority)
    kept = authority + kept                                                                 # the server-rendered authority sentences first, then the model's explanation
    lims = [" ".join(x.split())[:300] for x in model["limitations"][: int(limits["max_limitations"])] if x.strip() and not _NOT_PLAIN.search(x)]
    outcome = model["outcome"]
    if outcome in ("answered", "partial") and not kept:
        status = "refused"
    elif outcome == "answered" and rejected:
        status = "partial"
    else:
        status = outcome
    explained = sorted({facts[r]["kind"] for blk in kept if blk["kind"] != "authority" for r in blk["fact_refs"] if facts[r]["kind"] in EXPLANATION_FACTS})
    coverage = blocker_coverage(kept, pack_["facts"])
    shown_unexplained = [] if outcome == "out_of_scope" else coverage["unexplained"][:MAX_UNEXPLAINED_SHOWN]   # a question not about this request gets no blocker notes
    for fid in shown_unexplained:                                                         # AIG-03 v5: an offered blocker is never dropped silently; nothing is made up for it
        f = facts[fid]
        lims.append("Not explained in this answer: " + " ".join(str(f.get("display") or f["text"]).split()).removeprefix("Blocker: ")[:240])
    return {"status": status, "outcome": outcome, "blocks": kept, "rejected": rejected, "limitations": lims, "explained_fact_kinds": explained, "blocker_coverage": coverage,
            "authority_accounting": accounting}


def blocker_coverage(kept: list, facts: list) -> dict:
    """Which offered blocker facts a KEPT explanation (non-authority block) cites. Computed by the server from the pack, for any request: no expected ids."""
    offered = [f["id"] for f in facts if f["kind"] == "blocker"]
    cited = {r for b in kept if b["kind"] != "authority" for r in b["fact_refs"]}
    return {"offered": len(offered), "explained": [i for i in offered if i in cited], "unexplained": [i for i in offered if i not in cited]}


EXPLANATION_FACTS = frozenset({"blocker", "decision", "rule_change", "impact", "review_note", "request", "open_question", "waiting"})


def semantic_success(result: Mapping) -> bool:
    """AIG-03 scoring rule: an answer counts as semantically useful only when the model's OWN explanation survived the checks, i.e. at least one kept non-authority
    block cites a fact that carries business substance (blocker, decision, rule change, impact, review note, request, waiting). Server-rendered authority sentences
    alone (a template), an all-refused answer, a fallback or an insufficient / out-of-scope outcome are not semantic success."""
    if result.get("status") not in ("answered", "partial"):
        return False
    return any(b.get("kind") != "authority" for b in result.get("blocks") or []) and bool(result.get("explained_fact_kinds"))


def build_prompt(frozen: Mapping, pack_: Mapping, history: Iterable[Mapping], question: str, *, perspective: str) -> str:
    facts = "\n".join(f"{f['id']} [{f['kind']}] {f['text']}" for f in pack_["facts"]) or "(none)"
    hist = "\n".join(f"Q: {h['question']}\nA: {' '.join(b['text'] for b in h.get('blocks') or []) or '(no answer kept)'}" for h in history) or "(none)"
    prompt = (frozen["prompt_template"].replace("{perspective}", perspective).replace("{as_of}", str(pack_.get("as_of"))).replace("{facts}", facts)
              .replace("{history}", hist).replace("{question}", question))
    if len(prompt) > int(frozen["limits"]["max_prompt_chars"]):
        raise ValueError("context_too_large")
    return prompt
