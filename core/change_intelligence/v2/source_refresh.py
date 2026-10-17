"""Pure diff / rename-proof / digest logic of a MANUAL source refresh.

A refresh is an external OBSERVATION: a human uploads the whole new snapshot of a
workspace's source (same admission as an import) and Ripple diffs it against the
CURRENT head. This module is I/O free so the routes, the store and the worker all
share exactly one definition of "what changed".

Matching key of an uploaded file to a head artifact = the normalised logical path
(`source_admission.collision_key`). An artifact WITHOUT a logical path (published by
a fixture or by an apply) is addressed by its artifact id used as the path (D3).
Comparison is per artifact on the WHOLE-content digest (`blob_hash`, sha256 of every
byte): a changed last byte, an appended tail or a swap of two artifacts' contents is
a change; a set-of-hashes equality is never used.

Rename identity is never guessed from similar names or similar content. It exists
only with an explicit proof: `content_hash` (the server verifies the uploaded bytes
are identical to the deleted artifact's bytes) or `human_confirmed` (a named reason).
Without a proof a rename is a delete + add with a NEW artifact identity.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from core.change_intelligence.v2.serialize import canonical_hash
from core.change_intelligence.v2.source_admission import collision_key

PROVENANCE_KIND = "external_observation"
PROOF_KINDS = ("content_hash", "human_confirmed")
_PROOF_FIELDS = frozenset({"from_path", "to_path", "kind", "reason"})
MAX_RENAME_PROOFS = 500


class RefreshProofError(ValueError):
    """A rename proof is malformed, unknown or forged; nothing may be prepared."""

    error_code = "rename_proof_invalid"


def head_key(row: Mapping) -> str:
    return collision_key(row.get("logical_path") or row["artifact_id"])


def head_name(row: Mapping) -> str:
    return row.get("logical_path") or row["artifact_id"]


def normalise_proofs(proofs) -> list[dict]:
    """Validate the SHAPE of `rename_proofs` (never the truth) and return canonical
    dicts. Truth (paths exist, bytes identical) is checked in `compute_diff`."""
    if proofs is None:
        return []
    if not isinstance(proofs, list) or len(proofs) > MAX_RENAME_PROOFS:
        raise RefreshProofError("rename_proofs must be a bounded list")
    out = []
    for proof in proofs:
        if not isinstance(proof, dict) or set(proof) - _PROOF_FIELDS:
            raise RefreshProofError("rename proof must be an object with from_path,to_path,kind,reason")
        frm, to, kind = proof.get("from_path"), proof.get("to_path"), proof.get("kind")
        if not all(isinstance(x, str) and x.strip() for x in (frm, to)):
            raise RefreshProofError("rename proof needs from_path and to_path")
        if kind not in PROOF_KINDS:
            raise RefreshProofError(f"unknown rename proof kind {kind!r}")
        reason = proof.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise RefreshProofError("rename proof reason must be text")
        if kind == "human_confirmed" and not (reason and reason.strip()):
            raise RefreshProofError("a human_confirmed rename proof needs a reason")
        entry = {"from_path": frm, "to_path": to, "kind": kind}
        if reason and reason.strip():
            entry["reason"] = reason.strip()
        out.append(entry)
    return out


def compute_diff(head_rows: Sequence[Mapping], upload_items: Sequence[Mapping],
                 rename_proofs=None) -> dict:
    """Diff of the uploaded WHOLE snapshot against the head's rows.

    `head_rows`: `{artifact_id, logical_path|None, blob_hash}`; `upload_items`:
    admitted `{logical_path, blob_hash}`. Returns `{added, changed, deleted, renamed,
    unchanged_count, noop, plan}` where `plan` maps every uploaded normalised key to
    the artifact id it keeps (None = brand new identity). Raises `RefreshProofError`
    for any proof that is unknown, forged or ambiguous."""
    proofs = normalise_proofs(rename_proofs)
    head = {}
    for row in head_rows:
        key = head_key(row)
        if key in head:
            raise RefreshProofError(f"head has two artifacts for path key {key!r}")
        head[key] = row
    upload = {}
    for item in upload_items:
        upload[collision_key(item["logical_path"])] = item

    added_keys = sorted(k for k in upload if k not in head)
    deleted_keys = sorted(k for k in head if k not in upload)

    renamed, seen_from, seen_to = [], set(), set()
    plan = {}
    for proof in proofs:
        from_key, to_key = collision_key(proof["from_path"]), collision_key(proof["to_path"])
        if from_key in seen_from or to_key in seen_to:
            raise RefreshProofError("two rename proofs share a source or a target path")
        if from_key not in deleted_keys:
            raise RefreshProofError(f"rename source {proof['from_path']!r} is not a deleted artifact")
        if to_key not in added_keys:
            raise RefreshProofError(f"rename target {proof['to_path']!r} is not a newly uploaded path")
        source, target = head[from_key], upload[to_key]
        if proof["kind"] == "content_hash" and source["blob_hash"] != target["blob_hash"]:
            raise RefreshProofError("content_hash proof: the uploaded bytes differ from the artifact")
        seen_from.add(from_key)
        seen_to.add(to_key)
        entry = {"artifact_id": source["artifact_id"], "from_path": head_name(source),
                 "to_path": target["logical_path"], "proof_kind": proof["kind"]}
        if proof.get("reason"):
            entry["reason"] = proof["reason"]
        renamed.append(entry)
        plan[to_key] = source["artifact_id"]

    changed, unchanged = [], 0
    for key, item in upload.items():
        if key in head:
            row = head[key]
            plan[key] = row["artifact_id"]
            if row["blob_hash"] == item["blob_hash"]:
                unchanged += 1
            else:
                changed.append({"artifact_id": row["artifact_id"], "logical_path": item["logical_path"],
                                "before_hash": row["blob_hash"], "after_hash": item["blob_hash"]})
    for entry in renamed:
        to_key = collision_key(entry["to_path"])
        source_hash = head[collision_key(entry["from_path"])]["blob_hash"]
        if upload[to_key]["blob_hash"] != source_hash:
            changed.append({"artifact_id": entry["artifact_id"], "logical_path": entry["to_path"],
                            "before_hash": source_hash, "after_hash": upload[to_key]["blob_hash"]})

    added = [{"logical_path": upload[k]["logical_path"], "after_hash": upload[k]["blob_hash"]}
             for k in added_keys if k not in seen_to]
    for k in added_keys:
        if k not in seen_to:
            plan[k] = None
    deleted = [{"artifact_id": head[k]["artifact_id"], "logical_path": head_name(head[k]),
                "before_hash": head[k]["blob_hash"]} for k in deleted_keys if k not in seen_from]
    changed.sort(key=lambda c: c["logical_path"])
    renamed.sort(key=lambda r: r["to_path"])
    return {"added": added, "changed": changed, "deleted": deleted, "renamed": renamed,
            "unchanged_count": unchanged,
            "noop": not (added or changed or deleted or renamed), "plan": plan}


def public_diff(diff: Mapping) -> dict:
    return {k: diff[k] for k in ("added", "changed", "deleted", "renamed", "unchanged_count")}


def refresh_digest(base: Mapping, diff: Mapping, upload_items: Sequence[Mapping],
                   rename_proofs=None) -> str:
    """Digest a human commits against: the base head tuple, the diff, the exact uploaded
    manifest (path -> blob hash) and the proofs. A different upload or different base
    can never share a digest."""
    return canonical_hash({
        "base": {"version": base["version"], "source_revision_id": base["source_revision_id"]},
        "diff": public_diff(diff),
        "upload": sorted([i["logical_path"], i["blob_hash"]] for i in upload_items),
        "proofs": normalise_proofs(rename_proofs)})
