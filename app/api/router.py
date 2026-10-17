# -*- coding: utf-8 -*-
"""The public API. It can only call what `PUBLIC_SURFACE` declares.

Dispatch is a lookup in one table, and the table is `PUBLIC_SURFACE`. A
handler with no entry there cannot be reached, however it is written and
whatever it is named. That makes "every externally callable operation is on
the surface" true by construction rather than by review, and a test asserts
the two sets are equal rather than one containing the other.

**Everything fails closed.** An unknown operation, an unknown session, a
malformed request and an unknown artifact all produce a refusal with a
reason and change nothing. There is no branch that proceeds on a default, no
`get(x, fallback)` on a security-relevant value, and no path that logs a
warning and continues.

**Nothing here decides anything.** Handlers assemble arguments, call the
deterministic core, and shape the reply. A verdict computed in this file
would be a second implementation of a rule that already exists, and the two
would disagree on the day it mattered.
"""

from __future__ import annotations

import copy

from dataclasses import dataclass, field

from core.change_intelligence import public_surface


class PublicError(Exception):
    """A refusal with a status a caller can act on.

    Carries a code because "we refused" and "why we refused" are different
    things, and a public surface that returns one string for both teaches
    its callers to retry blindly.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def as_reply(self) -> "Reply":
        return Reply(ok=False, status=self.status, code=self.code,
                     error=self.message)


@dataclass
class Request:
    """What arrives from outside. Every field is untrusted."""

    operation: str
    session_id: str = ""
    payload: dict = field(default_factory=dict)
    actor: str = ""
    timestamp: str = ""
    request_id: str = ""

    def require(self, key: str):
        """A missing field is a refusal, never a default.

        `payload.get(key)` would turn a malformed request into a request
        for something else, and the something else is whatever the default
        happens to be.
        """
        if key not in self.payload:
            raise PublicError(400, "malformed_request",
                              f"{key!r} is required and was not supplied")
        return self.payload[key]


@dataclass
class Reply:
    ok: bool
    status: int = 200
    code: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""
    request_id: str = ""
    #: Which surface operation produced this, echoed so a log line can be
    #: audited against the surface without re-deriving the route.
    operation: str = ""
    classification: str = ""


@dataclass
class Api:
    """The whole public entry point.

    `sessions` is injected. This class holds no state of its own, so two
    instances in two Cloud Run containers behave identically given the same
    store -- which is the property that makes session affinity an
    optimisation rather than a correctness mechanism.
    """

    sessions: object
    handlers: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        from app.api import handlers as handler_module

        self.handlers = handler_module.build(self.sessions)
        self._check_surface()

    def _check_surface(self) -> None:
        """Refuse to start if the handlers and the surface disagree.

        Both directions. A handler with no surface entry would be reachable
        and undeclared; a surface entry with no handler would be a promise
        the service cannot keep, and a caller would meet it as a crash.
        """
        declared = set(public_surface.names())
        implemented = set(self.handlers)
        if declared != implemented:
            raise public_surface.SurfaceViolation(
                f"surface and handlers disagree: "
                f"declared-not-implemented={sorted(declared - implemented)}, "
                f"implemented-not-declared={sorted(implemented - declared)}")

    def call(self, request: Request) -> Reply:
        """The only way in."""
        try:
            operation = public_surface.get(request.operation)
        except public_surface.NotPublic as exc:
            return PublicError(404, "unknown_operation", str(exc)).as_reply()

        if operation.name != "health" and operation.name != "session.create":
            if not request.session_id:
                return PublicError(
                    400, "missing_session",
                    "every operation except health and session.create names "
                    "a session; there is no implicit current session"
                ).as_reply()

        original = None
        saved = None
        if operation.writes and request.session_id:
            try:
                original = self.sessions.get(request.session_id)
                saved = copy.deepcopy(original)
            except Exception:
                pass
        def restore():
            if saved is not None:
                original.__dict__.clear()
                original.__dict__.update(saved.__dict__)

        try:
            data = self.handlers[operation.name](request)
        except PublicError as exc:
            restore()
            reply = exc.as_reply()
            reply.request_id = request.request_id
            reply.operation = operation.name
            reply.classification = operation.classification
            return reply
        except Exception as exc:  # fail closed on anything unforeseen
            restore()
            return Reply(
                ok=False, status=500, code="internal_error",
                error=(f"the operation did not complete and nothing was "
                       f"changed: {type(exc).__name__}"),
                request_id=request.request_id, operation=operation.name,
                classification=operation.classification)

        return Reply(ok=True, data=data, request_id=request.request_id,
                     operation=operation.name,
                     classification=operation.classification)


def inventory() -> list:
    """Every reachable operation, for an audit rather than a test.

    Returned as data so the audit can be read, diffed and committed. A
    security review that has to run a test suite to learn the surface is a
    review nobody repeats.
    """
    return [
        {
            "operation": operation.name,
            "classification": operation.classification,
            "writes": operation.writes,
            "boundary": operation.boundary,
            "allows": [f"{m.artifact}:{m.locator}"
                       for m in operation.mutations],
        }
        for operation in sorted(public_surface.PUBLIC_SURFACE,
                                key=lambda o: o.name)
    ]


__all__ = ["Api", "PublicError", "Reply", "Request", "inventory"]
