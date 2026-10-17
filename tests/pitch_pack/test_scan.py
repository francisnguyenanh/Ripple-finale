"""Scanner/manifest tests with synthetic fixtures. Fake secrets are assembled at runtime so this file stays clean."""
import json

from tools.pitch_pack import manifest, scan

FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_GH = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"
FAKE_ASSIGN_VAL = "Zq8Xw3Lm9Rt2Vb7Nc4Hy"


def _w(p, rel, content):
    f = p / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        f.write_bytes(content)
    else:
        f.write_bytes(content.encode("utf-8"))
    return f


def test_planted_token_caught_and_masked(tmp_path, capsys):
    _w(tmp_path, "a.py", f'KEY = "{FAKE_AWS}"\nx = "{FAKE_GH}"\napi_token = "{FAKE_ASSIGN_VAL}"\n')
    rc = scan.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    for rule in ("aws_access_key", "github_token", "secret_assignment"):
        assert rule in out
    for secret in (FAKE_AWS, FAKE_GH, FAKE_ASSIGN_VAL):
        assert secret not in out
        assert secret[2:] not in out


def test_clean_dir_exit_zero(tmp_path):
    _w(tmp_path, "README.md", "Hello. Contact: noreply@example.com. sha256 " + "a" * 64 + "\n")
    assert scan.main([str(tmp_path)]) == 0


def test_email_private_path_and_dirs(tmp_path):
    _w(tmp_path, "n.md", "mail bob@corp.test and C:\\Users\\someone\\x and ripple_handover\n")
    _w(tmp_path, ".process_runs/r.log", "x")
    _w(tmp_path, ".git/config", "x")
    _w(tmp_path, "scratchpad/n.txt", "x")
    fs = scan.scan_dir(tmp_path)
    assert {"email", "private_path", "forbidden_dir"} <= {f.rule for f in fs}
    dirs = {f.masked for f in fs if f.rule == "forbidden_dir"}
    assert {".process_runs", ".git", "scratchpad"} <= dirs
    assert all("someone" not in f.render() and "bob@" not in f.render() for f in fs)


def test_private_split_labels_and_terms(tmp_path):
    _w(tmp_path, "eval/private/data.yaml", "x: 1\n")
    _w(tmp_path, "eval/gold_rules.yaml", "x: 1\n")
    _w(tmp_path, "eval/aliases.json", "{}")
    _w(tmp_path, "doc.md", "see PRIVATE-ID-42 here\n")
    terms = tmp_path.parent / (tmp_path.name + "_terms.txt")
    terms.write_text("PRIVATE-ID-42\n", encoding="utf-8")
    fs = scan.scan_dir(tmp_path, scan.load_private_terms(str(terms)))
    paths = {f.path for f in fs if f.rule == "private_split_or_labels_path"}
    assert {"eval/private/data.yaml", "eval/gold_rules.yaml", "eval/aliases.json"} <= paths
    assert any(f.rule == "private_split_term" for f in fs)
    assert all("PRIVATE-ID-42" not in f.render() for f in fs)


def test_large_files(tmp_path):
    _w(tmp_path, "big.bin", b"\x00" * 200_000)
    _w(tmp_path, "huge.txt", "a" * 500)
    rules = {f.rule for f in scan.scan_dir(tmp_path, max_bytes=300)}
    assert "large_file" in rules  # both exceed 300 bytes
    assert "large_binary" not in rules
    assert "large_binary" in {f.rule for f in scan.scan_dir(tmp_path, max_bytes=10_000_000)}


def test_high_entropy_token(tmp_path):
    tok = "Q7x9Zp2Lk4Mv8Bn3Cw6Td1Hs5Jr0FgaY2uE9"
    _w(tmp_path, "c.txt", f"blob {tok}\n")
    fs = scan.scan_dir(tmp_path)
    assert any(f.rule == "high_entropy_token" for f in fs)
    assert all(tok not in f.render() for f in fs)


def test_manifest_hashes_and_excludes_self(tmp_path):
    _w(tmp_path, "a.txt", "one\r\n")
    _w(tmp_path, "sub/b.txt", "two\n")
    out = tmp_path / "MANIFEST.json"
    assert manifest.main([str(tmp_path), "--out", str(out), "--source-commit", "abc"]) == 0
    m = json.loads(out.read_text())
    assert [f["path"] for f in m["files"]] == ["a.txt", "sub/b.txt"]
    assert m["files"][0]["sha256_lf"] != m["files"][0]["sha256"]
    assert m["source_commit"] == "abc" and len(m["overall_sha256"]) == 64
    assert manifest.main([str(tmp_path), "--out", str(out), "--source-commit", "abc"]) == 0
    assert json.loads(out.read_text())["overall_sha256"] == m["overall_sha256"]


# ---------------------------------------------------------------------------- allow-list: narrow exceptions, real secrets still caught
import json as _json
from pathlib import Path as _Path

_ALLOW = str(_Path(__file__).resolve().parents[2] / "tools" / "pitch_pack" / "scan_allowlist.json")


def _scan(tmp_path, capsys, extra=()):
    rc = scan.main([str(tmp_path), "--allowlist", _ALLOW, *extra])
    return rc, capsys.readouterr().out


def test_allowlist_entries_need_a_reason(tmp_path):
    bad = tmp_path / "a.json"
    bad.write_text(_json.dumps([{"rule": "email", "path_glob": "*", "reason": "short"}]), encoding="utf-8")
    import pytest
    with pytest.raises(SystemExit):
        scan.load_allowlist(str(bad))


def test_known_false_positives_are_allow_listed_and_reported_separately(tmp_path, capsys):
    _w(tmp_path, "frontend/package-lock.json", '"integrity": "sha512-' + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5xQ2wE4rT6yU8iO0pA/bC==" + '"\n')
    _w(tmp_path, "ripple/_testdata/suppliers.csv", "S1,Acme,buyer@partner01.example\n")
    _w(tmp_path, "infra/worker/m.py", "input_tokens = usage.total_tokens\n")
    rc, out = _scan(tmp_path, capsys)
    assert rc == 0 and "allow-listed=" in out and "findings=0" in out


def test_allow_list_does_not_reach_outside_its_paths(tmp_path, capsys):
    """The same harmless SHAPES are findings in a place that has no entry: an entry is limited to its path, not to a shape alone."""
    _w(tmp_path, "config/new.csv", "S1,Acme,buyer@partner01.example\n")
    _w(tmp_path, "config/m.py", "input_tokens = usage.total_tokens\n")
    _w(tmp_path, "elsewhere/package-lock.json", '"integrity": "sha512-' + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5xQ2wE4rT6yU8iO0pA/bC==" + '"\n')
    rc, out = _scan(tmp_path, capsys)
    assert rc == 1
    for needle in ("config/new.csv", "config/m.py", "elsewhere/package-lock.json"):
        assert needle in out, needle


def test_real_secrets_next_to_allow_listed_files_are_caught(tmp_path, capsys):
    """Planted fakes inside directories that DO have entries: only the allowed shape is excused, the secret is not."""
    key = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5xQ2wE4rT6yU8"
    _w(tmp_path, "infra/worker/ok.py", "input_tokens = usage.total_tokens\n")
    _w(tmp_path, "infra/worker/bad.py", 'api_token = "' + key[:24] + '"\n')
    _w(tmp_path, "ripple/_testdata/ok.csv", "S1,Acme,buyer@partner01.example\n")
    _w(tmp_path, "ripple/_testdata/bad.csv", "S2,Real,real.person@company.com\n")
    _w(tmp_path, "docs/release/bad.md", "token " + key + "\n")
    _w(tmp_path, "tests/pitch_pack/bad.py", 'k = "' + "AKIA" + 'ABCDEFGHIJKLMNOP"\n')
    rc, out = _scan(tmp_path, capsys)
    assert rc == 1
    for needle in ("infra/worker/bad.py", "ripple/_testdata/bad.csv", "docs/release/bad.md", "tests/pitch_pack/bad.py"):
        assert needle in out, needle
    assert "infra/worker/ok.py" not in out and "ripple/_testdata/ok.csv" not in out
    assert key[:24] not in out and "real.person" not in out


def test_real_secrets_in_the_same_places_are_still_caught(tmp_path, capsys):
    key = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5xQ2wE4rT6yU8"
    _w(tmp_path, "frontend/package-lock.json", '"token": "' + key + '"\n')                       # not an integrity digest shape
    _w(tmp_path, "a.csv", "x,real.person@company.com\n")                                           # real-looking e-mail on a non-reserved domain
    _w(tmp_path, "b.py", 'password = "hunter2hunter2hunter2"\n')                                    # lowercase+digits literal is not an identifier
    _w(tmp_path, "c.py", 'k = "' + key + '/' + key[:6] + '"\n')                                    # mixed-case token with a slash is not a path
    _w(tmp_path, "docs/other/big.png", b"\x00" * 150_000)                                           # a large binary outside the allowed screenshots
    _w(tmp_path, "d.py", "AKIA" + "ABCDEFGHIJKLMNOP and ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5\n")
    rc, out = _scan(tmp_path, capsys)
    assert rc == 1
    for needle in ("package-lock.json", "a.csv", "b.py", "c.py", "big.png", "d.py"):
        assert needle in out, needle
    assert key not in out and "hunter2" not in out and "real.person" not in out                      # values stay masked
