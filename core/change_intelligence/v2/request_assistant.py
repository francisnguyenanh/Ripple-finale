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

CONTEXT_VERSION = "request-assistant-context/14"                # context/14 (owner check before the config 31 collection): whether a reason is recorded for a step not started is read from the source, never from "not started" alone: the holder's own obligation reason (for example AI use switched off for them) is the recorded reason; "the record gives no reason" only for the holder's complete view; any other reader, or an incomplete view, is told only that what it can see gives no reason; context/13 (R23, development on config 30 H7-32 / H7-36): a premise part is offered the recorded steps (the obligation facts: each step, its state and its blocker), which the contract already asks it to describe beside a step the question names and the record lacks; a current step that has not started, with no blocker, says that the record gives no reason why (the phase is where it is, not why), so "why has nothing happened" can be answered with what is recorded; context/12 (R23, development on config 28 H7-43): the later-steps fact follows whether THIS request continues, read from the task view's structured codes (expected deliverables, stage): continues / after the open questions are answered / after a revised version / has ended / cannot continue and must be stated as a new, separate request (then its remaining steps are said not to take place on it and nothing is said about a new request's steps or holders); before, a request that cannot continue was offered "steps expected if the request goes ahead"; context/11 (R23 mandate, A-US stage 1 H7-26 / H7-27): a next part is offered where the request is now - the stage, the steps so far (current, blocked), the stage reason, any blocker and open question - besides the later steps and the decisions in effect, so a sequence can start from the blocked state and what must be resolved first; context/10 (R23, chain A-US acceptance H7-01 / H7-07 / H7-28): an effect part is answered from the proposal too (request, Before / After rule change, current rule, computed range), not from the impact facts alone, which name only the current rule, so "what would be different" and "what does it give up" have the after side; the part words say that what to review before deciding is the change part and that a downside is read from the before / after; the parties fact names the computed range only when the pack holds one; context/9 (R22, A164 / A166 H3-04): a separate later-steps fact (key steps:later:holders) says that who acts on each later step is recorded only when that step starts (true by construction: obligations exist only for started steps), so "who will act later" is a cited record fact, not model authority prose; context/8 (R21, A131 H3-03 / H3-14, A162 H3-01 / H3-03): when the record holds no blocker and no blocked or failed step, the stage fact says so, so "no blocker is recorded" is a cited fact, not an inference from silence; context/7 (R16): who the record names as affected (nobody; the rule's actor applies the rule), as a fact; context/6 (R15): the computed range names the rule's scope; context/5 (R14): rule parts labelled (who acts under the rule / applies to); how decisions are made, as a fact; context/4 (R12): the reader's position, the steps still to come, which decisions are in effect, the cases a threshold change moves (computed), impact relations in words; context/3: step phases from the record and the stage-reason / reason-gap facts (D1 / D4); AIG-02: tiered, stable selection with honest overflow (contract section 6)
ANSWER_SCHEMA = "request-assistant-answer/6"                    # answer/6 (R16): the question's parts, each answered from the facts that answer that kind of part; names the question brings and the record does not hold are declared, verified and stated by the server; answer/5: the server states authority from the task record; the model explains (answer/2-4: model-chosen assertions)
LOCALE = "en"                                                   # the only supported answer locale
TIERS = ("mandatory", "support", "history")
_KIND_RANK = {k: i for i, k in enumerate(("state", "request", "position", "obligation", "waiting", "blocker", "action", "step", "axis", "rule_change", "open_question",
                                         "impact", "decision_status", "decision", "review_note", "interpretation"))}
MAX_PACK_CHARS = 12000                                          # the facts' text budget of one pack (the prompt has its own, larger ceiling)


class ContextOverflow(ValueError):
    """The facts that must be complete (mandatory tier) do not fit the pack, or one of them would be cut. Refused before any provider call; nothing is dropped silently."""
    def __init__(self, reason: str, detail: Mapping):
        super().__init__(reason)
        self.reason, self.detail = reason, dict(detail)
KINDS = ("state", "obligation", "waiting", "blocker", "action", "step", "axis", "request", "review_note", "rule_change", "open_question", "decision", "impact", "interpretation",
         "stage_reason", "reason_gap",                                                       # context/3: the recorded stage reason / an undetermined cause (D1)
         "position", "decision_status")                                                       # context/4: what the reader is asked now; which decisions are in effect
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


def _not_started_reason(tv: Mapping, idle: list) -> tuple:
    """context/14: what the source holds on WHY a current step has not started, for this reader. The step's current obligations carry the holder's own reason
    when the reader is the holder (`assigned_me`) and may not do it (e.g. AI use switched off): that is a recorded reason. Only the holder's complete view
    (`availability` available, the step's obligation present) can show that none is recorded; another reader sees only its own rights, so it is told what IT
    can see. -> (sentence, reason_recorded: True / False / None = not decidable for this reader)."""
    if not idle:
        return "", True
    obs = [o for o in tv.get("obligations") or [] if o.get("state") == "current" and o.get("step") in {s["code"] for s in idle}]
    own = [o for o in obs if o.get("assigned_me")]
    names = ", ".join(s["label"] for s in idle)
    noted = [o["reason"] for o in own if o.get("reason") and not o.get("eligible_me")]
    if noted:
        return f" The record notes for {names}: {'; '.join(dict.fromkeys(noted))}.", True
    if own and (tv.get("availability") or "available") == "available":
        return f" The record gives no reason why {names} has not started.", False
    return f" What this reader can see of the record gives no reason why {names} has not started.", None


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
        why, recorded = _not_started_reason(tv, [s for s in known if s.get("phase") == "not_started"])
        out.append(fact("stage_reason", "What the workflow record shows for the current step: " + "; ".join(f"{s['label']}: {s['phase_label']}" for s in known) + "." + why,
                        "task_view", {"phases": {s["code"]: s["phase"] for s in known}, "reason_recorded": recorded}, key="stage_reason", **M))
    finished = (tv.get("stage") or {}).get("code") in ("APPLIED", "COMPLETED", "COMMITTED", "REJECTED")
    if unknown or (not current and not finished):
        names = ", ".join(s["label"] for s in unknown) or "the current stage"
        out.append(fact("reason_gap", f"The workflow record does not fully show why the request is at this stage ({names}): the cause is not determined.", "task_view",
                        key="reason_gap", **M))
    return out


def position_head(asked: list) -> str:
    """One complete canonical statement, shared by context and quotation validation."""
    return ("Asked of you on this request now: " + "; ".join(asked) + "." if asked
            else "Asked of you on this request now: nothing (no current step of this request is yours; this covers this request now, not later steps).")


def position_fact(tv: Mapping) -> dict:
    """context/4 (R12, live A123 H4-03 / H4-14): what THIS reader is asked to do now, from their own obligations (exact, from the task view). Another holder's step
    is never the reader's deliverable; the absence of a step of theirs is stated, not left to be inferred."""
    asked = [o["label"] for o in tv.get("obligations") or [] if o.get("obligation_me") and o.get("state") == "current"]
    optional = [a.get("label") for a in tv.get("permitted_actions") or [] if a.get("allowed") and not a.get("current")]
    head = position_head(asked)
    tail = " Decisions and actions are made only with this request's own controls; the assistant never acts."   # R14: a citable fact for what the prompt has the model say
    opt = " You may also, though not asked now: " + "; ".join(optional) + "." if optional else ""
    text = head + opt + tail
    if len(text) > MAX_FACT_CHARS:                                                         # whole, never cut: the optional actions give way to their count (each is an action fact)
        text = head + (f" You may also take {len(optional)} other action(s), not asked now (see the action facts)." if optional else "") + tail
    return fact("position", text, "task_view", {"asked": asked, "optional": optional}, key="position", tier="mandatory")


def computed_range_text(title: str, change: Mapping, scope: str | None = None) -> str:
    """context/4 (R12): the server-computed range of a threshold change as ONE whole fact: the fullest wording that fits `MAX_FACT_CHARS` (the other unchanged
    conditions summarised, then the rule named by its before / after fact instead of its title); never a range cut mid-sentence."""
    from core.change_intelligence.v2 import business_draft
    short = dict(change, also_requires="the rule's other unchanged conditions") if change.get("also_requires") else change
    where = f" (applies to: {scope})" if scope else ""                                    # context/6 (R15): the cases that move are named with the rule's scope
    forms = ((f"the rule '{title}'", change, False), (f"the rule '{title}'", short, False), ("the rule of the Before / After fact just before this one", short, False),
             ("the rule just before", short, True))                                         # the scope, bounds, unit and provenance stay in every form
    for name, ch, compact in forms:
        text = f"Computed by the server for {name}{where}, {business_draft.condition_change_text(ch, compact=compact)}."
        if len(text) <= MAX_FACT_CHARS:
            return text
    return text


_DECISION_WORDS = {"approve": "approved", "reject": "rejected", "request_changes": "changes requested", "clarify": "clarified"}


def decision_status_fact(decisions: Iterable[Mapping]) -> dict:
    """context/4 (R12, live A123 H4-14): which recorded decisions are in effect now and which are not, with why (the record's own reason, e.g. answered by a later
    version of the draft). History is kept as history facts; this states the current position so "the record does not say" is never the answer when it does."""
    ds = list(decisions or [])
    now = [d for d in ds if d.get("current")]
    old = [d for d in ds if not d.get("current")]
    word = lambda d: f"{d.get('kind')} decision: {_DECISION_WORDS.get(d.get('action'), d.get('action'))}"      # noqa: E731
    text = ("Decisions in effect now: " + ("; ".join(f"{word(d)} (reason given: {d.get('reason')})" for d in now) if now else "none") + ". "
            + ("Earlier decisions no longer in effect: " + "; ".join(f"{word(d)} ({d.get('stale_reason')}); nothing is pending from it" for d in old) + "."
               if old else "No earlier decision is out of effect.") if ds else "No decision has been recorded on this request yet.")
    return fact("decision_status", text, "decision", {"current": len(now), "not_current": len(old)}, key="decision_status", tier="mandatory")


CONTINUATION = ("continues", "after_answers", "after_revision", "ended", "new_request")


def continuation(tv: Mapping) -> str:
    """context/12: whether THIS request continues, from the task view's structured codes only (never its prose): the deliverable `new_request` (this request
    cannot continue; a new, separate request is to be stated), `none` or a rejected stage (ended), the open questions first (CLARIFY / `answers`), a revised
    version first (CHANGES_REQUESTED / `response`); otherwise it continues."""
    codes = {d.get("code") for d in tv.get("expected_deliverables") or []}
    stage = (tv.get("stage") or {}).get("code")
    if "new_request" in codes:
        return "new_request"
    if "none" in codes or stage == "REJECTED":
        return "ended"
    if stage == "CLARIFY" or "answers" in codes:
        return "after_answers"
    if stage == "CHANGES_REQUESTED" or "response" in codes:
        return "after_revision"
    return "continues"


_LATER_HEAD = {"continues": "Steps expected after the current one if the request goes ahead",
               "after_answers": "Steps expected after the current one once the open questions are answered and if the request goes ahead",
               "after_revision": "Steps expected after the current one once a revised version answers the requested changes and if the request goes ahead"}


def task_facts(tv: Mapping) -> list:
    """Facts from the reader's own task view (already computed under the reader's rights)."""
    M = {"tier": "mandatory"}
    stuck = any(s.get("state") == "blocked" or s.get("phase") in ("blocked", "failed") for s in tv.get("stages") or [])
    none = "" if (tv.get("blockers") or stuck) else " No blocker is recorded on this request."          # context/8: an exact absence from the record's own blocker list
    out = [fact("state", f"Current stage: {tv['stage']['label']}.{none}", "task_view", key="state", **M)]
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
    if any(o.get("code") == "decide_knowledge" and o.get("state") in ("current", "waiting") for o in tv.get("obligations") or []):   # context/12 (H7-29 class)
        out.append(fact("step", "Decision choices in this workflow: request changes returns it to the person who asked for an answer or a revised request, which comes "
                        "back to this decision; reject ends the knowledge change; approve records the decision only (a knowledge owner commits later).", "task_view",
                        key="decision:options", **M))
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
    later = [s for s in tv.get("stages") or [] if s.get("state") == "future"]
    cont = continuation(tv)
    if cont == "new_request":                                                              # context/12: no expected path for a request that cannot continue
        rest = f" Its remaining steps ({'; '.join(s['label'] for s in later)}) will not take place on this request." if later else ""
        out.append(fact("step", "This request cannot continue: it has to be stated again as a new change request, which is a separate request." + rest
                        + " The steps and holders of a new request are not part of this record.", "task_view", {"continuation": cont}, key="steps:later", **M))
    elif cont == "ended":
        out.append(fact("step", f"This request has ended ({tv['stage']['label'].rstrip('.')}): no further step of this request is expected.", "task_view",
                        {"continuation": cont}, key="steps:later", **M))
    elif later:                                                                            # context/4 (R12): what follows the current step, in the workflow's order
        out.append(fact("step", _LATER_HEAD[cont] + ", in the workflow's order (a decision can still end it or send it back): "
                        + "; ".join(s["label"] for s in later) + ".", "task_view",
                        {"phases": {s["code"]: "future" for s in later}, "continuation": cont}, key="steps:later", **M))
        out.append(fact("step", "Who acts on each later step is recorded only when that step starts.", "task_view", key="steps:later:holders", **M))   # context/9
    out.append(position_fact(tv))
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
CHECKS_VERSION = "request-assistant-checks/31"                  # checks/31 (2026-10-30, acceptance v8 H8-33 / H8-38, not a gate): a kept limitation whose text restates a pack fact verbatim carries that fact as its provenance (limitation_support basis fact_restated; the model's words unchanged; no other verdict changes); checks/30 (R23, development on config 32 H7-36 / H7-30): a sentence-initial whether-clause that is the subject of "is not shown / recorded / stated / known / clear" is an embedded question, so its status words are not claimed ("Whether the lack of progress is normal is not shown in the record" was refused); a limitation kept through an honest-absence form that rests on a record fact carries that provenance (limitation_support) for the reader, the collector and the raters; checks/29 (R23, development on config 30 H7-20): a contrastive marker (rather than, instead of, as opposed to) rejects the status words that follow it in its clause ("waiting for that step rather than stuck" was refused as claiming "stuck"); a status word before it or in another clause is still a claim; checks/28 (R23, after the config 29 development): the honest-absence forms are recognised whichever unstated pattern caught them (the checks/11 "who acts is not stated" form included: "do not record who acts on later steps" was refused); a claim about who acts on this request or on requests of its kind is authority prose for ANY actor - a title or a name as well as a role word - with an obligation, permission, final-say or authority predicate, a bare act verb ending the clause, a simple-tense act by a non-role actor, or a passive "by" a non-role actor ("the Chief Risk Officer must approve changes like this" was kept); a claim embedded under a not-stated / whether frame is judged as an unstated statement; checks/27 (R23 mandate, A-US stage 1 guard audit): four narrow honest-absence forms are no longer authority prose, each only when nothing else in the sentence claims or denies authority - a limitation that a TIME is not recorded with no actor in it; a limitation that who acts on LATER steps is not recorded when the pack holds the later-steps-holders fact; a limitation about a name the server verified as absent from the record; a block that restates the parties fact it cites (the rule's actors are not recorded as affected). Everything else stays refused; checks/26 (R21 probe, arm B H3-04: "Who will move the request next is not shown." reached the reader although the record says who it waits on): the passive form of "who acts is not stated" is authority prose too, when the who-clause carries an authority verb (move, act, approve, decide, handle, complete, ...); provenance ("who raised the blocker is not recorded") stays an honest limitation; checks/25 (R21, A160 H3-04 / H3-02): an exact server-owned position quotation is server content under ANY part (it answers only an obligation part; under another part it is shown as task record and the part stays open); "past" marks a no-longer-current decision as past; checks/24: pure execution refusal is assistant policy; complete canonical position head accepted; checks/23: exact task-record quotations retain server provenance and do not require model paraphrases; checks/22 (R17): the parts a first answer leaves open are found from server signals (no kept block, or only frame facts cited where answering facts exist) and at most one supplementary call completes only those parts through the same checks; the merged answer keeps every refusal and note of both calls; checks/21 (R16, A131): a declared name is verified and stated only when the answer has a premise part, and only the blocks of a premise part may not mention it (A131: ordinary question words were declared and honest sentences refused); "Yes" / "No" is an answer token only when followed by punctuation or the end (A131 H4-09: "No person, team or customer group is named" refused); checks/20 (R16, answer/6): the question's parts each need a kept block or the answer says which part is not answered; a name the question brings is outside the record only when the model declares it, it is a phrase of the question and its words are in no fact (then the server states the absence and the model's prose may not mention it); the capital-letter name extraction of checks/19 is withdrawn; checks/19: a name / title the question names and no fact names may only be said to be absent from the record (R15); checks/18: the negation must govern the status word (clause ends at a relative "that is" / "which"; a why / reason word presupposes the status; a sentence-initial "whether" or "whether ... or not" is no negation) (R11); checks/17: a negation or "whether" earlier in the same clause makes a status word not a claim; a clause break ends its reach (R10, A112 H3-14); checks/16: "who ... is not stated" is authority prose only with an authority verb (provenance such as who raised a block stays an honest limitation, R8 A117 H4-13); checks/15: "blocked" / "on hold" is a status the record must carry (A104 H-06); a cause citing a no-longer-current decision says it is past (H-13); checks/14: a block that cites only the proposal (the request, the draft rule change) must say it is proposed (RQA-V10 P1); checks/13: "the person who holds / acts is not named" is authority prose too; checks/12: a stated status must agree with the recorded step phases; checks/11: "who acts is not stated / unknown" is authority prose; checks/10: cause only from facts that record a reason (listed in the prompt), a withheld why is said, limitations guarded, authority claims about this request by anyone; checks/9: third-person authority bound to its clause + third-person permission; checks/8: server authority section + truncation notice; checks/6-7: corrected / typed model assertions
COMPATIBLE_CHECKS = frozenset({"request-assistant-checks/30", "request-assistant-checks/29", "request-assistant-checks/28", "request-assistant-checks/27", "request-assistant-checks/26", "request-assistant-checks/25", "request-assistant-checks/24", "request-assistant-checks/23", "request-assistant-checks/4", "request-assistant-checks/5", "request-assistant-checks/6", "request-assistant-checks/7", "request-assistant-checks/8", "request-assistant-checks/9", "request-assistant-checks/10", "request-assistant-checks/11", "request-assistant-checks/12", "request-assistant-checks/13", CHECKS_VERSION, "request-assistant-checks/14", "request-assistant-checks/15", "request-assistant-checks/16", "request-assistant-checks/17", "request-assistant-checks/18", "request-assistant-checks/19", "request-assistant-checks/20", "request-assistant-checks/21", "request-assistant-checks/22"})   # authority already typed and server-rendered: shown as stored, no legacy filtering
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


ANSWER6_KEYS = frozenset({"outcome", "asks", "unrecorded", "limitations"})


def _flatten_asks(obj, limits: Mapping) -> dict:
    """answer/6: {outcome, asks[{ask, part, blocks[]}], unrecorded[], limitations[]} -> the same object with `answer_blocks` flattened (each block carries its
    part and the index of its ask) so every block check applies unchanged. Shape errors are schema violations (the whole answer unavailable)."""
    if set(obj) != ANSWER6_KEYS or obj["outcome"] not in OUTCOMES:
        raise ValueError("keys_or_outcome")
    asks = obj["asks"]
    if not isinstance(asks, list) or len(asks) > MAX_ASKS:
        raise ValueError("asks")
    blocks, parts = [], []
    for k, a in enumerate(asks):
        if not isinstance(a, dict) or set(a) != {"ask", "part", "blocks"} or not isinstance(a["ask"], str) or len(a["ask"]) > MAX_ASK_CHARS or a["part"] not in PARTS \
                or not isinstance(a["blocks"], list):
            raise ValueError("ask")
        parts.append({"ask": a["ask"], "part": a["part"]})
        for b in a["blocks"]:
            if not isinstance(b, dict) or set(b) != {"text", "fact_refs", "kind"}:
                raise ValueError("block")
            blocks.append({**b, "part": a["part"], "ask": k})
    if not isinstance(obj["unrecorded"], list) or len(obj["unrecorded"]) > MAX_UNRECORDED or not all(isinstance(x, str) for x in obj["unrecorded"]):
        raise ValueError("unrecorded")
    return {"outcome": obj["outcome"], "answer_blocks": blocks, "limitations": obj["limitations"], "asks": parts, "unrecorded": list(obj["unrecorded"])}


def validate_model_object(obj, limits: Mapping, *, legacy: bool = False) -> dict:
    """answer/6 (live): exactly {outcome, asks, unrecorded, limitations}; any authority field is a schema violation, never read. `legacy=True` accepts the
    answer/2-5 shapes (answer_blocks, optional model assertions: stored answers, recorded model objects in replays)."""
    if not isinstance(obj, dict):
        raise ValueError("keys_or_outcome")
    if not legacy:
        obj = _flatten_asks(obj, limits)
    keys = MODEL_KEYS if legacy else REQUIRED_MODEL_KEYS | {"asks", "unrecorded"}
    if not REQUIRED_MODEL_KEYS <= set(obj) <= keys or obj["outcome"] not in OUTCOMES:
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
        if not isinstance(b, dict) or set(b) - {"part", "ask"} != {"text", "fact_refs", "kind"} or not isinstance(b["text"], str) or b["kind"] not in BLOCK_KINDS \
                or not isinstance(b["fact_refs"], list) or not all(isinstance(r, str) for r in b["fact_refs"]):
            raise ValueError("block")
    if not isinstance(obj["limitations"], list) or not all(isinstance(x, str) for x in obj["limitations"]):
        raise ValueError("limitations")
    return obj


# AIG-01: authority is stated only through typed assertions; a free block that claims who holds, must, may or is waited on is refused (defence: the lexical checks below)
_FREE_AUTHORITY = re.compile(r"(?i)\b(you|your role)\s+(can|cannot|can't|may|could|must|should|need to|have to|hold|are (?:not )?(?:allowed|able|permitted|asked|required|expected|responsible))\b"
                             r"|\b(is|are) (?:not )?yours\b|\bit'?s your (?:turn|step|task|move|job)\b|\bheld by you\b|\bwaiting (?:on|for) you\b")
_BARE_YES_NO = re.compile(r"(?i)^\s*(yes|no)\s*(?:[,.;:!?\-\u2013\u2014]|$)")             # checks/21: the answer token, not "No person ... is named"


# checks/10: an authority claim about THIS request by anyone, not only a known role word: an obligation / permission / future-act predicate whose object is the request
# or one of its steps ("the Finance Controller has to approve it", "someone in finance has to sign this off", "it must be decided by Hanako", "the decision lies with
# the CFO"). A business rule about a class of cases ("purchases from high-risk suppliers go to the Finance Controller") has no such object and stays an explanation.
# The assistant as subject is a decline ("the assistant cannot approve this request"), not a claim about who acts. Lexical defence in depth: the reader's authority
# comes from the server section; claims outside these structures are measured by the evaluation, not guaranteed here.
_THIS = r"(?:it|this|that|the (?:request|change|knowledge change|decision|draft|next step|step|approval)|this (?:request|change|step|decision|approval))\b"
_ACT_VERB = r"(?:approv|decid|reject|confirm|commit|review|sign|accept|answer|respond)\w*"   # decision / approval acts only ("apply to", "complete", "clarify" read as effects too)
# checks/16: the acts that make "who ..." a claim about authority (holding, deciding, approving, acting next, being responsible / allowed / expected)
_WHO_AUTH = (r"(?:\w+\s+){0,2}?(?:holds?|acts?|decides?|approves?|signs?|reviews?|confirms?|commits?|rejects?|moves?|takes?|owns?|is responsible|are responsible|"
             r"is next|must|has to|have to|should|needs? to|will|can|may|is (?:expected|asked|required|allowed|supposed)|responds?|answers?|receives?|handles?|picks? up|gets? it)\b")
_DEICTIC = re.compile(r"(?i)\b(?:has to|have to|must|needs? to|is (?:required|asked|expected|responsible) to|should|can(?:not)?|can't|may(?: not)?|could|is (?:not )?(?:allowed|able|permitted) to|will|is going to)"
                      r"\s+(?:now\s+|still\s+|first\s+|only\s+)?" + _ACT_VERB + r"(?:\s+(?:on|off|to))?\s+" + _THIS +
                      # R16: the same requirement with the request as the object of a purpose or time clause ("must sign before it can close", "has to approve
                      # for this to move on"); the frame, not the subject's title, makes it a claim about who must act on this request
                      r"|\b(?:has to|have to|must|needs? to|is (?:required|asked|expected) to|should|will)\s+(?:also\s+|still\s+|first\s+)?" + _ACT_VERB +
                      r"(?:\s+(?:off|it|this|them))?\s+(?:before|so that|for|until)\s+" + _THIS +
                      r"|\b" + _THIS + r"\s+(?:has to|must|needs to|is to|should|can|may|will)\s+(?:only\s+|now\s+|first\s+)?be\s+" + _ACT_VERB + r"(?:\s+off)?\s+by\b"
                      r"|\b(?:the )?(?:decision|next step|step|approval|responsibility|sign-off)\s+(?:now\s+)?(?:lies|rests|sits|is)\s+(?:with|in the hands of|up to)\b"
                      r"|\bis (?:the|its) (?:\w+ ){0,2}?(?:approver|reviewer|decider|holder|owner|signatory|one)\s+(?:for|of|on|to (?:approve|decide|review|sign))\s+" + _THIS +
                      r"|\b(?:it|this)\s+is\s+(?:\w+ ){0,3}?\w+'s\s+(?:call|decision|step|turn|task|job|responsibility)\b"
                      r"|\bwaiting on\b"
                      # checks/11: a statement that who acts is unknown / not stated is a claim about authority too (RQA-V8 P3: "The facts do not state who holds the
                      # responsibility for the next response" under a record that states it); who acts is told by the server section only
                      # checks/16 (R8, A117 H4-13): bound to an authority verb, as checks/13 is; "the facts do not identify who raised the block" is provenance, an
                      # honest limitation, and was refused under checks/11-15
                      r"|\b(?:do(?:es)? not|don't|doesn't|never|not)\s+(?:clearly\s+)?(?:state|record|say|specify|show|mention|indicate|name|identify)\s+who\s+" + _WHO_AUTH +
                      r"|\b(?:it is|it's)\s+(?:unknown|unclear|not known|not clear)\s+who\s+" + _WHO_AUTH +
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
# A104 (item H-13, 2026-10-27): a cause that cites a decision no longer current must say it is past ("earlier", "no longer current", "was revised", ...)
_PAST_MARK = re.compile(r"(?i)\b(?:earlier|past|previous(?:ly)?|no longer|not current|was revised|has been revised|were revised|before the (?:draft|request) was revised|outdated|out of date|superseded)\b")
# A104 (item H-06, 2026-10-27): "the request is blocked" with no blocker and no blocked phase reached the reader; "blocked" / "on hold" are stopped words too
_STOPPED_WORDS = re.compile(r"(?i)\b(?:stopped|halted|stalled|stuck|blocked|on hold)\b")
# checks/17 (R10, live A112 H3-14): "The facts do not indicate whether ... progress has stalled" was refused (the negation was more than 25 characters back); a
# negation or "whether" anywhere earlier in the same clause then excused the word. checks/18 (R11 review of checks/17): the negation must govern the status word,
# not merely precede it. The clause ends at punctuation, a joining word, "which", or a relative "that / who + verb" ("it is not the draft that is stalled"
# presupposes something is stalled); a reason / why / how-long word between the negation and the status word presupposes the status ("the facts do not record
# why it is stalled" says it is stalled; checks/16 refused it, checks/17 wrongly kept it); "whether" is an embedded question only after other words of its clause
# ("whether or not anyone acts the request is stuck" is a claim), and the "not" of "or not" negates nothing
_CLAUSE_SPLIT = re.compile(r"(?i)[.;,:!?]|\b(?:and|but|yet|although|though|while|whereas|because|so|therefore|thus|hence|which)\b"
                           r"|\b(?:that|who)\s+(?:is|was|are|were|has|have|had|remains?|seems?|stays?|looks?|gets?|got)\b")
# checks/29 (R23, config 30 development H7-20): a contrastive marker rejects the alternatives that FOLLOW it in its clause ("waiting rather than stuck or stopped");
# a status word before it ("stuck rather than waiting") or in another clause ("not merely waiting but stalled") is still claimed
_NEG_MARK = re.compile(r"(?i)\b(?:not|no|never|isn't|hasn't|wasn't|aren't|haven't|nothing|neither|nor|without|whether|rather than|instead of|as opposed to)\b")
_PRESUPPOSES = re.compile(r"(?i)\b(?:why|reasons?|causes?|caused|explain\w*|how long|since when|who|whom|when)\b")   # "do not record who put it on hold" presupposes it is on hold


def _status_negated(before: str) -> bool:
    clause = re.sub(r"(?i)\bor not\b", "or-else", _CLAUSE_SPLIT.split(before)[-1])
    marks = list(_NEG_MARK.finditer(clause))
    if not marks:
        return False
    m = marks[-1]
    if _PRESUPPOSES.search(clause[m.end():]):
        return False
    return not (m.group(0).lower() == "whether" and not clause[:m.start()].strip())


# checks/14 (live finding RQA-V10-20261023-01, P1): the request title "should be approved by the Finance Controller" and the draft rule change were restated as
# "Purchases ... are routed to the Finance Controller" — a proposal shown as the rule in effect, on a question inviting exactly that. A block whose cited facts are
# ALL proposal facts must carry the proposal mood; the current rule (`rule_change:scope:*`), the state or a decision make the block about what is, not what is asked.
_PROPOSAL_MOOD = re.compile(r"(?i)\b(?:propos\w*|request\w*|ask(?:s|ed|ing)?|wants?|wanted|should|would|draft\w*|plan(?:s|ned)?|suggest\w*|seek\w*|intend\w*|if (?:it is )?approved|once approved)\b")


def _proposal_fact(f: Mapping) -> bool:
    return f.get("kind") == "request" or (f.get("kind") == "rule_change" and str(f.get("key") or "").startswith("rule_change:draft:"))


def proposal_stated_as_in_effect(text: str, cited: list) -> bool:
    return bool(cited) and all(_proposal_fact(f) for f in cited) and not _PROPOSAL_MOOD.search(text)


# checks/27 (R23 mandate, judgement questions): a certainty the record cannot give - that nothing is wrong, that progress is healthy / on track, or that a delay
# is deliberate - is refused unless negated in its clause ("the record does not show that nothing is wrong" is fine). The record can show a blocker, a phase and
# the absence of a recorded blocker; it never shows health, intent or the absence of every problem.
_CERTAINTY_WORDS = re.compile(r"(?i)\b(?:healthy progress|progress(?:ing|es)? (?:is )?(?:normal(?:ly)?|healthy|well|fine|as expected)|on track|going well"
                              r"|nothing (?:is |has gone |went )?wrong|no (?:problems?|issues?)|deliberate(?:ly)?|intentional(?:ly)?|on purpose)\b")


_WHETHER_SUBJECT = re.compile(r"(?i)(?:^|(?<=[.;!?])\s*)(whether\b[^.;!?]*?)\b(?:is|are|was|were)\s+not\s+"
                              r"(?:shown|recorded|stated|known|clear|indicated|given|determined|said|specified)\b")


def _embedded_whether(text: str, pos: int) -> bool:
    """checks/30: the position lies inside a sentence-initial whether-clause that is the subject of a not-shown predicate ("Whether X is normal is not shown")."""
    return any(m.start(1) <= pos < m.end(1) for m in _WHETHER_SUBJECT.finditer(text))


def status_problem(text: str, facts: list) -> bool:
    for m in _CERTAINTY_WORDS.finditer(text):
        if not _status_negated(text[:m.start()]) and not _embedded_whether(text, m.start()):
            return True
    phases = set()
    for f in facts:
        if f.get("kind") == "step":
            phases |= set(((f.get("data") or {}).get("phases") or {}).values())
    if not phases:                                                                         # no recorded phases (an older context): nothing to check against
        return False
    blocked = any(f.get("kind") == "blocker" for f in facts) or bool(phases & {"blocked", "failed"})
    for rx, ok in ((_RUNNING_WORDS, "running" in phases), (_STOPPED_WORDS, blocked)):
        for m in rx.finditer(text):
            if not ok and not _status_negated(text[:m.start()]) and not _embedded_whether(text, m.start()):
                return True
    return False


# A106 (H2-09 / H2-10): "the facts do not state whether the Chief Financial Officer has the final say" leaves the reader thinking authority is unknown, while
# the record states who holds the step; a statement that authority is unstated is authority prose too (as "who acts is not named", checks/13)
_AUTH_UNSTATED = re.compile(r"(?i)\b(?:do(?:es)? not|don't|doesn't)\s+(?:state|say|record|show|mention|specify|indicate)\b[^.;]{0,60}?"
                            r"\b(?:final (?:say|word)|approv\w*|decides?|deciding|sign(?:s|ed)? off|overrul\w*|authori[sz]\w*|authorit(?:y|ies)"   # checks/28: + authority, final word
                            # R8 (A119 H3-04 / H4-14): who moves it on and what follows are told by the server section (waits, next steps)
                            r"|move[sd]? (?:\w+ ){0,3}?(?:forward|on)|moving (?:\w+ ){0,3}?(?:forward|on)|takes? (?:\w+ ){0,3}?forward|next steps?"
                            r"|steps? (?:that )?(?:occur|happen|follow|come)s? after)\b")


_WHO_ACTS = (r"who\s+(?:(?:will|would|should|must|can|is\s+to|is\s+going\s+to)\s+)?(?:be\s+responsible|is\s+responsible|is\s+next|goes\s+next|"
              r"mov(?:e|es|ing)|act(?:s)?|approv(?:e|es)|decid(?:e|es)|sign(?:s)?|handl(?:e|es)|complet(?:e|es)|advanc(?:e|es)|tak(?:e|es)|hold(?:s)?|own(?:s)?|"
              r"review(?:s)?|commit(?:s)?|appl(?:y|ies)|push(?:es)?|progress(?:es)?|carr(?:y|ies))\b")
_UNSTATED_WORD = r"(?:shown|stated|recorded|named|specified|indicated|known|mentioned|given|clear)"
# checks/26 (R21 probe): the passive forms "who <acts> ... is not shown" and "it is not shown who <acts>"; a who-clause without an authority verb is not matched
_AUTH_UNSTATED_PASSIVE = re.compile(r"(?i)\b" + _WHO_ACTS + r"[^.;]{0,60}?\b(?:is|are)\s+not\s+" + _UNSTATED_WORD + r"\b"
                                    r"|\b(?:is|are)\s+not\s+" + _UNSTATED_WORD + r"\s+(?:in\s+the\s+(?:record|facts)\s+)?" + _WHO_ACTS)


def free_text_authority(text: str) -> str | None:
    if _BARE_YES_NO.search(text):
        return "unbound_affirmation"
    if _AUTH_UNSTATED.search(text) or _AUTH_UNSTATED_PASSIVE.search(text):
        return "authority_in_free_text"
    if _FREE_AUTHORITY.search(text) or _HOLDS.search(text) or _NOT_HOLDS.search(text) or _ABLE.search(text) or _UNABLE.search(text) or _THIRD.search(text) or _WAITING.search(text) \
            or _deictic_claim(text) or actor_claim(text):
        return "authority_in_free_text"
    return None


# checks/27 (R23 mandate): honest absences that the unstated-authority patterns refused although they claim nothing about who holds, must or may act. Each form
# is accepted only when the ONLY authority signal in the sentence is the unstated pattern itself (no "you", no role word, no holding / permission / obligation /
# waiting predicate, no deictic claim), so a sentence that also denies or asserts an existing right, obligation, assignment or action stays refused.
_TIME_OBJECT = re.compile(r"(?i)\b(?:do(?:es)? not|don't|doesn't)\s+(?:state|say|record|show|mention|specify|indicate)\s+(?:any\s+|a\s+|an\s+|the\s+)?"
                          r"(?:timeline|timing|date|dates|deadline|schedule|estimate|time|how long|when)\b")
_LATER_SCOPE = re.compile(r"(?i)\b(?:later|future|subsequent|following|upcoming|remaining)\s+steps?\b|\buntil\s+(?:each|that|the)\s+step\s+starts\b")
_NOW_SCOPE = re.compile(r"(?i)\b(?:current|this)\s+step\b|\bnow\b|\bcurrently\b|\bat present\b")
_WHO = re.compile(r"(?i)\bwho(?:m|se)?\b")
_AFFECTED_RESTATE = re.compile(r"(?i)\bwho\s+appl(?:y|ies)\s+the\s+rule\b[^.;]{0,80}?\b(?:is|are)\s+not\s+(?:recorded|named|listed|identified)\s+as\s+(?:the\s+)?affected\b")


# the deictic rule's own "who acts is not stated / unknown" branch (checks/11) is the same unstated form; it is taken out before looking for ANOTHER signal
_UNSTATED_WHO = re.compile(r"(?i)\b(?:do(?:es)? not|don't|doesn't|never|not)\s+(?:clearly\s+)?(?:state|record|say|specify|show|mention|indicate|name|identify)\s+who\s+"
                           + _WHO_AUTH + r"|\b(?:it is|it's)\s+(?:unknown|unclear|not known|not clear)\s+who\s+" + _WHO_AUTH)


def _other_authority(text: str) -> bool:
    rest = _UNSTATED_WHO.sub(" ", text)
    return bool(_BARE_YES_NO.search(text) or _FREE_AUTHORITY.search(text) or _HOLDS.search(text) or _NOT_HOLDS.search(text) or _ABLE.search(text) or _UNABLE.search(text)
                or _THIRD.search(text) or _WAITING.search(text) or _deictic_claim(rest) or actor_claim(text) or re.search(r"(?i)\byou(?:r)?\b", text))


# checks/28: who acts on THIS request (or on requests of its kind) is told by the server section only, whoever the actor is. A business rule about a class of
# work items ("high-risk supplier purchases must be approved by the Finance Controller") has another object and stays an explanation of the change.
_LIKE_THIS = (r"(?:(?:changes?|requests?|change requests?|cases?|ones?)\s+(?:like|such as)\s+(?:this|it|these|that)"
              r"|(?:this|that|the same)\s+(?:kind|type|sort)\s+of\s+(?:change|request)s?"
              r"|(?:changes?|requests?)\s+of\s+(?:this|that)\s+(?:kind|type|sort)|such\s+(?:changes|requests))\b")
_CLAIM_OBJ = r"(?:" + _THIS + r"|" + _LIKE_THIS + r")"
_ACT_DONE = r"(?:approv|decid|reject|confirm|commit|review|sign|accept)(?:es|s|ed|d)?"
_ROLE_ACTOR = re.compile(r"(?i)\b(?:requester|approver|reviewer|knowledge owner|administrator|admin|system|person who asked)s?\b")
_ACTOR_CLAIMS = re.compile(
    r"(?i)\b(?:has to|have to|must|needs? to|is (?:required|asked|expected|responsible) to|should|can(?:not)?|can't|may(?: not)?|could|will|would|is going to"
    r"|is (?:not )?(?:allowed|able|permitted) to)\s+(?:also\s+|still\s+|first\s+|only\s+|now\s+|alone\s+)?" + _ACT_VERB +
    r"(?:\s+(?:off|on|to))?\s*(?:" + _CLAIM_OBJ + r"|(?:(?:too|as well|also|first|later|then|now|again|either)\s*)?(?=[.;!?]|$))"   # on it, or closing the clause
    r"|\b(?:has|have|holds?|had|gets?|keeps?)\s+(?:the\s+)?(?:final|last|deciding)\s+(?:say|word|decision|call|sign-?off)\b"
    r"(?!\s+(?:on|over|about|for|of)\s+(?!" + _CLAIM_OBJ + r"))"                                      # unless about another object (a business rule)
    r"|\b(?:has|have|holds?|had|lacks?)\s+(?:no\s+|any\s+|the\s+|full\s+|final\s+)?(?:authority|power|right|say)\s+(?:over|on|for|to\s+" + _ACT_VERB + r")\s+" + _CLAIM_OBJ +
    r"|\b(?:is|are)\s+(?:not\s+)?(?:the|its|their)\s+(?:\w+\s+){0,2}?(?:approver|decision-?maker|decider|signatory|authority)\s+(?:for|of|on|over)\s+" + _CLAIM_OBJ)
_SIMPLE_ACT = re.compile(r"(?i)(?P<actor>(?:\b[\w'-]+\s+){1,5}?)(?:also\s+|alone\s+|only\s+|then\s+|finally\s+)?" + _ACT_DONE + r"(?:\s+(?:off|on))?\s+" + _CLAIM_OBJ)
_PASSIVE_BY = re.compile(r"(?i)\b" + _CLAIM_OBJ + r"\s+(?:is|are|was|were|gets?|got|will be|would be)\s+(?:only\s+|also\s+|finally\s+|then\s+)?"
                         r"(?:approved|decided|signed off|rejected|reviewed|confirmed|committed|accepted)\s+(?:on\s+)?by\s+(?P<actor>[^.;,]{1,60})")
_NOT_ACTOR = re.compile(r"(?i)^(?:it|this|that|which|who|what|whether|if|when|once|after|before|and|or|but|then|so|to|not|never|nobody|no one|the request|the record|the facts)\b")
_UNSTATED_FRAME = re.compile(r"(?i)(?:\b(?:do(?:es)?\s+not|don't|doesn't|did\s+not|never|not)\s+(?:\w+\s+){0,2}?(?:state|say|record|show|mention|specify|indicate|tell|name|confirm|establish)"
                             r"|\b(?:unknown|unclear|not\s+(?:known|clear|stated|shown|recorded|specified))|\bwhether)\b")


def _embedded(before: str) -> bool:
    """The match sits under a not-stated / whether frame of its own clause ("the record does not show whether X has the final say")."""
    clause = re.split(r"[.;!?]|\b(?:but|however|although|though|while|whereas)\b", before)[-1]
    return bool(_UNSTATED_FRAME.search(clause))


def actor_claim(text: str) -> bool:
    """checks/28: a claim that some actor holds, must, may, has the final say over or has acted on this request (or requests of its kind)."""
    for m in _ACTOR_CLAIMS.finditer(text):
        if not _ASSISTANT_SUBJECT.search(text[max(0, m.start() - 60):m.start()]) and not _embedded(text[:m.start()]):
            return True
    for m in _SIMPLE_ACT.finditer(text):                                                    # "the Chief Risk Officer approves this request": a non-role actor
        actor = m.group("actor").strip()
        if actor and not _NOT_ACTOR.search(actor) and not _ROLE_ACTOR.search(actor) and not _ASSISTANT_SUBJECT.search(actor) \
                and not re.search(r"(?i)\b(?:you|your|the facts?|the record|record|facts|it|this|that|request)\s*$", actor) and not _embedded(text[:m.start()]):
            return True
    for m in _PASSIVE_BY.finditer(text):                                                    # "this change is approved by the Chief Risk Officer"
        if not _ROLE_ACTOR.search(m.group("actor")) and not _embedded(text[:m.start()]):
            return True
    return False


def _role_named(text: str, facts: Iterable[Mapping]) -> bool:
    low = text.lower()
    if any(w in low for words in _ROLE_WORDS.values() for w in words):
        return True
    return any(n and n.lower() in low for f in facts for n in _holder_names(f))


_ACTOR_CLAUSE = re.compile(r"(?i)\b(?:who(?:m|se)?|whether|if)\b[^,;]*?\b(?:holds?|acts?|decides?|approves?|signs?|reviews?|confirms?|commits?|rejects?|moves?|takes?"
                           r"|owns?|handles?|advances?|responsible|participates?|receives?)\b")


def _every_actor_clause_later(text: str) -> bool:
    """checks/28: each clause that leaves an actor open is about the later steps ("..., nor whether X moves it on" adds an open current actor)."""
    clauses = re.split(r"(?i)[,;]|\bnor\b", text)
    return all(_LATER_SCOPE.search(c) for c in clauses if _ACTOR_CLAUSE.search(c))


def honest_absence_basis(text: str, facts: list, verified: Iterable[str], cited: list, *, limitation: bool) -> dict | None:
    """checks/27-30: which accepted honest-absence form a sentence the unstated-authority patterns refused is, and what it rests on, or None.
    -> {"basis": "parties_restated" | "time_not_recorded" | "later_step_holders" | "verified_absent_name", "fact_refs": [...], "names": [...]}"""
    if not (_AUTH_UNSTATED.search(text) or _AUTH_UNSTATED_PASSIVE.search(text) or _UNSTATED_WHO.search(text)) or _other_authority(text):
        return None                                                                          # checks/28: whichever unstated form caught it
    if not limitation:                                                                       # a block: only the parties restatement, citing the parties fact
        parties = [f["id"] for f in cited if f.get("key") == "parties"]
        return {"basis": "parties_restated", "fact_refs": parties, "names": []} if _AFFECTED_RESTATE.search(text) and parties else None
    if _TIME_OBJECT.search(text) and not _WHO.search(text) and not _role_named(text, facts):
        return {"basis": "time_not_recorded", "fact_refs": [], "names": []}                  # a time the record does not give, no actor: nothing to cite
    holders = [f["id"] for f in facts if f.get("key") == "steps:later:holders"]
    if (_WHO.search(text) and _LATER_SCOPE.search(text) and not _NOW_SCOPE.search(text) and not _role_named(text, facts) and holders
            and _every_actor_clause_later(text)):
        return {"basis": "later_step_holders", "fact_refs": holders, "names": []}            # who acts on later steps: recorded only when each starts
    names = [v for v in verified or () if v and v.lower() in text.lower()]
    if names and not _role_named(text, facts) and not _NOW_SCOPE.search(text):
        return {"basis": "verified_absent_name", "fact_refs": [], "names": names}           # the server's own absence statement supports it
    return None


def _plain(text: str) -> str:
    return " ".join(str(text).lower().replace("’", "'").split()).rstrip(" .;")


def fact_restatement(text: str, facts: list) -> list:
    """checks/31 (acceptance v8 H8-33 / H8-38): the ids of the pack facts whose text the limitation restates verbatim (case, spacing and the final period
    aside); a limitation that only shares words with a fact, or adds a claim to it, is not a restatement (nothing is inferred, nothing is paraphrased)."""
    plain = _plain(text)
    if not plain:
        return []
    return [f["id"] for f in facts if isinstance(f.get("text"), str) and _plain(f["text"]) == plain]


def honest_absence(text: str, facts: list, verified: Iterable[str], cited: list, *, limitation: bool) -> bool:
    """checks/27: True when a sentence the unstated-authority patterns refused is one of the four accepted forms (see CHECKS_VERSION)."""
    return honest_absence_basis(text, facts, verified, cited, limitation=limitation) is not None


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
        if d.get("state") == "blocked":                                                    # A106 (H2-09): a blocked step says whose it is, as the open ones do
            text = render_assertion({"subject": "self", "relation": "blocked", "polarity": True, "scope": "current"}, f)
            who = "the person who asked" if d.get("holder") == "requester" else d.get("holder_label")
            if who and not d.get("you"):
                text = text.rstrip(".") + f" ({'open to' if d.get('holder') == 'role_pool' else 'held by'} {who})."
            add(text, f)
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


# ---- answer/6 (R16): the question's parts, the facts that answer each kind of part, names outside the record ------------------------------------------------
# A question is answered part by part. The model says which parts it asks (`asks`: a short restatement and a part kind), answers each from the facts, and declares
# the names the question brings that the record does not hold (`unrecorded`). The server computes, per pack, which facts answer each kind of part (`basis_map`, given
# to the model as data), verifies every declared name against the record (a phrase of the question whose words occur in no fact) and states the absence itself; a
# part with no kept block is disclosed. No name is recognised from capitals or role lists: provenance is the question and the pack.
PARTS = ("status", "obligation", "decision", "change", "effect", "premise", "next", "reason", "how", "action", "other")
PART_WORDS = {"status": "where the request stands, whether it is moving or stalled, whether it changed lately",
              "obligation": "what the person is asked to do or owes now (the authority section states whose the steps are)",
              "decision": "what was decided, which decisions are in effect and which are past",
              "change": "what the request proposes to change, which cases it moves, what to review before deciding",
              "effect": "who or what would be affected and how (the rule before vs after)",
              "premise": "something the question assumes (a person, title, sign-off, item, revision or step) that the facts may not show",
              "next": "what happens next, from the current or blocked step; later steps conditional",
              "reason": "why the request was made, its business meaning, or why it is where it is",
              "how": "how the person can do or record something on this request, with its controls",
              "action": "a request that the assistant performs an action (it never does)",
              "other": "another part about this request"}
MAX_ASKS, MAX_ASK_CHARS, MAX_UNRECORDED, MAX_UNRECORDED_CHARS = 4, 200, 4, 80


def _parts_of(f: Mapping) -> set:
    k, key = f["kind"], str(f.get("key") or "")
    out = set()
    if k in ("state", "step", "stage_reason", "reason_gap", "blocker"):
        out.add("status")
    if k in ("position", "obligation", "action"):
        out.add("obligation")
    if k in ("decision_status", "decision"):
        out.add("decision")
    if k in ("rule_change", "request"):
        out.add("change")
    if k in ("impact", "rule_change", "request"):                                        # context/10: the after side is in the proposal, not in the impact facts
        out.add("effect")
    if k in ("position", "state", "decision_status", "request", "blocker", "obligation") or key == "steps:later":   # R17: what bears on a premise: the step, what follows, what stands, what the change is about, a blocker; context/13: the recorded steps (obligations)
        out.add("premise")
    if key in ("steps:later", "position", "steps", "decision:options") or k in ("decision_status", "state", "stage_reason", "blocker", "open_question"):   # context/11: where it is now, then later
        out.add("next")
    if k in CAUSE_FACTS or k == "request":
        out.add("reason")
    if k in ("action", "obligation", "decision", "blocker", "open_question") or key == "decision:options":
        out.add("how")
    return out


def basis_map(facts: Iterable[Mapping]) -> dict:
    """part kind -> the ids of this pack's facts that answer it (deterministic from fact kinds and keys; the same rule the prompt shows)."""
    out = {p: [] for p in PARTS if p not in ("action", "other")}
    for f in facts:
        for p in _parts_of(f):
            out[p].append(f["id"])
    return out


def basis_text(facts: Iterable[Mapping]) -> str:
    return "\n".join(f"{p} ({PART_WORDS[p]}): {', '.join(ids) or '(no fact)'}" for p, ids in basis_map(facts).items())


def parties_fact(actors: Iterable[str], *, ranged: bool = True) -> dict:
    """context/7 (R16, A128 H4-09): who the record names as affected by the proposed change. The stored draft and the impact walk name rules, the actor who applies
    each rule and the cases that move; no part of the record names a person, team or customer group as affected, so this says so and names the actor as the one
    who applies the rule, not by that the one affected. One fact, from the stored draft only. context/10: the computed range is named only when the pack holds one
    (`ranged`); otherwise the rule's before / after says what would change."""
    acts = sorted({str(a) for a in actors if a})
    who = f" The actor of the changed rule(s), who applies the rule ({', '.join(acts)}), is not by that the one affected." if acts else ""
    where = "given by the computed range fact." if ranged else "given by the rule's before / after."
    return fact("impact", "Who the record names as affected by the proposed change: no person, team or customer group is named; which cases would change is "
                + where + who, "computed", {"named": []}, key="parties", tier="support")


_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
_STOP = frozenset(("the a an and or of for to in on at by with from this that these those is are was were be been being has have had do does did not no any our my your their its it "
                   "they them he she his her who whom whose which what when where how before after under over also still yet again then than too very can could would should "
                   "will may might must need needs just only all some more most much many one two first last next now here there please tell say does don't isn't").split())


def _record_words(facts: Iterable[Mapping]) -> set:
    return {w.lower() for f in facts for w in _WORD.findall(f"{f.get('text') or ''} {f.get('display') or ''}")}


def _in_record(word: str, record: set) -> bool:
    w = word.lower()
    return w in record or any(r.startswith(w[:5]) and w.startswith(r[:5]) for r in record if len(w) >= 5 and len(r) >= 5)


def verify_unrecorded(entries: Iterable, question: str, facts: Iterable[Mapping]) -> tuple:
    """-> (verified names, rejected). A declared name is verified as outside the record only when it is a phrase of the question itself (provenance: the person
    brought it), the phrase occurs in no fact, and at least one of its content words occurs in no fact (so the record cannot hold it under another grammar; a
    word shared by its first five letters counts as held). Anything else is not verified: the record may hold it under other words and the model must cite."""
    facts = list(facts)
    record, q = _record_words(facts), " ".join((question or "").split()).lower()
    text = " ".join(f"{f.get('text') or ''} {f.get('display') or ''}" for f in facts).lower()
    kept, rejected, words = [], [], {}
    for i, e in enumerate(entries or []):
        t = " ".join(str(e).split()).strip(" .,;:!?\"'")
        low = t.lower()
        ws = [w for w in _WORD.findall(t) if w.lower() not in _STOP]
        absent = [w for w in ws if not _in_record(w, record)]
        if not t or len(t) > MAX_UNRECORDED_CHARS or _NOT_PLAIN.search(t):
            rejected.append({"unrecorded": i, "kind": "unrecorded", "reason": "not_plain_text"})
        elif low not in q:
            rejected.append({"unrecorded": i, "kind": "unrecorded", "reason": "not_from_the_question"})
        elif not ws or low in text or not absent:
            rejected.append({"unrecorded": i, "kind": "unrecorded", "reason": "record_may_hold_it"})
        elif low not in {k.lower() for k in kept}:
            kept.append(t)
            words[t] = [w.lower() for w in absent]
    return kept, rejected, words


def mentions_unrecorded(text: str, verified: Iterable[str], words: Mapping) -> bool:
    """A model sentence that names a verified-unrecorded name (the phrase, or one of its words the record lacks) says something the record cannot support; the
    server has already stated the absence."""
    low = (text or "").lower()
    toks = {w.lower() for w in _WORD.findall(text or "")}
    return any(v.lower() in low or any(w in toks for w in words.get(v, ())) for v in verified)


def absence_blocks(verified: Iterable[str], facts: list) -> list:
    """The server's own statement for each verified name, cited to the request's state fact (the record as a whole was searched)."""
    anchor = next((f for f in facts if f["kind"] == "state"), facts[0] if facts else None)
    return [{"text": f"The record of this request does not name: {v}.", "fact_refs": [anchor["id"]] if anchor else [], "kind": "absence", "origin": "task_record"} for v in verified]


def record_quotation(text: str, cited: list) -> dict | None:
    """Exact server-owned position text only, not semantic approval of arbitrary prose.

    No substring matching of a model block: a prefix copied from a fact cannot license
    an added claim. Position facts are produced by the reader-scoped task projection.
    Rendering a matched quotation as task_record keeps it out of model-only scoring.
    """
    def norm(value):
        return " ".join(value.split()).rstrip(".").casefold()
    for f in cited:
        if f.get("kind") != "position" or f.get("source") != "task_view":
            continue
        choices = [f["text"]]
        asked = (f.get("data") or {}).get("asked")
        if isinstance(asked, list) and all(isinstance(x, str) for x in asked):
            head = position_head(asked)
            if f["text"].startswith(head):
                choices.append(head)
        # A complete bounded clause generated by position_fact, not a name/role list.
        if (f.get("data") or {}).get("asked") == []:
            clause = "no current step of this request is yours"
            if "(" + clause + ";" in f["text"]:
                choices.append(clause + ".")
        for canonical in choices:
            if norm(text) == norm(canonical):
                return {"text": canonical, "fact_refs": [f["id"]], "kind": "authority", "origin": "task_record"}
    return None


ACTION_REFUSAL = ("Assistant policy: this assistant only reads and explains; it does not execute the requested action. "
                  "Request actions, where available to you, use this request's own controls.")


def check_answer(model: Mapping, pack_: Mapping, limits: Mapping, *, legacy: bool = False, question: str | None = None) -> dict:
    """-> {status, outcome, blocks, rejected, limitations, ...}. A block is kept whole or refused with its reason, never edited. `status` is the server's verdict:
    `answered`, `partial` (some block refused, a part of the question not answered, or the model said partial), `insufficient_context`, `out_of_scope`, or
    `refused` (the model claimed an answer but nothing survived). answer/6: `model` is the validated object (blocks flattened with their part; `asks`; `unrecorded`)."""
    facts = {f["id"]: f for f in pack_["facts"]}
    pure_action = not legacy and bool(model.get("asks")) and all(a.get("part") == "action" for a in model["asks"])
    premise = any(a.get("part") == "premise" for a in model.get("asks") or [])             # checks/21: names are stated and guarded only with a premise part
    verified, u_rejected, u_words = ([], [], {}) if (legacy or not premise) else verify_unrecorded(model.get("unrecorded") or [], question or "", pack_["facts"])
    if not legacy and not premise and model.get("unrecorded"):
        u_rejected = [{"unrecorded": i, "kind": "unrecorded", "reason": "no_premise_part"} for i in range(len(model["unrecorded"]))]
    kept, rejected, kept_asks, cited_by_ask = [], list(u_rejected), set(), {}
    record_quotes = []
    for i, b in enumerate(model["answer_blocks"]):
        text = " ".join(b["text"].split())
        refs = list(dict.fromkeys(b["fact_refs"]))
        cited = [facts[r] for r in refs if r in facts]
        quote = record_quotation(text, cited) if not legacy and b["kind"] == "fact" else None      # checks/25: any part (A160 H3-04: the quote under "next" was refused)
        reason = None
        if pure_action:
            reason = "execution_refusal_owned_by_policy"
        elif not text or len(text) > int(limits["max_block_chars"]) or _NOT_PLAIN.search(text):
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
        elif not legacy and b["kind"] == "cause" and any(f["kind"] == "decision" and f.get("tier") == "history" for f in cited) and not _PAST_MARK.search(text):
            reason = "outdated_decision_as_cause"                                             # checks/15 (A104 H-13): a decision that is no longer current is not why it is here now
        elif not legacy and status_problem(text, pack_["facts"]):
            reason = "status_contradicts_record"                                              # checks/12: "underway" / "stopped" against the recorded phases
        elif not legacy and proposal_stated_as_in_effect(text, cited):
            reason = "proposal_stated_as_in_effect"                                           # checks/14: what is asked is never shown as what is
        elif verified and b.get("part") == "premise" and mentions_unrecorded(text, verified, u_words):
            reason = "unrecorded_name_in_prose"                                               # checks/20: the server states the absence; nothing else about it is supported
        elif quote is not None:
            record_quotes.append({**quote, "part": b["part"], "ask": b.get("ask")})
            if b.get("part") == "obligation":                                                 # checks/25: server text answers only an obligation part
                kept_asks.add(b.get("ask"))
            continue
        else:                                                                               # a wrong claim keeps its specific reason; a right one is still refused in free text:
            reason = authority_problem(text, cited, b["kind"]) or free_text_authority(text)   # authority is stated only through typed assertions (AIG-01)
            if reason == "authority_in_free_text" and not legacy and honest_absence(text, pack_["facts"], verified, cited, limitation=False):
                reason = None                                                                 # checks/27: the parties restatement
        if reason:
            rejected.append({"index": i, "kind": b["kind"], "reason": reason, **({"part": b["part"]} if b.get("part") else {})})
            continue
        kept.append({"text": text, "fact_refs": refs, "kind": b["kind"], **({"part": b["part"], "ask": b.get("ask")} if b.get("part") else {})})
        kept_asks.add(b.get("ask"))
        cited_by_ask.setdefault(b.get("ask"), set()).update(refs)
    if legacy:                                                                              # answer/2-4 objects (replays): model-chosen assertions, checked
        authority, a_rejected = check_assertions(model.get("authority_assertions") or [], facts)
        rejected += a_rejected
        accounting = authority_accounting(model.get("authority_assertions") or [], a_rejected, authority)
    else:                                                                                   # answer/5-6: the record states the authority; the model only explains
        authority = authority_summary(pack_["facts"]) + absence_blocks(verified, pack_["facts"])
        authority += record_quotes
        accounting = {"source": "task_record", "shown_authority": len(authority), "absences_stated": len(verified),
                      "model_prose_authority_refused": sum(1 for r in rejected if r["reason"] in AUTHORITY_TEXT_REASONS)}
    model_kept = kept if not legacy else authority + kept                                  # answer/5: the record's section never stands in for the model's answer
    kept = authority + kept                                                                 # the server-rendered sentences first, then the model's explanation
    lims, support = [], []
    for j, x in enumerate([] if pure_action else model["limitations"][: int(limits["max_limitations"])]):        # checks/10: the model's limitations are prose the reader sees: same authority guard
        x = " ".join(x.split())[:300]
        if not x or _NOT_PLAIN.search(x):
            continue
        why = (free_text_authority(x) or ("status_contradicts_record" if status_problem(x, pack_["facts"]) else None)) if not legacy else None   # checks/21: a limitation may name it
        basis = honest_absence_basis(x, pack_["facts"], verified, [], limitation=True) if why == "authority_in_free_text" else None
        if basis:
            why = None                                                                        # checks/27: an honest absence of a time, a later-step holder or a verified-absent name
            if basis["fact_refs"] or basis["names"]:                                          # checks/30: what it rests on is shown with it (the model's words unchanged)
                support.append({"text": x, "basis": basis["basis"], "fact_refs": basis["fact_refs"],
                                **({"server_statement": [f"The record of this request does not name: {n}." for n in basis["names"]]} if basis["names"] else {})})
        if why:
            rejected.append({"limitation": j, "kind": "limitation", "reason": why})
            continue
        if basis is None and not legacy:
            restated = fact_restatement(x, pack_["facts"])
            if restated:                                                                      # checks/31 (H8-33 / H8-38): a kept limitation that restates a pack fact verbatim shows that fact
                support.append({"text": x, "basis": "fact_restated", "fact_refs": restated})
        lims.append(x)
    if pure_action:
        lims.append(ACTION_REFUSAL)
    if not legacy:
        accounting["model_prose_authority_refused"] = sum(1 for r in rejected if r["reason"] in AUTHORITY_TEXT_REASONS)
    if not legacy and any(r["reason"] == "cause_without_cause_fact" for r in rejected):     # checks/10: a withheld "why" is said, never silent; nothing is made up for it
        lims.append("An explanation of why was not shown: " + ("it did not cite the fact that records the reason." if cause_basis(pack_["facts"])
                                                              else "the facts given to the assistant record no reason (no blocker, decision, open question, rule change or impact)."))
    outcome = model["outcome"]
    uncovered = []
    if not legacy and outcome in ("answered", "partial"):                                   # checks/20: a part of the question with no kept block is said, never silent
        for k, a in enumerate(model.get("asks") or []):
            if k not in kept_asks and a["part"] not in NEVER_OPEN_PARTS:                          # R18: a request to act is answered by the decline limitation, not disclosed as unanswered
                ask = " ".join(str(a.get("ask") or "").split())
                ok = ask and not _NOT_PLAIN.search(ask) and not free_text_authority(ask) and not status_problem(ask, pack_["facts"])
                uncovered.append(ask if ok else f"a part of the question ({PART_WORDS[a['part']].split(' (')[0]})")
    for u in uncovered:
        lims.append(f"Not answered in this answer: {u}.")
    if pure_action:
        status = "refused"
    elif outcome in ("answered", "partial") and not model_kept and not any(q.get("part") == "obligation" for q in record_quotes):
        status = "refused"
    elif outcome == "answered" and (rejected or uncovered):
        status = "partial"
    else:
        status = outcome
    explained = sorted({facts[r]["kind"] for blk in kept if blk.get("origin") != "task_record" and blk["kind"] != "authority" for r in blk["fact_refs"] if facts[r]["kind"] in EXPLANATION_FACTS})
    coverage = blocker_coverage(kept, pack_["facts"])
    shown_unexplained = [] if outcome == "out_of_scope" else coverage["unexplained"][:MAX_UNEXPLAINED_SHOWN]   # a question not about this request gets no blocker notes
    cov = pack_.get("coverage") or {}
    omitted = sum(int(t.get("omitted") or 0) for t in (cov.get("tiers") or {}).values()) + sum(int(v) for k, v in (cov.get("upstream") or {}).items() if k.endswith("_omitted") and isinstance(v, int))
    if not legacy and outcome != "out_of_scope" and omitted:                               # the reader is told the assistant did not see everything (counts only)
        lims.append(f"Some facts about this request were not available to the assistant: {omitted} omitted.")
    for fid in shown_unexplained:                                                         # AIG-03 v5: an offered blocker is never dropped silently; nothing is made up for it
        f = facts[fid]
        lims.append("Not explained in this answer: " + " ".join(str(f.get("display") or f["text"]).split()).removeprefix("Blocker: ")[:240])
    out = {"status": status, "outcome": outcome, "blocks": kept, "rejected": rejected, "limitations": lims, "explained_fact_kinds": explained, "blocker_coverage": coverage,
           **({"limitation_support": support} if not legacy else {}),
           "authority_accounting": accounting}
    if pure_action:
        out["policy_refusal"] = {"origin": "assistant_policy", "reason": "read_only_assistant",
                                 "model_limitations_withheld": len(model["limitations"])}
    if not legacy:
        out["parts"] = {"asked": [a["part"] for a in model.get("asks") or []], "not_answered": len(uncovered), "unrecorded_verified": verified,
                        "coverage": [{"ask": " ".join(str(a.get("ask") or "").split()), "part": a["part"], "blocks": sum(1 for b in model_kept if b.get("ask") == k),
                                      **({"record_blocks": sum(1 for b in record_quotes if b.get("ask") == k and b.get("part") == "obligation")} if any(b.get("ask") == k and b.get("part") == "obligation" for b in record_quotes) else {}),
                                      "cited": sorted(cited_by_ask.get(k, ()), key=lambda r: int(r[1:]) if r[1:].isdigit() else 0)} for k, a in enumerate(model.get("asks") or [])]}
    return out


# ---- checks/22 (R17): the parts a first answer leaves open, and at most one supplementary call for them ----------------------------------------------------
# Server signals only (no oracle, no model self-report): an ask with no kept block, or an ask of a kind that asks for substance whose kept blocks cite none of
# the facts that answer that kind although the pack holds some (A134 H3-13: a how question answered from the allowed action alone while the decision with the
# reviewer's point was in the pack; A134 H4-08: a why question answered from the current rule alone while the request, the draft and the blocker were in the
# pack; A131 H3-09: a premise part answered from the stage alone while the expected steps were in the pack). The frame facts (stage, steps so far, stage
# reason, actions, axes) say where the request stands and what is allowed; they are the substance of a status or obligation part and of no other.
FRAME_KINDS = frozenset({"state", "step", "stage_reason", "reason_gap", "action", "axis"})
BLOCK_ONLY_PARTS = frozenset({"status", "obligation", "other"})
NEVER_OPEN_PARTS = frozenset({"action"})                          # R18: a request to act is answered by the decline limitation; never a reason for a second call
SERVER_NOTE_PREFIXES = ("Not answered in this answer: ", "Not explained in this answer: ", "Some facts about this request were not available", "An explanation of why was not shown: ",
                        "A completion of the parts left open ")
SUPPLEMENT_NOTE = {"timeout": "A completion of the parts left open did not arrive in time; the answer above is what the first pass gave.",
                   "invalid": "A completion of the parts left open was not usable; the answer above is what the first pass gave.",
                   "provider_failed": "A completion of the parts left open could not be obtained; the answer above is what the first pass gave.",
                   "no_call_budget": "A completion of the parts left open was not allowed by the call budget; the answer above is what the first pass gave.",
                   "not_configured": "A completion of the parts left open is not configured on this server; the answer above is what the first pass gave."}


def answering_facts(part: str, facts: Iterable[Mapping]) -> list:
    """The pack's facts whose content answers a part of that kind beyond the frame: the basis minus the frame kinds; for `reason` also minus the current rule
    ("Rule to change: ..."), which says what is, not why a change is asked. Steps expected after the current one count (they answer next / premise)."""
    out = []
    for f in facts:
        if part not in _parts_of(f) or (f["kind"] in FRAME_KINDS and not (f["kind"] == "step" and f.get("key") == "steps:later")):
            continue
        if part == "reason" and str(f.get("key") or "").startswith("rule_change:scope:"):
            continue
        out.append(f["id"])
    return out


def open_parts(result: Mapping, facts: Iterable[Mapping]) -> list:
    """-> [{index, ask, part, signal, facts}] for every ask the checked answer leaves open: `no_block` (nothing of the model's kept for it) or `frame_only`
    (a part that asks for substance, answered only from frame facts while answering facts exist). Deterministic from the stored coverage and the pack."""
    facts = list(facts)
    out = []
    for k, c in enumerate(((result.get("parts") or {}).get("coverage")) or []):
        if c["part"] in NEVER_OPEN_PARTS or (c["part"] == "obligation" and c.get("record_blocks", 0)):
            continue
        ans = answering_facts(c["part"], facts) if c["part"] not in BLOCK_ONLY_PARTS else []
        if c["blocks"] == 0:
            out.append({"index": k, "ask": c["ask"], "part": c["part"], "signal": "no_block", "facts": ans or basis_map(facts).get(c["part"], [])})
        elif ans and not set(c["cited"]) & set(ans):
            out.append({"index": k, "ask": c["ask"], "part": c["part"], "signal": "frame_only", "facts": ans})
    return out


_SUPPLEMENT_PLACEHOLDER = re.compile(r"\{(perspective|as_of|authority|facts|question|answered|open)\}")


def supplement_prompt(frozen: Mapping, pack_: Mapping, question: str, result: Mapping, open_: list, *, perspective: str) -> str:
    """The second call's prompt: the same pack and authority, the question, what the first pass already answered (not to be repeated) and the open parts with the
    facts that answer each. Same placeholder discipline as `build_prompt` (one pass, data neutralised)."""
    facts = "\n".join(f"{f['id']} [{f['kind']}] {f['text']}" for f in pack_["facts"]) or "(none)"
    authority = "\n".join(b["text"] for b in authority_summary(pack_["facts"])) or "(none)"
    answered = "\n".join(f"- ({b.get('part') or 'part'}) {b['text']}" for b in result.get("blocks") or [] if b.get("origin") != "task_record" and b["kind"] != "authority") or "(nothing kept)"
    answered += "\nIts limitations (restate in your limitations only the ones still true after your answer; drop one your answer resolves):\n" + (
        "\n".join(f"- {l}" for l in result.get("limitations") or [] if not l.startswith(SERVER_NOTE_PREFIXES)) or "(none)")
    opened = "\n".join(f"- part {o['part']}: {o['ask']} (facts that answer this kind of part: {', '.join(o['facts']) or 'none in this request'})" for o in open_)
    values = {"perspective": perspective, "as_of": str(pack_.get("as_of")), "authority": authority, "facts": facts, "question": question, "answered": answered, "open": opened}
    prompt = _SUPPLEMENT_PLACEHOLDER.sub(lambda m: _neutralise(values[m.group(1)]), frozen["supplement_template"])
    if len(prompt) > int(frozen["limits"]["max_prompt_chars"]):
        raise ValueError("context_too_large")
    return prompt


def merge_supplement(first: Mapping, second: Mapping | None, open_: list, outcome: str, facts: list) -> dict:
    """The one answer the person sees after a supplementary call: the first pass's blocks, then the kept blocks of the second (marked `call: 2`), every refusal of
    both, the first pass's notes minus "Not answered" for a part the second pass covered, the second pass's own limitations, and a server note when the second
    pass gave nothing usable (`outcome` other than completed). Status: refused when nothing of the model's survived, partial when something was refused or
    a part stays open, else the first pass's outcome. The authority section and absence statements stay those of the first pass."""
    blocks = list(first["blocks"])
    rejected = list(first["rejected"])
    lims = list(first["limitations"])
    covered = set()
    if second is not None:
        seen = {b["text"] for b in first["blocks"]}
        added = [dict(b, call=2) for b in second["blocks"] if b.get("origin") != "task_record" and b["kind"] != "authority" and b["text"] not in seen]   # never a repeat of the first pass
        blocks += added
        rejected += [dict(r, call=2) for r in second["rejected"]]
        covered = {b.get("part") for b in added if b.get("part")}
        for o in open_:
            if o["signal"] == "no_block" and o["part"] in covered:
                lims = [l for l in lims if l != f"Not answered in this answer: {o['ask']}."]
        second_model_lims = [l for l in second["limitations"] if not l.startswith(SERVER_NOTE_PREFIXES)]
        if covered:                                                                             # R18: one consistent "Not covered": the second pass saw the first's limitations and restated the ones still true
            lims = [l for l in lims if l.startswith(SERVER_NOTE_PREFIXES)] + second_model_lims
        else:
            lims += [l for l in second_model_lims if l not in lims]
    if outcome != "completed":
        lims.append(SUPPLEMENT_NOTE[outcome])
    model_blocks = [b for b in blocks if b.get("origin") != "task_record" and b["kind"] != "authority"]
    still_open = [l for l in lims if l.startswith("Not answered in this answer: ")]
    if first["outcome"] in ("answered", "partial") and not model_blocks and not any(c.get("record_blocks", 0) for c in (first.get("parts") or {}).get("coverage", [])):
        status = "refused"
    elif first["outcome"] == "insufficient_context":
        status = "partial" if covered else first["status"]                                     # R18: the second pass answered from the same pack what the first called insufficient
    elif first["outcome"] in ("answered", "partial") and (rejected or still_open or first["outcome"] == "partial"):
        status = "partial"
    else:
        status = first["outcome"]                                                              # every part answered and nothing refused across both passes
    fmap = {f["id"]: f for f in facts}
    explained = sorted({fmap[r]["kind"] for b in model_blocks for r in b["fact_refs"] if r in fmap and fmap[r]["kind"] in EXPLANATION_FACTS})
    parts = dict(first.get("parts") or {})
    parts["supplement"] = {"open": [{"part": o["part"], "ask": o["ask"], "signal": o["signal"]} for o in open_], "outcome": outcome, "covered": sorted(covered),
                           "blocks_added": len(model_blocks) - len([b for b in first["blocks"] if b.get("origin") != "task_record" and b["kind"] != "authority"])}
    return {**first, "status": status, "blocks": blocks, "rejected": rejected, "limitations": lims, "explained_fact_kinds": explained,
            "blocker_coverage": blocker_coverage(blocks, facts), "parts": parts}


def blocker_coverage(kept: list, facts: list) -> dict:
    """Which offered blocker facts a KEPT explanation (non-authority block) cites. Computed by the server from the pack, for any request: no expected ids."""
    offered = [f["id"] for f in facts if f["kind"] == "blocker"]
    cited = {r for b in kept if b["kind"] != "authority" and b.get("origin") != "task_record" for r in b["fact_refs"]}
    return {"offered": len(offered), "explained": [i for i in offered if i in cited], "unexplained": [i for i in offered if i not in cited]}


EXPLANATION_FACTS = frozenset({"blocker", "decision", "rule_change", "impact", "review_note", "request", "open_question", "waiting", "stage_reason"})


def semantic_success(result: Mapping) -> bool:
    """AIG-03 scoring rule: an answer counts as semantically useful only when the model's OWN explanation survived the checks, i.e. at least one kept non-authority
    block cites a fact that carries business substance (blocker, decision, rule change, impact, review note, request, waiting). Server-rendered authority sentences
    alone (a template), an all-refused answer, a fallback or an insufficient / out-of-scope outcome are not semantic success."""
    if result.get("status") not in ("answered", "partial"):
        return False
    return any(b.get("kind") != "authority" and b.get("origin") != "task_record" for b in result.get("blocks") or []) and bool(result.get("explained_fact_kinds"))


def cause_basis(facts: Iterable[Mapping]) -> list:
    """The facts of this pack a `cause` block may cite (checks/10): the ones that record a reason. Empty when the pack records none."""
    return [f for f in facts if f["kind"] in CAUSE_FACTS]


_PLACEHOLDER = re.compile(r"\{(perspective|as_of|authority|cause_facts|basis|facts|history|question)\}")
_SECTION_TAG = re.compile(r"<(/?)(facts|history|question|authority)\b", re.I)


def _neutralise(text: str) -> str:
    """A data value cannot open or close one of the prompt's sections: `<facts`, `</question` ... (any case) become `&lt;facts`, `&lt;/question`."""
    return _SECTION_TAG.sub(lambda m: "&lt;" + m.group(1) + m.group(2), text)


def build_prompt(frozen: Mapping, pack_: Mapping, history: Iterable[Mapping], question: str, *, perspective: str) -> str:
    facts = "\n".join(f"{f['id']} [{f['kind']}] {f['text']}" for f in pack_["facts"]) or "(none)"
    hist = "\n".join(f"Q: {h['question']}\nA: {' '.join(b['text'] for b in h.get('blocks') or []) or '(no answer kept)'}" for h in history) or "(none)"
    authority = "\n".join(b["text"] for b in authority_summary(pack_["facts"])) or "(none)"      # answer/5: the server's own statement, shown to the model as data
    causes = ", ".join(f"{f['id']} [{f['kind']}]" for f in cause_basis(pack_["facts"])) or "(none)"   # checks/10: the same rule the validator applies
    values = {"perspective": perspective, "as_of": str(pack_.get("as_of")), "authority": authority, "cause_facts": causes, "facts": facts, "history": hist,
              "question": question, "basis": basis_text(pack_["facts"])}                   # answer/6: which facts answer each kind of part, computed per pack
    # AH-03: ONE pass over the template's own placeholders, so a placeholder written inside the data (a source-derived fact, a question) is never expanded;
    # the section tags inside the data are neutralised (conditional, as `live_analysis._escape_source_delimiters`: data without them is byte-identical).
    prompt = _PLACEHOLDER.sub(lambda m: _neutralise(values[m.group(1)]) if m.group(1) in values else m.group(0), frozen["prompt_template"])
    if len(prompt) > int(frozen["limits"]["max_prompt_chars"]):
        raise ValueError("context_too_large")
    return prompt
