"""Immutable GCS blob port for versioned workspaces."""

from __future__ import annotations

import hashlib

from google.api_core.exceptions import PreconditionFailed


class ImmutableGCSBlobs:
    """Content-addressed bytes; caller-supplied bucket supports test doubles.

    The object key is a digest, never a workspace path. SQL metadata gates
    tenant access; this adapter is only available to the trusted store service.
    The generation precondition prevents overwriting a previously staged key.
    """

    def __init__(self, bucket, prefix: str = "v2-blobs"):
        if not prefix or prefix.startswith("/") or ".." in prefix.split("/"):
            raise ValueError("invalid blob prefix")
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _blob(self, digest: str):
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError("sha256 digest required")
        hexpart = digest.removeprefix("sha256:")
        if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart):
            raise ValueError("invalid sha256 digest")
        return self.bucket.blob(f"{self.prefix}/{hexpart}")

    def put_immutable(self, digest: str, content: bytes) -> None:
        if not isinstance(content, bytes) or "sha256:" + hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("blob content does not match digest")
        blob = self._blob(digest)
        try:
            blob.upload_from_string(content, if_generation_match=0)
        except PreconditionFailed:
            # Idempotent retry is valid only if the existing bytes match.
            if blob.download_as_bytes() != content:
                raise ValueError("existing blob at digest key is corrupted")

    def read(self, digest: str) -> bytes:
        return self._blob(digest).download_as_bytes()
