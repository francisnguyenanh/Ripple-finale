"""tools/provision_workspace: first workspace and accounts of a deployment, credentials only from the environment, idempotent, never printed.
Needs PITCH_BRIEF_TEST_DSN (skipped otherwise, which is NOT a pass)."""
from __future__ import annotations

from uuid import uuid4

import psycopg2
import pytest

from tests.pitch_brief.conftest import DSN
from tools import provision_workspace as P


@pytest.fixture
def db(monkeypatch):
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("needs a disposable PostgreSQL; set PITCH_BRIEF_TEST_DSN")
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store
    PostgresV2Store(DSN, None).migrate()
    PostgresIdentityStore(DSN).migrate()
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    return uuid4().hex[:10]


def test_creates_once_reads_credentials_from_the_environment_and_never_prints_them(db, monkeypatch, capsys):
    n = db
    args = ["--tenant", f"t-{n}", "--project", f"p-{n}", "--workspace", f"ws-{n}", "--admin", f"admin-{n}", "--member", f"req-{n}=requester"]
    monkeypatch.setenv("RIPPLE_PROVISION_ADMIN_CREDENTIAL", f"secret-admin-{n}")
    monkeypatch.setenv(P.credential_env(f"req-{n}"), f"secret-req-{n}")
    assert P.main(args) == 0
    out = capsys.readouterr()
    assert "created" in out.out and f"secret-" not in out.out + out.err
    from infra.adapters.v2_identity import PostgresIdentityStore
    ids = PostgresIdentityStore(DSN)
    assert ids.login(f"admin-{n}", f"secret-admin-{n}") and ids.login(f"req-{n}", f"secret-req-{n}")
    monkeypatch.setenv("RIPPLE_PROVISION_ADMIN_CREDENTIAL", "another")
    monkeypatch.setenv(P.credential_env(f"req-{n}"), "another")
    assert P.main(args) == 0                                                   # idempotent: nothing is changed on a second run
    assert "left as it is" in capsys.readouterr().out
    assert ids.login(f"admin-{n}", f"secret-admin-{n}")


def test_missing_credential_or_ungrantable_role_writes_nothing(db, monkeypatch):
    n = db
    monkeypatch.delenv("RIPPLE_PROVISION_ADMIN_CREDENTIAL", raising=False)
    assert P.main(["--tenant", f"t-{n}", "--project", "p", "--workspace", "w", "--admin", "a"]) == 2
    monkeypatch.setenv("RIPPLE_PROVISION_ADMIN_CREDENTIAL", "x" * 12)
    monkeypatch.setenv(P.credential_env("m1"), "y" * 12)
    assert P.main(["--tenant", f"t-{n}", "--project", "p", "--workspace", "w", "--admin", "a", "--member", "m1=admin"]) == 2
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_workspaces WHERE tenant_id=%s", (f"t-{n}",))
        assert cur.fetchone()[0] == 0


def test_the_model_data_policy_is_set_only_when_named(db, monkeypatch):
    n = db
    monkeypatch.setenv("RIPPLE_PROVISION_ADMIN_CREDENTIAL", f"secret-admin-{n}")
    base = ["--tenant", f"t-{n}", "--project", f"p-{n}", "--workspace", f"ws-{n}", "--admin", f"admin-{n}"]
    assert P.main(base) == 0
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_project_model_policy WHERE tenant_id=%s AND project_id=%s", (f"t-{n}", f"p-{n}"))
        assert cur.fetchone()[0] == 0                                          # not named: no policy, AI jobs stay refused (fail closed)
    assert P.main(base + ["--external-provider", "allow"]) == 0
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT external_provider, prompt_storage FROM v2_project_model_policy WHERE tenant_id=%s AND project_id=%s", (f"t-{n}", f"p-{n}"))
        assert cur.fetchone() == ("allow", "hash_only")

