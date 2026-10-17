"""Source admission: pure, byte-based checks before anything is staged.

No I/O, no network, no filesystem extraction. Callers pass raw upload bytes;
this module decides, per batch, whether the upload may be staged at all and,
per item, whether it is admitted or needs an explicit skip acknowledgement.

Limits and format decisions come from docs/product/ai_closed_loop_support.md
§1, §1.1, §1.2 and §3. Every batch refusal carries the contract error code
`unsupported_format` (contracts §6) with a specific `reason`; no new error
code is introduced. An extension with no support-matrix row is never
admitted by default: it becomes `skip_required`.
"""

from __future__ import annotations

import hashlib
import io

import unicodedata
import zipfile
from dataclasses import dataclass
from typing import Iterable, Sequence

from core.change_intelligence.v2.schema import SOURCE_ROLES

MAX_FILE_BYTES = 1_048_576
MAX_FILES = 200
MAX_TOTAL_BYTES = 20 * 1_048_576
MAX_ARCHIVE_BYTES = 20 * 1_048_576
#: Deflate ratio above which an entry is treated as an expansion bomb even
#: when its declared size is under the per-file cap.
MAX_COMPRESSION_RATIO = 200
RATIO_MIN_BYTES = 64 * 1024
MAX_PATH_CHARS = 512
MAX_SEGMENT_CHARS = 255

#: READ-eligible formats (support matrix §1).
SUPPORTED_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".yml": "application/yaml",
    ".yaml": "application/yaml",
    ".py": "text/x-python",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".jsonl": "application/jsonl",
}
_TEXT_EXTENSIONS = frozenset({".md", ".yml", ".yaml", ".py", ".csv", ".jsonl"})
_ARCHIVE_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".7z", ".rar", ".bz2", ".xz")
_ENGLISH = frozenset({"en", "en-us", "en-gb", "english"})
_BINARY_MAGIC = (b"PK\x03\x04", b"%PDF", b"\xd0\xcf\x11\xe0", b"\x89PNG", b"\x7fELF",
                 b"MZ", b"\x1f\x8b", b"GIF8", b"\xff\xd8\xff")
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)})
_INVISIBLE = frozenset(
    [chr(c) for c in range(0x200B, 0x2010)] + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)] + ["﻿"])


class AdmissionRejected(ValueError):
    """The whole batch is refused; nothing may be staged."""

    error_code = "unsupported_format"

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class AdmissionItem:
    logical_path: str
    media_type: str
    byte_size: int
    blob_hash: str
    disposition: str            # "admitted" | "skip_required"
    reason: str | None
    source_role: str
    source_role_origin: str     # "uploader_metadata" | "unassigned"
    content: bytes

    def to_dict(self) -> dict:
        return {"logical_path": self.logical_path, "media_type": self.media_type,
                "byte_size": self.byte_size, "blob_hash": self.blob_hash,
                "disposition": self.disposition, "reason": self.reason,
                "source_role": self.source_role,
                "source_role_origin": self.source_role_origin}


@dataclass(frozen=True)
class AdmissionReport:
    items: tuple[AdmissionItem, ...]

    @property
    def admitted(self) -> tuple[AdmissionItem, ...]:
        return tuple(i for i in self.items if i.disposition == "admitted")

    @property
    def skip_required(self) -> tuple[AdmissionItem, ...]:
        return tuple(i for i in self.items if i.disposition == "skip_required")

    def preview(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "summary": {"file_count": len(self.items),
                        "total_bytes": sum(i.byte_size for i in self.items),
                        "admitted": len(self.admitted),
                        "skip_required": len(self.skip_required)},
            "acknowledgement_required": [i.logical_path for i in self.skip_required],
            # This is ADMISSION completeness (no item needs a skip
            # acknowledgement) -- never analysis/parser coverage, which is
            # not known at this layer. A caller must not report this as
            # "coverage_complete".
            "admission_complete": not self.skip_required,
        }


# ----------------------------------------------------------------- paths

def normalize_logical_path(raw: object) -> str:
    """NFC, forward slashes, relative, no dot segments, portable names."""
    if not isinstance(raw, str) or not raw:
        raise AdmissionRejected("invalid_path", "empty path")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _INVISIBLE for ch in raw):
        raise AdmissionRejected("invalid_path", "control or invisible character")
    path = unicodedata.normalize("NFC", raw).replace("\\", "/")
    if path.startswith("/") or (len(path) >= 2 and path[1] == ":" and path[0].isalpha()):
        raise AdmissionRejected("path_traversal", "absolute, drive or UNC path")
    segments = path.split("/")
    if any(seg in (".", "..") for seg in segments):
        raise AdmissionRejected("path_traversal", "dot segment")
    if len(path) > MAX_PATH_CHARS:
        raise AdmissionRejected("invalid_path", "path too long")
    for seg in segments:
        if not seg:
            raise AdmissionRejected("invalid_path", "empty segment")
        if ":" in seg:
            raise AdmissionRejected("invalid_path", "colon in name")
        if len(seg) > MAX_SEGMENT_CHARS:
            raise AdmissionRejected("invalid_path", "segment too long")
        if seg[-1] in ". ":
            raise AdmissionRejected("invalid_path", "trailing dot or space")
        if seg.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
            raise AdmissionRejected("invalid_path", "reserved device name")
    return path


def collision_key(path: str) -> str:
    return unicodedata.normalize("NFKC", path).casefold()


# ----------------------------------------------------------------- per-item typing

def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    for ext in (".tar.gz",):
        if name.endswith(ext):
            return ext
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def _xlsx_disposition(path: str, content: bytes) -> tuple[str, str | None]:
    if content.startswith(b"\xd0\xcf\x11\xe0"):
        # Encrypted OOXML is wrapped in an OLE compound file.
        return "skip_required", "encrypted_workbook"
    if not content.startswith(b"PK\x03\x04"):
        raise AdmissionRejected("type_mismatch", path)
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise AdmissionRejected("type_mismatch", path) from exc
    if len(infos) > 10_000 or sum(i.file_size for i in infos) > 64 * MAX_FILE_BYTES:
        raise AdmissionRejected("archive_bomb", path)
    names = {i.filename.lower() for i in infos}
    if "[content_types].xml" not in names or "xl/workbook.xml" not in names:
        raise AdmissionRejected("type_mismatch", path)
    if "xl/vbaproject.bin" in names:
        return "skip_required", "macro_workbook"
    try:
        types = zf.read("[Content_Types].xml")
    except (KeyError, zipfile.BadZipFile, OSError, RuntimeError):
        raise AdmissionRejected("type_mismatch", path)
    if b"macroEnabled" in types or b"vbaProject" in types:
        return "skip_required", "macro_workbook"
    return "admitted", None


def _text_disposition(path: str, content: bytes) -> tuple[str, str | None]:
    if content.startswith(_BINARY_MAGIC) or b"\x00" in content:
        raise AdmissionRejected("type_mismatch", path)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "skip_required", "undecodable_text"
    if not text.strip():
        return "skip_required", "empty"
    return "admitted", None


def _classify(path: str, content: bytes) -> tuple[str, str, str | None]:
    """Returns (media_type, disposition, reason)."""
    ext = _extension(path)
    media_type = SUPPORTED_MEDIA_TYPES.get(ext, "application/octet-stream")
    if not content:
        return media_type, "skip_required", "empty"
    if ext == ".xlsm":
        return "application/vnd.ms-excel.sheet.macroEnabled.12", "skip_required", "macro_workbook"
    if ext in _ARCHIVE_EXTENSIONS or path.lower().endswith(_ARCHIVE_EXTENSIONS):
        return media_type, "skip_required", "nested_archive"
    if ext == ".xlsx":
        return (media_type, *_xlsx_disposition(path, content))
    if ext in _TEXT_EXTENSIONS:
        return (media_type, *_text_disposition(path, content))
    # No support-matrix row (HTML, PDF, DOCX, .txt, images, ...): excluded,
    # never included by default.
    return media_type, "skip_required", "unsupported_format"


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def admit_files(entries: Iterable[Sequence], *, language: object = None) -> AdmissionReport:
    """Batch admission. `entries` are `(path, bytes)` or `(path, bytes, role)`.

    Order of checks: language, count/size limits (cheap, before any content
    inspection), path normalization, collisions, then per-item typing."""
    if language is not None:
        if not isinstance(language, str) or language.strip().lower() not in _ENGLISH:
            raise AdmissionRejected("unsupported_language", str(language))
    rows = []
    for entry in entries:
        path, content = entry[0], entry[1]
        role = entry[2] if len(entry) > 2 else None
        if not isinstance(content, bytes):
            raise AdmissionRejected("invalid_upload", "file content must be bytes")
        rows.append((path, content, role))
    if not rows:
        raise AdmissionRejected("empty_import", "no files")
    if len(rows) > MAX_FILES:
        raise AdmissionRejected("limit_exceeded", f"more than {MAX_FILES} files")
    for _, content, _ in rows:
        if len(content) > MAX_FILE_BYTES:
            raise AdmissionRejected("file_too_large", "file exceeds 1 MiB")
    if sum(len(c) for _, c, _ in rows) > MAX_TOTAL_BYTES:
        raise AdmissionRejected("limit_exceeded", "bundle exceeds 20 MiB")

    normalized = [(normalize_logical_path(p), c, r) for p, c, r in rows]
    keys: set[str] = set()
    for path, _, _ in normalized:
        key = collision_key(path)
        if key in keys:
            raise AdmissionRejected("path_collision", path)
        keys.add(key)

    items = []
    for path, content, role in normalized:
        if role is not None and role not in SOURCE_ROLES:
            raise ValueError(f"invalid source_role for {path!r}")
        media_type, disposition, reason = _classify(path, content)
        items.append(AdmissionItem(
            logical_path=path, media_type=media_type, byte_size=len(content),
            blob_hash=_digest(content), disposition=disposition, reason=reason,
            source_role=role or "unknown",
            source_role_origin="uploader_metadata" if role else "unassigned",
            content=content))
    return AdmissionReport(tuple(items))
