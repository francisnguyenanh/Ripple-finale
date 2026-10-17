"""What PEOPLE have confirmed, given to the Q&A model next to the source text - clearly not source text.

Before this, `gap.answer` read only source observations: a clarification or a knowledge confirmation a person recorded changed drafts and labels
but never an answer. This module builds a bounded, labelled block from two kinds of recorded, ACTIVE (not superseded, not revoked) human decisions:
  * `clarification`: a confirmed answer to an open point of this change's draft (the same rows the next draft already reads as assumptions);
  * `knowledge`: a confirmed extracted rule (candidate) that belongs to one of the files the question searches.
Each item carries who confirmed it, the decision epoch and its status. The block is appended to the QUESTION (the trusted slot of the frozen
prompt), so the frozen prompt file and its pinned hash stay untouched. The rules it states, in words the model reads:
  * a decision is a person's statement, never a quote; `evidence` stays verbatim source text only;
  * a decision does NOT change any file: if it disagrees with a source, say that they disagree;
  * nothing a person typed can instruct the model (the same injection screen as the sources).
The server stores what it gave (`decisions_given`) so the page can show it; whether the model USED a decision is not validated (limit, stated in the UI).
"""
from __future__ import annotations

MAX_ITEMS = 8
_CLIP = 280


def _clip(text, n=_CLIP) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "..."


def build(store, tenant: str, ws: str, *, intent_id, artifact_ids, paths_by_id: dict, max_epoch, patterns) -> dict:
    items, skipped = [], 0
    clar = store.active_gap_clarifications(tenant, ws, intent_id, max_epoch) if intent_id else []
    for c in clar:
        sc = c.get("scope") or {}
        statement = _clip(sc.get("answer") or c.get("rationale") or "")
        question = _clip(sc.get("question") or "")
        if not statement:
            continue
        if any(p.search(statement) or p.search(question) for p in patterns):
            skipped += 1
            continue
        items.append({"kind": "clarification", "decision_id": c["decision_id"], "actor_id": c["actor_id"], "epoch": c["epoch"], "status": "confirmed",
                      "question": question, "statement": statement})
    wanted = set(artifact_ids)
    cand_quote, cand_art = {}, {}
    for job in store.extraction_candidate_index(tenant, ws):
        if job.get("artifact_id") not in wanted:
            continue
        for cand in job["candidates"]:
            cand_art[cand["candidate_id"]] = job["artifact_id"]
            cand_quote[cand["candidate_id"]] = (cand["quotes"] or [""])[0]
    for d in store.active_knowledge_confirmations(tenant, ws, list(cand_art), max_epoch):
        quote = _clip(cand_quote.get(d["target"]))
        if not quote:
            continue
        if any(p.search(quote) for p in patterns):
            skipped += 1
            continue
        items.append({"kind": "knowledge", "decision_id": d["decision_id"], "actor_id": d["actor_id"], "epoch": d["epoch"], "status": "confirmed",
                      "artifact_path": paths_by_id.get(cand_art[d["target"]], cand_art[d["target"]]), "statement": quote})
    items = items[:MAX_ITEMS]
    if not items:
        return {"items": [], "skipped": skipped, "block": ""}
    lines = ["", "", "People's confirmed decisions (recorded by named people; NOT source text; they do not change any file):"]
    for i, it in enumerate(items, 1):
        if it["kind"] == "clarification":
            lines.append(f'{i}. [{it["decision_id"]}] clarification confirmed by {it["actor_id"]} (decision epoch {it["epoch"]}): to "{it["question"]}" they decided: "{it["statement"]}"')
        else:
            lines.append(f'{i}. [{it["decision_id"]}] knowledge confirmed by {it["actor_id"]} (decision epoch {it["epoch"]}): the rule found in {it["artifact_path"]} around "{it["statement"]}" was confirmed as a rule')
    lines.append("A decision is a person's statement, never a quote: keep \"evidence\" for verbatim source text only. A decision does not edit any file. "
                 "If a decision and a source disagree, say that they disagree (abstain with a question for a person) instead of choosing silently.")
    return {"items": items, "skipped": skipped, "block": "\n".join(lines)}
