"""Runtime operator switches, kept in the database (no redeploy, no terminal, no operator login): `ai_switch` (new AI starts) and `worker_claims` (the worker claims any job).

Active only when `RIPPLE_OPERATOR_FLAGS=1` (a deployment setting; off for local tools and tests, so nothing changes there). Values are "on" / "off"; a flag never set is "on" (the
environment arming in `provider_gate` still applies on top). History is append-only: every change keeps who, when and why.

Failure behaviour (fail CLOSED): when the switches are active and the database cannot be read, AI is treated as OFF and the worker claims nothing. A flag read is cached for
`RIPPLE_FLAG_CACHE_SECONDS` (default 5), so a switch takes effect within a few seconds on every instance and worker, without a redeploy.
What a switch does NOT do: it does not cancel an attempt that is already running (its cost is incurred and settled by the normal path); it stops NEW work. Queued jobs stay QUEUED.
"""
from __future__ import annotations

import os
import threading
import time

KEYS = ("ai_switch", "worker_claims")
#: the longest trial window the tool will record (minutes): the approved 90 minutes minus the 5 minutes the worker needs to finish a claim in flight before its hard stop
MAX_WINDOW_MINUTES = 85
_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}
_ready = False


def enabled() -> bool:
    return os.environ.get("RIPPLE_OPERATOR_FLAGS") == "1" and bool(os.environ.get("RIPPLE_V2_DSN"))


def _ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("RIPPLE_FLAG_CACHE_SECONDS", "5")))
    except ValueError:
        return 5.0


def _conn():
    from infra import db_pool
    return db_pool.connection(os.environ["RIPPLE_V2_DSN"])


def _ensure(cur) -> None:
    global _ready
    if not _ready:
        cur.execute("""CREATE TABLE IF NOT EXISTS v2_operator_flags (
            id BIGSERIAL PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL CHECK (value IN ('on','off')),
            set_by TEXT NOT NULL, reason TEXT NOT NULL, set_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS v2_operator_window (
            id BIGSERIAL PRIMARY KEY, ends_at TIMESTAMPTZ NOT NULL, set_by TEXT NOT NULL, reason TEXT NOT NULL, set_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        _ready = True


def reset_cache() -> None:
    global _win_cache
    with _lock:
        _cache.clear()
        _win_cache = None


def get(key: str) -> str:
    """'on' or 'off'. RAISES when the switches are active and the database cannot be read: callers treat that as OFF (fail closed)."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _ttl():
            return hit[1]
    with _conn() as conn:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _ensure(cur)
                cur.execute("SELECT value FROM v2_operator_flags WHERE key=%s ORDER BY id DESC LIMIT 1", (key,))
                row = cur.fetchone()
        finally:
            conn.autocommit = False
    value = row[0] if row else "on"
    with _lock:
        _cache[key] = (now, value)
    return value


def ai_on() -> bool:
    """True when new AI starts are allowed by the switch (always True while the switches are not active)."""
    if not enabled():
        return True
    try:
        return get("ai_switch") == "on"
    except Exception:                      # noqa: BLE001 - fail closed
        return False


def claims_on() -> bool:
    if not enabled():
        return True
    try:
        return get("worker_claims") == "on"
    except Exception:                      # noqa: BLE001 - fail closed
        return False


def status() -> dict:
    out = {"active": enabled(), "window": window_status()}
    for k in KEYS:
        try:
            out[k] = get(k) if enabled() else "on"
        except Exception:                  # noqa: BLE001
            out[k] = "unknown (treated as off)"
    return out


def set_flag(key: str, value: str, actor: str, reason: str, tenant: str | None = None, workspace: str | None = None) -> dict:
    if key not in KEYS or value not in ("on", "off"):
        raise ValueError("unknown switch or value")
    if len((reason or "").strip()) < 10:
        raise ValueError("a reason of at least 10 characters is required")
    with _conn() as conn:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _ensure(cur)
                cur.execute("INSERT INTO v2_operator_flags (key, value, set_by, reason) VALUES (%s,%s,%s,%s) RETURNING id, set_at", (key, value, actor, reason.strip()[:300]))
                row = cur.fetchone()
                if tenant and workspace:
                    from psycopg2.extras import Json
                    cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'operator.switch',%s,%s)",
                                (tenant, workspace, actor, Json({"key": key, "value": value, "reason": reason.strip()[:300]})))
        finally:
            conn.autocommit = False
    reset_cache()
    return {"key": key, "value": value, "set_by": actor, "set_at": row[1].isoformat()}


def history(limit: int = 10) -> list[dict]:
    with _conn() as conn:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _ensure(cur)
                cur.execute("SELECT key, value, set_by, reason, set_at FROM v2_operator_flags ORDER BY id DESC LIMIT %s", (limit,))
                return [{"key": r[0], "value": r[1], "set_by": r[2], "reason": r[3], "set_at": r[4].isoformat()} for r in cur.fetchall()]
        finally:
            conn.autocommit = False


# ---------------------------------------------------------------------------------------------- the trial window
# One absolute end time, in the database, shared by the web (which REFUSES new AI starts after it, with its own explained message) and the worker (which stops claiming at it and
# switches both switches OFF when it exits). It does not depend on any agent, terminal or machine. It is append-only; the newest row counts. No row = no window (nothing changes).
_win_cache: tuple[float, object] | None = None


def window_ends_at():
    """The end of the current trial window (a timezone-aware datetime) or None. Cached like the switches. A database that cannot be read means "no window known": the switches fail closed anyway."""
    global _win_cache
    now = time.monotonic()
    with _lock:
        if _win_cache and now - _win_cache[0] < _ttl():
            return _win_cache[1]
    try:
        with _conn() as conn:
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    _ensure(cur)
                    cur.execute("SELECT ends_at FROM v2_operator_window ORDER BY id DESC LIMIT 1")
                    row = cur.fetchone()
            finally:
                conn.autocommit = False
    except Exception:                       # noqa: BLE001
        return None
    value = row[0] if row else None
    with _lock:
        _win_cache = (now, value)
    return value


def window_over(now=None) -> bool:
    """True once a recorded window has ended (always False while the switches are not active or no window exists)."""
    if not enabled():
        return False
    from datetime import datetime, timezone
    end = window_ends_at()
    return end is not None and (now or datetime.now(timezone.utc)) >= end


def window_status(now=None) -> dict:
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    end = window_ends_at() if enabled() else None
    if end is None:
        return {"state": "none", "server_time": now.isoformat()}
    left = int((end - now).total_seconds())
    return {"state": "open" if left > 0 else "ended", "ends_at": end.isoformat(), "seconds_left": max(0, left), "server_time": now.isoformat()}


MAX_SERVING_DAYS = 90


def window_creation_closed(now=None) -> bool:
    """True from `RIPPLE_WINDOW_CREATE_MARGIN_SECONDS` (default 0) before the recorded end: the web stops ACCEPTING new jobs a little before the worker stops CLAIMING, so a job accepted
    inside the window has time to run and none is accepted only to wait for ever. No window recorded / switches not active: False."""
    if not enabled():
        return False
    from datetime import datetime, timedelta, timezone
    end = window_ends_at()
    if end is None:
        return False
    try:
        margin = max(0.0, float(os.environ.get("RIPPLE_WINDOW_CREATE_MARGIN_SECONDS", "0")))
    except ValueError:
        margin = 0.0
    return (now or datetime.now(timezone.utc)) >= end - timedelta(seconds=margin)


def set_window_until(ends_at, actor: str, reason: str) -> dict:
    """Record a SERVING window with an absolute end (timezone-aware, in the future, at most MAX_SERVING_DAYS away): the evaluation deadline. Same table, same append-only rule, same effect on web and
    worker as the trial window; a later row (shorter or longer) replaces it, so the operator can end it now (`set_window(0, ...)`) or extend it by recording a new end."""
    global _win_cache
    from datetime import datetime, timedelta, timezone
    if ends_at.tzinfo is None:
        raise ValueError("the end must carry a time zone")
    now = datetime.now(timezone.utc)
    if not now < ends_at <= now + timedelta(days=MAX_SERVING_DAYS):
        raise ValueError(f"a serving window must end in the future and within {MAX_SERVING_DAYS} days")
    if len((reason or "").strip()) < 10:
        raise ValueError("a reason of at least 10 characters is required")
    with _conn() as conn:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _ensure(cur)
                cur.execute("INSERT INTO v2_operator_window (ends_at, set_by, reason) VALUES (%s, %s, %s) RETURNING ends_at", (ends_at, actor, reason.strip()[:300]))
                end = cur.fetchone()[0]
        finally:
            conn.autocommit = False
    with _lock:
        _win_cache = None
    return {"ends_at": end.isoformat(), "set_by": actor}


def set_window(minutes: float, actor: str, reason: str) -> dict:
    """Record a window that ends `minutes` from now (0 ends it now). Refuses more than MAX_WINDOW_MINUTES: a longer window needs a new approval, not a bigger number."""
    global _win_cache
    if not 0 <= minutes <= MAX_WINDOW_MINUTES:
        raise ValueError(f"a window is at most {MAX_WINDOW_MINUTES} minutes")
    if len((reason or "").strip()) < 10:
        raise ValueError("a reason of at least 10 characters is required")
    with _conn() as conn:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _ensure(cur)
                cur.execute("INSERT INTO v2_operator_window (ends_at, set_by, reason) VALUES (now() + (%s * interval '1 minute'), %s, %s) RETURNING ends_at",
                            (minutes, actor, reason.strip()[:300]))
                end = cur.fetchone()[0]
        finally:
            conn.autocommit = False
    with _lock:
        _win_cache = None
    return {"ends_at": end.isoformat(), "set_by": actor}
