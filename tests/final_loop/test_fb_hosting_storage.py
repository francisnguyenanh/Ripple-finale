"""FB-H1 / FB-B1 (local): the Hosting configuration validator refuses an API rewrite until a session strategy is decided and insists on no-store/no-swallow rules; the immutable
blob adapter's contract (digest-addressed keys, generation precondition, idempotent retry, collision guard, prefix validation) is proven against a fake bucket. Neither proves a
real Firebase Hosting site or a real bucket's IAM; those stay OPEN."""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib

import pytest
from google.api_core.exceptions import PreconditionFailed

from infra.adapters.v2_blobs import ImmutableGCSBlobs
from tools.firebase import validate_hosting as vh

SAMPLE = json.loads(pathlib.Path("config/firebase.hosting.sample.json").read_text(encoding="utf-8"))


def test_the_sample_hosting_config_passes_and_each_rule_has_a_finding():
    assert vh.validate(SAMPLE) == []
    api = copy.deepcopy(SAMPLE)
    api["hosting"]["rewrites"].insert(0, {"source": "/v2/**", "run": {"serviceId": "ripple-api", "region": "asia-northeast1"}})
    assert [c for c, _ in vh.validate(api)] == ["api_rewrite_blocked"]
    api["ripple"].update(allow_api_rewrite=True, session_strategy="__session cookie with migration reviewed")
    assert vh.validate(api) == []                                                                                          # decided strategy: the rewrite is allowed
    swallow = copy.deepcopy(SAMPLE)
    swallow["hosting"]["rewrites"] = [{"source": "**", "destination": "/index.html"}]
    assert "spa_fallback_swallows_api" in [c for c, _ in vh.validate(swallow)]
    cache = copy.deepcopy(SAMPLE)
    cache["hosting"]["headers"][1]["headers"][0]["value"] = "public, max-age=600"
    cache["hosting"]["headers"].append({"source": "/v2/**", "headers": [{"key": "Cache-Control", "value": "public, max-age=60"}]})
    codes = [c for c, _ in vh.validate(cache)]
    assert "index_not_no_store" in codes and "api_publicly_cacheable" in codes
    root = copy.deepcopy(SAMPLE)
    root["hosting"]["public"] = "."
    root["hosting"]["ignore"] = []
    codes = [c for c, _ in vh.validate(root)]
    assert "public_dir_unsafe" in codes and "dotfiles_not_ignored" in codes
    assert vh.validate({}) == [("no_hosting_block", "the file has no hosting object")]
    assert vh.main(["config/firebase.hosting.sample.json"]) == 0


class _FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_from_string(self, content, if_generation_match=None):
        if if_generation_match == 0 and self.name in self.store:
            raise PreconditionFailed("object exists")
        self.store[self.name] = content

    def download_as_bytes(self):
        return self.store[self.name]


class _FakeBucket:
    def __init__(self):
        self.objects: dict = {}
        self.uploads = 0

    def blob(self, name):
        return _FakeBlob(self.objects, name)


def test_the_immutable_blob_adapter_keeps_its_contract_on_any_bucket():
    bucket = _FakeBucket()
    blobs = ImmutableGCSBlobs(bucket, prefix="ripple/v2-blobs")
    content = b"document bytes"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    blobs.put_immutable(digest, content)
    assert list(bucket.objects) == ["ripple/v2-blobs/" + digest[7:]] and blobs.read(digest) == content                 # digest-addressed key under the prefix, never a workspace path
    blobs.put_immutable(digest, content)                                                                                   # idempotent retry: same bytes, no error
    with pytest.raises(ValueError):
        blobs.put_immutable(digest, b"other bytes")                                                                        # content must match its digest
    bucket.objects["ripple/v2-blobs/" + digest[7:]] = b"tampered"
    with pytest.raises(ValueError):
        blobs.put_immutable(digest, content)                                                                               # precondition hit and the stored bytes differ: collision guard
    for bad in ("md5:abc", "sha256:xyz", "sha256:" + "0" * 63):
        with pytest.raises(ValueError):
            blobs.read(bad)
    for prefix in ("", "/abs", "a/../b"):
        with pytest.raises(ValueError):
            ImmutableGCSBlobs(bucket, prefix=prefix)
