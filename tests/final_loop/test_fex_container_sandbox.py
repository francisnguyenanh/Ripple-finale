"""FEX-04 / AIX-C1 sandbox contract (LOCAL, container runtime): the declared commands run in a locked-down container per command. Negative checks prove each limit with a
command that tries to break it: network egress, writes to the checkout, host environment and identity, process count, time. Skipped when no container engine or no local
`python:3.12-slim` image exists (nothing is pulled). A container shares the host kernel: this is not the VM-class boundary the plan asks for before hostile code runs."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from infra.worker import code_change as cc
from infra.worker import sandbox_container as sc

IMAGE = "python:3.12-slim"
RUNTIME = {"runtime": {"kind": "container", "image": IMAGE, "memory": "256m", "cpus": "1", "pids": 64, "tmp_mb": 16}}


def _engine_ready() -> bool:
    ok, _ = sc.available(IMAGE)
    return ok


pytestmark = pytest.mark.skipif(not _engine_ready(), reason="no container engine or no local python:3.12-slim image")


def _run(tmp_path, *argv, timeout=60):
    return cc.run_commands(str(tmp_path), [list(argv)], timeout_seconds=timeout, repo=RUNTIME)


def test_a_passing_and_a_failing_pytest_run_inside_the_container_with_vendored_tooling(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert 1 + 1 == 3\n", encoding="utf-8")
    ok = _run(tmp_path, "{python}", "-m", "pytest", "-q", "test_ok.py")
    assert ok["status"] == "PASSED" and ok["commands"][0]["exit"] == 0 and ok["runtime"]["kind"] == "container" and ok["runtime"]["limits"]["pids"] == 64, ok
    bad = _run(tmp_path, "{python}", "-m", "pytest", "-q", "test_bad.py")
    assert bad["status"] == "FAILED" and bad["commands"][0]["exit"] == 1
    assert cc.isolation_for(RUNTIME) == sc.ISOLATION_CONTAINER and "not a verified boundary" in sc.ISOLATION_CONTAINER and cc.isolation_for({}) == cc.ISOLATION


def test_network_writes_environment_and_privileges_are_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPPLE_FAKE_SECRET", "must-not-leak")
    net = _run(tmp_path, "{python}", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), 3)")
    assert net["status"] == "FAILED" and net["commands"][0]["exit"] != 0                                                  # no network at all
    write = _run(tmp_path, "{python}", "-c", "open('/work/planted.txt', 'w').write('x')")
    assert write["status"] == "FAILED" and not (tmp_path / "planted.txt").exists()                                      # the checkout is read-only
    root = _run(tmp_path, "{python}", "-c", "open('/etc/planted', 'w').write('x')")
    assert root["status"] == "FAILED"                                                                                     # the root filesystem is read-only
    probe = _run(tmp_path, "{python}", "-c", "import os, json; print(json.dumps({'uid': os.getuid(), 'keys': sorted(os.environ)}))")
    assert probe["status"] == "PASSED"
    seen = json.loads(probe["commands"][0]["output_tail"].strip().splitlines()[-1])
    assert seen["uid"] == 65534 and not any(k.startswith(("RIPPLE_", "GOOGLE_", "AWS_")) for k in seen["keys"]) and "must-not-leak" not in probe["commands"][0]["output_tail"]
    caps = _run(tmp_path, "sh", "-c", "grep CapEff /proc/self/status")
    assert caps["status"] == "PASSED" and caps["commands"][0]["output_tail"].strip().endswith("0000000000000000")       # every capability dropped
    sock = _run(tmp_path, "sh", "-c", "test ! -e /var/run/docker.sock")
    assert sock["status"] == "PASSED"                                                                                     # no engine socket inside


def test_process_count_and_time_are_bounded_and_an_overrun_container_is_killed(tmp_path):
    forks = _run(tmp_path, "{python}", "-c",
                 "import os, sys, time\nn = 0\ntry:\n    for _ in range(500):\n        if os.fork() == 0:\n            time.sleep(30); os._exit(0)\n        n += 1\nexcept OSError:\n    print('limited at', n); sys.exit(3)\nsys.exit(0)",
                 timeout=90)
    assert forks["commands"][0]["exit"] == 3 and "limited at" in forks["commands"][0]["output_tail"]                     # the PID limit stops a fork storm
    slow = _run(tmp_path, "{python}", "-c", "import time; time.sleep(120)", timeout=5)
    assert slow["status"] == "FAILED" and slow["commands"][0]["exit"] is None and "container killed" in slow["commands"][0]["output_tail"]
    left = subprocess.run(["docker", "ps", "-q", "--filter", "name=ripple-sbx-"], capture_output=True, text=True).stdout.strip()
    assert left == ""                                                                                                     # nothing keeps running after the kill


def test_a_missing_image_is_an_infrastructure_failure_not_a_product_failure(tmp_path):
    out = cc.run_commands(str(tmp_path), [["{python}", "-c", "pass"]], timeout_seconds=10, repo={"runtime": {"kind": "container", "image": "ripple-no-such-image:0"}})
    assert out["status"] == "INFRASTRUCTURE_FAILED" and "not present locally" in out["commands"][0]["output_tail"]


# ---- real caller: candidate -> validation through the routes and the worker, the declared repository asking for the container runtime ---------------------------------------

from tests.final_loop.test_fex_code_change import _committed, _config, _settle, fixture_repo  # noqa: E402,F401  (fixture)
from tests.final_loop.test_kno01_business_store import ws  # noqa: E402,F401  (fixture)
from tests.final_loop.test_syn01_code import code_env  # noqa: E402,F401  (fixture)


def test_a_candidate_is_validated_inside_the_container_through_the_real_routes_and_worker(code_env, fixture_repo, monkeypatch):
    q = code_env
    repo, target = fixture_repo
    cfg = _config(q.env.ws, repo, target)
    cfg["workspaces"][q.env.ws]["repos"]["fixture"].update(RUNTIME)
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(cfg))
    monkeypatch.setenv("RIPPLE_EXTENSION_MOCK_TARGET", "1")
    cid, approver, committed = _committed(q)
    r = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"})
    assert r.status_code == 200, r.text
    cand_id = r.json()["job_id"]
    assert _settle(q, q.admin, cid, "candidate")["candidate"]["status"] == "PROPOSED"
    v = q.req.post(f"/v2/business/changes/{cid}/code/candidate/validate", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id})
    assert v.status_code == 200, v.text
    val = _settle(q, q.admin, cid, "validation")["validation"]
    assert val["report_status"] == "PASSED" and val["commands"][0]["exit"] == 0, val                                    # the planted acceptance test passes with the patch, inside the container
    assert "container per command" in val["isolation"] and "not a verified boundary" in val["isolation"]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""
