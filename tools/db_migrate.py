"""Apply the product schema to one PostgreSQL database (one-shot, idempotent): the v2 store's migrations in their coded order, then the identity store's.

A deployment runs this as a one-shot job BEFORE a new revision takes traffic (the stores do not migrate at web start-up). It prints the target as host/port/database only (never the
password) and applies nothing without `--apply`; take a backup of a non-disposable database first. The SQL files are idempotent (`CREATE ... IF NOT EXISTS`, guarded `ALTER`s).

  python -m tools.db_migrate                 # show the target named by RIPPLE_V2_DSN (the DSN the served app uses for both stores), apply nothing
  python -m tools.db_migrate --apply         # apply both migrations; exit 0 when done
Exit codes: 0 done / shown, 2 missing or unreadable DSN, 3 a migration failed (the failing statement's database error is printed without parameters).
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2.extensions


def target(dsn: str) -> str:
    try:
        p = psycopg2.extensions.parse_dsn(dsn)
    except psycopg2.ProgrammingError:
        raise SystemExit(2)
    return f"host={p.get('host', 'localhost')} port={p.get('port', '5432')} dbname={p.get('dbname', '?')}"


MIGRATE_LOCK_ID = 0x52495047                                   # "RIPG": the migration chain of one database runs one at a time


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Apply the Ripple schema to one PostgreSQL database.")
    ap.add_argument("--apply", action="store_true", help="apply the migrations (default: show the target only)")
    a = ap.parse_args(argv)
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    if not dsn:
        print("RIPPLE_V2_DSN is not set", file=sys.stderr)
        return 2
    print(f"target: {target(dsn)}")
    if not a.apply:
        print("nothing applied (pass --apply)")
        return 0
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store
    try:
        # REL-06 follow-up (2026-10-30): two migrations of the same database serialize on one session advisory lock (held on its own connection for the whole
        # chain; released with the session, so a killed migration never blocks the next one)
        guard = psycopg2.connect(dsn)
        guard.autocommit = True
        with guard.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK_ID,))
        try:
            PostgresV2Store(dsn, None).migrate()             # the blob port is not used by migrate()
            PostgresIdentityStore(dsn).migrate()
        finally:
            try:
                with guard.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATE_LOCK_ID,))
            finally:
                guard.close()
    except psycopg2.Error as exc:
        print(f"migration failed: {type(exc).__name__}: {str(exc.pgerror or exc).strip()[:300]}", file=sys.stderr)
        return 3
    print("applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
