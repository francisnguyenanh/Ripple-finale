"""The worker-as-a-service entry (option B, not deployed): liveness, restart-by-exit and that it adds no claim path. No database."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from infra.worker import service


def _get(port, path="/healthz"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health_is_200_while_the_loop_passes_and_503_when_it_stops_passing():
    now = [100.0]
    live = service.Liveness(clock=lambda: now[0])
    server = service.serve_health(live, 0, stale_after=60)
    port = server.server_address[1]
    try:
        assert _get(port)[0] == 200
        live.touch()
        now[0] += 59
        assert _get(port)[0] == 200
        now[0] += 5                                   # no pass for 64 s: a stuck loop fails the liveness probe and the platform restarts the container
        code, body = _get(port)
        assert code == 503 and body["ok"] is False
        live.touch()
        assert _get(port)[0] == 200
    finally:
        server.shutdown()


def test_the_loop_marks_every_pass_and_uses_only_the_existing_claim_function():
    calls = []

    def claim(store, tenant, ws, wid):
        calls.append((tenant, ws))
        return None
    live = service.Liveness()
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 3
    out = service.run_service(object(), [("t", "w")], poll_seconds=0, liveness=live, claim_fn=claim, sleep=lambda s: None, stop=stop)
    assert out == {"stopped": "test_stop"} and live.passes >= 4 and calls == [("t", "w")] * 3


def test_an_exception_in_the_loop_escapes_so_the_process_exits_and_the_platform_restarts_it():
    def boom(*a):
        raise RuntimeError("database gone")
    try:
        service.run_service(object(), [("t", "w")], poll_seconds=0, liveness=service.Liveness(), claim_fn=boom, sleep=lambda s: None)
    except RuntimeError as exc:
        assert "database gone" in str(exc)
    else:
        raise AssertionError("the loop must not swallow a failure")


def test_a_long_provider_call_is_busy_not_stuck_and_a_hung_claim_is_stuck():
    now = [0.0]
    live = service.Liveness(clock=lambda: now[0], busy_limit=600)
    live.begin()
    now[0] += 500                                           # a provider call of 8 minutes (timeout 180 s + retries) is still normal work
    assert live.state() == "busy" and live.healthy()
    now[0] += 200                                           # 700 s in one claim: hung -> the probe fails and the container is restarted
    assert live.state() == "stuck" and not live.healthy()
    live.end()
    assert live.state() == "polling"


def test_a_paused_worker_stays_healthy_because_every_pass_still_completes():
    now = [0.0]
    live = service.Liveness(clock=lambda: now[0])
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 50

    def paused_claim(store, tenant, ws, wid):               # worker_claims OFF: claim_and_admit_one returns None at once
        now[0] += 5
        return None
    service.run_service(object(), [("t", "w")], poll_seconds=0, liveness=live, claim_fn=paused_claim, sleep=lambda s: None, stop=stop)
    assert live.state() == "polling" and live.healthy() and live.passes >= 50


def test_the_wrapper_marks_busy_during_the_claim_and_clears_it_after_even_on_failure():
    live = service.Liveness()
    seen = []

    def claim(store, tenant, ws, wid):
        seen.append(live.state())
        raise RuntimeError("boom")
    try:
        service.run_service(object(), [("t", "w")], poll_seconds=0, liveness=live, claim_fn=claim, sleep=lambda s: None)
    except RuntimeError:
        pass
    assert seen == ["busy"] and live.busy_since is None


def test_the_busy_limit_follows_the_approved_configuration_not_a_guess():
    from types import SimpleNamespace
    cfg = SimpleNamespace(request_timeout_seconds=180.0)
    assert service.busy_limit_for(cfg) == 2 * 195.0                       # lease = timeout 180 + margin 15; the probe tolerates twice the lease
    assert service.busy_limit_for(None) == 60.0                           # no configuration: the base lease of 30 s, doubled
    live = service.Liveness(busy_limit=service.busy_limit_for(cfg))
    assert live.busy_limit == 390.0


def test_the_service_manifest_declares_the_probes_one_instance_and_no_batch_bounds():
    import pathlib

    import pytest
    import yaml
    manifest = pathlib.Path("tools/pitch_cloud/worker_service.yaml")
    if not manifest.exists():
        pytest.skip("the operator's Cloud Run manifest carries private deployment identifiers and is not part of the release package")
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    tpl = doc["spec"]["template"]
    ann = tpl["metadata"]["annotations"]
    c = tpl["spec"]["containers"][0]
    assert ann["autoscaling.knative.dev/minScale"] == "1" and ann["autoscaling.knative.dev/maxScale"] == "1"
    assert ann["run.googleapis.com/cpu-throttling"] == "false" and tpl["spec"]["containerConcurrency"] == 1
    assert c["command"] == ["python"] and c["args"] == ["-m", "infra.worker.service"]
    live, start = c["livenessProbe"], c["startupProbe"]                  # declared in the Cloud Run configuration, not only served by the container
    assert live["httpGet"]["path"] == "/healthz" and live["periodSeconds"] == 30 and live["failureThreshold"] == 3
    assert start["httpGet"]["path"] == "/healthz" and start["failureThreshold"] * start["periodSeconds"] >= 120
    names = {e["name"] for e in c["env"]}
    assert not names & {"RIPPLE_WORKER_MAX_RUNTIME_SECONDS", "RIPPLE_WORKER_IDLE_EXIT_SECONDS", "RIPPLE_WORKER_MAX_JOBS", "RIPPLE_PROVIDER_MODE"}      # a service never self-ends; arming stays with the switches
    dsn = next(e for e in c["env"] if e["name"] == "RIPPLE_V2_DSN")
    assert "value" not in dsn and dsn["valueFrom"]["secretKeyRef"]["name"] == "ripple-v2-dsn"                   # a secret by reference only
    import re
    assert re.fullmatch(r"[\w-]+@[\w.-]+\.iam\.gserviceaccount\.com", tpl["spec"]["serviceAccountName"])


def test_the_deadline_stops_claims_once_and_never_before_it_is_reached():
    from datetime import datetime, timezone
    now = [datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)]
    calls = []
    d = service.Deadline("2026-10-13T10:00:00Z", set_flag=lambda *a: calls.append(a), now=lambda: now[0])
    assert d.check() is False and calls == []
    now[0] = datetime(2026, 10, 13, 10, 0, 1, tzinfo=timezone.utc)
    assert d.check() is True and d.check() is False                       # fires once
    assert calls == [("worker_claims", "off", "service", calls[0][3])] and "deadline" in calls[0][3]
    assert service.Deadline(None).check() is False                        # no deadline configured: never


def test_the_loop_hands_the_deadline_to_every_pass():
    from datetime import datetime, timezone
    flags = []
    d = service.Deadline("2000-01-01T00:00:00Z", set_flag=lambda *a: flags.append(a), now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 3
    service.run_service(object(), [("t", "w")], poll_seconds=0, liveness=service.Liveness(), claim_fn=lambda *a: None, sleep=lambda s: None, stop=stop, deadline=d)
    assert len(flags) == 1
