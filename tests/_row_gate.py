"""Test-only switch for the apply row-digest gate.

Some pre-existing tests forge stored proposal/approval row shapes with a
DB-level UPDATE AFTER approval (`_mutate_result` / `mutate_job_result`) to
reach apply branches behind the digest gate. That is exactly the tamper
`infra/worker/apply.py::_verify_bound_rows` now rejects, so those helpers call
`disable()`; the root conftest autouse fixture calls `restore()` after every
test. Tamper rejection itself is covered by dedicated tests."""
_ORIG = None


def disable():
    global _ORIG
    from infra.worker import apply as apply_mod
    if _ORIG is None:
        _ORIG = apply_mod._verify_bound_rows
    apply_mod._verify_bound_rows = lambda *a, **k: None


def restore():
    global _ORIG
    if _ORIG is not None:
        from infra.worker import apply as apply_mod
        apply_mod._verify_bound_rows = _ORIG
        _ORIG = None
