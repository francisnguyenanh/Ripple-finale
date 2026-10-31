"""SEC-02 counter primitives (no HTTP, no database): the in-process throttle's state machine per key, the three layers, re-arm after a lock,
success clearing, and the source-address contract (trusted hops, missing / short / invalid / IPv6 forwarded headers)."""
from types import SimpleNamespace

import pytest

from infra import login_protection as lp
from infra.v2_auth import LoginThrottle


def _req(peer="203.0.113.5", xff=None):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_three_layers_with_their_own_limits(monkeypatch):
    monkeypatch.setenv("RIPPLE_LOGIN_PRIVILEGED_ACCOUNTS", "root-admin")
    ks = lp.keys("user-a", "198.51.100.7")
    assert [k[0] for k in ks] == ["as:user-a|198.51.100.7", "s:198.51.100.7", "a:user-a"]
    assert [k[1] for k in ks] == [5, 100, 60] and [k[2] for k in ks] == [300, 300, 60]
    assert [k[1:] for k in lp.keys("root-admin", "x")][2] == (20, 300), "a privileged account locks earlier and longer"
    monkeypatch.setenv("RIPPLE_LOGIN_ACCOUNT_GLOBAL_MAX_FAILURES", "0")
    assert [k[0][:2] for k in lp.keys("user-a", "x")] == ["as", "s:"], "a layer with limit 0 is off"


def test_pair_lock_does_not_touch_other_sources_and_rearms_after_the_lock():
    clock = [1000.0]
    t = LoginThrottle(clock=lambda: clock[0])
    for _ in range(5):
        t.failure("user-a", "10.0.0.1")
    assert t.retry_after("user-a", "10.0.0.1") == 301
    assert t.retry_after("user-a", "10.0.0.2") == 0, "another source is not locked by the pair"
    assert t.retry_after("user-b", "10.0.0.1") == 0, "another account from that source is not locked"
    clock[0] += 301
    assert t.retry_after("user-a", "10.0.0.1") == 0
    for _ in range(4):
        t.failure("user-a", "10.0.0.1")
    assert t.retry_after("user-a", "10.0.0.1") == 0, "after a lock the counter restarts from 0: four failures do not lock again"
    t.failure("user-a", "10.0.0.1")
    assert t.retry_after("user-a", "10.0.0.1") > 0, "the fifth does (re-arm)"


def test_account_global_lock_from_many_sources_expires_and_does_not_relock_by_itself():
    clock = [5000.0]
    t = LoginThrottle(clock=lambda: clock[0])
    for i in range(12):
        for _ in range(5):
            t.failure("user-a", f"10.0.{i}.1")
    assert t.retry_after("user-a", "10.0.99.9") == 61, "60 failures from 12 sources lock the account for everyone, 60 s"
    clock[0] += 61
    assert t.retry_after("user-a", "10.0.99.9") == 0
    clock[0] += 10
    assert t.retry_after("user-a", "10.0.99.9") == 0, "no re-lock without new failures, whatever the window"


def test_success_clears_the_account_counters_but_not_a_live_lock():
    clock = [1.0]
    t = LoginThrottle(clock=lambda: clock[0])
    for _ in range(3):
        t.failure("user-a", "10.0.0.1")
    t.success("user-a")
    for _ in range(4):
        t.failure("user-a", "10.0.0.1")
    assert t.retry_after("user-a", "10.0.0.1") == 0, "the three earlier failures were cleared by the success"
    t.failure("user-a", "10.0.0.1")
    assert t.retry_after("user-a", "10.0.0.1") > 0
    t.success("user-a")
    assert t.retry_after("user-a", "10.0.0.1") > 0, "a success cannot happen on a locked key; even if called, the lock stays"


def test_window_expiry_of_counts():
    clock = [100.0]
    t = LoginThrottle(clock=lambda: clock[0])
    for _ in range(4):
        t.failure("user-a", "10.0.0.1")
    clock[0] += 301
    t.failure("user-a", "10.0.0.1")
    assert t.retry_after("user-a", "10.0.0.1") == 0, "failures older than the window no longer count"


def test_source_address_contract(monkeypatch):
    monkeypatch.delenv("RIPPLE_TRUSTED_PROXY_HOPS", raising=False)
    assert lp.source_address(_req("203.0.113.5", xff="1.2.3.4, 5.6.7.8")) == "203.0.113.5", "without a trusted proxy the header is client data"
    assert lp.source_address(_req("testclient")) == "testclient"
    monkeypatch.setenv("RIPPLE_TRUSTED_PROXY_HOPS", "1")
    assert lp.source_address(_req(xff="1.2.3.4, 5.6.7.8")) == "5.6.7.8"
    assert lp.source_address(_req(xff="9.9.9.9")) == "9.9.9.9"
    assert lp.source_address(_req(xff=None)) == "untrusted", "no header behind a configured proxy: not from the ingress"
    assert lp.source_address(_req(xff="garbage")) == "invalid"
    assert lp.source_address(_req(xff="2001:db8::1, [2001:DB8:0:0:0:0:0:2]")) == "2001:db8::2"
    monkeypatch.setenv("RIPPLE_TRUSTED_PROXY_HOPS", "2")
    assert lp.source_address(_req(xff="1.2.3.4, 5.6.7.8, 10.0.0.1")) == "5.6.7.8"
    assert lp.source_address(_req(xff="5.6.7.8")) == "untrusted", "fewer entries than hops"


def test_logs_never_carry_the_account_or_the_full_address():
    assert len(lp.account_log("user-admin")) == 8 and "user" not in lp.account_log("user-admin")
    assert lp.source_log("203.0.113.77") == "203.0.113.0/24" and lp.source_log("2001:db8:1:2:3:4:5:6") == "2001:db8:1::/48"
