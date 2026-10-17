"""Operator-supplied VERIFICATION CONTRACT -- pure, deterministic helpers shared by the route
(`infra/v2_verification_routes.py`), the review check (`infra/worker/review.py`) and the
post-apply verification (`infra/worker/verification.py`).

Principle: the expected value comes from an authenticated human operator/approver and is stored
with provenance (`v2_verification_contracts`). It is NEVER derived from the AI output, the
proposal's own edits, a repo file, an env var or a held-out label. Nothing here executes user
code: bytes are only parsed (`yaml.safe_load`, `openpyxl.load_workbook`) and compared.

Kinds (the existing oracle shapes): `yaml_value_equals` (locator = dotted path, same grammar as the
proposal's `path`) and `xlsx_cell_equals` (locator = `Sheet!A1`, column upper-cased).

Top-level imports are deliberately light (`canonical_hash` only): the store imports this module
lazily to hash a row, and the tree-diff / snapshot helpers of `verification.py` are imported
inside the functions that need them (no import cycle review -> contract -> verification).
"""

from __future__ import annotations

import io
import re

from core.change_intelligence.v2.serialize import canonical_hash

CONTRACT_KINDS = {"yaml_value_equals": (".yaml", ".yml"), "xlsx_cell_equals": (".xlsx",)}
#: proposal edit_kind -> the only contract kind that may cover it
EDIT_KIND_TO_CONTRACT_KIND = {"yaml_typed_edit": "yaml_value_equals",
                              "xlsx_typed_edit": "xlsx_cell_equals"}
PROVENANCE_MAX_KEYS = 8
PROVENANCE_MAX_LEN = 200
_CELL = re.compile(r"^(?P<sheet>.+)!(?P<col>[A-Za-z]{1,3})(?P<row>[1-9][0-9]{0,6})$")

_HASH_FIELDS = ("contract_id", "tenant_id", "project_id", "workspace_id", "proposal_job_id",
                "logical_path", "kind", "locator", "expected", "author_account_id", "provenance")


def contract_fields_hash(**fields) -> str:
    """sha256 over exactly the stored identity/value fields (never `created_at`)."""
    return canonical_hash({name: fields[name] for name in _HASH_FIELDS})


def contract_hash_of(contract: dict) -> str:
    """Recompute the hash of a stored row -- compared with the embedded `contract_sha256`."""
    return contract_fields_hash(**{name: contract.get(name) for name in _HASH_FIELDS})


def normalize_locator(kind: str, locator) -> str | None:
    """Canonical locator or `None` when it is not valid for the kind."""
    if not isinstance(locator, str):
        return None
    value = locator.strip()
    if not value or len(value) > 300:
        return None
    if kind == "yaml_value_equals":
        return value if all(part for part in value.split(".")) else None
    if kind == "xlsx_cell_equals":
        match = _CELL.match(value)
        if not match:
            return None
        return f"{match.group('sheet')}!{match.group('col').upper()}{match.group('row')}"
    return None


def kind_matches_path(kind: str, logical_path: str) -> bool:
    return isinstance(logical_path, str) and logical_path.lower().endswith(
        CONTRACT_KINDS.get(kind, ("\0",)))


def clean_provenance(raw) -> dict | None:
    """A short str->str mapping, or `None` when malformed (route answers 400)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > PROVENANCE_MAX_KEYS:
        return None
    out = {}
    for key, value in raw.items():
        if (not isinstance(key, str) or not isinstance(value, str) or not key
                or len(key) > 64 or len(value) > PROVENANCE_MAX_LEN):
            return None
        out[key] = value
    return out


def locator_for_edit(edit_kind: str | None, edit: dict) -> str | None:
    """The contract-grammar locator of one recorded proposal edit."""
    if edit_kind == "xlsx_typed_edit":
        return normalize_locator("xlsx_cell_equals", f"{edit.get('sheet')}!{edit.get('cell')}")
    if edit_kind == "yaml_typed_edit":
        return normalize_locator("yaml_value_equals", edit.get("path"))
    return None


# --------------------------------------------------------------------------- parsing / values

def load_document(kind: str, content: bytes):
    """`("yaml", obj)` / `("xlsx", workbook)`, or `None` when the bytes do not parse."""
    try:
        if kind == "yaml_value_equals":
            import yaml
            return "yaml", yaml.safe_load(content.decode("utf-8"))
        import openpyxl
        return "xlsx", openpyxl.load_workbook(io.BytesIO(content))
    except Exception:  # noqa: BLE001 -- any parse failure is "unreadable", never a crash
        return None


def value_at(doc, locator: str):
    """Value read from a parsed document at the locator, or `verification._MISSING`."""
    from infra.worker import verification as v
    if doc is None:
        return v._MISSING
    if doc[0] == "yaml":
        return v._yaml_lookup(doc[1], locator)
    snapshot = v._xlsx_snapshot(doc[1])["cells"]
    return snapshot.get(locator, v._MISSING)


def diff_locators(before_doc, after_doc) -> list[str]:
    """Deterministic, sorted locators that differ between two parsed documents of the same kind
    (yaml: `verification._tree_diff`; xlsx: sheets / non-empty cells / defined names)."""
    from infra.worker import verification as v
    if before_doc[0] == "yaml":
        return sorted({d["locator"] for d in v._tree_diff(before_doc[1] or {}, after_doc[1] or {})})
    before, after = v._xlsx_snapshot(before_doc[1]), v._xlsx_snapshot(after_doc[1])
    out = []
    if before["sheets"] != after["sheets"]:
        out.append("$sheets")
    for key in sorted(set(before["cells"]) | set(after["cells"])):
        a, b = before["cells"].get(key, v._MISSING), after["cells"].get(key, v._MISSING)
        if a is v._MISSING or b is v._MISSING or not v._strict_eq(a, b):
            out.append(key)
    for key in sorted(set(before["names"]) | set(after["names"])):
        if before["names"].get(key) != after["names"].get(key):
            out.append(f"$name:{key}")
    return sorted(out)


def evaluate_value(contract: dict, doc):
    """`("PASS"|"FAIL", actual_or_None)` of the contract's expected value against a parsed doc."""
    from infra.worker import verification as v
    actual = value_at(doc, contract["locator"])
    ok = actual is not v._MISSING and v._strict_eq(actual, contract["expected"])
    return ("PASS" if ok else "FAIL"), (None if actual is v._MISSING else v._json_safe(actual))


def describe(contract: dict, *, actual=None, diff=None, note: str = "") -> str:
    """Deterministic, human-readable detail that EMBEDS `contract_sha256` (so the review result,
    and through it `approval_digest`, is bound to the exact contract)."""
    parts = [f"contract_sha256={contract.get('contract_sha256')}",
             f"contract_id={contract.get('contract_id')}",
             f"kind={contract.get('kind')}", f"locator={contract.get('locator')}",
             f"expected={contract.get('expected')!r}", f"actual={actual!r}"]
    if diff is not None:
        parts.append(f"diff={diff}")
    if note:
        parts.append(note)
    return "; ".join(parts)


__all__ = [
    "CONTRACT_KINDS", "EDIT_KIND_TO_CONTRACT_KIND", "clean_provenance", "contract_fields_hash",
    "contract_hash_of", "describe", "diff_locators", "evaluate_value", "kind_matches_path",
    "load_document", "locator_for_edit", "normalize_locator", "value_at",
]
