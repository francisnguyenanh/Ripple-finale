"""Create a deployment's first workspace and accounts (one-shot, idempotent; an operator step after `tools.db_migrate --apply`, before testers sign in).

Credentials never appear on the command line, in output or in logs: each comes from an environment variable that the platform fills from its secret store.

  RIPPLE_V2_DSN                      the database the served app uses (secret)
  RIPPLE_PROVISION_ADMIN_CREDENTIAL  the first admin's password (secret)
  RIPPLE_PROVISION_CREDENTIAL_<ID>   the password of each extra account; <ID> = the account id in upper case, every non-alphanumeric character as "_" (secret)

  python -m tools.provision_workspace --tenant T --project P --workspace W --admin ADMIN [--member ACCOUNT=role[,role]] ... [--external-provider allow|deny]

`--external-provider` sets the project's model-data policy (whether job inputs may be sent to an external model provider; prompts are stored as hashes only).
Without it the policy is left as it is; a project that has none refuses every AI job (`model_policy_not_admitted`), which is the fail-closed default.
Setting `allow` is the deployment owner's data decision, recorded in the change ticket.

The first admin is created only when the tenant has no member yet (otherwise it is left as it is); an extra account is created and granted only when it does
not exist yet, and an existing account's password and roles are never changed. Roles must be grantable roles of the product (`GRANTABLE_ROLES`).
Exit codes: 0 done, 2 a missing or invalid input (nothing written), 3 a database error.
"""
from __future__ import annotations

import argparse
import os
import re
import sys


def credential_env(account: str) -> str:
    return "RIPPLE_PROVISION_CREDENTIAL_" + re.sub(r"[^A-Z0-9]", "_", account.upper())


def parse_member(text: str) -> tuple[str, tuple[str, ...]]:
    account, _, roles = text.partition("=")
    parts = tuple(r.strip() for r in roles.split(",") if r.strip())
    if not account.strip() or not parts:
        raise ValueError(f"--member must be ACCOUNT=role[,role], got {text!r}")
    return account.strip(), parts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create a deployment's first workspace and accounts.")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--admin", required=True)
    ap.add_argument("--member", action="append", default=[])
    ap.add_argument("--external-provider", choices=("allow", "deny"), default=None)
    a = ap.parse_args(argv)
    from infra.adapters.v2_policy import GRANTABLE_ROLES
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    admin_cred = os.environ.get("RIPPLE_PROVISION_ADMIN_CREDENTIAL", "")
    try:
        members = [parse_member(m) for m in a.member]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    problems = [] if dsn else ["RIPPLE_V2_DSN is not set"]
    problems += [] if admin_cred else ["RIPPLE_PROVISION_ADMIN_CREDENTIAL is not set"]
    for account, roles in members:
        if not os.environ.get(credential_env(account)):
            problems.append(f"{credential_env(account)} is not set")
        bad = sorted(set(roles) - set(GRANTABLE_ROLES))
        if bad:
            problems.append(f"{account}: not grantable roles {bad}")
    if problems:
        print("; ".join(problems), file=sys.stderr)
        return 2
    import psycopg2
    from infra.adapters.v2_identity import IdentityDenied, PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store

    class _NoBlobs:
        def __getattr__(self, name):
            raise RuntimeError("provisioning reads no blob")

    try:
        store, ids = PostgresV2Store(dsn, _NoBlobs()), PostgresIdentityStore(dsn)
        store.create_workspace(a.tenant, a.project, a.workspace)
        try:
            ids.bootstrap_admin(a.tenant, a.project, a.workspace, a.admin, admin_cred)
            print(f"admin {a.admin}: created")
        except IdentityDenied:
            print(f"admin: tenant {a.tenant} already has members; left as it is")
        if a.external_provider:
            store.set_project_model_policy(a.tenant, a.project, prompt_storage="hash_only", external_provider=a.external_provider)
            print(f"project {a.project}: model-data policy external_provider={a.external_provider}, prompts stored as hashes")
        with psycopg2.connect(dsn) as cx, cx.cursor() as cur:
            cur.execute("SELECT account_id FROM v2_accounts WHERE account_id = ANY(%s)", ([m for m, _ in members],))
            have = {r[0] for r in cur.fetchall()}
        for account, roles in members:
            if account in have:
                print(f"member {account}: exists; left as it is")
                continue
            ids.create_account(account, os.environ[credential_env(account)])
            ids.grant_membership(a.tenant, a.project, a.workspace, account, roles)
            print(f"member {account}: created with {', '.join(roles)}")
    except psycopg2.Error as exc:
        print(f"database error: {type(exc).__name__}", file=sys.stderr)
        return 3
    print(f"workspace {a.tenant}/{a.workspace} (project {a.project}): ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
