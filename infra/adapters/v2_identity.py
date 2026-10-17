"""Durable v2 identity and workspace membership.

Provisioning methods are operator-only Python calls, never public routes. A
credential is an opaque token issued by that operator; the browser receives a
different, short-lived session token after login. Neither token carries roles.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import secrets

import psycopg2
import psycopg2.errors
from psycopg2.extras import Json, RealDictCursor


class MembershipNotFound(LookupError):
    """Revoke target has no membership (a 404, not an authentication failure)."""


class IdentityDenied(PermissionError):
    """Authentication or current membership is absent."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")
    return value


def _expiry(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("credential expiry must be ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("credential expiry must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("credential expiry must include timezone")
    return parsed.astimezone(timezone.utc)


class PostgresIdentityStore:
    """Fresh SQL authority is read for every request; cookies contain only tokens."""

    def __init__(self, dsn: str):
        self.dsn = _valid(dsn, "PostgreSQL DSN")

    @contextmanager
    def _tx(self):
        from infra import db_pool
        with db_pool.connection(self.dsn) as conn:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    yield cur

    def migrate(self) -> None:
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        sql = (migrations / "ai2_t01_identity.sql").read_text(encoding="utf-8")
        # extension FB-A1 (local build): external identity links (additive; depends on v2_accounts)
        sql += "\n" + (migrations / "ext_t06_external_identities.sql").read_text(encoding="utf-8")
        with self._tx() as cur:
            cur.execute(sql)

    def create_account(self, account_id: str, credential: str,
                       credential_expires_at: str | None = None) -> None:
        """Private operator provisioning, not self-registration."""
        _valid(account_id, "account ID")
        _valid(credential, "credential")
        with self._tx() as cur:
            cur.execute("""INSERT INTO v2_accounts
                (account_id, credential_digest, credential_expires_at)
                VALUES (%s,%s,%s)""",
                (account_id, _digest(credential), _expiry(credential_expires_at)))

    def revoke_credential(self, account_id: str) -> None:
        """Operator revocation blocks the credential and all live sessions."""
        with self._tx() as cur:
            cur.execute("""UPDATE v2_accounts SET disabled_at=now()
                WHERE account_id=%s AND disabled_at IS NULL""", (account_id,))
            if cur.rowcount != 1:
                raise IdentityDenied("account unavailable")
            cur.execute("""UPDATE v2_auth_sessions SET revoked_at=now()
                WHERE account_id=%s AND revoked_at IS NULL""", (account_id,))
            cur.execute("""INSERT INTO v2_audit
                (tenant_id, workspace_id, event_type, actor_id, payload)
                SELECT tenant_id, workspace_id, 'credential.revoke', 'operator',
                       jsonb_build_object('account_id', account_id)
                FROM v2_memberships WHERE account_id=%s""", (account_id,))

    def bootstrap_admin(self, tenant_id: str, project_id: str,
                        workspace_id: str, account_id: str, credential: str) -> None:
        """Create a tenant's first admin once, from a trusted operator process."""
        for name, value in (("tenant", tenant_id), ("project", project_id),
                            ("workspace", workspace_id), ("account", account_id),
                            ("credential", credential)):
            _valid(value, name)
        with self._tx() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (tenant_id,))
            cur.execute("SELECT 1 FROM v2_memberships WHERE tenant_id=%s LIMIT 1", (tenant_id,))
            if cur.fetchone():
                raise IdentityDenied("tenant already bootstrapped")
            cur.execute("""INSERT INTO v2_accounts (account_id, credential_digest)
                VALUES (%s,%s)""", (account_id, _digest(credential)))
            self._grant(cur, tenant_id, project_id, workspace_id, account_id, ("admin",))

    def provision_demo_accounts(self, tenant_id: str, project_id: str,
                                workspace_id: str, *, prefix: str = "demo") -> dict[str, dict]:
        """Private one-time demo provisioning; return tokens to the operator only."""
        _valid(prefix, "account prefix")
        issued = {}
        with self._tx() as cur:
            for role in ("requester", "knowledge_owner", "change_approver",
                         "restore_operator"):
                account_id = f"{prefix}-{role}"
                credential = secrets.token_urlsafe(32)
                cur.execute("""INSERT INTO v2_accounts (account_id, credential_digest)
                    VALUES (%s,%s)""", (account_id, _digest(credential)))
                self._grant(cur, tenant_id, project_id, workspace_id,
                            account_id, (role,))
                issued[role] = {"account_id": account_id, "credential": credential}
        return issued

    def _grant(self, cur, tenant_id, project_id, workspace_id, account_id, roles):
        if not isinstance(roles, (tuple, list, set, frozenset)):
            raise ValueError("roles must be a collection")
        role_set = tuple(sorted(set(roles)))
        if not role_set or any(not isinstance(role, str) or not role.strip() for role in role_set):
            raise ValueError("non-empty roles required")
        cur.execute("""INSERT INTO v2_memberships
            (tenant_id, project_id, workspace_id, account_id, roles)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id, project_id, workspace_id, account_id)
            DO UPDATE SET roles=EXCLUDED.roles, granted_at=now()""",
            (tenant_id, project_id, workspace_id, account_id, list(role_set)))
        cur.execute("""UPDATE v2_workspaces
            SET policy_version=(policy_version::bigint + 1)::text
            WHERE tenant_id=%s AND project_id=%s AND workspace_id=%s""",
            (tenant_id, project_id, workspace_id))
        if cur.rowcount != 1:
            raise IdentityDenied("workspace not found")
        cur.execute("""INSERT INTO v2_audit
            (tenant_id, workspace_id, event_type, actor_id, payload)
            VALUES (%s,%s,'membership.grant','operator',%s)""",
            (tenant_id, workspace_id, Json({"account_id": account_id, "roles": role_set})))

    def grant_membership(self, tenant_id: str, project_id: str, workspace_id: str,
                         account_id: str, roles: tuple[str, ...]) -> None:
        for name, value in (("tenant", tenant_id), ("project", project_id),
                            ("workspace", workspace_id), ("account", account_id)):
            _valid(value, name)
        try:
            with self._tx() as cur:
                self._grant(cur, tenant_id, project_id, workspace_id, account_id, roles)
        except psycopg2.errors.ForeignKeyViolation as exc:
            raise ValueError("unknown account or workspace") from exc

    def revoke_membership(self, tenant_id: str, project_id: str,
                          workspace_id: str, account_id: str) -> None:
        with self._tx() as cur:
            cur.execute("""DELETE FROM v2_memberships WHERE tenant_id=%s
                AND project_id=%s AND workspace_id=%s AND account_id=%s
                RETURNING roles""", (tenant_id, project_id, workspace_id, account_id))
            prior = cur.fetchone()
            if prior is None:
                raise MembershipNotFound("membership not found")
            cur.execute("""UPDATE v2_workspaces
                SET policy_version=(policy_version::bigint + 1)::text
                WHERE tenant_id=%s AND project_id=%s AND workspace_id=%s""",
                (tenant_id, project_id, workspace_id))
            cur.execute("""INSERT INTO v2_audit
                (tenant_id, workspace_id, event_type, actor_id, payload)
                VALUES (%s,%s,'membership.revoke','operator',%s)""",
                (tenant_id, workspace_id, Json({"account_id": account_id,
                                                 "roles": prior["roles"]})))

    def audit_events(self, tenant_id: str, workspace_id: str) -> list[dict]:
        with self._tx() as cur:
            cur.execute("""SELECT event_type, actor_id, payload FROM v2_audit
                WHERE tenant_id=%s AND workspace_id=%s
                  AND event_type LIKE 'membership.%%' ORDER BY id""",
                (tenant_id, workspace_id))
            return [{"event_type": row["event_type"], "actor_id": row["actor_id"],
                     **row["payload"]} for row in cur.fetchall()]

    def login(self, account_id: str, credential: str, *, lifetime_seconds: int = 3600) -> str:
        if not isinstance(account_id, str) or not isinstance(credential, str):
            raise IdentityDenied("invalid credentials")
        if type(lifetime_seconds) is not int or lifetime_seconds <= 0 or lifetime_seconds > 86400:
            raise ValueError("session lifetime must be between 1 and 86400 seconds")
        with self._tx() as cur:
            cur.execute("""SELECT credential_digest FROM v2_accounts
                WHERE account_id=%s AND disabled_at IS NULL
                  AND (credential_expires_at IS NULL OR credential_expires_at > now())""",
                (account_id,))
            row = cur.fetchone()
            if row is None or not hmac.compare_digest(row["credential_digest"], _digest(credential)):
                raise IdentityDenied("invalid credentials")
            token = secrets.token_urlsafe(32)
            cur.execute("""INSERT INTO v2_auth_sessions
                (token_digest, account_id, expires_at)
                VALUES (%s,%s,now() + (%s * interval '1 second'))""",
                (_digest(token), account_id, lifetime_seconds))
            return token

    def issue_session(self, account_id: str, *, lifetime_seconds: int = 3600) -> str:
        """A session for an account whose identity was verified elsewhere (extension FB-A1 exchange). Same lifetime rules and account checks as `login`; no credential is read."""
        if not isinstance(account_id, str) or not account_id:
            raise IdentityDenied("sign-in refused")
        if type(lifetime_seconds) is not int or lifetime_seconds <= 0 or lifetime_seconds > 86400:
            raise ValueError("session lifetime must be between 1 and 86400 seconds")
        with self._tx() as cur:
            cur.execute("""SELECT 1 FROM v2_accounts WHERE account_id=%s AND disabled_at IS NULL
                  AND (credential_expires_at IS NULL OR credential_expires_at > now())""", (account_id,))
            if cur.fetchone() is None:
                raise IdentityDenied("sign-in refused")
            token = secrets.token_urlsafe(32)
            cur.execute("""INSERT INTO v2_auth_sessions (token_digest, account_id, expires_at)
                VALUES (%s,%s,now() + (%s * interval '1 second'))""", (_digest(token), account_id, lifetime_seconds))
            return token

    def logout(self, token: str) -> None:
        with self._tx() as cur:
            cur.execute("UPDATE v2_auth_sessions SET revoked_at=now() WHERE token_digest=%s",
                        (_digest(token),))

    def _principal(self, cur, token: str):
        if not isinstance(token, str) or not token:
            raise IdentityDenied("sign in required")
        cur.execute("""SELECT s.account_id, s.selected_tenant_id, s.selected_project_id,
            s.selected_workspace_id FROM v2_auth_sessions s
            JOIN v2_accounts a ON a.account_id=s.account_id
            WHERE s.token_digest=%s AND s.revoked_at IS NULL AND s.expires_at>now()
              AND a.disabled_at IS NULL
              AND (a.credential_expires_at IS NULL OR a.credential_expires_at>now())""",
            (_digest(token),))
        row = cur.fetchone()
        if row is None:
            raise IdentityDenied("sign in required")
        return row

    def resolve_context(self, token: str, workspace_id: str | None = None) -> dict:
        with self._tx() as cur:
            principal = self._principal(cur, token)
            result = {"account_id": principal["account_id"], "actor_kind": "human"}
            if workspace_id is not None:
                cur.execute("""SELECT m.tenant_id, m.project_id, m.workspace_id,
                    m.roles, w.policy_version FROM v2_memberships m
                    JOIN v2_workspaces w USING (tenant_id, project_id, workspace_id)
                    WHERE m.account_id=%s AND m.workspace_id=%s""",
                    (principal["account_id"], workspace_id))
                memberships = cur.fetchmany(2)
                if len(memberships) != 1:
                    raise IdentityDenied("workspace unavailable")
                result.update(memberships[0])
            return result

    def current_authority(self, account_id: str, tenant_id: str,
                          workspace_id: str) -> dict:
        with self._tx() as cur:
            cur.execute("""SELECT m.project_id, m.roles, w.policy_version
                FROM v2_memberships m JOIN v2_workspaces w
                USING (tenant_id, project_id, workspace_id)
                JOIN v2_accounts a ON a.account_id=m.account_id
                WHERE m.account_id=%s AND m.tenant_id=%s AND m.workspace_id=%s
                  AND a.disabled_at IS NULL
                  AND (a.credential_expires_at IS NULL OR a.credential_expires_at>now())""",
                (account_id, tenant_id, workspace_id))
            rows = cur.fetchmany(2)
            if len(rows) != 1:
                raise IdentityDenied("workspace unavailable")
            return dict(rows[0])

    def list_workspaces(self, token: str) -> list[dict]:
        with self._tx() as cur:
            principal = self._principal(cur, token)
            cur.execute("""SELECT m.tenant_id, m.project_id, m.workspace_id,
                m.roles, w.policy_version, w.display_name, w.description FROM v2_memberships m
                JOIN v2_workspaces w USING (tenant_id, project_id, workspace_id)
                WHERE m.account_id=%s ORDER BY m.tenant_id, m.project_id, m.workspace_id""",
                (principal["account_id"],))
            return [dict(row) for row in cur.fetchall()]

    def select_workspace(self, token: str, workspace_id: str) -> dict:
        context = self.resolve_context(token, workspace_id)
        with self._tx() as cur:
            cur.execute("""UPDATE v2_auth_sessions SET selected_tenant_id=%s,
                selected_project_id=%s, selected_workspace_id=%s
                WHERE token_digest=%s AND revoked_at IS NULL AND expires_at>now()""",
                (context["tenant_id"], context["project_id"], workspace_id, _digest(token)))
            if cur.rowcount != 1:
                raise IdentityDenied("sign in required")
        return context

    def read_head(self, token: str, workspace_id: str) -> dict:
        with self._tx() as cur:
            principal = self._principal(cur, token)
            cur.execute("""SELECT w.workspace_id, w.version, w.source_revision_id,
                w.knowledge_revision_id, w.decision_epoch, w.policy_version
                FROM v2_memberships m JOIN v2_workspaces w
                USING (tenant_id, project_id, workspace_id)
                WHERE m.account_id=%s AND m.workspace_id=%s""",
                (principal["account_id"], workspace_id))
            rows = cur.fetchmany(2)
            if len(rows) != 1:
                raise IdentityDenied("workspace unavailable")
            return dict(rows[0])
