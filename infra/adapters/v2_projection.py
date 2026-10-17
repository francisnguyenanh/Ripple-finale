"""Realtime status projection (extension FB-R1 / FS-R1, local build): committed SQL rows -> minimal opaque hints in a sink, through a durable checkpoint.

What a hint says: "something in this scope changed; re-fetch the authorized API". It carries an opaque key (a hash, never a job id, artifact id, rule text, amount or locator),
a monotonic revision, the schema version and an expiry; nothing else. The sink is a port with a conditional monotonic put (an older revision is ignored); `MemorySink` is the
local/mock sink and simulates an outage. The checkpoint advances only after the sink accepted the whole batch, so a crash or outage replays the same rows (idempotent by key +
revision). The projector never creates jobs, documents or provider calls, and the API stays the only source of truth: a listener re-fetches, it never trusts a hint's content.
"""
from __future__ import annotations

import datetime
import hashlib

from infra.adapters.v2_business import read_snapshot

SCHEMA = "ripple-hint/1"
PROJECTOR_ID = "hints-v1"
HINT_TTL_SECONDS = 3600


class SinkUnavailable(Exception):
    pass


class MemorySink:
    """Local sink: {scope: {key: hint}} with the conditional monotonic update the real sink must implement. `outage=True` makes every put fail."""

    def __init__(self):
        self.docs: dict = {}
        self.outage = False
        self.puts = 0
        self.ignored_older = 0
        self.policies: dict = {}

    def put(self, scope: str, key: str, hint: dict) -> bool:
        if self.outage:
            raise SinkUnavailable("sink outage")
        self.puts += 1
        current = self.docs.setdefault(scope, {}).get(key)
        if current is not None and current["revision"] >= hint["revision"]:
            self.ignored_older += 1
            return False
        self.docs[scope][key] = dict(hint)
        return True

    def put_policy(self, feed: str, policy_version: int) -> bool:
        """The workspace's current membership policy version, which listener tokens must carry (monotonic: an older version is ignored)."""
        if self.outage:
            raise SinkUnavailable("sink outage")
        if self.policies.get(feed, -1) >= policy_version:
            return False
        self.policies[feed] = policy_version
        return True


def scope_for(tenant_id: str, workspace_id: str) -> str:
    """One feed per workspace in this build (the MVP of the plan: one user/workspace document-status flow); recipient-level partitions are a later profile."""
    return "ws/" + hashlib.sha256(f"{tenant_id}/{workspace_id}".encode("utf-8")).hexdigest()[:24]


def opaque_key(*parts: str) -> str:
    return hashlib.sha256("/".join(parts).encode("utf-8")).hexdigest()[:24]


def _checkpoint(cur, tenant_id, workspace_id, projector_id):
    cur.execute("SELECT outbox_watermark, documents_watermark, revision, projected_count, updated_at FROM v2_projection_checkpoints WHERE tenant_id=%s AND workspace_id=%s AND projector_id=%s",
                (tenant_id, workspace_id, projector_id))
    r = cur.fetchone()
    if r is None:
        return {"outbox_watermark": 0, "documents_watermark": datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc), "revision": 0, "projected_count": 0, "updated_at": None}
    return {"outbox_watermark": r[0], "documents_watermark": r[1], "revision": r[2], "projected_count": r[3], "updated_at": r[4]}


def pending(store, tenant_id, workspace_id, *, projector_id: str = PROJECTOR_ID) -> dict:
    with read_snapshot(store) as cur:
        cp = _checkpoint(cur, tenant_id, workspace_id, projector_id)
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND id > %s", (tenant_id, workspace_id, cp["outbox_watermark"]))
        lag_outbox = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s AND created_at > %s", (tenant_id, workspace_id, cp["documents_watermark"]))
        lag_docs = cur.fetchone()[0]
    return {"checkpoint": {**cp, "documents_watermark": cp["documents_watermark"].isoformat(), "updated_at": cp["updated_at"].isoformat() if cp["updated_at"] else None},
            "lag": {"outbox_rows": lag_outbox, "documents": lag_docs}}


def project_once(store, sink, tenant_id, workspace_id, *, projector_id: str = PROJECTOR_ID, batch: int = 100, now: datetime.datetime | None = None) -> dict:
    """One bounded pass: read committed rows past the checkpoint, put one hint per row (conditional monotonic), then advance the checkpoint. Outage -> nothing advances."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    scope = scope_for(tenant_id, workspace_id)
    with read_snapshot(store) as cur:
        cp = _checkpoint(cur, tenant_id, workspace_id, projector_id)
        cur.execute("SELECT id, kind, payload->>'kind', payload->>'job_id' FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND id > %s ORDER BY id LIMIT %s",
                    (tenant_id, workspace_id, cp["outbox_watermark"], batch))
        outbox_rows = cur.fetchall()
        cur.execute("SELECT artifact_id, created_at FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s AND created_at > %s ORDER BY created_at, artifact_id LIMIT %s",
                    (tenant_id, workspace_id, cp["documents_watermark"], batch))
        doc_rows = cur.fetchall()
    revision, hints = cp["revision"], []
    expires = (now + datetime.timedelta(seconds=HINT_TTL_SECONDS)).isoformat()
    for oid, kind, job_kind, job_id in outbox_rows:
        revision += 1
        family = "work" if kind == "JOB_QUEUED" else "knowledge" if kind == "SOURCE_PUBLISHED" else "change" if kind == "CHANGE_COMPLETED" else "other"
        key = opaque_key(tenant_id, workspace_id, "job", job_id or "") if job_id else opaque_key(tenant_id, workspace_id, kind, str(oid))
        hints.append({"schema": SCHEMA, "key": key, "family": family, "revision": revision, "changed_at": now.isoformat(), "expires_at": expires})
    for artifact_id, created_at in doc_rows:
        revision += 1
        hints.append({"schema": SCHEMA, "key": opaque_key(tenant_id, workspace_id, "document", artifact_id), "family": "document", "revision": revision, "changed_at": now.isoformat(), "expires_at": expires})
    written = ignored = 0
    for h in hints:                                                                  # a failing sink raises here: the checkpoint below is never reached (replay on the next pass)
        if sink.put(scope, h["key"], h):
            written += 1
        else:
            ignored += 1
    if hints:
        with store._tx() as cur:
            cur.execute("""INSERT INTO v2_projection_checkpoints (tenant_id, workspace_id, projector_id, outbox_watermark, documents_watermark, revision, projected_count, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (tenant_id, workspace_id, projector_id) DO UPDATE SET outbox_watermark=GREATEST(v2_projection_checkpoints.outbox_watermark, EXCLUDED.outbox_watermark),
                    documents_watermark=GREATEST(v2_projection_checkpoints.documents_watermark, EXCLUDED.documents_watermark), revision=GREATEST(v2_projection_checkpoints.revision, EXCLUDED.revision),
                    projected_count=v2_projection_checkpoints.projected_count + %s, updated_at=now()""",
                        (tenant_id, workspace_id, projector_id, max([cp["outbox_watermark"]] + [r[0] for r in outbox_rows]), max([cp["documents_watermark"]] + [r[1] for r in doc_rows]), revision, written, written))
    return {"scope": scope, "hints": len(hints), "written": written, "ignored_older": ignored, "revision": revision, **pending(store, tenant_id, workspace_id, projector_id=projector_id)}


# ---- FS-C1: conversation read model (interview sessions, multi-agent runs) -------------------------------------------------------------------------------------

CONV_SCHEMA = "ripple-conv-hint/1"
CONV_PROJECTOR_ID = "conversations-v1"
PROFILES = ("redacted", "analyst")


def conv_scope_for(tenant_id: str, workspace_id: str, profile: str) -> str:
    return f"conv/{profile}/" + hashlib.sha256(f"{tenant_id}/{workspace_id}/{profile}".encode("utf-8")).hexdigest()[:24]


def feed_id(scope: str) -> str:
    """The sink's document-path form of a scope ("/" -> "~"); also the value a client token lists in `ripple_feeds`."""
    return scope.replace("/", "~")


def feed_grants(tenant_id: str, workspace_id: str, permissions) -> dict:
    """The feeds a member of this workspace may listen to, from the CURRENT permissions: the workspace hint feed and the redacted conversation feed for every member,
    the analyst conversation feed only with `knowledge.confirm` (the same rule as the analyst projection). This is the content of the `ripple_feeds` claim the Firestore
    rules check; issuing a token that carries it is the activation step (not built: `token_issued` is false)."""
    scopes = [scope_for(tenant_id, workspace_id), conv_scope_for(tenant_id, workspace_id, "redacted")]
    if "knowledge.confirm" in permissions:
        scopes.append(conv_scope_for(tenant_id, workspace_id, "analyst"))
    return {"schema": "ripple-feeds/1", "claim": "ripple_feeds", "feeds": [feed_id(s) for s in scopes], "token_issued": False}


def project_conversations(store, sink, tenant_id, workspace_id, *, profile: str = "redacted", batch: int = 100, now: datetime.datetime | None = None) -> dict:
    """Redacted read model of the canonical conversations: one bounded document per interview turn (state and, for the `analyst` profile, the server's QUESTION text; never the
    person's answer, never a model prompt) and one per multi-agent run (status and counts; never a finding statement). Canonical rows stay in SQL; the projection is rebuilt from
    the checkpoint (`updated_at` watermark) and is disposable. Same monotonic/outage rules as the status hints."""
    if profile not in PROFILES:
        raise ValueError("profile")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    pid = f"{CONV_PROJECTOR_ID}:{profile}"
    scope = conv_scope_for(tenant_id, workspace_id, profile)
    with read_snapshot(store) as cur:
        cp = _checkpoint(cur, tenant_id, workspace_id, pid)
        cur.execute("SELECT session_id, gap_id, status, turns, cap, updated_at FROM v2_gap_interviews WHERE tenant_id=%s AND workspace_id=%s AND updated_at > %s ORDER BY updated_at, session_id LIMIT %s",
                    (tenant_id, workspace_id, cp["documents_watermark"], batch))
        sessions = cur.fetchall()
        cur.execute("SELECT run_id, workflow, status, report, updated_at FROM v2_ma_runs WHERE tenant_id=%s AND workspace_id=%s AND updated_at > %s ORDER BY updated_at, run_id LIMIT %s",
                    (tenant_id, workspace_id, cp["documents_watermark"], batch))
        runs = cur.fetchall()
    from infra.adapters.v2_business import _as_object
    revision, docs, latest = cp["revision"], [], cp["documents_watermark"]
    expires = (now + datetime.timedelta(seconds=HINT_TTL_SECONDS)).isoformat()
    for session_id, gap_id, status, turns, cap, updated_at in sessions:
        latest = max(latest, updated_at)
        turns = _as_object(turns) or []
        for t in turns:
            revision += 1
            state = "answered" if t.get("text") is not None and t.get("reading") is None and not t.get("unknown") else "read" if t.get("reading") is not None else "unknown_declared" if t.get("unknown") else "asked" if t.get("question") else "pending"
            doc = {"schema": CONV_SCHEMA, "kind": "interview_turn", "key": opaque_key(tenant_id, workspace_id, "interview", session_id, str(t.get("turn"))), "session_key": opaque_key(tenant_id, workspace_id, "interview", session_id),
                   "turn": t.get("turn"), "of": cap, "state": state, "session_status": status, "revision": revision, "changed_at": now.isoformat(), "expires_at": expires}
            if profile == "analyst":
                doc["question"] = t.get("question")
                doc["reading_status"] = t.get("reading_status")
                doc["questioner"] = t.get("questioner")
            docs.append(doc)
    for run_id, workflow, status, report, updated_at in runs:
        latest = max(latest, updated_at)
        revision += 1
        rep = _as_object(report) or {}
        findings = rep.get("findings") or []
        doc = {"schema": CONV_SCHEMA, "kind": "ma_run", "key": opaque_key(tenant_id, workspace_id, "ma_run", run_id), "workflow": workflow, "status": status, "revision": revision,
               "changed_at": now.isoformat(), "expires_at": expires}
        if profile == "analyst":
            doc.update(findings=len(findings), critical=sum(1 for f in findings if f.get("severity") == "critical"), unresolved=len(rep.get("unresolved_disagreements") or []),
                       missing_required_roles=len(rep.get("missing_required_roles") or []), report_hash=rep.get("report_hash"))
        docs.append(doc)
    written = ignored = 0
    for d in docs:
        if sink.put(scope, d["key"], d):
            written += 1
        else:
            ignored += 1
    if docs:
        with store._tx() as cur:
            cur.execute("""INSERT INTO v2_projection_checkpoints (tenant_id, workspace_id, projector_id, outbox_watermark, documents_watermark, revision, projected_count, updated_at)
                VALUES (%s,%s,%s,0,%s,%s,%s,now())
                ON CONFLICT (tenant_id, workspace_id, projector_id) DO UPDATE SET documents_watermark=GREATEST(v2_projection_checkpoints.documents_watermark, EXCLUDED.documents_watermark),
                    revision=GREATEST(v2_projection_checkpoints.revision, EXCLUDED.revision), projected_count=v2_projection_checkpoints.projected_count + %s, updated_at=now()""",
                        (tenant_id, workspace_id, pid, latest, revision, written, written))
    return {"scope": scope, "profile": profile, "documents": len(docs), "written": written, "ignored_older": ignored, "revision": revision, "sessions": len(sessions), "runs": len(runs)}


def status(store, tenant_id, workspace_id, *, sink=None) -> dict:
    out = pending(store, tenant_id, workspace_id)
    out.update(schema=SCHEMA, projector_id=PROJECTOR_ID, scope=scope_for(tenant_id, workspace_id), sink=getattr(sink, "kind", "memory") if sink is not None else "off",
               statement="Hints only say that something changed; the authorized API is the truth. A lag above zero means the feed is behind the committed state; a client keeps bounded polling as its fallback.")
    return out
