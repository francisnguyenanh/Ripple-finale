"""Scoped evidence validator, pure (no I/O, no filesystem/network).

Consumes the typed locator/observation contract
(`PostgresV2Store.source_observations`) and validates a caller-submitted
`EvidenceRef` against the observations that were actually persisted for one
committed source revision. This module never opens a file, follows a URL, or
accepts a caller-supplied path -- a ref may only name an `artifact_id` that
already exists in the pinned revision (contracts C09); anything that looks
like a filesystem/network reference is rejected outright (`invalid_reference`).

`citation_valid` (this module) is deliberately kept separate from semantic
"claim_supported": a syntactically/positionally correct quote does not mean
the claim built on it is correct. This module never sets `support` to a
confirmed value; it can only report that a citation is valid and therefore
UNASSESSED for entailment, or invalid with a reason. Assessing entailment is
a reviewer/human job, not a locator check.

Validation rules, recorded here rather than merely asserted:

- **Required hash and parser version.** contracts §2.1 defines `EvidenceRef` as source/artifact
  revision + CONTENT HASH + typed locator + quote/raw_value + parser/extractor
  VERSION -- none of these are optional in the contract. `blob_hash` and
  `parser_version` are therefore now REQUIRED fields on every ref; a ref that
  omits either is rejected (`missing_hash`/`missing_parser_version`) before
  any artifact/observation lookup runs, rather than silently skipping the
  check it cannot perform without them.
- **Quote and raw_value.** When a ref supplies both `quote` and `raw_value`, BOTH are
  now validated against the actual observation; disagreement between a
  correct quote and a fabricated `raw_value` (or vice versa) is rejected as
  `conflicting_evidence`, not silently passed on whichever field happened to
  be checked. Quote fidelity is now case-SENSITIVE (only interior whitespace
  runs are collapsed, to tolerate a wrapped line) -- comparing case-folded
  text made an altered-case quote look like the same evidence, which is
  exactly the silent-alteration risk contracts C-family evidence rules
  exist to prevent.
- **Evidence read.** A valid citation's full evidence (source revision, artifact
  id + blob hash, typed locator, parser version, and the exact raw
  observation value/quote) is now RETURNED in the report -- this is the
  "evidence read" surface a UI renders next to a claim, not merely a
  boolean. A ref may also carry an opaque, caller-assigned `claim_id`, which
  is echoed back unchanged in the report: (parser_version, artifact_id,
  locator, observation_id) is the parser->chunk provenance chain; echoing
  `claim_id` alongside it is the chunk->claim half of the mapping. See `PostgresV2Store.read_evidence` for the
  companion direct-by-`observation_id` read path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

#: Fields that would make a ref a disguised filesystem/network reference
#: rather than an opaque reference into an already-committed revision's
#: observations (same defence-in-depth idea as
#: `infra.v2_source_routes._REFERENCE_FIELDS`).
_FORBIDDEN_REF_FIELDS = ("path", "file", "url", "source_url", "local_path")


@dataclass(frozen=True)
class EvidenceValidationReport:
    ref_index: int
    artifact_id: Optional[str]
    citation_valid: bool
    reason: Optional[str]
    observation_id: Optional[str]
    #: "UNASSESSED" when citation_valid; None when the citation itself failed
    #: (there is nothing to assess entailment for).
    support: Optional[str]
    #: Echoed back unchanged from the ref -- the chunk->claim half of the
    #: provenance mapping. None if the ref did not supply one.
    claim_id: Optional[str] = None
    #: Full evidence for a valid citation (the "evidence read" output);
    #: None when citation_valid is False.
    evidence: Optional[dict] = None

    def to_dict(self) -> dict:
        return {"ref_index": self.ref_index, "artifact_id": self.artifact_id,
                "citation_valid": self.citation_valid, "reason": self.reason,
                "observation_id": self.observation_id, "support": self.support,
                "claim_id": self.claim_id, "evidence": self.evidence}


def _normalise(text: str) -> str:
    """Whitespace-only fold. Deliberately NOT case-folded: a quote
    whose case was silently changed must not compare equal to the source."""
    return re.sub(r"\s+", " ", str(text)).strip()


def _invalid(ref_index: int, artifact_id: Optional[str], reason: str,
            claim_id: Optional[str] = None) -> EvidenceValidationReport:
    return EvidenceValidationReport(ref_index=ref_index, artifact_id=artifact_id,
                                    citation_valid=False, reason=reason,
                                    observation_id=None, support=None, claim_id=claim_id)


def validate_ref(
    ref_index: int,
    ref: Mapping[str, object],
    *,
    revision_id: str,
    artifacts_by_id: Mapping[str, Mapping[str, object]],
    observations: list[Mapping[str, object]],
) -> EvidenceValidationReport:
    """Validate one `EvidenceRef` against one committed revision's real
    artifacts/observations. Pure: every input is already-fetched data, no
    store access happens here.

    `artifacts_by_id`: `{artifact_id: {blob_hash, logical_path, ...}}` from
    `source_revision_view(revision_id)["artifacts"]`.
    `observations`: `source_observations(revision_id)` rows (or an explicit
    per-parser-version query when the caller pins one).
    """
    if not isinstance(ref, Mapping):
        return _invalid(ref_index, None, "malformed_reference")

    claim_id = ref.get("claim_id")
    if claim_id is not None and not isinstance(claim_id, str):
        return _invalid(ref_index, ref.get("artifact_id"), "malformed_reference")

    bad_fields = [f for f in _FORBIDDEN_REF_FIELDS if f in ref]
    if bad_fields:
        return _invalid(ref_index, ref.get("artifact_id"), "invalid_reference", claim_id)

    ref_revision_id = ref.get("source_revision_id")
    if ref_revision_id is not None and ref_revision_id != revision_id:
        # C09/contracts §2.1: an EvidenceRef is bound to one revision's
        # content hash. A ref that names a different revision than the one
        # being validated is a cross-snapshot quote, not a resolvable claim
        # here -- the caller must validate it against ITS OWN revision.
        return _invalid(ref_index, ref.get("artifact_id"), "cross_snapshot_reference", claim_id)

    artifact_id = ref.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        return _invalid(ref_index, None, "fabricated_reference", claim_id)

    locator = ref.get("locator")
    if not isinstance(locator, Mapping) or not locator:
        return _invalid(ref_index, artifact_id, "malformed_locator", claim_id)

    # `blob_hash` and `parser_version` are REQUIRED parts of the
    # EvidenceRef identity (contracts §2.1), not optional pins checked
    # only when present -- a ref that omits either can never be verified
    # against the exact bytes/extraction it claims to cite.
    ref_hash = ref.get("blob_hash")
    if not isinstance(ref_hash, str) or not ref_hash:
        return _invalid(ref_index, artifact_id, "missing_hash", claim_id)
    ref_parser_version = ref.get("parser_version")
    if not isinstance(ref_parser_version, str) or not ref_parser_version:
        return _invalid(ref_index, artifact_id, "missing_parser_version", claim_id)

    # A field's PRESENCE in the ref, not its
    # truthiness, is what "supplied" means -- `ref.get("raw_value") is not
    # None` treats an explicit `raw_value: null` exactly like the caller
    # never sent the field at all, so a real quote plus a null raw_value
    # slipped through as `citation_valid=true`. Presence is checked with the
    # `in` operator so JSON `null` is a supplied (and here, contradictory)
    # value, never silently equivalent to omission.
    quote_present, raw_value_present = "quote" in ref, "raw_value" in ref
    quote, raw_value = ref.get("quote"), ref.get("raw_value")
    if not quote_present and not raw_value_present:
        return _invalid(ref_index, artifact_id, "empty_evidence", claim_id)
    if quote_present and (quote is None or (isinstance(quote, str) and not quote.strip())
                          or not isinstance(quote, str)):
        return _invalid(ref_index, artifact_id, "empty_evidence", claim_id)

    artifact = artifacts_by_id.get(artifact_id)
    if artifact is None:
        # Covers both a fabricated artifact_id and an artifact that existed
        # in a DIFFERENT revision but not this one (e.g. deleted/renamed
        # since).
        return _invalid(ref_index, artifact_id, "artifact_not_found", claim_id)

    if ref_hash != artifact.get("blob_hash"):
        return _invalid(ref_index, artifact_id, "hash_mismatch", claim_id)

    candidates = [o for o in observations
                  if o["artifact_id"] == artifact_id and o["locator"] == locator
                  and o["parser_version"] == ref_parser_version]
    if not candidates:
        return _invalid(ref_index, artifact_id, "locator_not_found", claim_id)

    observation = candidates[0]
    actual = observation.get("raw_value")

    # Validate EVERY field the ref actually SUPPLIED (by presence,
    # not truthiness -- an explicit null is supplied), not just one branch --
    # a correct quote alongside a fabricated OR NULL `raw_value` (or the
    # reverse) must not be certified `citation_valid=True`.
    if quote_present:
        if not isinstance(actual, str) or _normalise(quote) not in _normalise(actual):
            return _invalid(ref_index, artifact_id, "quote_mismatch", claim_id)
    if raw_value_present and raw_value != actual:
        if quote_present:
            return _invalid(ref_index, artifact_id, "conflicting_evidence", claim_id)
        return _invalid(ref_index, artifact_id, "value_mismatch", claim_id)

    evidence = {"source_revision_id": revision_id, "artifact_id": artifact_id,
                "blob_hash": artifact.get("blob_hash"), "locator": observation["locator"],
                "parser_version": observation["parser_version"],
                "raw_value": actual, "observation_id": observation["observation_id"]}

    return EvidenceValidationReport(
        ref_index=ref_index, artifact_id=artifact_id, citation_valid=True,
        reason=None, observation_id=observation["observation_id"], support="UNASSESSED",
        claim_id=claim_id, evidence=evidence)


def validate_refs(
    refs: list[Mapping[str, object]],
    *,
    revision_id: str,
    artifacts_by_id: Mapping[str, Mapping[str, object]],
    observations: list[Mapping[str, object]],
) -> list[EvidenceValidationReport]:
    return [validate_ref(i, ref, revision_id=revision_id, artifacts_by_id=artifacts_by_id,
                         observations=observations)
            for i, ref in enumerate(refs)]
