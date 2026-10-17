"""Oracle pinning: `RIPPLE_VERIFICATION_ORACLE_FILE` is the
accepted independent oracle IF it is operator-provided and reviewed, its
version and hash are pinned in the evidence, it is never given to a model, and a
missing / unreadable / changed / not-run oracle keeps `acceptance_tests`
NOT_RUN and completion blocked.

Contract:
* the oracle file may carry a top-level `"version"` string;
* the persisted report carries `tests_result.oracle_file =
  {"sha256": <hex sha256 of the file bytes>, "version": <str|None>}` and the
  change view exposes the same object at `verification.oracle_file`;
* optional operator pin: env `RIPPLE_VERIFICATION_ORACLE_SHA256`. When set and
  different from the file's actual sha256, the oracle is "changed" ->
  `acceptance_tests` NOT_RUN, reason `oracle_hash_mismatch`;
* a configured but missing / unreadable file -> report `NOT_RUN` (reason
  `oracle_unavailable`), NOT a crashed job;
* the oracle content (ids, expected values, requirement refs) is never sent to a
  provider (verification is `external = False`, zero provider calls).
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from tests.ai8t03.conftest import *  # noqa: F401,F403
from tests.ai8t03.conftest import (
    ORACLE_ENV, applied_change, change_dto, exceptions_of, install_provider, job_row, pump,
    report_of,
)

PIN_ENV = "RIPPLE_VERIFICATION_ORACLE_SHA256"


def _oracle(tmp_path, monkeypatch, artifact_id, *, expected=30, version="rev-2026-09-1",
            marker=None):
    f = tmp_path / f"reviewed-oracle-{uuid4().hex[:6]}.json"
    body = {"version": version, "oracles": [{
        "oracle_id": "reviewed-oracle-1", "artifact_id": artifact_id,
        "kind": "yaml_value_equals", "path": "refund_window_days", "expected": expected,
        "requirement_ref": marker or "human-confirmed requirement R1"}]}
    f.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setenv(ORACLE_ENV, str(f))
    return f, hashlib.sha256(f.read_bytes()).hexdigest()


def _run(setup, monkeypatch, owner):
    change = applied_change(setup, monkeypatch, owner)
    change["provider"] = install_provider(monkeypatch, "ok")
    return change


def _blocked(change):
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["completion"]["eligible"] is False, dto
    assert dto["completion"]["blocked_reasons"], dto
    assert dto["state"] not in ("MACHINE_VERIFIED", "AWAITING_ACCEPTANCE", "COMPLETED"), dto
    return dto


def test_a1_pass_through_the_real_path_pins_oracle_hash_and_version(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner)
    f, sha = _oracle(tmp_path, monkeypatch, change["target"])
    monkeypatch.setenv(PIN_ENV, sha)  # operator pin agrees with the reviewed file
    log = pump(setup)
    assert not exceptions_of(log), log
    report = report_of(change, setup["store"], setup["tenant"])
    assert report["status"] == "PASSED", report["reasons"]
    cfg = report["oracle_config"]  # key under which the report carries the oracle config
    assert cfg["sha256"] == sha and cfg["version"] == "rev-2026-09-1", cfg
    assert report["tests_result"]["oracle_config"] == cfg
    # the pin is part of the digest-bound report: changing it changes the digest
    assert report["report_digest"]


def test_a1_changed_file_versus_operator_pin_is_not_run_and_blocks(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner)
    f, sha = _oracle(tmp_path, monkeypatch, change["target"])
    monkeypatch.setenv(PIN_ENV, "0" * 64)  # reviewed hash no longer matches the file
    pump(setup)
    report = report_of(change, setup["store"], setup["tenant"])
    assert report["status"] == "NOT_RUN", report["reasons"]
    assert "oracle_hash_mismatch" in " ".join(report["reasons"])
    assert report["checks"]["acceptance_tests"]["status"] == "NOT_RUN"
    _blocked(change)


def test_a1_missing_or_unreadable_oracle_file_is_not_run_and_blocks(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner)
    monkeypatch.setenv(ORACLE_ENV, str(tmp_path / "does-not-exist.json"))
    log = pump(setup)
    assert not exceptions_of(log), log
    report = report_of(change, setup["store"], setup["tenant"])
    assert report["status"] == "NOT_RUN", report["reasons"]
    assert "oracle_unavailable" in " ".join(report["reasons"])
    _blocked(change)


def test_a1_unreadable_oracle_path_directory_is_not_run(setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner)
    monkeypatch.setenv(ORACLE_ENV, str(tmp_path))  # a directory: cannot be read as a file
    pump(setup)
    report = report_of(change, setup["store"], setup["tenant"])
    assert report["status"] == "NOT_RUN", report["reasons"]
    _blocked(change)


def test_a1_unset_config_is_not_run_and_carries_no_oracle_pin(
        setup, monkeypatch, owner, tmp_path):
    monkeypatch.delenv(ORACLE_ENV, raising=False)
    from tests.ai8t03.conftest import review_without_contract_defect
    review_without_contract_defect(monkeypatch)  # W1: no contract either (see helper)
    change = _run(setup, monkeypatch, owner)
    pump(setup)
    report = report_of(change, setup["store"], setup["tenant"])
    assert report["status"] == "NOT_RUN"
    assert not report.get("oracle_config")
    _blocked(change)


def test_a1_oracle_content_never_reaches_a_model_or_prompt(
        setup, monkeypatch, owner, tmp_path):
    marker = f"ORACLE-SECRET-{uuid4().hex}"
    change = _run(setup, monkeypatch, owner)
    f, sha = _oracle(tmp_path, monkeypatch, change["target"], marker=marker)
    log = pump(setup)
    assert not exceptions_of(log), log
    # (1) the reanalysis provider seam saw calls, none containing oracle content
    assert change["provider"].calls, "control: the reanalysis stage did call the provider"
    seen = json.dumps(change["provider"].calls, default=str)
    for needle in (marker, "reviewed-oracle-1", sha, "yaml_value_equals"):
        assert needle not in seen, f"oracle content reached the provider: {needle}"
    # (2) the stages that read the oracle make zero provider calls
    for job_id in (change["verify_job_id"], change["rebuild_job_id"]):
        attempts = setup["store"].job_attempts(setup["tenant"], change["workspace"], job_id)
        assert attempts and all(a["provider_calls"] == 0 for a in attempts), attempts
    # (3) no downstream job payload (extraction prompts derive from these) carries it
    with setup["store"]._tx() as cur:
        cur.execute("SELECT job_id, payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s "
                    "AND kind=ANY(%s)", (setup["tenant"], change["workspace"],
                                          ["extraction", "invalidation", "finding", "relation"]))
        for job_id, payload in cur.fetchall():
            assert marker not in json.dumps(payload, default=str), job_id


def test_a1_production_modules_do_not_import_the_oracle_loader_into_model_paths():
    """Static guard: the oracle env/loader lives only in the verification module;
    no extraction / prompt-building module references it."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    holders = [p.relative_to(root).as_posix() for p in (root / "infra").rglob("*.py")
               if "RIPPLE_VERIFICATION_ORACLE" in p.read_text(encoding="utf-8", errors="ignore")
               or "_load_oracles" in p.read_text(encoding="utf-8", errors="ignore")]
    assert holders == ["infra/worker/verification.py"], holders


def test_a1_no_mint_of_acceptance_via_an_untrusted_request_field(
        setup, monkeypatch, owner, tmp_path):
    """The oracle location is operator configuration only: a job payload naming
    another oracle file is ignored (control: with no env, NOT_RUN)."""
    monkeypatch.delenv(ORACLE_ENV, raising=False)
    from tests.ai8t03.conftest import review_without_contract_defect
    review_without_contract_defect(monkeypatch)  # W1: no contract either (see helper)
    change = _run(setup, monkeypatch, owner)
    rogue = tmp_path / "rogue.json"
    rogue.write_text(json.dumps({"oracles": [{
        "oracle_id": "rogue", "artifact_id": change["target"], "kind": "yaml_value_equals",
        "path": "refund_window_days", "expected": 30}]}), encoding="utf-8")
    with setup["store"]._tx() as cur:
        cur.execute("UPDATE v2_jobs SET payload = payload || %s::jsonb WHERE tenant_id=%s AND "
                    "workspace_id=%s AND job_id=%s",
                    (json.dumps({"oracle_file": str(rogue)}), setup["tenant"],
                     change["workspace"], change["apply_job_id"]))
    pump(setup)
    report = report_of(change, setup["store"], setup["tenant"])
    assert report["status"] == "NOT_RUN", report["reasons"]


REVIEWED_ORACLE = "tests/ai8t03/fixtures/reviewed_oracle.json"


def test_a1_pass_with_the_committed_reviewed_oracle_file_pins_its_hash_and_version(
        setup, monkeypatch, owner):
    """PASS through the real path with an operator-reviewed, committed oracle
    file (byte-stable, so its sha256 is a run ref): the report pins exactly
    this file's hash and version, and the operator pin agrees."""
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[2] / REVIEWED_ORACLE
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv(ORACLE_ENV, str(path))
    monkeypatch.setenv(PIN_ENV, sha)
    change = applied_change(setup, monkeypatch, owner, target="config-reviewed-oracle.yaml")
    install_provider(monkeypatch, "ok")
    log = pump(setup)
    assert not exceptions_of(log), log
    report = report_of(change, setup["store"], setup["tenant"])
    assert report["status"] == "PASSED", report["reasons"]
    assert report["oracle_config"]["sha256"] == sha
    assert report["oracle_config"]["version"] == "w1-reviewed-2026-09-29"
    assert report["tests_result"]["oracles"][0]["status"] == "PASS"
    print(f"REVIEWED_ORACLE sha256={sha} version=w1-reviewed-2026-09-29")
