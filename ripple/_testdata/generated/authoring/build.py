"""Unified orchestrator to build and package Harbor Procurement Mini bundles."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def build_bundle(
    root_dir: Path,
    bundle_id: str = "harbor_demo",
    seed: int = 20260925,
    ref_date: str = "2026-09-01",
    output_dir: Path = None,
) -> Path:
    if output_dir is None:
        target_input = root_dir / "bundles" / bundle_id / "input"
    else:
        target_input = output_dir

    print(f"=== Starting build for bundle '{bundle_id}' ===")
    print(f"Target directory: {target_input}")
    print(f"Seed: {seed}, Reference Date: {ref_date}")

    # 1. Prepare target directory
    target_input.mkdir(parents=True, exist_ok=True)

    # 2. Copy canonical templates (app, config, docs, tests, README, requirements)
    templates_dir = root_dir / "authoring" / "templates" / "input"
    print("Copying templates from authoring/templates/input...")
    shutil.copytree(templates_dir, target_input, dirs_exist_ok=True)

    # 3. Generate data records
    print("Generating deterministic data records...")
    from authoring.builders.generate_records import generate_all_data
    data_dir = target_input / "data"
    private_val_path = root_dir / "evaluator_private" / "validation" / "edge_cases_24.jsonl"
    catalog_path = root_dir / "authoring" / "builders" / "case_catalog.json"
    generate_all_data(data_dir, private_val_path, catalog_path, seed=seed, ref_date=ref_date)

    # 4. Build Excel workbooks (embedding C01: 500,000 JPY manager threshold)
    print("Building Excel workbooks (approval_matrix.xlsx, supplier_directory.xlsx, procurement_summary_202609.xlsx)...")
    from authoring.builders.build_workbooks import build_all_workbooks
    build_all_workbooks(data_dir)

    # 5. Render HTML forms
    print("Rendering 12 HTML invoices and 12 HTML goods receipts...")
    from authoring.builders.render_forms import render_all_forms
    forms_dir = target_input / "forms"
    render_all_forms(data_dir, forms_dir)

    # 6. Apply C02 mutation to docs/finance_desk_guide.md
    print("Applying C02 mutation to docs/finance_desk_guide.md...")
    fin_guide = target_input / "docs" / "finance_desk_guide.md"
    fin_guide.write_text(
        fin_guide.read_text(encoding="utf-8").replace(
            "capital expenditures (CAPEX) of **2,000,000 JPY or higher**",
            "all purchasing commitments of **2,000,000 JPY or higher**",
        ),
        encoding="utf-8"
    )

    # 7. Run characterization tests
    print("Running characterization tests...")
    test_res = subprocess.run(
        [sys.executable, "-m", "pytest", str(target_input / "tests")],
        capture_output=True,
        text=True,
        cwd=target_input,
    )
    if test_res.returncode != 0:
        print("ERROR: Characterization tests failed:\n", test_res.stderr, file=sys.stderr)
        sys.exit(1)
    print("Tests passed: 15/15.")

    # 8. Run private ID leakage scan
    print("Running leakage scan...")
    from evaluator_private.check_leakage import scan_directory
    findings = scan_directory(target_input)
    if findings:
        print(f"ERROR: {len(findings)} private ID leaks detected in public bundle!", file=sys.stderr)
        sys.exit(1)
    print("Leakage scan clean: 0 leaks.")

    # 9. Generate Evidence Manifest
    manifest_entries = []
    for f_path in sorted(target_input.rglob("*")):
        if f_path.is_file():
            rel = str(f_path.relative_to(target_input)).replace("\\", "/")
            manifest_entries.append({
                "path": rel,
                "size_bytes": f_path.stat().st_size,
                "sha256": compute_file_sha256(f_path),
            })

    manifest = {
        "bundle_id": bundle_id,
        "seed": seed,
        "reference_date": ref_date,
        "file_count": len(manifest_entries),
        "files": manifest_entries,
    }

    manifest_path = root_dir / "operator" / "evidence_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Build complete. Manifest written to {manifest_path} ({len(manifest_entries)} files tracked).")
    return target_input


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Harbor Procurement Mini - Bundle Builder")
    parser.add_argument("--bundle", default="harbor_demo", help="Bundle ID")
    parser.add_argument("--seed", type=int, default=20260925, help="Random seed")
    parser.add_argument("--reference-date", default="2026-09-01", help="Reference date")
    parser.add_argument("--output", type=Path, default=None, help="Explicit output directory")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    # Ensure root is in sys.path
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    build_bundle(root, bundle_id=args.bundle, seed=args.seed, ref_date=args.reference_date, output_dir=args.output)
