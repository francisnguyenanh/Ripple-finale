"""The committed baseline id is what a fresh checkout builds.

A Windows clone with core.autocrlf=true once built f583155a06d7 instead of
the committed and deployed b376ac0a4b62, because the seed's line endings were
rewritten on checkout. .gitattributes now forbids that; this test catches
any other way the bytes drift from the pinned id.
"""
import hashlib
import json
import pathlib

from reference_system.reset.baseline import build_baseline
from tools import build_baseline as tool

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEED = ROOT / "reference_system"


def test_the_seed_builds_the_committed_baseline_id():
    committed = json.loads((SEED / "reset" / "baseline_manifest.json").read_text(encoding="utf-8"))
    built = build_baseline(SEED)
    assert built["manifest"] == committed["manifest"]
    assert built["baseline_id"] == committed["baseline_id"]


def test_the_seed_is_never_converted_on_checkout():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "reference_system/** -text" in attributes


def test_the_tool_writes_the_layout_the_gcs_store_reads(tmp_path):
    built = tool.main(tmp_path)
    manifest = json.loads((tmp_path / "MANIFEST.json").read_text(encoding="utf-8"))
    assert set(manifest) == {"baseline_id", "artifacts"}      # GCSBaselineStore._load_manifest
    assert manifest["baseline_id"] == built["baseline_id"]
    for path, digest in manifest["artifacts"].items():
        assert hashlib.sha256((tmp_path / path).read_bytes()).hexdigest() == digest
    assert len(manifest["artifacts"]) == built["artifact_count"] == 20
