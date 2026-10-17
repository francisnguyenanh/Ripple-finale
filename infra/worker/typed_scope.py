"""Which files a person NAMED in their own words, read by the server (no model) so the exact files are shown before they confirm.

Only files that exist in the current source revision and whose file name really appears in the typed text are returned (nothing invented, nothing widened).
The role is a reading of the words around the name: "in_scope" (limited to / only), "not_changed" (do not / not / leave / except / without) or "mentioned" (no cue).
It narrows nothing and widens nothing: the verbatim text is stored next to it and stays the authority for what the person said."""
from __future__ import annotations

import re

_NEG = re.compile(r"\b(do\s+not|don't|dont|does\s+not|not|never|without|except|excluding|exclude|leave|leaving|skip|keep\s+\S+\s+unchanged|unchanged)\b", re.I)
_ONLY = re.compile(r"\b(only|limited\s+to|limit(?:ed)?\s+(?:this\s+change\s+)?to|just|restricted\s+to|restrict\s+to)\b", re.I)
_BOUNDARY = re.compile(r"[.;:\n!?]")


def known_files(view_artifacts) -> list[dict]:
    """The files of a source revision view: name (last path part), logical path and artifact id."""
    out = []
    for a in view_artifacts or []:
        path = str(a.get("logical_path") or "")
        out.append({"name": path.replace("\\", "/").split("/")[-1], "logical_path": path, "artifact_id": a.get("artifact_id")})
    return [x for x in out if x["name"]]


def files_in_text(text: str, known: list[dict]) -> list[dict]:
    text = str(text or "")
    hits = []
    for k in known:
        for m in re.finditer(r"(?<![\w.\-])" + re.escape(k["name"]) + r"(?![\w\-]|\.\w)", text, re.I):
            hits.append((m.start(), m.end(), k))
    hits.sort(key=lambda h: h[0])
    out, seen, prev_end, prev_role = [], set(), 0, "mentioned"
    for start, end, k in hits:
        seg = text[prev_end:start]
        cut = max((mm.end() for mm in _BOUNDARY.finditer(seg)), default=0)
        clause = seg[cut:]
        if _BOUNDARY.search(seg) and not clause.strip():
            clause = ""
        if _NEG.search(clause):
            role = "not_changed"
        elif _ONLY.search(clause):
            role = "in_scope"
        elif not _BOUNDARY.search(seg) and prev_role != "mentioned":
            role = prev_role                      # "do not update A or B": B shares the cue of A
        else:
            role = "mentioned"
        prev_end, prev_role = end, role
        if k["artifact_id"] in seen:
            continue
        seen.add(k["artifact_id"])
        out.append({"name": k["name"], "logical_path": k["logical_path"], "artifact_id": k["artifact_id"], "role": role, "quote": text[start:end]})
    return out
