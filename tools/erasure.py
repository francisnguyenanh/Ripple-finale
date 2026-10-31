"""Erasure of a data subject's personal free text on the immutable ledger (REL-06, 2026-10-31): operator tool, one connection, audited.

    python -m tools.erasure list    --tenant T --workspace W                                  # subjects with a key, their state, plain rows that predate the envelope
    python -m tools.erasure shred   --tenant T --workspace W --subject <account_id> --reason "<why>" [--apply]
    python -m tools.erasure export  --tenant T --workspace W --out keys.json                 # the key history (wrapped keys and shred rows) for a restore
    python -m tools.erasure replay  --tenant T --workspace W --in keys.json [--apply]        # after a restore of an older backup: re-apply the key history

`shred` deletes the subject's wrapped key from `v2_subject_key_material` and appends one history row (`v2_subject_keys`, action 'shredded') plus one
audit event `SUBJECT_ERASED` {subject, key_id, reason}, in one transaction; it never changes a ledger row, a hash or the audit chain. From the commit on,
every reader in every process opens that subject's enveloped text as `[erased on <date>]` (logical erasure), and this database holds no material the
deployment key could unwrap (cryptographic erasure of the key, within this database only: a backup taken before the shred still holds the wrapped key
until it is restored and the history replayed; derived copies such as model answers quoting the text are listed, not erased).
Rows written as plain text before `RIPPLE_TEXT_ENVELOPE=1` was turned on are listed (they cannot be erased by key: a recorded limit, an operator
decision, never a silent change). RIPPLE_V2_DSN names the database; RIPPLE_DATA_KEY is not needed to shred or replay (only to open text).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from psycopg2.extras import Json

AUDIT_EVENT = "SUBJECT_ERASED"
TEXT_COLUMNS = (("v2_request_messages", "question", "asked_by"), ("v2_chat_turns", "text", "created_by"), ("v2_business_change_intents", "request_text", "confirmed_by"))


def _connect(dsn):
    import psycopg2
    return psycopg2.connect(dsn)


def list_subjects(conn, tenant, ws) -> dict:
    from infra import text_envelope
    with conn.cursor() as cur:
        cur.execute("""SELECT subject_id, key_id, action, created_at, shredded_at, reason FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s
                       ORDER BY subject_id, created_at, id""", (tenant, ws))
        keys = {}
        for subject, key_id, action, at, shredded_at, reason in cur.fetchall():
            keys.setdefault(subject, []).append({"key_id": key_id, "action": action, "at": at.isoformat(), "shredded_at": shredded_at.isoformat() if shredded_at else None, "reason": reason})
        plain = {}
        for table, column, who in TEXT_COLUMNS:
            cur.execute(f"SELECT {who}, count(*) FROM {table} WHERE tenant_id=%s AND workspace_id=%s AND {column} IS NOT NULL AND {column} NOT LIKE 'env1:%%' GROUP BY {who}", (tenant, ws))
            for subject, n in cur.fetchall():
                plain.setdefault(subject, {})[f"{table}.{column}"] = n
        cur.execute("SELECT subject_id, key_id FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s", (tenant, ws))
        material = {r[0]: r[1] for r in cur.fetchall()}
    return {"subjects": {s: {"state": ("shredded" if any(k["action"] == "shredded" for k in ks) else "keyed"), "material_present": s in material, "history": ks}
                         for s, ks in keys.items()},
            "plain_rows_before_envelope": plain, "derived_copies_not_erased": list(text_envelope.DERIVED_COPIES)}


def shred_subject(conn, tenant, ws, subject, *, actor_id, reason, apply) -> dict:
    from infra import text_envelope
    with conn.cursor() as cur:
        if not apply:
            cur.execute("SELECT action FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s ORDER BY created_at DESC, id DESC LIMIT 1", (tenant, ws, subject))
            row = cur.fetchone()
            state = "no_key" if row is None else ("already_shredded" if row[0] == "shredded" else "would_shred")
            conn.rollback()
            return {"subject": subject, "dry_run": True, "state": state}
        done = text_envelope.shred(cur, tenant, ws, subject, actor_id=actor_id, reason=reason)
        if done["state"] == "shredded":
            cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)",
                        (tenant, ws, AUDIT_EVENT, actor_id, Json({"subject": subject, "key_id": done["key_id"], "reason": reason, "locations": list(text_envelope.LOCATIONS)})))
        conn.commit()
        return done


def export_keys(conn, tenant, ws) -> list:
    """The key history (no material) plus, for every key still live, its wrapped material (so a restore can read live subjects again). A shredded
    key is exported as its tombstone only: the export never carries material the database no longer holds."""
    with conn.cursor() as cur:
        cur.execute("""SELECT subject_id, key_id, action, reason, actor_id, created_at, shredded_at FROM v2_subject_keys
                       WHERE tenant_id=%s AND workspace_id=%s ORDER BY created_at, id""", (tenant, ws))
        rows = [{"subject_id": r[0], "key_id": r[1], "action": r[2], "reason": r[3], "actor_id": r[4], "created_at": r[5].isoformat(),
                 "shredded_at": r[6].isoformat() if r[6] else None} for r in cur.fetchall()]
        cur.execute("SELECT key_id, wrapped_key FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s", (tenant, ws))
        live = dict(cur.fetchall())
    for r in rows:
        if r["action"] == "created" and r["key_id"] in live:
            r["wrapped_key"] = live[r["key_id"]]
    return rows


def replay_keys(conn, tenant, ws, rows, *, apply) -> dict:
    """Re-apply an exported key history onto a restored database: a history row (subject, key_id, action) that is missing is inserted with its
    recorded times; present rows are left alone; the material of a key whose history ends in a shred is DELETED (a backup taken before the shred
    carried it); the material of a live key is inserted when the export carries it and the database lacks it. Idempotent.
    -> {inserted, present, material_deleted, material_inserted}."""
    inserted = present = deleted = added = 0
    shredded = {r["key_id"] for r in rows if r["action"] == "shredded"}
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("SELECT 1 FROM v2_subject_keys WHERE tenant_id=%s AND workspace_id=%s AND subject_id=%s AND key_id=%s AND action=%s",
                        (tenant, ws, r["subject_id"], r["key_id"], r["action"]))
            if cur.fetchone():
                present += 1
            else:
                inserted += 1
                if apply:
                    cur.execute("""INSERT INTO v2_subject_keys (tenant_id, workspace_id, subject_id, key_id, wrapped_key, action, reason, actor_id, created_at, shredded_at)
                                   VALUES (%s,%s,%s,%s,'',%s,%s,%s,%s,%s)""",
                                (tenant, ws, r["subject_id"], r["key_id"], r["action"], r.get("reason", ""), r.get("actor_id", "system"), r["created_at"], r.get("shredded_at")))
            if r["action"] == "created" and r["key_id"] not in shredded and r.get("wrapped_key"):
                cur.execute("SELECT 1 FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND key_id=%s", (tenant, ws, r["key_id"]))
                if cur.fetchone() is None:
                    added += 1
                    if apply:
                        cur.execute("""INSERT INTO v2_subject_key_material (tenant_id, workspace_id, subject_id, key_id, wrapped_key, created_at) VALUES (%s,%s,%s,%s,%s,%s)
                                       ON CONFLICT DO NOTHING""", (tenant, ws, r["subject_id"], r["key_id"], r["wrapped_key"], r["created_at"]))
        for key_id in sorted(shredded):
            cur.execute("SELECT 1 FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND key_id=%s", (tenant, ws, key_id))
            if cur.fetchone():
                deleted += 1
                if apply:
                    cur.execute("DELETE FROM v2_subject_key_material WHERE tenant_id=%s AND workspace_id=%s AND key_id=%s", (tenant, ws, key_id))
        conn.commit() if apply else conn.rollback()
    return {"inserted": inserted, "present": present, "material_deleted": deleted, "material_inserted": added, "applied": apply}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Erase a data subject's personal free text by shredding their key (REL-06).")
    ap.add_argument("command", choices=("list", "shred", "export", "replay"))
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--subject", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--actor", default="operator")
    ap.add_argument("--out", default="")
    ap.add_argument("--in", dest="inp", default="")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    if not dsn:
        print("RIPPLE_V2_DSN is not set", file=sys.stderr)
        return 2
    conn = _connect(dsn)
    try:
        if a.command == "list":
            print(json.dumps(list_subjects(conn, a.tenant, a.workspace), indent=1)); return 0
        if a.command == "shred":
            if not a.subject or len(a.reason.strip()) < 8:
                print("--subject and --reason (at least 8 characters) are required: both are written to the audit", file=sys.stderr); return 2
            print(json.dumps(shred_subject(conn, a.tenant, a.workspace, a.subject, actor_id=a.actor, reason=a.reason.strip(), apply=a.apply), indent=1)); return 0
        if a.command == "export":
            rows = export_keys(conn, a.tenant, a.workspace)
            (open(a.out, "w", encoding="utf-8") if a.out else sys.stdout).write(json.dumps(rows, indent=1)); return 0
        if a.command == "replay":
            if not a.inp:
                print("--in is required", file=sys.stderr); return 2
            rows = json.load(open(a.inp, encoding="utf-8"))
            print(json.dumps(replay_keys(conn, a.tenant, a.workspace, rows, apply=a.apply), indent=1)); return 0
    finally:
        conn.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
