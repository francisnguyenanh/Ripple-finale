"""Typed, allowlisted, bounded diagnostics (AH-05). Operational reasons and provider diagnostics are persisted on job / attempt rows, logged and returned by
read APIs; they must never carry raw prompt, model output, source text, secrets or DSNs. Exception messages are free text (an SDK message, a schema error that
quotes the offending value, a database message), so nothing is kept from them unless it passes a conservative shape check; unknown keys are dropped.

* `safe_code(text)`      -> a reason code (`[a-z][a-z0-9_]*`, <= 64) or "error".
* `safe_detail(text)`    -> the text when it is short, single-line ASCII without quotes, brackets, URLs, e-mail, sensitive key=value (password, token, dsn ...) or token-like runs; else None.
* `safe_reason(exc)`     -> "<code>" or "<code>: <safe detail>" for a worker failure (the code keeps existing `startswith` contracts).
* `sanitize_reason(s)`   -> the same shape applied to a stored reason string (old rows written before this module).
* `safe_diagnostics(d)`  -> provider diagnostics with allowlisted keys and typed values only.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

MAX_DETAIL = 240
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_UNSAFE = re.compile(r"[\"'`{}\[\]<\\|@\n\r\t]|\w>|://|\$\{")          # a comparison " > " is allowed; a tag "x>" / "<x" is not
_SENSITIVE_KEY = re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|dsn|session|credential|host|user|dbname)\s*[=:]")
_TOKEN_RUN = re.compile(r"(?=[A-Za-z0-9+/_\-]*\d)(?=[A-Za-z0-9+/_\-]*[A-Za-z])[A-Za-z0-9+/_\-]{20,}")   # key / digest-like (letters AND digits); a CamelCase class name is not
_ENUM = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.=\-]{0,63}$")
_INT_KEYS = frozenset({"status_code", "candidates", "requested_output_cap", "text_chars", "message_chars", "code"})
_ENUM_KEYS = frozenset({"stage", "error_class", "finish_reason", "block_reason", "response_id", "model_version", "pinned_model", "exception"})
_USAGE_KEYS = frozenset({"prompt_token_count", "candidates_token_count", "thoughts_token_count", "total_token_count", "cached_content_token_count",
                         "input_tokens", "output_tokens", "thought_tokens", "cached_input_tokens", "total_tokens"})


def safe_code(text: Any) -> str:
    s = str(text or "").strip()
    return s if _CODE.match(s) else "error"


def safe_detail(text: Any) -> Optional[str]:
    if not isinstance(text, str) or "\n" in text or "\r" in text:                    # a multi-line message is an echo or a trace, never a reason
        return None
    s = " ".join(text.split())
    if not s or len(s) > MAX_DETAIL or not s.isascii() or _UNSAFE.search(s) or _TOKEN_RUN.search(s) or _SENSITIVE_KEY.search(s):
        return None
    return s


def _join(code: str, detail: Optional[str]) -> str:
    return f"{code}: {detail}" if detail else code


def _safe_prefix(detail: str) -> Optional[str]:
    """The detail itself when safe; else its longest safe leading run of ':'-separated segments (e.g. "ProviderInvalidResponseError" of
    "ProviderInvalidResponseError: <unsafe provider text>"), so safe metadata survives an unsafe tail."""
    whole = safe_detail(detail)
    if whole is not None:
        return whole
    parts = detail.split(":")
    for n in range(len(parts) - 1, 0, -1):
        if not all(_TOKEN.match(x.strip()) for x in parts[:n]):           # only single-token segments (class names, keys): never a multi-word fragment of text
            continue
        head = safe_detail(":".join(parts[:n]))
        if head is not None:
            return head
    return None


def sanitize_reason(text: Any) -> Optional[str]:
    """A stored reason "<code>[: detail]" reduced to its safe shape. None stays None."""
    if text is None:
        return None
    s = str(text)
    code, sep, rest = s.partition(":")
    return _join(safe_code(code), _safe_prefix(rest) if sep else None)


def safe_reason(exc: BaseException, *, prefix: Optional[str] = None) -> str:
    """The reason persisted for a failed attempt. `prefix` (e.g. "provider_execution_failed") is a fixed code; the exception class name is safe metadata."""
    if prefix:
        cls = type(exc).__name__ if _ENUM.match(type(exc).__name__) else "Exception"
        return _join(safe_code(prefix), _safe_prefix(f"{cls}: {exc}") or cls)
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and _CODE.match(reason):
        s = str(exc)
        rest = s[len(reason) + 1:].strip() if s.startswith(reason + ":") else ""
        return _join(reason, _safe_prefix(rest) if rest else None)
    return safe_code(type(exc).__name__.lower()) if _CODE.match(type(exc).__name__.lower()) else "error"


def _num(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _sec(v: Any) -> Optional[float]:
    """A duration in seconds: a finite, non-negative number below one hour, rounded to milliseconds."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not 0 <= v < 3600:
        return None
    return round(float(v), 3)


def safe_diagnostics(diag: Any) -> dict:
    """Provider diagnostics with only allowlisted keys and typed values. `message` / `finish_message` keep a safe short form or only their length."""
    if not isinstance(diag, Mapping):
        return {}
    out: dict = {}
    for k, v in diag.items():
        if k in _INT_KEYS:
            if _num(v) is not None:
                out[k] = v
        elif k in _ENUM_KEYS:
            if isinstance(v, str) and _ENUM.match(v):
                out[k] = v
        elif k == "usage" and isinstance(v, Mapping):
            u = {uk: uv for uk, uv in v.items() if uk in _USAGE_KEYS and _num(uv) is not None}
            if u:
                out[k] = u
        elif k == "safety_flagged" and isinstance(v, list):
            rows = []
            for r in v[:12]:
                if isinstance(r, Mapping):
                    row = {rk: r[rk] for rk in ("category", "probability") if isinstance(r.get(rk), str) and _ENUM.match(r[rk])}
                    row["blocked"] = bool(r.get("blocked"))
                    rows.append(row)
            if rows:
                out[k] = rows
        elif k == "rqa_calls" and isinstance(v, list):                     # R21: the request assistant's call timeline (role, seconds, outcome) of a failed attempt
            rows = []
            for r in v[:4]:
                if isinstance(r, Mapping):
                    row = {rk: r[rk] for rk in ("role", "outcome") if isinstance(r.get(rk), str) and _ENUM.match(r[rk])}
                    if _sec(r.get("s")) is not None:
                        row["s"] = _sec(r.get("s"))
                    rows.append(row)
            if rows:
                out[k] = rows
        elif k == "rqa_prepare_s":
            if _sec(v) is not None:
                out[k] = _sec(v)
        elif k == "rqa_primary_error":
            if isinstance(v, str) and _ENUM.match(v):
                out[k] = v
        elif k == "rqa_primary_diagnostics" and isinstance(v, Mapping):   # the answer call's own diagnostics, under the same allowlist (one level only)
            inner = safe_diagnostics({ik: iv for ik, iv in v.items() if not str(ik).startswith("rqa_")})
            if inner:
                out[k] = inner
        elif k in ("message", "finish_message"):
            if isinstance(v, str):
                safe = safe_detail(v)
                if safe:
                    out[k] = safe
                else:
                    out[k + "_chars"] = len(v)
    return out
