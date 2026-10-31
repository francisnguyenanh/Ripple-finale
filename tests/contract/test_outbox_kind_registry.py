"""REL-01 (audit 2026-10-30): every outbox kind the product writes is registered in the worker (a claim-consumer or an acknowledged no-consumer
kind), and the worker is the only claimer. A new kind written without a registry entry fails here before it can park in production. Pure file test."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"


def _sources():
    return [p for p in INFRA.rglob("*.py") if "migrations" not in p.parts]


def written_kinds() -> set[str]:
    kinds = set()
    for p in _sources():
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"INSERT INTO v2_outbox[^;]*?VALUES\s*\((?:[^)]*?)'([A-Z_]+)'", text, re.S):
            kinds.add(m.group(1))
        if "_record_commit(" in text or "INSERT INTO v2_outbox" in text:      # an EVENT constant of a module that writes the outbox (not an audit-only event)
            for m in re.finditer(r"^EVENT\s*=\s*\"([A-Z_]+)\"", text, re.M):
                kinds.add(m.group(1))
        for m in re.finditer(r"_record_commit\([^;]*?\"([A-Z][A-Z_]+)\"", text, re.S):
            kinds.add(m.group(1))
    return kinds


def test_every_written_kind_is_registered():
    from infra.worker import job_worker
    registry = job_worker.OUTBOX_CLAIM_CONSUMERS | job_worker.OUTBOX_NO_CLAIM_CONSUMER
    kinds = written_kinds()
    assert {"JOB_QUEUED", "SOURCE_PUBLISHED", "BUSINESS_KNOWLEDGE_COMMITTED", "BUSINESS_CHANGE_INTENT_CONFIRMED", "DECISION_RECORDED",
            "HITL_ANSWER_RECORDED", "KNOWLEDGE_PUBLISHED", "CHANGE_COMPLETED"} <= kinds, sorted(kinds)
    assert kinds <= registry, f"unregistered outbox kinds: {sorted(kinds - registry)}"
    assert not (job_worker.OUTBOX_CLAIM_CONSUMERS & job_worker.OUTBOX_NO_CLAIM_CONSUMER)


def test_the_worker_is_the_only_claimer():
    callers = [p.relative_to(ROOT).as_posix() for p in _sources() if re.search(r"\.claim_outbox\(", p.read_text(encoding="utf-8"))]
    assert callers == ["infra/worker/job_worker.py"], callers


def test_no_claim_consumer_kinds_are_only_read_by_row_id():
    """The kinds acknowledged on first claim must not be read anywhere by their outbox status (that would make the acknowledgement visible)."""
    from infra.worker import job_worker
    for p in _sources():
        text = p.read_text(encoding="utf-8")
        for kind in job_worker.OUTBOX_NO_CLAIM_CONSUMER:
            for m in re.finditer(r"FROM v2_outbox[^;]*?status[^;]*?;?", text, re.S):
                assert kind not in m.group(0), (p.name, kind)
