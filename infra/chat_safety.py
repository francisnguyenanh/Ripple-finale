"""Deterministic scan of a person's chat text (and of model output) for attempts to gain rights or reach outside the request.

It never blocks a legitimate business request and never grants anything: authority comes ONLY from the signed-in session, RBAC,
the model-data policy and server-side checks. A hit adds a short reason code + business wording that the UI shows ("this part is
ignored"), an audit event with the CODE only (never the text), and nothing else. The patterns are narrow on purpose (a role claim, an
override of the rules, skipping a control, secrets, other tenants, running things): the word "admin" or "ignore" alone is not a hit.
Not a claim to stop every jailbreak: the controls are structural (server-side validation, allowlists, separate permissions)."""
from __future__ import annotations

import re

_I = re.I
RULES = [
    ("ROLE_CLAIM", re.compile(r"\b(i\s*am|i'm|acting\s+as|speaking\s+as|you\s+are\s+now|you're\s+now|as\s+(?=the|an\b))\s*(the\s+|an?\s+|your\s+)?(admin(istrator)?|system|developer|root|owner|super\s*user|operator)\b(?!\s+(approval|approver|limit|threshold|rule|role|level|review|account|setting|config))|"
                              r"(tôi|toi|mình|tao)\s+là\s+(admin|quản\s*trị|hệ\s*thống|developer)|(管理者|システム|開発者)(です|として)", _I),
     "Claims about who you are do not change what you may do. Your rights come from your sign-in only."),
    ("OVERRIDE", re.compile(r"\b(ignore|disregard|forget|override|drop)\s+(?:(?:all|any|the|your|these|those|of|my|every)\s+)*(?:(?:previous|prior|above|earlier|preceding|system|safety|security|existing)\s+)*(instruction|rule|prompt|polic(y|ies)|guideline|safeguard|restriction)s?\b|"
                            r"\b(developer|system)\s+(mode|message|override)\b|\bjailbreak\b|\bDAN\b|bỏ\s+qua\s+(mọi\s+|các\s+|tất\s+cả\s+)?(quy\s*tắc|hướng\s*dẫn)|以前の指示を無視", _I),
     "Instructions to ignore the rules are not followed. The rules do not depend on what is written here."),
    ("SKIP_CONTROL", re.compile(r"\b(skip|bypass|without|no\s+need\s+for|disable|turn\s+off|waive|circumvent)\b[^.\n]{0,25}\b(approval|approver|review|verification|contract|confirmation|check|four[- ]eyes|second\s+person)s?\b|"
                                r"không\s+cần\s+(duyệt|phê\s*duyệt|xác\s*nhận)|承認(なし|不要|を省略)", _I),
     "Approvals, reviews and checks cannot be skipped from here. They stay required."),
    ("SELF_APPLY", re.compile(r"\b(auto[- ]?apply|apply|publish|deploy|merge|push)\b[^.\n]{0,25}\b(directly|immediately|automatically|right\s+away|on\s+your\s+own|yourself)\b|"
                              r"\b(you|ai|model)\s+(can|may|should)\s+(apply|approve|publish|deploy)\b", _I),
     "Nothing is applied from the chat. Applying needs the normal approvals."),
    ("SECRET_REQUEST", re.compile(r"\b(show|reveal|print|export|give|send|leak|dump|tell|read\s+out|list)\b\W+(me\W+)?(the\W+|your\W+|all\W+|every\W+)?(\w+\W+){0,3}"
                                  r"(passwords?|credentials?|tokens?|api[ -]?keys?|secrets?|cookies?|dsn|connection\s+strings?|system\s+prompt|your\s+instructions|private\s+keys?)", _I),
     "Secrets, credentials and internal instructions are never provided."),
    ("CROSS_SCOPE", re.compile(r"\b(other|another|different|all)\s+(tenants?|workspaces?|projects?|customers?|companies|accounts?)\b[^.\n]{0,40}\b(data|files?|rules?|documents?|secrets?|read|show|access|export)\b|"
                               r"\b(data|files?|rules?|documents?|read|show|access|export)\b[^.\n]{0,40}\b(other|another|different|all)\s+(tenants?|workspaces?|projects?|customers?|companies)\b", _I),
     "Only this workspace is available to you here."),
    ("BUDGET_OR_POLICY", re.compile(r"\b(raise|increase|change|set|edit|remove|lift|unlock)\b[^.\n]{0,20}\b(budget|quota|cap|rate\s+limit|permissions?|policy|policies|roles?)\b(?![^.\n]{0,20}\b(rule|threshold|limit\s+of\s+the)\b)", _I),
     "Budgets, quotas, permissions and policies cannot be changed from here."),
    ("EXEC_OR_LINK", re.compile(r"https?://|\bwww\.|<\s*script|javascript:|\b(curl|wget|powershell|bash\s+-c|sudo|rm\s+-rf|chmod|eval\()\b|\bdrop\s+table\b|\bselect\b[^.\n]{1,40}\bfrom\b|`[^`\n]{3,}`|\$\(", _I),
     "Links, commands and code in your message are shown as text. They are never opened or run."),
]
# Output of the model must not carry executable-looking content into fields/messages either.
_OUT = [r for c, r, _ in RULES if c == "EXEC_OR_LINK"][0]


def scan(text: str) -> list[dict]:
    """-> [{"code", "business"}] (one per rule at most). Technical views show the code; nothing else is stored from the text."""
    out = []
    t = text or ""
    for code, rx, business in RULES:
        if rx.search(t):
            out.append({"code": code, "business": business})
    return out


def unsafe_output(value) -> bool:
    """True when a model-produced string looks like a link, command or markup (never rendered as such; used to refuse the value)."""
    return isinstance(value, str) and bool(_OUT.search(value))


def codes(text: str) -> list[str]:
    return [h["code"] for h in scan(text)]


# ---- the chat only UNDERSTANDS a request: it never changes anything, so a reply may not say something was done.
# What exists at each point (and the only words allowed for it): understood (this reply) -> saved request (after the person confirms) -> draft (after the person asks) -> proposal -> approved -> applied -> verified.
_VERBS = r"updated|changed|applied|set|lowered|raised|reduced|increased|modified|published|saved|made|completed|implemented|configured|adjusted|edited|revised|written|committed|recorded|logged|stored|registered|noted|captured|filed|submitted|created|added"
_PASSIVE = r"updated|changed|applied|lowered|raised|reduced|increased|modified|published|saved|implemented|configured|adjusted|in\s+effect|recorded|logged|stored|registered|noted|captured|filed|submitted|created|added"
_DONE = re.compile(
    r"\b(i|we)\s*(?:'ve|have|had)?\s*(?:just\s+|already\s+|now\s+)?(" + _VERBS + r")\b|"
    r"\b(has|have|had|is|are|was|were)\s+(?:now\s+|already\s+|successfully\s+)?(?:been\s+)?(" + _PASSIVE + r")\b|"
    r"\b(is|are)\s+now\s+(set|routed|required|enforced)\b|\bwill\s+now\s+be\b|\bhas\s+now\s+been\b|"
    r"(đã|da)\s+(cập\s*nhật|thay\s*đổi|áp\s*dụng|sửa|điều\s*chỉnh|hạ|nâng|lưu)|(更新|変更|適用|修正|設定|保存)(しました|済み|されました|した)", _I)

NOT_DONE_NOTE = ("Nothing has been changed. I only restated what I understood; it is a request until you confirm it, and it becomes a change only after a draft, a proposal, review, approval and apply.")


def claims_completion(value) -> bool:
    """True when a model reply says that something has been updated / applied / done."""
    return isinstance(value, str) and bool(_DONE.search(value))


# ---- the words of a preview are decided by the SERVER state, not by the model. A preview is, by construction, NOT recorded: the person has not confirmed it and the server has stored nothing.
# The model's own sentence is shown only as "AI wording" and only when it makes no claim that something was saved / recorded / done; it can never state the state.
PREVIEW_LINE = {"READY": "Proposed understanding (preview). Nothing is recorded until you confirm it.",
                "CLARIFY": "More detail is needed. Nothing is recorded.",
                "UNSUPPORTED": "This cannot be done here. Nothing is recorded."}


_STATE_WORDS = re.compile(r"\b(record(?:ed|ing|s)?|sav(?:ed|ing|es?)|stor(?:ed|ing|es?)|logg(?:ed|ing)|regist(?:ered|ering)|submitt(?:ed|ing)|filed|captur(?:ed|ing)|not(?:ed|ing)|"
                          r"creat(?:ed|ing)|added|updated|applied|committed|persist(?:ed|ing)|accepted|approved|done|completed)\b", _I)


def mentions_state_change(value) -> bool:
    """Fail-safe lexical check: ANY word that names a stored/changed state (recorded, saved, stored, ... in any grammatical form, also a bare "Recorded the instruction ...").
    A phrase list can always be walked around ("Recorded the instruction" was not a claim of the form "has been recorded"); the preview's wording is therefore shown
    only when it uses none of these words. Over-dropping a harmless sentence costs nothing: the server's own state line is always there."""
    return isinstance(value, str) and bool(_STATE_WORDS.search(value))


def server_turn_text(status: str, message: str) -> str:
    """The text of an assistant turn: the server's statement of state, then the model's wording when (and only when) it makes no claim of completion or persistence."""
    line = PREVIEW_LINE.get(status, "Nothing is recorded.")
    msg = (message or "").strip()
    if not msg or claims_completion(msg) or mentions_state_change(msg):
        return line
    return f"{line} AI wording: {msg}"


def preview_turn_text(u: dict) -> str:
    """The assistant turn of a preview, built ONLY from state the server holds: the structured understanding it verified. The model's own free sentence is NOT shown
    at the confirmation step (no word list can guarantee that a free sentence states the true state); it stays in `ai_message` of the stored understanding for the
    audit. What the person reads about the state is this sentence; the details are in the structured preview next to it."""
    status = (u or {}).get("status") or ""
    line = PREVIEW_LINE.get(status, "Nothing is recorded.")
    return line + " The details are in the structured preview below." if status in ("READY", "CLARIFY") else line


# What a confirmation SAVES and what is still NOT done, by the action the person confirmed (written by the server after the save succeeded).
NOT_DONE_AFTER_CONFIRM = {
    "gap_reply": {"decision": "the decision is not recorded and no source file was changed; press \"Record this decision\" to record it",
                  "comment": "the comment is not saved; press \"Save this comment\" to save it"},
    "ask": "no question has been asked of the AI and nothing was changed",
    "question_reply": "no answer is recorded and no source file was changed; use the button below to record it",
    "feedback": "no feedback comment is recorded and no draft was changed",
    "whatif": "no simulation has run and nothing was changed",
}


def confirmed_preview_text(outcome: dict) -> str:
    action = (outcome or {}).get("action")
    what = NOT_DONE_AFTER_CONFIRM.get(action)
    if isinstance(what, dict):
        what = what.get((outcome or {}).get("kind")) or "nothing is recorded yet"
    return ("You confirmed this preview. At this step the server saved: your confirmation of this preview. "
            f"Not done at this step: {what or 'nothing is recorded or changed yet'}.")


def confirmed_request_text(intent_id, status) -> str:
    return (f"You confirmed this understanding. At this step the server saved: your request as a structured request (intent {intent_id}, status {status}). "
            "Not done at this step: the request is not confirmed as an intent; no draft, proposal, approval or apply exists; no source file was changed. Next: Confirm intent.")
