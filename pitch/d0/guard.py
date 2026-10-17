"""Client guard: D0 must make zero provider/network calls.

`no_provider_calls()` replaces every provider-client entry point the production extraction path can
reach with a function that raises, blocks socket connects, and counts attempts. Used around every D0
run (extract_from_* enter it themselves) and by the proof test.

v0.3: the guard is a process-wide, lock-protected, depth-counted state. The first enter installs the
raisers, the last exit (in ANY order, so overlapping or threaded runs are safe) restores the originals,
and an exit without a matching enter raises. Nested/overlapping entries share one counter list.
"""
from __future__ import annotations

import contextlib
import socket
import sys
import threading


class ProviderCallAttempted(RuntimeError):
    """D0 (or code it called) tried to construct/call a provider client or open a network connection."""


_lock = threading.RLock()
_depth = 0
_undo: list = []
_counter: list = []


def _raiser(name: str):
    def _blocked(*args, **kwargs):
        _counter.append(name)
        raise ProviderCallAttempted(f"D0 forbids provider/network access: {name}")
    return _blocked


def _install() -> None:
    def patch(obj, attr, label):
        if obj is None or not hasattr(obj, attr):
            return
        _undo.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, _raiser(label))

    # Production client factory (only patched where the module is already loaded: D0 never imports it).
    for modname in ("infra.worker.live_analysis", "infra.worker.extraction"):
        patch(sys.modules.get(modname), "_client_for_model", f"{modname}._client_for_model")
    router = sys.modules.get("ai_module.router")
    patch(getattr(router, "AIRouter", None) if router else None, "__init__", "ai_module.router.AIRouter")
    patch(sys.modules.get("google.genai"), "Client", "google.genai.Client")
    patch(socket, "create_connection", "socket.create_connection")
    patch(socket.socket, "connect", "socket.socket.connect")


def _enter():
    global _depth
    with _lock:
        if _depth == 0:
            _counter.clear()
            _install()
        _depth += 1
        return _counter


def _exit():
    global _depth
    with _lock:
        if _depth <= 0:
            raise RuntimeError("no_provider_calls exit without matching enter")
        _depth -= 1
        if _depth == 0:
            for obj, attr, original in reversed(_undo):
                setattr(obj, attr, original)
            _undo.clear()


def active_depth() -> int:
    return _depth


@contextlib.contextmanager
def no_provider_calls():
    counter = _enter()
    try:
        yield counter
    finally:
        _exit()
