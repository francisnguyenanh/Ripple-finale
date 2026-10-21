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

CONTEXT_VERSION = "request-assistant-context/3"                 # context/3: step phases from the record and the stage-reason / reason-gap facts (D1 / D4); AIG-02: tiered, stable selection with honest overflow (contract section 6)
ANSWER_SCHEMA = "request-assistant-answer/5"                    # the server states authority from the task record; the model explains (answer/2-4: model-chosen assertions)
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
KINDS = ("state", "obligation", "waiting", "blocker", "action", "step", "axis", "request", "review_note", "rule_change", "open_question", "decision", "impact", "interpretation",
         "stage_reason", "reason_gap")                                                       # context/3: the recorded stage reason / an undetermined cause (D1)
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


REASON_PHASES = ("not_started", "running", "awaiting_decision", "awaiting_action", "awaiting_other_step")


def stage_reason_facts(tv: Mapping) -> list:
    """D1 (context/3): why the request is where it is, ONLY as far as the workflow record shows it. A recorded blocker is itself the reason (no stage reason is added
    beside it). Without a blocker, each current step's recorded phase is the reason ("not started yet", "in progress", "waiting for a decision / an action"); it
    never says that nothing blocks the request. A current step whose phase the record does not show, or an unfinished request without a current step, gives a
    `reason_gap` fact instead: the cause is not fully known."""
    M = {"tier": "mandatory"}
    if tv.get("blockers"):
        return []
    current = [s for s in tv.get("stages") or [] if s.get("state") in ("current", "waiting")]
    known = [s for s in current if s.get("phase") in REASON_PHASES]
    unknown = [s for s in current if s.get("phase") not in REASON_PHASES]
    out = []
    if known:
        out.append(fact("stage_reason", "What the workflow record shows for the current step: " + "; ".join(f"{s['label']}: {s['phase_label']}" for s in known) + ".",
                        "task_view", {"phases": {s["code"]: s["phase"] for s in known}}, key="stage_reason", **M))
    finished = (tv.get("stage") or {}).get("code") in ("APPLIED", "COMPLETED", "COMMITTED", "REJECTED")
    if unknown or (not current and not finished):
        names = ", ".join(s["label"] for s in unknown) or "the current stage"
        out.append(fact("reason_gap", f"The workflow record does not fully show why the request is at this stage ({names}): the cause is not determined.", "task_view",
                        key="reason_gap", **M))
    return out


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
            held = f"open to {o.get('holder_label') or 'the role pool'} (not assigned to anyone yet)" if o.get("holder") == "role_pool" else f"held by {o.get('holder_label') or o.get('holder')}"
            plain = f"{held}, {_STATE_WORD.get(o.get('state'), o.get('state'))}" + ("; you may also do it, but you are not asked to" if o.get("eligible_me") else "")
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
    shown_steps = [s for s in tv.get("stages") or [] if s.get("state") not in ("future",)]
    if shown_steps:                                                                        # context/3: the phase the record shows, never the bare word "current"
        words = [f"{s['label']}: {s.get('phase_label') or _STEP_WORD.get(s['state'], s['state'])}" for s in shown_steps]
        out.append(fact("step", "Steps so far: " + "; ".join(words) + ".", "task_view", {"phases": {s["code"]: s.get("phase") or s["state"] for s in shown_steps}},
                        display="Steps so far: " + "; ".join(words) + ".", key="steps", **M))
    out += stage_reason_facts(tv)
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
# a `cause` block says WHY the request is where it is, about the request (never who may / must act): it must cite a fact that records a reason. checks/10: the request
# itself and a wait say WHAT is asked and WHERE it stands, not why (a "cause" citing only them restated the stage: E1 E-15 / E-16 / E-21 / E-33, RQA-DECLINE B2); the
# prompt lists exactly these facts per pack (`cause_basis`) so the model and the validator use one rule.
CAUSE_FACTS = frozenset({"blocker", "decision", "rule_change", "impact", "review_note", "open_question", "stage_reason"})   # context/3: + the recorded stage reason (D1)
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
CHECKS_VERSION = "request-assistant-checks/13"                  # checks/13: "the person who holds / acts is not named" is authority prose too; checks/12: a stated status must agree with the recorded step phases; checks/11: "who acts is not stated / unknown" is authority prose; checks/10: cause only from facts that record a reason (listed in the prompt), a withheld why is said, limitations guarded, authority claims about this request by anyone; checks/9: third-person authority bound to its clause + third-person permission; checks/8: server authority section + truncation notice; checks/6-7: corrected / typed model assertions
COMPATIBLE_CHECKS = frozenset({"request-assistant-checks/4", "request-assistant-checks/5", "request-assistant-checks/6", "request-assistant-checks/7", "request-assistant-checks/8", "request-assistant-checks/9", "request-assistant-checks/10", "request-assistant-checks/11", "request-assistant-checks/12", CHECKS_VERSION})   # authority already typed and server-rendered: shown as stored, no legacy filtering
_ROLE_WORDS = {"requester": ("requester", "person who asked"), "approver": ("approver",), "reviewer": ("reviewer",), "knowledge owner": ("knowledge owner",),
               "administrator": ("administrator", "admin"), "system": ("system", "the ai")}
# A third-person authority claim: a role word as the SUBJECT of a holding / obligation / permission predicate. The gap may not cross into a new clause with its own
# subject (", and the reason given is to ..." describes the reason, not the role; class of RQA-DECLINE H2, 2026-10-21); an elliptical continuation (", and must now
# decide") keeps the role as subject. Third-person permission ("the approver can / cannot decide") is a claim too. Mentioning a role is not a claim by itself.
_CLAUSE_BREAK = (r"[,;]?\s*\b(?:and|but|or|so|yet|while|whereas|because|since|although|though)\s+"
                 r"(?:the|a|an|this|that|these|those|it|its|they|their|there|his|her|our|your|my)\b")
_THIRD = re.compile(r"(?i)\b(?:the |an |a )?(requester|approver|reviewer|knowledge owner|administrator|admin|system)s?\b(?:(?!" + _CLAUSE_BREAK + r")[^.;]){0,40}?"
                    r"\b(holds?|must|needs? to|has to|is (?:asked|responsible|expected|required) to|is the one|is to"
                    r"|(?:can(?:not)?|can't|may(?: not)?|could|is (?:not )?(?:allowed|able|permitted) to)\s+(?:now\s+)?"
                    r"(?:approv|decid|confirm|commit|apply|clarif|answer|review|reject|accept)\w*)\b")
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


def validate_model_object(obj, limits: Mapping, *, legacy: bool = False) -> dict:
    """answer/5 (live): exactly {outcome, answer_blocks, limitations}; an authority field is a schema violation, never read. `legacy=True` accepts the answer/2-3
    shape with model assertions (stored answers, recorded model objects in replays)."""
    keys = MODEL_KEYS if legacy else REQUIRED_MODEL_KEYS
    if not isinstance(obj, dict) or not REQUIRED_MODEL_KEYS <= set(obj) <= keys or obj["outcome"] not in OUTCOMES:
        raise ValueError("keys_or_outcome")
    obj = {**obj, "authority_assertions": obj.get("authority_assertions", [])}
    asr = obj["authority_assertions"]
    if not isinstance(asr, list) or len(asr) > int(limits.get("max_assertions", 4)):
        raise ValueError("authority_assertions")
    typed = []
    for a in asr:
        if not isinstance(a, dict) or set(a) != ASSERTION_KEYS or a["subject"] not in ("self", "holder") or a["relation"] not in RELATIONS \
                or not isinstance(a["polarity"], bool) or a["scope"] not in ("current", "later") or not isinstance(a["fact_ref"], str):
            raise ValueError("assertion")                                                 # answer/2-3 objects (stored answers, replays) keep their shape
        typed.append(dict(a))
    obj["authority_assertions"] = typed
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


# checks/10: an authority claim about THIS request by anyone, not only a known role word: an obligation / permission / future-act predicate whose object is the request
# or one of its steps ("the Finance Controller has to approve it", "someone in finance has to sign this off", "it must be decided by Hanako", "the decision lies with
# the CFO"). A business rule about a class of cases ("purchases from high-risk suppliers go to the Finance Controller") has no such object and stays an explanation.
# The assistant as subject is a decline ("the assistant cannot approve this request"), not a claim about who acts. Lexical defence in depth: the reader's authority
# comes from the server section; claims outside these structures are measured by the evaluation, not guaranteed here.
_THIS = r"(?:it|this|that|the (?:request|change|knowledge change|decision|draft|next step|step|approval)|this (?:request|change|step|decision|approval))\b"
_ACT_VERB = r"(?:approv|decid|reject|confirm|commit|review|sign|accept|answer|respond)\w*"   # decision / approval acts only ("apply to", "complete", "clarify" read as effects too)
_DEICTIC = re.compile(r"(?i)\b(?:has to|have to|must|needs? to|is (?:required|asked|expected|responsible) to|should|can(?:not)?|can't|may(?: not)?|could|is (?:not )?(?:allowed|able|permitted) to|will|is going to)"
                      r"\s+(?:now\s+|still\s+|first\s+|only\s+)?" + _ACT_VERB + r"(?:\s+(?:on|off|to))?\s+" + _THIS +
                      r"|\b" + _THIS + r"\s+(?:has to|must|needs to|is to|should|can|may|will)\s+(?:only\s+|now\s+|first\s+)?be\s+" + _ACT_VERB + r"(?:\s+off)?\s+by\b"
                      r"|\b(?:the )?(?:decision|next step|step|approval|responsibility|sign-off)\s+(?:now\s+)?(?:lies|rests|sits|is)\s+(?:with|in the hands of|up to)\b"
                      r"|\bis (?:the|its) (?:\w+ ){0,2}?(?:approver|reviewer|decider|holder|owner|signatory|one)\s+(?:for|of|on|to (?:approve|decide|review|sign))\s+" + _THIS +
                      r"|\b(?:it|this)\s+is\s+(?:\w+ ){0,3}?\w+'s\s+(?:call|decision|step|turn|task|job|responsibility)\b"
                      r"|\bwaiting on\b"
                      # checks/11: a statement that who acts is unknown / not stated is a claim about authority too (RQA-V8 P3: "The facts do not state who holds the
                      # responsibility for the next response" under a record that states it); who acts is told by the server section only
                      r"|\b(?:do(?:es)? not|don't|doesn't|never|not)\s+(?:clearly\s+)?(?:state|record|say|specify|show|mention|indicate|name|identify)\s+who\b"
                      r"|\b(?:it is|it's)\s+(?:unknown|unclear|not known|not clear)\s+who\b"
                      # checks/13: the same claim about a person / holder who ACTS ("do not explicitly name the individual person who holds the next action": RQA-V9 P3);
                      # bound to an acting verb, so "the facts do not name the people who asked for this change" stays an honest limitation
                      r"|\b(?:do(?:es)? not|don't|doesn't|never|not)\s+(?:explicitly\s+|clearly\s+|specifically\s+)?(?:state|record|say|specify|show|mention|indicate|name|identify|give)"
                      r"\s+(?:the\s+|a\s+|any\s+)?(?:\w+\s+){0,2}?(?:person|people|individual|one|holder|owner|actor|party)\s+(?:who|that)\s+(?:\w+\s+)?"
                      r"(?:holds?|acts?|decides?|is responsible|must|has to|should|approves?|is next|takes?|owns?)\b"
                      r"|\b(?:the\s+)?(?:holder|person|individual|owner|actor|party)\s+(?:who|that)\s+(?:holds?|acts?|decides?|is responsible|must|takes?|owns?)\b[^.;]{0,40}\b(?:is|are)\s+not\s+"
                      r"(?:named|stated|specified|identified|recorded|given|known)\b")
_ASSISTANT_SUBJECT = re.compile(r"(?i)\b(?:the assistant|this assistant|i|we)\b[^.;,]*$")


def _deictic_claim(text: str) -> bool:
    return any(not _ASSISTANT_SUBJECT.search(text[max(0, m.start() - 60):m.start()]) for m in _DEICTIC.finditer(text))


# checks/12 (D4): a status the model states must agree with the phases the record shows. "Underway / in progress / being written" needs a step the record shows
# as running; "stopped / halted / stalled" needs a recorded blocker or a blocked / failed step. A negated phrase ("has not started") is not a claim.
_RUNNING_WORDS = re.compile(r"(?i)\b(?:under ?way|in progress|being (?:written|prepared|drafted|processed|reviewed|applied|analysed|analyzed)|is (?:currently |now )?running"
                            r"|has (?:already )?started|already started|currently working)\b")
_STOPPED_WORDS = re.compile(r"(?i)\b(?:stopped|halted|stalled|stuck)\b")
_NEG_BEFORE = re.compile(r"(?i)\b(?:not|no|never|isn't|hasn't|wasn't|nothing)\b[^.;,]{0,25}$")


def status_problem(text: str, facts: list) -> bool:
    phases = set()
    for f in facts:
        if f.get("kind") == "step":
            phases |= set(((f.get("data") or {}).get("phases") or {}).values())
    if not phases:                                                                         # no recorded phases (an older context): nothing to check against
        return False
    blocked = any(f.get("kind") == "blocker" for f in facts) or bool(phases & {"blocked", "failed"})
    for rx, ok in ((_RUNNING_WORDS, "running" in phases), (_STOPPED_WORDS, blocked)):
        for m in rx.finditer(text):
            if not ok and not _NEG_BEFORE.search(text[:m.start()]):
                return True
    return False


def free_text_authority(text: str) -> str | None:
    if _BARE_YES_NO.search(text):
        return "unbound_affirmation"
    if _FREE_AUTHORITY.search(text) or _HOLDS.search(text) or _NOT_HOLDS.search(text) or _ABLE.search(text) or _UNABLE.search(text) or _THIRD.search(text) or _WAITING.search(text) \
            or _deictic_claim(text):
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
    if d.get("holder") == "requester":                                                     # a role word read by someone who may hold that role too: name the relation instead
        holder = "the person who asked"
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
        if pos:
            return f"This later step will be yours: {label}." if later else f"You hold the current step: {label}."
        if d.get("holder") == "role_pool":                                                # nobody holds a pool step yet: say who it is open to and what THIS reader may do
            return (f"This step is not assigned to you: {label}. It is open to {holder}; you may act on it, but you are not asked to." if d.get("may")
                    else f"This step is not assigned to you: {label}. It is open to {holder}; you cannot act on it now.")
        return f"This step is not yours: {label} (held by {holder})."
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


MAX_AUTHORITY_BLOCKS = 6
AUTHORITY_TEXT_REASONS = frozenset({"authority_in_free_text", "unbound_affirmation", "contradicts_cited_permission", "contradicts_cited_obligation", "claims_holder_without_fact",
                                    "claims_requirement_on_optional", "holder_not_grounded"})   # checks/9: a third-person holder claim is prose authority too


def authority_summary(facts: list) -> list:
    """answer/5: who holds each current or blocked step, what this reader may do and who the request waits on, rendered by the server from the reader's own task
    facts (never chosen by the model). Assignment comes only from the record (`you` = obligation_me / assigned), never from a role or from eligibility."""
    out = []

    def add(text, f):
        out.append({"text": text, "fact_refs": [f["id"]], "kind": "authority", "origin": "task_record"})
    for f in facts:
        if f["kind"] != "obligation":
            continue
        d = f.get("data") or {}
        if d.get("state") == "blocked":
            add(render_assertion({"subject": "self", "relation": "blocked", "polarity": True, "scope": "current"}, f), f)
        elif d.get("state") == "current":
            if d.get("you"):
                add(render_assertion({"subject": "self", "relation": "holds", "polarity": True, "scope": "current"}, f).rstrip(".") + ("." if d.get("required") else " (optional)."), f)
            else:
                add(render_assertion({"subject": "self", "relation": "holds", "polarity": False, "scope": "current"}, f), f)
    for f in facts:
        if f["kind"] == "waiting":
            add(render_assertion({"subject": "holder", "relation": "waiting_on", "polarity": True, "scope": "current"}, f), f)
    for f in facts:
        d = f.get("data") or {}
        if f["kind"] == "action" and d.get("allowed") and not d.get("current"):            # the reader's own current step is already stated above
            add(render_assertion({"subject": "self", "relation": "may", "polarity": True, "scope": "current"}, f), f)
    if not out:
        state = next((f for f in facts if f["kind"] == "state"), None)
        if state is not None:
            add("No step on this request is open right now.", state)
    return out[:MAX_AUTHORITY_BLOCKS]


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
            "model_unknown_fact": reasons.count("assertion_unknown_fact"), "authority_missing": not shown, "server_corrections": sum(1 for b in shown if b.get("corrected")),
            "shown_authority": len(shown), "shown_from_model": sum(1 for b in shown if not b.get("corrected"))}


def _akey(a: Mapping) -> tuple:
    return (a["fact_ref"], a["subject"], a["relation"], a["scope"], a["polarity"])


def _nums(text: str) -> set:
    return {n.replace(",", "") for n in _NUM.findall(text or "")}


def check_answer(model: Mapping, pack_: Mapping, limits: Mapping, *, legacy: bool = False) -> dict:
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
        elif not legacy and status_problem(text, pack_["facts"]):
            reason = "status_contradicts_record"                                              # checks/12: "underway" / "stopped" against the recorded phases
        else:                                                                               # a wrong claim keeps its specific reason; a right one is still refused in free text:
            reason = authority_problem(text, cited, b["kind"]) or free_text_authority(text)   # authority is stated only through typed assertions (AIG-01)
        if reason:
            rejected.append({"index": i, "kind": b["kind"], "reason": reason})
            continue
        kept.append({"text": text, "fact_refs": refs, "kind": b["kind"]})
    if legacy:                                                                              # answer/2-4 objects (replays): model-chosen assertions, checked
        authority, a_rejected = check_assertions(model.get("authority_assertions") or [], facts)
        rejected += a_rejected
        accounting = authority_accounting(model.get("authority_assertions") or [], a_rejected, authority)
    else:                                                                                   # answer/5: the record states the authority; the model only explains
        authority = authority_summary(pack_["facts"])
        accounting = {"source": "task_record", "shown_authority": len(authority),
                      "model_prose_authority_refused": sum(1 for r in rejected if r["reason"] in AUTHORITY_TEXT_REASONS)}
    model_kept = kept if not legacy else authority + kept                                  # answer/5: the record's section never stands in for the model's answer
    kept = authority + kept                                                                 # the server-rendered authority sentences first, then the model's explanation
    lims = []
    for j, x in enumerate(model["limitations"][: int(limits["max_limitations"])]):        # checks/10: the model's limitations are prose the reader sees: same authority guard
        x = " ".join(x.split())[:300]
        if not x or _NOT_PLAIN.search(x):
            continue
        why = (free_text_authority(x) or ("status_contradicts_record" if status_problem(x, pack_["facts"]) else None)) if not legacy else None
        if why:
            rejected.append({"limitation": j, "kind": "limitation", "reason": why})
            continue
        lims.append(x)
    if not legacy:
        accounting["model_prose_authority_refused"] = sum(1 for r in rejected if r["reason"] in AUTHORITY_TEXT_REASONS)
    if not legacy and any(r["reason"] == "cause_without_cause_fact" for r in rejected):     # checks/10: a withheld "why" is said, never silent; nothing is made up for it
        lims.append("An explanation of why was not shown: " + ("it did not cite the fact that records the reason." if cause_basis(pack_["facts"])
                                                              else "the facts given to the assistant record no reason (no blocker, decision, open question, rule change or impact)."))
    outcome = model["outcome"]
    if outcome in ("answered", "partial") and not model_kept:
        status = "refused"
    elif outcome == "answered" and rejected:
        status = "partial"
    else:
        status = outcome
    explained = sorted({facts[r]["kind"] for blk in kept if blk["kind"] != "authority" for r in blk["fact_refs"] if facts[r]["kind"] in EXPLANATION_FACTS})
    coverage = blocker_coverage(kept, pack_["facts"])
    shown_unexplained = [] if outcome == "out_of_scope" else coverage["unexplained"][:MAX_UNEXPLAINED_SHOWN]   # a question not about this request gets no blocker notes
    cov = pack_.get("coverage") or {}
    omitted = sum(int(t.get("omitted") or 0) for t in (cov.get("tiers") or {}).values()) + sum(int(v) for k, v in (cov.get("upstream") or {}).items() if k.endswith("_omitted") and isinstance(v, int))
    if not legacy and outcome != "out_of_scope" and omitted:                               # the reader is told the assistant did not see everything (counts only)
        lims.append(f"Some facts about this request were not available to the assistant: {omitted} omitted.")
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


EXPLANATION_FACTS = frozenset({"blocker", "decision", "rule_change", "impact", "review_note", "request", "open_question", "waiting", "stage_reason"})


def semantic_success(result: Mapping) -> bool:
    """AIG-03 scoring rule: an answer counts as semantically useful only when the model's OWN explanation survived the checks, i.e. at least one kept non-authority
    block cites a fact that carries business substance (blocker, decision, rule change, impact, review note, request, waiting). Server-rendered authority sentences
    alone (a template), an all-refused answer, a fallback or an insufficient / out-of-scope outcome are not semantic success."""
    if result.get("status") not in ("answered", "partial"):
        return False
    return any(b.get("kind") != "authority" for b in result.get("blocks") or []) and bool(result.get("explained_fact_kinds"))


def cause_basis(facts: Iterable[Mapping]) -> list:
    """The facts of this pack a `cause` block may cite (checks/10): the ones that record a reason. Empty when the pack records none."""
    return [f for f in facts if f["kind"] in CAUSE_FACTS]


def build_prompt(frozen: Mapping, pack_: Mapping, history: Iterable[Mapping], question: str, *, perspective: str) -> str:
    facts = "\n".join(f"{f['id']} [{f['kind']}] {f['text']}" for f in pack_["facts"]) or "(none)"
    hist = "\n".join(f"Q: {h['question']}\nA: {' '.join(b['text'] for b in h.get('blocks') or []) or '(no answer kept)'}" for h in history) or "(none)"
    authority = "\n".join(b["text"] for b in authority_summary(pack_["facts"])) or "(none)"      # answer/5: the server's own statement, shown to the model as data
    causes = ", ".join(f"{f['id']} [{f['kind']}]" for f in cause_basis(pack_["facts"])) or "(none)"   # checks/10: the same rule the validator applies
    prompt = (frozen["prompt_template"].replace("{perspective}", perspective).replace("{as_of}", str(pack_.get("as_of"))).replace("{authority}", authority)
              .replace("{cause_facts}", causes).replace("{facts}", facts).replace("{history}", hist).replace("{question}", question))
    if len(prompt) > int(frozen["limits"]["max_prompt_chars"]):
        raise ValueError("context_too_large")
    return prompt
