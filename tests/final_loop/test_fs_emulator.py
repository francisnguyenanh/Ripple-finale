"""FS-R1 / FS-C1 Firestore sink and rules. Two halves:

* always: the sink refuses every target this build must not write to (non-loopback host, non-demo project, missing configuration) and an unreachable emulator is a sink outage,
  never a fallback; the route answers 503 `sink_refused` for such a configuration.
* opt-in (`FIRESTORE_EMULATOR_HOST` + `RIPPLE_FIRESTORE_PROJECT`, a LOCAL Firestore emulator started from `config/firebase.emulator.json`): the conditional monotonic put on the
  emulator, the rules of `config/firestore.rules` (a token reads only the feeds it lists; no client write; nothing else readable) and the projection route writing hints into
  the emulator. Emulator evidence proves the adapter, the rules and the pipeline shape only — not a real Firestore database, latency, cost or a listener client.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid

import pytest
import requests

from infra.adapters import v2_projection as pj
from infra.adapters.v2_firestore_sink import FirestoreEmulatorSink, SinkRefused, feed_id
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401

HOST = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
PROJECT = os.environ.get("RIPPLE_FIRESTORE_PROJECT", "")
emulator = pytest.mark.skipif(not (HOST and PROJECT), reason="opt-in: a local Firestore emulator (FIRESTORE_EMULATOR_HOST, RIPPLE_FIRESTORE_PROJECT=demo-...)")


def hint(key, revision):
    return {"schema": pj.SCHEMA, "key": key, "family": "work", "revision": revision, "changed_at": "2026-10-17T12:00:00+00:00", "expires_at": "2026-10-17T13:00:00+00:00"}


# ---- always ---------------------------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host,project", [("10.0.0.5:8085", "demo-ripple-test"), ("firestore.googleapis.com", "demo-ripple-test"), ("127.0.0.1:8085", "ripple-prod"),
                                          ("127.0.0.1.example.com:8085", "demo-ripple-test")])
def test_the_sink_refuses_any_target_that_is_not_a_loopback_emulator_with_a_demo_project(host, project):
    with pytest.raises(SinkRefused):
        FirestoreEmulatorSink(host, project)


def test_missing_configuration_is_refused_and_an_unreachable_emulator_is_an_outage_not_a_fallback(monkeypatch):
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.setenv("RIPPLE_FIRESTORE_PROJECT", "demo-ripple-test")
    with pytest.raises(SinkRefused):
        FirestoreEmulatorSink.from_env()
    dead = FirestoreEmulatorSink("127.0.0.1:9", "demo-ripple-test", timeout=1.0)
    with pytest.raises(pj.SinkUnavailable):
        dead.put("ws/abc", "k1", hint("k1", 1))


def test_the_route_answers_sink_refused_for_a_cloud_host_and_moves_no_checkpoint(qa_env, monkeypatch):
    q = qa_env
    monkeypatch.setenv("RIPPLE_PROJECTION_SINK", "firestore-emulator")
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "firestore.googleapis.com")
    monkeypatch.setenv("RIPPLE_FIRESTORE_PROJECT", "demo-ripple-test")
    before = pj.pending(q.env.store, q.env.tenant, q.env.ws)
    r = q.admin.post("/v2/projection/run", json={"workspace_id": q.env.ws})
    assert r.status_code == 503 and r.json()["code"] == "sink_refused"
    assert q.admin.post("/v2/projection/conversations", json={"workspace_id": q.env.ws}).json()["code"] == "sink_refused"
    assert pj.pending(q.env.store, q.env.tenant, q.env.ws)["lag"] == before["lag"]
    assert q.admin.get("/v2/projection/status", params={"workspace_id": q.env.ws}).json()["sink"] == "off"


def test_feed_grants_follow_current_permissions_and_name_no_business_content(qa_env):
    q = qa_env
    admin = q.admin.get("/v2/projection/feeds", params={"workspace_id": q.env.ws})
    req = q.req.get("/v2/projection/feeds", params={"workspace_id": q.env.ws})
    assert admin.status_code == 200 and req.status_code == 200
    a, r = admin.json(), req.json()
    ws_feed = feed_id(pj.scope_for(q.env.tenant, q.env.ws))
    analyst = feed_id(pj.conv_scope_for(q.env.tenant, q.env.ws, "analyst"))
    redacted = feed_id(pj.conv_scope_for(q.env.tenant, q.env.ws, "redacted"))
    assert a["feeds"] == [ws_feed, redacted, analyst] and a["claim"] == "ripple_feeds" and a["token_issued"] is False
    assert r["feeds"] == [ws_feed, redacted]                                                                              # no knowledge.confirm: no analyst feed
    assert q.env.ws not in json.dumps(a) and q.env.tenant not in json.dumps(a)


# ---- opt-in: the local emulator -------------------------------------------------------------------------------------------------------------------------------------------

def _token(feeds, ws, policy, sub="reader-1", until_ms=None):
    """A hand-built unsigned token with the server's claim names (for rules cases the issuer would never produce, e.g. an expired one). The emulator accepts it."""
    now = int(time.time())
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode("utf-8")).rstrip(b"=").decode("ascii")       # noqa: E731
    claims = {"iss": f"https://securetoken.google.com/{PROJECT}", "aud": PROJECT, "sub": sub, "user_id": sub, "iat": now, "exp": now + 3600, "auth_time": now,
              "firebase": {"sign_in_provider": "custom", "identities": {}}, "ripple_feeds": feeds, "ripple_ws": ws, "ripple_policy": policy,
              "ripple_until_ms": until_ms if until_ms is not None else (now + 900) * 1000}
    return enc({"alg": "none", "typ": "JWT"}) + "." + enc(claims) + "."


def _claims(token):
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _client_get(path, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    s = requests.Session()
    s.trust_env = False
    return s.get(f"http://{HOST}/v1/projects/{PROJECT}/databases/(default)/documents/{path}", headers=headers, timeout=5)


@emulator
def test_conditional_monotonic_put_on_the_emulator():
    sink = FirestoreEmulatorSink(HOST, PROJECT)
    scope, key = f"ws/{uuid.uuid4().hex[:24]}", "k" + uuid.uuid4().hex[:12]
    assert sink.put(scope, key, hint(key, 1)) is True
    assert sink.put(scope, key, hint(key, 1)) is False and sink.put(scope, key, hint(key, 0)) is False and sink.ignored_older == 2
    assert sink.put(scope, key, hint(key, 3)) is True
    doc, _ = sink.get(scope, key)
    assert doc == hint(key, 3)


@emulator
def test_rules_a_token_reads_only_its_listed_feeds_and_no_client_writes():
    sink = FirestoreEmulatorSink(HOST, PROJECT)
    tag = uuid.uuid4().hex[:24]
    mine, other, analyst = f"ws/{tag}", f"ws/x{tag[1:]}", f"conv/analyst/{tag}"
    for scope in (mine, other, analyst):
        assert sink.put(scope, "k1", hint("k1", 1)) is True
    assert sink.put_policy(feed_id(mine), 5) is True and sink.put_policy(feed_id(mine), 4) is False
    reader = _token([feed_id(mine), feed_id(f"conv/redacted/{tag}")], feed_id(mine), 5)
    assert _client_get(f"feeds/{feed_id(mine)}/items/k1", reader).status_code == 200
    assert _client_get(f"feeds/{feed_id(mine)}/items", reader).status_code == 200                                          # list inside its own feed
    assert _client_get(f"feeds/{feed_id(other)}/items/k1", reader).status_code == 403                                      # another workspace's feed
    assert _client_get(f"feeds/{feed_id(analyst)}/items/k1", reader).status_code == 403                                    # the analyst profile is a separate feed
    assert _client_get(f"feeds/{feed_id(mine)}/items/k1").status_code == 403                                                # no token
    s = requests.Session()
    s.trust_env = False
    w = s.patch(f"http://{HOST}/v1/projects/{PROJECT}/databases/(default)/documents/feeds/{feed_id(mine)}/items/k1",
                headers={"Authorization": f"Bearer {reader}"}, json={"fields": {"revision": {"integerValue": "99"}}}, timeout=5)
    assert w.status_code == 403                                                                                              # clients never write
    doc, _ = sink.get(mine, "k1")
    assert doc["revision"] == 1
    expired = _token([feed_id(mine)], feed_id(mine), 5, until_ms=int(time.time() - 5) * 1000)
    assert _client_get(f"feeds/{feed_id(mine)}/items/k1", expired).status_code == 403                                       # past ripple_until_ms
    stale = _token([feed_id(mine)], feed_id(mine), 4)
    assert _client_get(f"feeds/{feed_id(mine)}/items/k1", stale).status_code == 403                                         # issued under an older policy version
    no_policy = _token([feed_id(other)], feed_id(other), 1)
    assert _client_get(f"feeds/{feed_id(other)}/items/k1", no_policy).status_code == 403                                    # no policy document for that workspace
    assert _client_get(f"feed_policies/{feed_id(mine)}", reader).status_code == 403                                          # policy documents are not readable


@emulator
def test_the_projection_route_writes_opaque_hints_into_the_emulator_and_a_second_pass_writes_nothing(qa_env, monkeypatch):
    q = qa_env
    monkeypatch.setenv("RIPPLE_PROJECTION_SINK", "firestore-emulator")
    r = q.admin.post("/v2/projection/run", json={"workspace_id": q.env.ws})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["written"] == out["hints"] > 0 and out["lag"]["outbox_rows"] == 0
    monkeypatch.setenv("RIPPLE_FEED_TOKEN_ISSUER", "emulator")
    issued = q.req.post("/v2/projection/feed-token", json={"workspace_id": q.env.ws})
    assert issued.status_code == 200, issued.text
    listing = _client_get(f"feeds/{feed_id(out['scope'])}/items?pageSize=300", issued.json()["token"]).json()
    docs = listing.get("documents") or []
    assert len(docs) == out["written"]
    blob = json.dumps(docs)
    assert q.env.ws not in blob and q.env.tenant not in blob and "rule-" not in blob and "jpy" not in blob.lower()
    assert all(set(d["fields"]) == {"schema", "key", "family", "revision", "changed_at", "expires_at"} for d in docs)
    again = q.admin.post("/v2/projection/run", json={"workspace_id": q.env.ws}).json()
    assert again["hints"] == 0 and again["written"] == 0
    assert q.admin.get("/v2/projection/status", params={"workspace_id": q.env.ws}).json()["sink"] == "firestore-emulator"
    conv = q.admin.post("/v2/projection/conversations", json={"workspace_id": q.env.ws, "profile": "redacted"})
    assert conv.status_code == 200, conv.text


@emulator
def test_issued_tokens_carry_only_server_claims_and_stop_working_on_revocation_scope_and_tenant_boundaries(qa_env, monkeypatch):
    q = qa_env
    from tests.final_loop.test_chg04_review import login
    monkeypatch.setenv("RIPPLE_PROJECTION_SINK", "firestore-emulator")
    monkeypatch.setenv("RIPPLE_FEED_TOKEN_ISSUER", "emulator")
    assert q.admin.post("/v2/projection/run", json={"workspace_id": q.env.ws}).status_code == 200
    ws_feed = feed_id(pj.scope_for(q.env.tenant, q.env.ws))
    member = f"fr-{q.env.nonce}"
    reader = login(q, member, ("requester",))
    assert reader.post("/v2/projection/feed-token", json={"workspace_id": q.env.ws, "feeds": ["ws~anything"]}).status_code == 400   # no claim from the client
    t = reader.post("/v2/projection/feed-token", json={"workspace_id": q.env.ws})
    assert t.status_code == 200, t.text
    body, claims = t.json(), _claims(t.json()["token"])
    assert claims["ripple_feeds"] == q.req.get("/v2/projection/feeds", params={"workspace_id": q.env.ws}).json()["feeds"] == body["feeds"]
    assert claims["ripple_ws"] == ws_feed and claims["sub"].startswith("rpl-") and member not in t.text and q.env.tenant not in t.text
    assert claims["ripple_until_ms"] - claims["iat"] * 1000 == 900_000 and body["token_kind"] == "emulator-unsigned"
    listing = _client_get(f"feeds/{ws_feed}/items?pageSize=5", body["token"])
    assert listing.status_code == 200 and listing.json().get("documents")
    analyst = feed_id(pj.conv_scope_for(q.env.tenant, q.env.ws, "analyst"))
    assert _client_get(f"feeds/{analyst}/items?pageSize=5", body["token"]).status_code == 403                               # outside the member's scope
    other_ws = feed_id(pj.scope_for("tenant-other", "ws-other"))
    FirestoreEmulatorSink(HOST, PROJECT).put(pj.scope_for("tenant-other", "ws-other"), "k1", hint("k1", 1))
    assert _client_get(f"feeds/{other_ws}/items/k1", body["token"]).status_code == 403                                      # another tenant's workspace
    admin_feed_grant = q.admin.post("/v2/projection/feed-token", json={"workspace_id": q.env.ws}).json()["token"]
    assert analyst in _claims(admin_feed_grant)["ripple_feeds"]
    # revocation: the membership change bumps the policy version and the rules refuse every token issued before it
    r = q.admin.post("/v2/membership/revoke", json={"workspace_id": q.env.ws, "account_id": member})
    assert r.status_code == 200, r.text
    assert _client_get(f"feeds/{ws_feed}/items?pageSize=5", body["token"]).status_code == 403
    assert reader.post("/v2/projection/feed-token", json={"workspace_id": q.env.ws}).status_code in (401, 403)            # no membership: no new token
    assert _client_get(f"feeds/{ws_feed}/items?pageSize=5", admin_feed_grant).status_code == 403                                 # the remaining members ask again
    fresh = q.admin.post("/v2/projection/feed-token", json={"workspace_id": q.env.ws}).json()
    assert fresh["policy_version"] > body["policy_version"] and _client_get(f"feeds/{ws_feed}/items?pageSize=5", fresh["token"]).status_code == 200


def test_the_token_route_is_off_without_the_emulator_issuer_and_refuses_client_claims(qa_env, monkeypatch):
    q = qa_env
    monkeypatch.delenv("RIPPLE_FEED_TOKEN_ISSUER", raising=False)
    r = q.req.post("/v2/projection/feed-token", json={"workspace_id": q.env.ws})
    assert r.status_code == 503 and r.json()["code"] == "issuer_off"
    assert q.req.post("/v2/projection/feed-token", json={"workspace_id": q.env.ws, "claims": {"ripple_feeds": ["x"]}}).status_code == 400
    monkeypatch.setenv("RIPPLE_FEED_TOKEN_ISSUER", "emulator")
    monkeypatch.setenv("RIPPLE_PROJECTION_SINK", "memory")
    r = q.req.post("/v2/projection/feed-token", json={"workspace_id": q.env.ws})
    assert r.status_code == 503 and r.json()["code"] == "sink_refused"                                                     # the emulator issuer needs the emulator sink
