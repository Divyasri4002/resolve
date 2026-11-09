"""
ROLE-1..12 — the Phase 4.6.1 role-separation battery, proven against ISOLATED PostgreSQL 16.

ISOLATION REQUIREMENT: RESOLVE_PG_ADMIN_URL must point at a THROWAWAY PostgreSQL 16
instance — never at the dev compose volume. These tests create and drop sandbox
databases and set resolve_app's password on that cluster:

    docker run --rm -d --name resolve-role-test-pg -p 55432:5432 \
        -e POSTGRES_PASSWORD=throwaway postgres:16-alpine
    RESOLVE_PG_ADMIN_URL=postgresql+asyncpg://postgres:throwaway@localhost:55432/postgres \
        python -m pytest tests/test_role_separation.py -q

(In the dev environment the suite runs inside Docker against a sibling container.)

Every sandbox is driven through the REAL production sequence, never a shortcut:
roles prepared (the 01-roles.sh equivalent) -> bootstrap (create_all + the additive PG
upgrades, exactly what the db-bootstrap compose service does) -> db/migrations/
role_separation.sql executed VERBATIM -> assertions as resolve_app (and, for contrast,
as the admin identity). Tests skip when the URL is unset, following the M21
RESOLVE_PG_TEST_URL convention.
"""

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.db_enforcement import verify_database_enforcement
from app.main import app
from app.models.db_models import AuditLogRecord, ResolveOrderRecord

BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_SQL = BACKEND_DIR / "db" / "migrations" / "role_separation.sql"
INIT_SCRIPT = BACKEND_DIR / "db" / "init" / "01-roles.sh"


def _compose_file() -> Path:
    """
    Where the deployment compose file lives, or a loud failure.

    The backend image is built from ./backend, so docker-compose.yml is not baked in —
    mount it read-only for the test run (`-v <repo>/docker-compose.yml:/docker-compose.yml:ro`)
    or set RESOLVE_DEPLOY_ROOT. A silent skip would look like the topology had been proven.
    """
    candidates = []
    configured = os.environ.get("RESOLVE_DEPLOY_ROOT")
    if configured:
        candidates.append(Path(configured) / "docker-compose.yml")
    candidates.append(Path(__file__).resolve().parents[2] / "docker-compose.yml")
    for path in candidates:
        if path.is_file():
            return path
    raise AssertionError(
        "docker-compose.yml is not visible to this test run. Mount it read-only "
        "(-v <repo>/docker-compose.yml:/docker-compose.yml:ro) or set RESOLVE_DEPLOY_ROOT "
        "so the committed deployment topology can be verified. Nothing here guesses."
    )

ADMIN_URL = os.environ.get("RESOLVE_PG_ADMIN_URL", "")
# Sandbox-only credential for the throwaway cluster's resolve_app role. This is not a
# deployment secret and must never become one: production sets resolve_app's password
# through POSTGRES_APP_PASSWORD / an operator ALTER ROLE, never through this file.
APP_TEST_PASSWORD = "sandbox_only_role_test_pw"


class RoleSandbox:
    def __init__(self) -> None:
        self.name = f"role_test_{uuid.uuid4().hex[:10]}"

    def _url(self, **overrides):
        return make_url(ADMIN_URL).set(database=self.name, **overrides)

    def admin_url(self) -> str:
        return self._url(drivername="postgresql+asyncpg")

    def app_url(self) -> str:
        return self._url(
            drivername="postgresql+asyncpg",
            username="resolve_app",
            password=APP_TEST_PASSWORD,
        ).render_as_string(hide_password=False)

    async def _admin_conn(self, dbname: str | None = None):
        url = self._url(drivername="postgresql")
        if dbname is not None:
            url = url.set(database=dbname)
        return await asyncpg.connect(url.render_as_string(hide_password=False))

    async def prepare_roles(self) -> None:
        """The 01-roles.sh equivalent for an existing cluster: both roles, idempotently,
        plus resolve_app's sandbox password (fresh volumes get it from the init script)."""
        conn = await self._admin_conn("postgres")
        try:
            await conn.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'resolve_owner') THEN "
                "CREATE ROLE resolve_owner NOLOGIN; END IF; "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'resolve_app') THEN "
                "CREATE ROLE resolve_app LOGIN; END IF; "
                "END $$;"
            )
            await conn.execute(f"ALTER ROLE resolve_app PASSWORD '{APP_TEST_PASSWORD}'")
        finally:
            await conn.close()

    async def create(self) -> None:
        conn = await self._admin_conn("postgres")
        try:
            await conn.execute(f'CREATE DATABASE "{self.name}"')
        finally:
            await conn.close()

    async def drop(self) -> None:
        conn = await self._admin_conn("postgres")
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                self.name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{self.name}"')
        finally:
            await conn.close()

    async def bootstrap(self) -> None:
        """Exactly what the compose db-bootstrap service does: schema creation and the
        additive PG upgrades under a controlled owner-identity connection."""
        from app.database import apply_postgresql_schema_upgrades
        from app.models import db_models  # noqa: F401  (registers every model)

        engine = create_async_engine(self.admin_url(), future=True)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await apply_postgresql_schema_upgrades(conn)
        finally:
            await engine.dispose()

    async def migrate(self) -> None:
        """db/migrations/role_separation.sql, executed VERBATIM — the file is pure SQL
        precisely so a driver can run it unmodified."""
        conn = await self._admin_conn()
        try:
            await conn.execute(MIGRATION_SQL.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    def app_engine(self):
        return create_async_engine(self.app_url(), future=True)

    async def app_conn(self):
        url = self._url(
            drivername="postgresql",
            username="resolve_app",
            password=APP_TEST_PASSWORD,
        )
        return await asyncpg.connect(url.render_as_string(hide_password=False))


@pytest.fixture
async def pg_sandbox():
    sb = RoleSandbox()
    await sb.prepare_roles()
    await sb.create()
    try:
        yield sb
    finally:
        await sb.drop()


async def _separated_sandbox():
    """A sandbox taken through the full production sequence (fresh-install ordering)."""
    sb = RoleSandbox()
    await sb.prepare_roles()
    await sb.create()
    await sb.bootstrap()
    await sb.migrate()
    return sb


# --------------------------------------------------------------------------- #
# ROLE-1: the fresh-install sequence yields a topology startup verification passes.
# --------------------------------------------------------------------------- #
async def test_role1_fresh_separation_passes_verification(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    engine = sb.app_engine()
    try:
        report = await verify_database_enforcement(engine)
    finally:
        await engine.dispose()

    assert report.protected_ops_allowed is True
    assert report.runtime_user == "resolve_app"
    assert report.failures == []


# --------------------------------------------------------------------------- #
# ROLE-2: ownership — every object owned by resolve_owner, nothing by resolve_app.
# --------------------------------------------------------------------------- #
async def test_role2_ownership_topology(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    conn = await sb._admin_conn()
    try:
        rows = await conn.fetch(
            "SELECT c.relname AS name, c.relowner::regrole::text AS owner "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        )
        owners = {r["name"]: r["owner"] for r in rows}
        assert len(owners) >= 14, "bootstrap must have created every business table"
        assert set(owners.values()) == {"resolve_owner"}, owners

        db_owner = await conn.fetchval(
            "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1",
            sb.name,
        )
        assert db_owner == "resolve_owner"
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# ROLE-3: the runtime role creates nothing — no tables, no schemas.
# --------------------------------------------------------------------------- #
async def test_role3_runtime_creates_nothing(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    conn = await sb.app_conn()
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE public.role3_hack (id int)")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE SCHEMA role3_schema")
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# ROLE-4: the runtime role alters and drops nothing.
# --------------------------------------------------------------------------- #
async def test_role4_runtime_alters_and_drops_nothing(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    conn = await sb.app_conn()
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("ALTER TABLE missions ADD COLUMN role4_probe text")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DROP TABLE messages")
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# ROLE-5: normal runtime writes are preserved (SELECT / INSERT / UPDATE).
# --------------------------------------------------------------------------- #
async def test_role5_runtime_writes_preserved(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    engine = sb.app_engine()
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            session.add(
                ResolveOrderRecord(
                    order_id="ro_role5",
                    mission_id="m_role5",
                    user_id="u_role5",
                    title="Role 5 probe",
                    amount=100.0,
                    razorpay_order_id="order_test",
                    razorpay_payment_id="pay_test",
                )
            )
            await session.commit()

        async with maker() as session:
            row = await session.get(ResolveOrderRecord, "ro_role5")
            assert row is not None
            row.status = "payment_verified"
            await session.commit()

        async with maker() as session:
            row = await session.get(ResolveOrderRecord, "ro_role5")
            assert row.status == "payment_verified"
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# ROLE-6: DELETE and TRUNCATE are refused on the three protected trigger-source
# tables (the privilege check fires on the statement, not on rows).
# --------------------------------------------------------------------------- #
async def test_role6_protected_tables_not_deletable(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    conn = await sb.app_conn()
    try:
        for table in ("resolve_orders", "purchase_authorizations", "integration_events"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(f"DELETE FROM {table}")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(f"TRUNCATE {table}")
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# ROLE-7: audit_log is insert-only at the GRANT level, and DELETE is granted
# nowhere — insert-only is enforced by the database, not just by convention.
# --------------------------------------------------------------------------- #
async def test_role7_audit_log_insert_only(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    engine = sb.app_engine()
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            session.add(
                AuditLogRecord(
                    mission_id="m_role7",
                    actor="buyer_agent",
                    action="role7_probe",
                    decision="approved",
                    reason="role separation battery",
                )
            )
            await session.commit()
    finally:
        await engine.dispose()

    conn = await sb.app_conn()
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE audit_log SET decision = 'blocked'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM audit_log")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM missions")
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# ROLE-8: the ledger surface is default-deny and fail-closed — with no ledger
# tables at all (the pre-4.6.2 deployment) startup enforcement passes; with
# ledger-named placeholder tables that the migration did not install, the runtime
# role holds nothing (Phase 4.6.1 posture) and startup enforcement refuses (the
# named-but-uninstalled evidence surface is exactly what 4.6.2 must not bless).
# --------------------------------------------------------------------------- #
async def test_role8_future_ledger_tables_default_deny(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    engine = sb.app_engine()
    try:
        report = await verify_database_enforcement(engine)
        assert report.protected_ops_allowed is True, report.failures
    finally:
        await engine.dispose()

    admin_conn = await sb._admin_conn()
    try:
        # The superuser is the only controlled entry to the NOLOGIN owner role.
        await admin_conn.execute("SET ROLE resolve_owner")
        await admin_conn.execute(
            "CREATE TABLE purchase_events (event_id bigint PRIMARY KEY, payload jsonb NOT NULL)"
        )
        await admin_conn.execute("CREATE TABLE ledger_state (id int PRIMARY KEY, head text)")
        await admin_conn.execute(
            "CREATE TABLE ledger_publish_state (pk text PRIMARY KEY, pin text)"
        )
        await admin_conn.execute("RESET ROLE")
    finally:
        await admin_conn.close()

    engine = sb.app_engine()
    try:
        report = await verify_database_enforcement(engine)
        assert report.protected_ops_allowed is False
        assert any(
            "expected ledger trigger" in f or "expected ledger function" in f
            for f in report.failures
        ), report.failures
    finally:
        await engine.dispose()

    conn = await sb.app_conn()
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO purchase_events (event_id, payload) VALUES (1, '{}')"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("INSERT INTO ledger_state (id, head) VALUES (1, 'h')")
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# ROLE-9: no escalation — the runtime role cannot assume the owner, and the
# owner cannot log in at all.
# --------------------------------------------------------------------------- #
async def test_role9_no_privilege_escalation(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    conn = await sb.app_conn()
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("SET ROLE resolve_owner")
    finally:
        await conn.close()

    admin_conn = await sb._admin_conn()
    try:
        owner_login = await admin_conn.fetchval(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'resolve_owner'"
        )
        assert owner_login is False
        # And the owner role has no password to leak: it cannot authenticate.
        owner_password = await admin_conn.fetchval(
            "SELECT rolpassword IS NOT NULL FROM pg_authid WHERE rolname = 'resolve_owner'"
        )
        assert owner_password is False
    finally:
        await admin_conn.close()


# --------------------------------------------------------------------------- #
# ROLE-10: the committed deployment files declare the topology — and commit no
# owner credential. (Pure source scan; runs without PostgreSQL.)
# --------------------------------------------------------------------------- #
def test_role10_deployment_files_declare_topology():
    compose = _compose_file().read_text(encoding="utf-8")
    assert "postgresql+asyncpg://resolve_app:" in compose, (
        "the backend must connect as the runtime role"
    )
    assert "docker-entrypoint-initdb.d" in compose
    assert ":/migrations:ro" in compose
    assert "POSTGRES_APP_PASSWORD" in compose
    assert "db-bootstrap" in compose
    assert "python -m app.bootstrap_database" in compose
    assert "RESOLVE_DB_BOOTSTRAP_URL" in compose
    # The NOLOGIN owner is never named in compose, because nothing ever connects as it.
    assert "resolve_owner" not in compose

    migration = MIGRATION_SQL.read_text(encoding="utf-8")
    assert "CREATE ROLE resolve_owner NOLOGIN" in migration
    assert "CREATE ROLE resolve_app LOGIN" in migration
    assert "OWNER TO resolve_owner" in migration
    assert "REVOKE DELETE, TRUNCATE ON public.resolve_orders FROM resolve_app" in migration
    assert (
        "REVOKE DELETE, TRUNCATE ON public.purchase_authorizations FROM resolve_app"
        in migration
    )
    assert "REVOKE DELETE, TRUNCATE ON public.integration_events FROM resolve_app" in migration
    assert "REVOKE ALL ON public.purchase_events FROM resolve_app" in migration
    assert "GRANT SELECT, INSERT ON public.audit_log TO resolve_app" in migration
    # No credential ever passes through the migration file: the word "password" may
    # appear only in explanatory comments, never in executed SQL.
    sql_only = "\n".join(
        line for line in migration.splitlines() if not line.strip().startswith("--")
    )
    assert "PASSWORD" not in sql_only.upper()
    assert "ALTER ROLE" not in sql_only.upper()

    init_script = INIT_SCRIPT.read_text(encoding="utf-8")
    assert "CREATE ROLE resolve_owner NOLOGIN" in init_script
    assert "CREATE ROLE resolve_app LOGIN" in init_script
    assert "POSTGRES_APP_PASSWORD" in init_script

    main_py = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    assert "initialize_db_enforcement" in main_py


# --------------------------------------------------------------------------- #
# ROLE-11: data written before the migration survives it unchanged, and the
# migration is idempotent — a second run changes nothing and breaks nothing.
# --------------------------------------------------------------------------- #
async def test_role11_data_preserved_and_migration_idempotent(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()

    admin_engine = create_async_engine(sb.admin_url(), future=True)
    try:
        admin_maker = async_sessionmaker(admin_engine, expire_on_commit=False)
        async with admin_maker() as session:
            session.add(
                ResolveOrderRecord(
                    order_id="ro_pre_a",
                    mission_id="m_pre_a",
                    user_id="u_pre",
                    title="Pre-migration A",
                    amount=10.0,
                    razorpay_order_id="order_pre_a",
                    razorpay_payment_id="pay_pre_a",
                )
            )
            session.add(
                ResolveOrderRecord(
                    order_id="ro_pre_b",
                    mission_id="m_pre_b",
                    user_id="u_pre",
                    title="Pre-migration B",
                    amount=20.0,
                    razorpay_order_id="order_pre_b",
                    razorpay_payment_id="pay_pre_b",
                )
            )
            await session.commit()
    finally:
        await admin_engine.dispose()

    await sb.migrate()
    await sb.migrate()  # idempotent: the second run must be a clean no-op

    engine = sb.app_engine()
    try:
        report = await verify_database_enforcement(engine)
        assert report.protected_ops_allowed is True

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            rows = (
                (await session.execute(ResolveOrderRecord.__table__.select())).mappings().all()
            )
            assert len(rows) == 2
            by_id = {r["order_id"]: r for r in rows}
            assert by_id["ro_pre_a"]["title"] == "Pre-migration A"
            assert by_id["ro_pre_b"]["amount"] == 20.0

            # Runtime privileges still apply to the transferred pre-existing tables.
            session.add(
                ResolveOrderRecord(
                    order_id="ro_post",
                    mission_id="m_post",
                    user_id="u_pre",
                    title="Post-migration write",
                    amount=30.0,
                    razorpay_order_id="order_post",
                    razorpay_payment_id="pay_post",
                )
            )
            await session.commit()
            count = (
                await session.execute(
                    select(func.count()).select_from(ResolveOrderRecord)
                )
            ).scalar_one()
            assert count == 3
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# ROLE-12: fail closed — a drifted topology is refused with named failures, the
# gate turns a refused report into 503, and the application's startup path
# performs no DDL on PostgreSQL.
# --------------------------------------------------------------------------- #
async def test_role12_refuses_drifted_topology(pg_sandbox):
    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    # A deployment still connecting with the old (bootstrap/owner) identity is refused.
    admin_engine = create_async_engine(sb.admin_url(), future=True)
    try:
        report = await verify_database_enforcement(admin_engine)
        assert report.protected_ops_allowed is False
        assert any("runtime identity" in f for f in report.failures)
    finally:
        await admin_engine.dispose()


async def test_role12_gate_raises_503(monkeypatch):
    from fastapi import HTTPException

    from app import db_enforcement

    refused = db_enforcement.EnforcementReport(
        dialect="postgresql",
        protected_ops_allowed=False,
        runtime_user="postgres",
        failures=["runtime identity is 'postgres', expected 'resolve_app'"],
    )
    monkeypatch.setattr(db_enforcement, "_report", refused)
    with pytest.raises(HTTPException) as excinfo:
        db_enforcement.require_protected_ops()
    assert excinfo.value.status_code == 503
    assert "runtime identity is 'postgres'" in excinfo.value.detail


async def test_role12_startup_performs_no_ddl_on_pg(pg_sandbox, monkeypatch):
    from app import database as app_database

    sb = pg_sandbox
    await sb.bootstrap()
    await sb.migrate()

    engine = sb.app_engine()
    try:
        # init_models() over the runtime engine — the same call the lifespan makes — must
        # complete without touching DDL. That the runtime role cannot execute DDL at all
        # is proven by ROLE-3/ROLE-4: had the startup path attempted create_all or the PG
        # upgrades, this call would fail with an InsufficientPrivilege error.
        monkeypatch.setattr(app_database, "engine", engine)
        await app_database.init_models()
    finally:
        await engine.dispose()


async def test_role12_route_level_fail_closed(monkeypatch):
    """Both protected routes return 503 when the verification report is refused — and
    the refusal happens before any mission or body processing."""
    from fastapi import HTTPException  # noqa: F401  (documented failure shape)

    from app import db_enforcement

    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def override_get_db():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    refused = db_enforcement.EnforcementReport(
        dialect="postgresql",
        protected_ops_allowed=False,
        runtime_user="postgres",
        failures=["runtime identity is 'postgres', expected 'resolve_app'"],
    )
    monkeypatch.setattr(db_enforcement, "_report", refused)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            purchase = await client.post("/missions/does-not-matter/order/purchase")
            linkage = await client.post(
                "/integrations/shopify/order-linkage", content=b"{}"
            )
        assert purchase.status_code == 503
        assert "Protected database operations are refused" in purchase.text
        assert linkage.status_code == 503
        assert "Protected database operations are refused" in linkage.text
    finally:
        app.dependency_overrides.clear()
        await test_engine.dispose()
