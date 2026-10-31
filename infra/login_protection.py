"""Login protection for `POST /v2/auth/login` (SEC-02, audit 2026-10-30; remediation plan A1.1).

Three failure counters, each a key with its own limit and lock:
  L1 `as:<account>|<source>`  the pair                      (RIPPLE_LOGIN_MAX_FAILURES 5 / RIPPLE_LOGIN_LOCKOUT_SECONDS 300)
  L2 `s:<source>`             the source address            (RIPPLE_LOGIN_SOURCE_MAX_FAILURES 100 / RIPPLE_LOGIN_SOURCE_LOCKOUT_SECONDS 300)
  L3 `a:<account>`            the account from every source (RIPPLE_LOGIN_ACCOUNT_GLOBAL_MAX_FAILURES 60 / RIPPLE_LOGIN_ACCOUNT_GLOBAL_LOCKOUT_SECONDS 60;
                              accounts named in RIPPLE_LOGIN_PRIVILEGED_ACCOUNTS: RIPPLE_LOGIN_PRIVILEGED_MAX_FAILURES 20 / RIPPLE_LOGIN_PRIVILEGED_LOCKOUT_SECONDS 300)
State machine of one key, evaluated inside ONE database transaction under one advisory transaction lock (every failure write and every lock read
for a decision takes it; failed logins are rare, so serializing them is the smallest mechanism that keeps the bounds exact under concurrency):
  CLEAN --fail--> COUNTING(1) --fail--> COUNTING(n+1); at n+1 >= limit --> LOCKED(locked_until = now + lockout), failures := 0.
  LOCKED: every attempt is answered 429 BEFORE the credential check and changes no counter (no lock extension, no row growth).
  after locked_until: the key counts again from the failures it holds (0 after a lock); the next `limit` failures lock it again (re-arm).
  window rollover: counters belong to a window (RIPPLE_LOGIN_WINDOW_SECONDS 300); locks do not: a key is locked while max(locked_until) over all
  its rows is in the future; retention never removes a row whose lock is live. A successful login clears the account's `a:` and `as:` rows of the
  current window (a locked key cannot succeed: it is refused before the check).
What this gives and what it does not: one source cannot lock an account for other sources (L1); spraying from one source is bounded (L2);
distributed guessing of one account is rate-limited (L3). It does NOT remove lockout denial of service: with `account_global_max` sources an attacker
re-locks an account as soon as its lock expires (recorded residual risk; no CAPTCHA or external service is used).
Bounded storage: per source and window at most 1 + 2 x RIPPLE_LOGIN_SOURCE_MAX_FAILURES rows (account rows are written only while the source row is
within its own allowance); system ceiling RIPPLE_LOGIN_MAX_ROWS (200000) checked exactly after retention: a new key above it is not written and the
attempt is refused (fail closed), one log line per minute. Logs carry an 8-character hash of the account and the /24 or /48 of the source, never a
credential. Source address: `source_address` (RIPPLE_TRUSTED_PROXY_HOPS, see there).
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import time
from dataclasses import dataclass

LOG = logging.getLogger("ripple.login")
ADVISORY_LOCK_ID = 0x5249504C                              # "RIPL": one lock for every failure write of this table
_last_ceiling_log = 0.0


def _int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    account_max: int
    account_lockout: int
    source_max: int
    source_lockout: int
    global_max: int
    global_lockout: int
    privileged_max: int
    privileged_lockout: int
    window: int
    max_rows: int
    privileged: frozenset


def limits() -> Limits:
    return Limits(
        account_max=_int("RIPPLE_LOGIN_MAX_FAILURES", 5), account_lockout=max(1, _int("RIPPLE_LOGIN_LOCKOUT_SECONDS", 300)),
        source_max=_int("RIPPLE_LOGIN_SOURCE_MAX_FAILURES", 100), source_lockout=max(1, _int("RIPPLE_LOGIN_SOURCE_LOCKOUT_SECONDS", 300)),
        global_max=_int("RIPPLE_LOGIN_ACCOUNT_GLOBAL_MAX_FAILURES", 60), global_lockout=max(1, _int("RIPPLE_LOGIN_ACCOUNT_GLOBAL_LOCKOUT_SECONDS", 60)),
        privileged_max=_int("RIPPLE_LOGIN_PRIVILEGED_MAX_FAILURES", 20), privileged_lockout=max(1, _int("RIPPLE_LOGIN_PRIVILEGED_LOCKOUT_SECONDS", 300)),
        window=max(1, _int("RIPPLE_LOGIN_WINDOW_SECONDS", 300)), max_rows=max(1000, _int("RIPPLE_LOGIN_MAX_ROWS", 200000)),
        privileged=frozenset(a.strip() for a in os.environ.get("RIPPLE_LOGIN_PRIVILEGED_ACCOUNTS", "").split(",") if a.strip()))


def keys(account, source, lim: Limits | None = None) -> list[tuple[str, int, int]]:
    """(key, limit, lockout seconds) of the three layers, in the order they are written; a layer with limit 0 is off."""
    lim = lim or limits()
    acct = (account if isinstance(account, str) else "")[:200]
    src = (source or "")[:100]
    priv = acct in lim.privileged
    out = [("as:" + acct + "|" + src, lim.account_max, lim.account_lockout),
           ("s:" + src, lim.source_max, lim.source_lockout),
           ("a:" + acct, lim.privileged_max if priv else lim.global_max, lim.privileged_lockout if priv else lim.global_lockout)]
    return [k for k in out if k[1] > 0]


# ----------------------------------------------------------------------------------------------------------------------- source address
def _normalise(addr: str) -> str:
    try:
        return str(ipaddress.ip_address(addr.strip().strip("[]")))
    except ValueError:
        return "invalid"


def source_address(request, hops: int | None = None) -> str:
    """The caller's address under the ingress contract RIPPLE_TRUSTED_PROXY_HOPS (an operator setting, verified on the deployment):
    0 (default): no trusted proxy, `X-Forwarded-For` is client data and ignored, the peer address counts;
    n > 0: the n-th entry from the end of `X-Forwarded-For` was appended by a trusted hop and counts; a header with fewer entries means the request
    did not come through the configured ingress -> `untrusted` (one shared bucket, refused early, logged); an entry that is not an address -> `invalid`.
    IPv6 addresses are normalised (`::1`, `[::1]` -> `::1`)."""
    hops = _int("RIPPLE_TRUSTED_PROXY_HOPS", 0) if hops is None else hops
    peer = request.client.host if getattr(request, "client", None) else "unknown"
    if hops <= 0:
        try:
            return str(ipaddress.ip_address(peer.strip("[]")))                 # the transport's peer: an address, or a test transport's name as it is
        except ValueError:
            return peer[:100]
    raw = request.headers.get("x-forwarded-for", "") or ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < hops:
        LOG.warning("login_source_untrusted hops=%s entries=%s", hops, len(parts))
        return "untrusted"
    return _normalise(parts[-hops])


def source_log(source: str) -> str:
    try:
        ip = ipaddress.ip_address(source)
        return str(ipaddress.ip_network(f"{ip}/{24 if ip.version == 4 else 48}", strict=False))
    except ValueError:
        return source[:16]


def account_log(account) -> str:
    return hashlib.sha256((account if isinstance(account, str) else "").encode("utf-8")).hexdigest()[:8]


# ----------------------------------------------------------------------------------------------------------------------- shared (database) state
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS v2_login_failures (key TEXT NOT NULL, window_start BIGINT NOT NULL, failures INTEGER NOT NULL, "
    "locked_until BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (key, window_start));\n"
    "ALTER TABLE v2_login_failures ADD COLUMN IF NOT EXISTS locked_until BIGINT NOT NULL DEFAULT 0;"
)


def ensure_schema(conn) -> None:
    """Idempotent; the production path is the migration `infra/migrations/v2_042_login_failures.sql` (same statements)."""
    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA)


def shared_wait(conn, account, source, now: float | None = None) -> int:
    """Seconds until the next attempt of this (account, source) is accepted; 0 when no layer is locked. Reads only."""
    t = time.time() if now is None else now
    with conn, conn.cursor() as cur:
        wait = 0
        for key, _limit, _lock in keys(account, source):
            cur.execute("SELECT COALESCE(max(locked_until), 0) FROM v2_login_failures WHERE key=%s", (key,))
            until = cur.fetchone()[0]
            if until > t:
                wait = max(wait, int(until - t) + 1)
        return wait


def shared_failure(conn, account, source, now: float | None = None) -> bool:
    """Record one failed attempt (the credential check ran and failed). Returns True when the row ceiling refused a new key (the caller answers 429:
    nothing could be counted, so nothing is admitted). One transaction, one advisory lock."""
    global _last_ceiling_log
    lim = limits()
    t = time.time() if now is None else now
    w = int(t // lim.window)
    layers = keys(account, source, lim)
    refused = False
    with conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_ID,))
        cur.execute("DELETE FROM v2_login_failures WHERE window_start < %s AND locked_until <= %s", (w - 2, int(t)))         # retention, not a bound
        source_layer = next((k for k in layers if k[0].startswith("s:")), None)
        source_failures = 0
        if source_layer is not None:
            source_failures = _bump(cur, source_layer, w, t)
        cur.execute("SELECT count(*) FROM v2_login_failures")
        total = cur.fetchone()[0]
        for layer in layers:
            if layer is source_layer:
                continue
            if source_layer is not None and source_failures > lim.source_max:
                continue                                                    # per-source bound: this source wrote its allowance of account rows for this window
            cur.execute("SELECT 1 FROM v2_login_failures WHERE key=%s AND window_start=%s", (layer[0], w))
            exists = cur.fetchone() is not None
            if not exists and total >= lim.max_rows:
                refused = True
                if t - _last_ceiling_log >= 60:
                    _last_ceiling_log = t
                    LOG.error("login_rows_ceiling_reached rows=%s ceiling=%s", total, lim.max_rows)
                continue
            if not exists:
                total += 1
            _bump(cur, layer, w, t)
    return refused


def _bump(cur, layer, w: int, t: float) -> int:
    key, limit, lockout = layer
    cur.execute("INSERT INTO v2_login_failures (key, window_start, failures, locked_until) VALUES (%s,%s,1,0) "
                "ON CONFLICT (key, window_start) DO UPDATE SET failures = v2_login_failures.failures + 1 RETURNING failures", (key, w))
    failures = cur.fetchone()[0]
    if failures >= limit:
        cur.execute("UPDATE v2_login_failures SET locked_until=%s, failures=0 WHERE key=%s AND window_start=%s", (int(t) + lockout, key, w))
        LOG.warning("login_locked layer=%s account=%s source=%s seconds=%s", key[:2].rstrip(":"),
                    account_log(key.split("|")[0][3:] if key.startswith("as:") else key[2:] if key.startswith("a:") else ""),
                    source_log(key.split("|", 1)[1] if key.startswith("as:") else key[2:] if key.startswith("s:") else ""), lockout)
    return failures


def shared_clear(conn, account, now: float | None = None) -> None:
    """A successful login clears the account's counters (`a:` and every `as:` of it) that are not locked; a lock is never cleared by a success
    (a locked key cannot reach the check)."""
    t = time.time() if now is None else now
    acct = (account if isinstance(account, str) else "")[:200]
    with conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_ID,))
        cur.execute("DELETE FROM v2_login_failures WHERE (key = %s OR left(key, %s) = %s) AND locked_until <= %s",
                    ("a:" + acct, len("as:" + acct + "|"), "as:" + acct + "|", int(t)))
