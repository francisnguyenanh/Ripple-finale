"""Tier 1: build the immutable baseline, in the layout the service reads.

Run:
    python -m tools.build_baseline [OUT_DIR]        # default work/baseline

Writes every seed artifact under OUT_DIR plus OUT_DIR/MANIFEST.json
({"baseline_id", "artifacts": {path: sha256}}), which is exactly what
infra.adapters.cloud_adapters.GCSBaselineStore loads. Refuses when the built
baseline_id differs from reference_system/reset/baseline_manifest.json: a
different id means different bytes (for example a checkout that rewrote line
endings), and the deployed service would then disagree with the repository.

Upload:
    gcloud storage cp -r OUT_DIR/* gs://<bucket>/baseline/
"""
import json
import pathlib
import sys

from reference_system.reset.baseline import build_baseline

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEED = ROOT / "reference_system"
COMMITTED = SEED / "reset" / "baseline_manifest.json"


def main(out_dir):
    baseline = build_baseline(SEED)
    committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
    if baseline["baseline_id"] != committed["baseline_id"]:
        changed = sorted(p for p, h in baseline["manifest"].items() if committed["manifest"].get(p) != h)
        sys.exit(f"baseline_id {baseline['baseline_id'][:12]} != committed {committed['baseline_id'][:12]}; "
                 f"changed: {', '.join(changed) or '(set of files)'}. See .gitattributes.")
    out = pathlib.Path(out_dir)
    for path, body in baseline["artifacts"].items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    (out / "MANIFEST.json").write_text(
        json.dumps({"baseline_id": baseline["baseline_id"], "artifacts": baseline["manifest"]},
                   indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return baseline


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "work" / "baseline")
    built = main(target)
    print(f"baseline {built['baseline_id'][:12]}: {built['artifact_count']} artifacts -> {target}")
