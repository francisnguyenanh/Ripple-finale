"""SYN-04: one read model for where a business change stands, per axis and per step, with the next actor and action. Pure over facts the adapters read.

The axes stay separate and none implies another: the knowledge decision (accepted meaning), the source files (written by the product or by a person, then READ BACK
through the carry-over), the code (a developer's checklist, never verified by the product), behaviour (never tested by the product) and the user's result
decision (not recorded by the business loop in Final). A change is `knowledge_only_complete` when its knowledge is committed, whatever the files show;
it is `fully_implemented` only when the sources were read back as verified and no code item is pending; it is never "done" because a job completed, a document
exists or a query was answered. Documents never block anything (not_requested is a normal state).
"""
from __future__ import annotations

from typing import Mapping, Optional

STEPS = ("understand", "confirm", "review", "commit", "source_sync", "code_sync", "readback", "behaviour", "user_result")
STATES = ("done", "current", "waiting", "blocked", "failed", "redo", "partial", "todo", "not_required", "rejected")
TITLES = {"understand": "Understanding of the request", "confirm": "Confirmation by the person who asked", "review": "Decision on the knowledge change",
          "commit": "Knowledge commit", "source_sync": "Source files", "code_sync": "Code", "readback": "Read-back of the files into the knowledge",
          "behaviour": "Behaviour", "user_result": "Result accepted by the user"}
WHO = {"understand": "the product (model + server checks)", "confirm": "the person who asked", "review": "a change approver (a different person)", "commit": "a knowledge owner",
       "source_sync": "a knowledge owner or an operator (automatic edit) or a person (manual)", "code_sync": "a developer", "readback": "a knowledge owner (accept the derived version)",
       "behaviour": "a person or a test outside the product", "user_result": "the person who asked"}
SCOPES = ("FULL", "NARROW")


def by_key_state(steps, key) -> Optional[str]:
    return next((s["state"] for s in steps if s["key"] == key), None)


def _step(key, state, what="", why="", **extra) -> dict:
    assert state in STATES, state
    return {"key": key, "title": TITLES[key], "state": state, "who": WHO[key], "what": what, "why": why, **extra}


def build_journey(f: Mapping) -> dict:
    """`f` (facts, all optional except `understanding`): understanding{status, ready, can_confirm, clarify}, confirmed, knowledge_decision{action, by}, file_writes_decision,
    knowledge_moved, committed{business_revision}, completion{knowledge_result, source_sync, code_sync, behavioral_verification}, source{automatic_edit_available,
    file_writes_approved, job{status, reason}, manual_count}, readback{target_statuses{rule_id: status}, pending_snapshot}, documents{freshness}, code{supported, uncertain,
    unresolved}, user_result, labels{accepted_target, observed_current}."""
    u = dict(f.get("understanding") or {})
    confirmed = bool(f.get("confirmed"))
    kd, fd = f.get("knowledge_decision"), f.get("file_writes_decision")
    moved = bool(f.get("knowledge_moved"))
    committed = f.get("committed")
    comp = dict(f.get("completion") or {})
    source = dict(f.get("source") or {})
    rb = dict(f.get("readback") or {})
    docs = dict(f.get("documents") or {})
    code = dict(f.get("code") or {})
    steps = []

    # 1. understanding
    st = u.get("status")
    if st == "done" and u.get("ready") and not u.get("clarify"):
        steps.append(_step("understand", "done", "The request was understood as validated operations.", ""))
    elif st == "done":
        steps.append(_step("understand", "blocked", "The request has to be restated.", "The understanding asks a question or could not map a part of the request; nothing is confirmable."))
    elif st == "failed":
        steps.append(_step("understand", "failed", "The understanding failed.", str(u.get("reason") or "")))
    elif st == "cancelled":
        steps.append(_step("understand", "failed", "The understanding was cancelled.", ""))
    else:
        steps.append(_step("understand", "current", "The product is reading the request.", "A model proposal is validated by the server before anything is shown as understood."))

    # 2. confirmation
    if confirmed:
        steps.append(_step("confirm", "done", "The person who asked confirmed the understanding.", ""))
    elif steps[0]["state"] == "done" and u.get("can_confirm"):
        steps.append(_step("confirm", "current", "Confirm that this is what you mean.", "Confirming records the request only; no rule, file or code changes."))
    else:
        steps.append(_step("confirm", "todo", "Not confirmable yet.", ""))

    # 3. review (knowledge decision)
    rejected = kd is not None and kd.get("action") == "reject"
    if rejected:
        steps.append(_step("review", "rejected", "The knowledge change was rejected.", "A rejected change stays rejected; a new request starts a new change.", by=kd.get("by")))
    elif kd is not None and kd.get("action") == "approve":
        steps.append(_step("review", "done", "The knowledge change was accepted by a reviewer.", "", by=kd.get("by")))
    elif kd is not None and kd.get("action") == "request_changes":
        steps.append(_step("review", "redo", "Changes were requested.", "The person who asked restates or clarifies; the reviewer decides again on the revised draft.", by=kd.get("by")))
    elif confirmed and moved and not committed:
        steps.append(_step("review", "blocked", "The business knowledge moved after this request was confirmed.", "State and confirm the request again against the current knowledge."))
    elif confirmed:
        steps.append(_step("review", "current", "A change approver reviews the meaning, impact and draft and decides.", "Accepting the knowledge change and approving automatic file edits are separate decisions."))
    else:
        steps.append(_step("review", "todo", "", ""))

    # 4. commit
    if committed:
        steps.append(_step("commit", "done", f"Committed as business version {committed.get('business_revision')}.", "The decided meaning is the knowledge from now on; files and code are separate axes."))
    elif rejected:
        steps.append(_step("commit", "not_required", "Nothing to commit for a rejected change.", ""))
    elif steps[2]["state"] == "done":
        steps.append(_step("commit", "current", "A knowledge owner commits the accepted change.", "Only the knowledge changes; pending file and code work is listed."))
    else:
        steps.append(_step("commit", "todo", "", ""))

    # 5. source files
    ss = comp.get("source_sync")
    job = source.get("job") or None
    if rejected:
        steps.append(_step("source_sync", "not_required", "No file follows a rejected change.", ""))
    elif ss == "not_required":
        steps.append(_step("source_sync", "not_required", "No source file carries this rule.", "Knowledge-only change."))
    elif ss == "verified_unchanged":
        steps.append(_step("source_sync", "done", "The files already say what was decided.", ""))
    elif not committed:
        steps.append(_step("source_sync", "todo", "After the commit: the approved automatic edit, or a manual edit.", ""))
    elif job is not None and job.get("status") == "COMPLETED":
        steps.append(_step("source_sync", "partial", "The approved edit was written as a new source revision.", "Published bytes are not yet read back into the knowledge: not verified until the derived version is accepted."))
    elif job is not None and job.get("status") == "FAILED":
        steps.append(_step("source_sync", "failed", "The automatic edit failed and published nothing.", str(job.get("reason") or ""), ))
    elif job is not None:
        steps.append(_step("source_sync", "current", "The automatic edit is running.", ""))
    else:
        what = "Choose: let the product write the approved edit, or edit the files by hand and refresh the sources." if source.get("automatic_edit_available") else "A person edits the files and refreshes the sources."
        why = "" if source.get("file_writes_approved") or not source.get("automatic_edit_available") else "The automatic edit needs its own approval (file_writes) before it can be written."
        steps.append(_step("source_sync", "current", what, why, manual_count=source.get("manual_count", 0)))

    # 6. code
    cs = comp.get("code_sync")
    if rejected or cs == "not_required":
        steps.append(_step("code_sync", "not_required", "No code location carries this rule." if not rejected else "", ""))
    elif cs == "verified_unchanged":
        steps.append(_step("code_sync", "done", "The code already implements the decided rule (read back).", ""))
    else:
        steps.append(_step("code_sync", "waiting", "A developer applies the recommendations; the product never edits code.", "Not verified by the product.",
                           supported=code.get("supported", 0), uncertain=code.get("uncertain", 0), unresolved=code.get("unresolved", 0)))

    # 7. read-back
    statuses = dict(rb.get("target_statuses") or {})
    if rejected or not committed:
        steps.append(_step("readback", "todo" if not rejected else "not_required", "", ""))
    elif statuses and all(s == "verified" for s in statuses.values()):
        steps.append(_step("readback", "done", "The current files were read back and match the decided rule.", "Source-aligned, not behaviour.", statuses=statuses))
    elif any(s == "divergent" for s in statuses.values()):
        steps.append(_step("readback", "blocked", "The current files state something different from the decided rule.", "A person reconciles the files or the decision.", statuses=statuses))
    elif rb.get("pending_snapshot"):
        steps.append(_step("readback", "partial", "A derived knowledge version with the read-back is waiting for acceptance.", "Accepting it is a separate human decision (KNO-05).", statuses=statuses))
    elif by_key_state(steps, "source_sync") == "partial":
        steps.append(_step("readback", "current", "Run the analysis to read the published files back, then accept the derived knowledge version.", "Published bytes count only once they are read back and accepted.", statuses=statuses))
    else:
        steps.append(_step("readback", "waiting", "The next analysis after a file change reads the files back.", "", statuses=statuses))

    # 8. behaviour, 9. user result
    bv = comp.get("behavioral_verification")
    steps.append(_step("behaviour", "done" if bv == "verified" else "todo", "Behaviour is not tested by the product." if bv != "verified" else "Verified outside the product.", ""))
    ur = f.get("user_result")
    steps.append(_step("user_result", "done" if ur == "accept" else ("rejected" if ur == "reject" else "todo"), "No result decision is recorded for this change." if not ur else f"Result decision: {ur}.", ""))

    scope = "NARROW" if comp.get("source_sync") == "not_required" and comp.get("code_sync") == "not_required" else "FULL"
    by_key = {s["key"]: s for s in steps}
    nxt = next((s for s in steps if s["state"] in ("current", "redo", "blocked", "failed")), None)
    if rejected:
        nxt = None
    knowledge_only_complete = bool(committed)
    fully_implemented = bool(committed) and by_key["readback"]["state"] == "done" and by_key["source_sync"]["state"] in ("done", "partial", "not_required") and by_key["code_sync"]["state"] in ("done", "not_required")
    return {"scope": scope, "steps": steps, "next": None if nxt is None else {"step": nxt["key"], "who": nxt["who"], "what": nxt["what"], "why": nxt["why"], "state": nxt["state"]},
            "knowledge_only_complete": knowledge_only_complete, "fully_implemented": fully_implemented, "behaviour_verified": bv == "verified", "rejected": rejected,
            "documents": {"freshness": docs.get("freshness", "not_requested"), "blocks": False, "statement": "Documents are views generated on request; they never gate a change."},
            "labels": dict(f.get("labels") or {}),
            "statement": ("The knowledge change is committed: the decided meaning answers questions now, even while the files still show the old value. "
                          if committed else "The knowledge change is not committed yet. ")
                         + ("Fully implemented in the sources (read back and verified); behaviour is a separate, untested axis." if fully_implemented
                            else "Not fully implemented: source or code work is pending or not yet read back. A completed job, an existing document or an answered question is not completion.")}
