"""Quality evidence pins: every frozen prompt/schema/lexicon file that a measured AI role depends on is listed with its hash in
docs/evaluation/FINAL_AI_QUALITY_PLAN.md. Changing one of those files without updating the table fails here, so quality evidence recorded for an old
prompt can never be read as evidence for a new one. Pure file test: no DB, no provider.
"""
import hashlib
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "evaluation" / "FINAL_AI_QUALITY_PLAN.md"
ROW = re.compile(r"^\| (?P<role>[a-z][\w.]*) \| (?P<path>[\w./-]+) \| (?P<sha>[0-9a-f]{64}) \| (?P<evidence>.+?) \|$", re.M)
REQUIRED_ROLES = {"knowledge.qa", "change.understand", "change.impact_deps", "change.draft_guidance", "change.draft", "extraction", "retrieval.lexicon", "retrieval.semantic",
                  "knowledge.gap_proposals", "gap.answer_interpret", "document.wording", "knowledge.mining", "gap.interview",
                  "change.risk_memo", "ma.synthesis_memo", "ci.semantic_drift", "source.correspondence", "code.diff_proposal", "roi.analyst"}


def pinned_sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def rows():
    return {m["role"]: m for m in ROW.finditer(PLAN.read_text(encoding="utf-8"))}


def test_every_measured_role_is_pinned_and_each_pinned_file_matches_its_recorded_hash():
    pins = rows()
    assert REQUIRED_ROLES <= set(pins), sorted(REQUIRED_ROLES - set(pins))
    for role, m in pins.items():
        path = ROOT / m["path"]
        assert path.is_file(), f"{role}: {m['path']} missing"
        assert pinned_sha(path) == m["sha"], f"{role}: {m['path']} changed; update the pin and reset its evidence to none"


def test_no_pinned_row_claims_gate_evidence_on_this_build():
    """A pilot run may be cited, but only as a pilot that is not a quality gate; a row may never read as measured quality on this build."""
    for role, m in rows().items():
        ev = m["evidence"]
        assert "none" in ev or "older build" in ev or "did not meet" in ev or "measured limits" in ev, (role, ev)
        if "pilot" in ev:
            assert "not a quality gate" in ev and "gate evidence none" in ev, (role, ev)
