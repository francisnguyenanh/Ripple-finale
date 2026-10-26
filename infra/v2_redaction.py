"""Server-side redaction of what a signed-in member receives. Presentation is never the control: the data is removed here,
before the response leaves the server.

The raw source quotes of CLAIMS (what the AI extracted from a file) belong to `evidence.read` (see /v2/hitl/queue and
/v2/knowledge/overview). The job record of an extraction carries the same quotes, so it gets the same treatment.
"""
from __future__ import annotations

import copy

from infra.adapters.v2_policy import permissions_for

QUOTE_KEYS = ("quotes", "quote", "evidence", "evidence_quotes")


def can_read_evidence(authority: dict) -> bool:
    return "evidence.read" in permissions_for(authority.get("roles"))


# Source text echoed inside AI outputs (a draft's citation, an answer's evidence span, a finding's quote). A member without `evidence.read` keeps the
# business reading, the file, the location and the grounding status; the verbatim source text is removed from THIS response only (the stored
# output and its history are untouched). Kinds whose "quote" is the person's own words are not touched.
USER_TEXT_KINDS = ("intent.interpret",)
SOURCE_QUOTE_KEYS = ("quote", "quotes", "evidence_quote", "evidence_quotes")


def _strip_quotes(node) -> bool:
    found = False
    if isinstance(node, dict):
        had = False
        for k in SOURCE_QUOTE_KEYS:
            if k in node and node[k] not in (None, "", []):
                node[k] = [] if isinstance(node[k], list) else ""
                had = True
        if had:
            node["quotes_redacted"] = True
            found = True
        for v in list(node.values()):
            found = _strip_quotes(v) or found
    elif isinstance(node, list):
        for v in node:
            found = _strip_quotes(v) or found
    return found


def strip_source_text_of_unaddressed(result) -> bool:
    """A draft's `unaddressed` entries echo a passage of the SOURCE file (`text`). Without `evidence.read` the file, the location and the fact that nobody classified it stay;
    the passage itself is removed from this response (the stored draft is untouched). Applies to carried entries too (same list)."""
    found = False
    if isinstance(result, dict):
        for u in result.get("unaddressed") or []:
            if isinstance(u, dict) and u.get("text"):
                u["text"] = ""
                u["text_redacted"] = True
                found = True
    return found


_PASSAGE_MARK = ": not classified by the model"


def scrub_question(q):
    """A decision remembers the open point it answered (`question`). For an `unaddressed` point that text ended with the source passage: cut it for a member without `evidence.read`."""
    if isinstance(q, str) and _PASSAGE_MARK in q:
        return q.split(_PASSAGE_MARK, 1)[0] + _PASSAGE_MARK + " - (source text hidden for your role)"
    return q


def scrub_clarification_questions(result) -> bool:
    found = False
    if isinstance(result, dict):
        for key in ("clarifications_used", "feedback_log"):
            for c in result.get(key) or []:
                if isinstance(c, dict) and isinstance(c.get("question"), str) and _PASSAGE_MARK in c["question"]:
                    c["question"] = scrub_question(c["question"])
                    c["text_redacted"] = True
                    found = True
    return found


def redact_gaps(gaps: dict, authority: dict) -> dict:
    """The open-points list for a member without `evidence.read`: a gap of kind `unaddressed` keeps path and location, not the source passage printed in its text."""
    if can_read_evidence(authority) or not isinstance(gaps, dict):
        return gaps
    out = copy.deepcopy(gaps)
    for g in out.get("gaps") or []:
        if g.get("kind") == "unaddressed":
            where = str(g.get("text") or "").split(": not classified by the model", 1)[0]
            g["text"] = f"{where}: not classified by the model - (source text hidden for your role)"
            g["text_redacted"] = True
    scrub_clarification_questions(out)
    return out


def strip_raw_model_output(result) -> bool:
    """The raw model object kept on some job results (`model_output`, e.g. an extraction's reading of a file) repeats the source text it read. A member without
    `evidence.read` gets the validated result only; the raw object is removed from THIS response (the stored record is untouched)."""
    if isinstance(result, dict) and result.get("model_output") not in (None, ""):
        result["model_output"] = ""
        result["model_output_redacted"] = True
        return True
    return False


def redact_job_record(record: dict, authority: dict) -> dict:
    """The job record as this member may see it: extraction candidates keep their meaning (subject, value, unit, scope) and lose
    the raw source quotes unless the member holds `evidence.read`. Everything else is returned as it is."""
    if can_read_evidence(authority):
        return record
    if record.get("kind") != "extraction":
        if record.get("kind") in USER_TEXT_KINDS or not isinstance(record.get("execution_result"), dict):
            return record
        out = copy.deepcopy(record)
        stripped = _strip_quotes(out["execution_result"])
        if strip_source_text_of_unaddressed(out["execution_result"]):
            stripped = True
        if scrub_clarification_questions(out["execution_result"]):
            stripped = True
        if strip_raw_model_output(out["execution_result"]):
            stripped = True
        if stripped:
            out["execution_result"]["evidence_redacted"] = True
        _strip_quotes(out.get("payload"))
        # a confirmed KNOWLEDGE decision is shown with the source passage it is about: that passage is source text, hidden like any quote
        for item in out["execution_result"].get("decisions_given") or []:
            if isinstance(item, dict) and item.get("kind") == "knowledge" and item.get("statement"):
                item["statement"] = ""
                item["statement_hidden"] = True
        return out
    out = copy.deepcopy(record)
    res = out.get("execution_result")
    if isinstance(res, dict):
        for key in ("candidates", "rejected"):
            for item in res.get(key) or []:
                if isinstance(item, dict):
                    had = any(item.get(k) for k in QUOTE_KEYS)
                    for k in QUOTE_KEYS:
                        if k in item:
                            item[k] = [] if isinstance(item[k], list) else ""
                    if had:
                        item["quotes_redacted"] = True
        strip_raw_model_output(res)
        res["evidence_redacted"] = True
    return out
