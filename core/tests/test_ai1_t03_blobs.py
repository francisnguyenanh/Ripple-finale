"""GCS adapter contract; runs without a PostgreSQL service."""

import hashlib

import pytest
from google.api_core.exceptions import PreconditionFailed

from infra.adapters.v2_blobs import ImmutableGCSBlobs


class FakeGCSBlob:
    def __init__(self):
        self.content = None

    def upload_from_string(self, content, *, if_generation_match):
        assert if_generation_match == 0
        if self.content is not None:
            raise PreconditionFailed("already exists")
        self.content = content

    def download_as_bytes(self):
        return self.content


class FakeGCSBucket:
    def __init__(self):
        self.objects = {}

    def blob(self, key):
        return self.objects.setdefault(key, FakeGCSBlob())


def test_gcs_blob_adapter_is_content_addressed_and_never_overwrites():
    bucket = FakeGCSBucket()
    blobs = ImmutableGCSBlobs(bucket)
    content = b"immutable bytes"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    blobs.put_immutable(digest, content)
    blobs.put_immutable(digest, content)  # same-content retry
    assert blobs.read(digest) == content
    bucket.blob("v2-blobs/" + digest[7:]).content = b"corrupted"
    with pytest.raises(ValueError, match="corrupted"):
        blobs.put_immutable(digest, content)
