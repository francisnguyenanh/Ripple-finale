"""Publication scan of the FINAL tree including the export manifest (tools/pitch_pack/scan.py, scan_manifest): valid file names of listed, present files are not
"tokens"; secrets, emails, local/private paths, credential URLs, traversal and malformed content in the manifest are still findings. Pure files, no network.
The sensitive-looking values are assembled at run time, so this file itself carries none of them (it is part of the scanned tree)."""
from __future__ import annotations

import json

from tools.pitch_pack import scan as sc

NAME = "tests/ai8t02/" + "test_publish_f5_" + "manual_work.py"           # a real file name the entropy heuristic flags
AT = "@"
MAIL = "person" + AT + "corp-example" + ".net"
TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8"
DSN = "postgresql" + "://admin:" + "hunter2pass" + AT + "db.internal/x"
LOCAL = "C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "ripple_" + "state" + "\\ledger.json"
RANDOM = "Zx9" + "Q7mL2pV8rT4wY6uB1nK3sD5fG0hJ" + "aC"


def tree(tmp_path, manifest, files=(NAME,)):
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "SUBMISSION_MANIFEST.json").write_text(manifest if isinstance(manifest, str) else json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return [f for f in sc.scan_dir(tmp_path) if f.path == "SUBMISSION_MANIFEST.json"]


def rules(found):
    return sorted({f.rule for f in found})


def test_valid_listed_file_names_and_dotfiles_are_not_tokens_and_the_rest_of_the_manifest_is_scanned(tmp_path):
    listed = [NAME, ".gitattributes", ".github/workflows/offline-tests.yml"]
    assert tree(tmp_path, {"files": [{"path": p, "sha256": "a" * 64} for p in listed], "note": "self-check"}, files=listed) == []
    assert sc.scan_text("x.txt", f'"path": "{NAME}",', [])                                     # the same string elsewhere is still flagged (no blanket rule)


def test_sensitive_content_in_any_manifest_field_is_still_found(tmp_path):
    bad = {"files": [{"path": NAME, "sha256": "a" * 64}], "note": f"token {TOKEN} and {DSN}", "scan_summary": f"copied from {LOCAL}", "owner": MAIL, "extra": RANDOM}
    assert {"github_token", "dsn_with_password", "private_path", "email", "high_entropy_token"} <= set(rules(tree(tmp_path, bad)))


def test_invalid_or_absent_paths_and_a_malformed_manifest_are_findings(tmp_path):
    for p in ("../outside.txt", "/etc/passwd", "C:" + "/" + "Us" + "ers/x/a.py", "a\\b.py", "https://h/x", "~/x", "docs//x.md", "docs/./x.md", "missing/file.py"):
        assert "manifest_bad_path" in rules(tree(tmp_path, {"files": [{"path": p, "sha256": "a" * 64}]})), p
    assert "manifest_bad_hash" in rules(tree(tmp_path, {"files": [{"path": NAME, "sha256": "not-a-hash"}]}))
    assert "manifest_malformed" in rules(tree(tmp_path, '{"files": "nope"}'))
    assert "manifest_malformed" in rules(tree(tmp_path, "{not json"))
    hidden = "docs/owner." + MAIL + ".md"
    assert "email" in rules(tree(tmp_path, {"files": [{"path": hidden, "sha256": "a" * 64}]}, files=(hidden,)))   # a valid path line still gets every other rule
