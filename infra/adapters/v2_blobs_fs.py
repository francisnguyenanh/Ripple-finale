"""Local, persistent, content-addressed blob port for the live-local mode (same contract as `ImmutableGCSBlobs`).

Bytes live under one directory outside the repository; the key is the sha256 digest, never a workspace path (SQL metadata gates
tenant access, exactly as for the GCS adapter). Writes are atomic (temp file + rename) and immutable: a second put of the same digest
must carry the same bytes. Nothing here deletes a blob; a reset is an explicit operator command in `tools/judge_local/live_local.py`.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import tempfile


class FileBlobs:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> pathlib.Path:
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError("sha256 digest required")
        hexpart = digest.removeprefix("sha256:")
        if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart):
            raise ValueError("invalid sha256 digest")
        return self.root / hexpart[:2] / hexpart

    def put_immutable(self, digest: str, content: bytes) -> None:
        if not isinstance(content, bytes) or "sha256:" + hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("blob content does not match digest")
        path = self._path(digest)
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError("existing blob at digest key is corrupted")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def read(self, digest: str) -> bytes:
        data = self._path(digest).read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("stored blob does not match its digest")
        return data
