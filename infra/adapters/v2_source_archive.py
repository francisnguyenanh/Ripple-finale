"""Bounded in-memory expansion of a zip/tar(.gz) source upload.

Lives in infra, not core: reading archive members is stream I/O, which the
tier-2 architecture rule keeps out of `core/change_intelligence`
(tests/contract/test_architecture.py Q5). Nothing is written to a
filesystem. Header checks (paths, links, special members, declared sizes,
compression ratio, counts) run before any member is decompressed, and every
member read is capped, so a lying header cannot expand past the limits.
Entry paths go through the same `normalize_logical_path` as direct uploads.
"""

from __future__ import annotations

import io
import tarfile
import zipfile

from core.change_intelligence.v2.source_admission import (
    MAX_ARCHIVE_BYTES, MAX_COMPRESSION_RATIO, MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES,
    RATIO_MIN_BYTES, AdmissionRejected, normalize_logical_path,
)


def _archive_kind(filename: str) -> str | None:
    lower = filename.lower()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith((".tar", ".tar.gz", ".tgz")):
        return "tar"
    return None


def _check_counts(count: int, total: int) -> None:
    if count > MAX_FILES:
        raise AdmissionRejected("limit_exceeded", f"more than {MAX_FILES} files")
    if total > MAX_TOTAL_BYTES:
        raise AdmissionRejected("limit_exceeded", "bundle exceeds 20 MiB")


def _read_capped(stream, declared: int) -> bytes:
    data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES or len(data) != declared:
        raise AdmissionRejected("archive_bomb", "entry size differs from its header")
    return data


def _expand_zip(data: bytes) -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise AdmissionRejected("invalid_archive", str(exc)) from exc
    files = [i for i in infos if not i.is_dir()]
    # Header-only checks first: nothing is decompressed until all pass.
    for info in infos:
        normalize_logical_path(info.filename.rstrip("/") if info.is_dir() else info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise AdmissionRejected("link_entry", info.filename)
        if mode not in (0, 0o100000, 0o040000):
            raise AdmissionRejected("special_entry", info.filename)
    total = 0
    for info in files:
        if info.file_size > MAX_TOTAL_BYTES:
            raise AdmissionRejected("archive_bomb", "declared entry size exceeds bundle cap")
        if info.file_size > MAX_FILE_BYTES:
            raise AdmissionRejected("archive_bomb" if info.file_size > 4 * MAX_FILE_BYTES
                                    else "file_too_large", info.filename)
        if (info.file_size >= RATIO_MIN_BYTES
                and info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1)):
            raise AdmissionRejected("archive_bomb", "compression ratio over limit")
        total += info.file_size
    _check_counts(len(files), total)
    out = []
    for info in files:
        if info.flag_bits & 0x1:
            raise AdmissionRejected("invalid_archive", "encrypted archive entry")
        try:
            with zf.open(info) as stream:
                out.append((info.filename, _read_capped(stream, info.file_size)))
        except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
            raise AdmissionRejected("invalid_archive", str(exc)) from exc
    return out


def _expand_tar(data: bytes) -> list[tuple[str, bytes]]:
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise AdmissionRejected("invalid_archive", str(exc)) from exc
    out, total, seen = [], 0, 0
    try:
        for member in tf:   # streaming; stops early on the first violation
            seen += 1
            if seen > MAX_FILES * 2:
                raise AdmissionRejected("limit_exceeded", "too many archive members")
            if member.issym() or member.islnk():
                raise AdmissionRejected("link_entry", member.name)
            if not (member.isfile() or member.isdir()):
                raise AdmissionRejected("special_entry", member.name)
            normalize_logical_path(member.name.rstrip("/") if member.isdir() else member.name)
            if member.isdir():
                continue
            if member.size > MAX_TOTAL_BYTES:
                raise AdmissionRejected("archive_bomb", "declared member size exceeds bundle cap")
            if member.size > MAX_FILE_BYTES:
                raise AdmissionRejected("file_too_large", member.name)
            total += member.size
            _check_counts(len(out) + 1, total)
            stream = tf.extractfile(member)
            out.append((member.name, _read_capped(stream, member.size)))
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise AdmissionRejected("invalid_archive", str(exc)) from exc
    return out


def expand_archive(filename: object, data: object) -> list[tuple[str, bytes]]:
    """Expand a zip/tar(.gz) upload in memory under the admission limits."""
    if not isinstance(filename, str) or not isinstance(data, bytes):
        raise AdmissionRejected("invalid_archive", "filename and bytes required")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise AdmissionRejected("limit_exceeded", "archive exceeds 20 MiB")
    kind = _archive_kind(filename)
    if kind == "zip":
        if not data.startswith(b"PK\x03\x04"):
            raise AdmissionRejected("type_mismatch", "not a zip archive")
        return _expand_zip(data)
    if kind == "tar":
        if not tarfile.is_tarfile(io.BytesIO(data)):
            raise AdmissionRejected("type_mismatch", "not a tar archive")
        return _expand_tar(data)
    raise AdmissionRejected("unsupported_archive", filename)
