"""
Shared PostgreSQL 16 sandbox for the Phase 4.6.2 ledger battery.

Same isolation contract as the ROLE-1..12 battery (tests/test_role_separation.py):
RESOLVE_PG_ADMIN_URL must point at a THROWAWAY PostgreSQL 16 instance — never at the
dev compose volume. Every sandbox is a fresh database driven through the REAL
production sequence:

    roles prepared -> bootstrap (create_all + additive PG upgrades)
    -> db/migrations/role_separation.sql executed VERBATIM
    -> db/migrations/ledger_install.sql executed VERBATIM

Tests skip when RESOLVE_PG_ADMIN_URL is unset, following the M21 convention. Nothing
here is a substitute for the migration: the SQL files are executed byte-for-byte by
the driver, never re-implemented in Python.
"""
import os
import uuid
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROLE_SEPARATION_SQL = BACKEND_DIR / "db" / "migrations" / "role_separation.sql"
LEDGER_INSTALL_SQL = BACKEND_DIR / "db" / "migrations" / "ledger_install.sql"

ADMIN_URL = os.environ.get("RESOLVE_PG_ADMIN_URL", "")
# Sandbox-only credential (see test_role_separation.py — never a deployment secret).
APP_TEST_PASSWORD = "sandbox_only_role_test_pw"


class LedgerSandbox:
    def __init__(self) -> None:
        self.name = f"ledger_test_{uuid.uuid4().hex[:10]}"
        self._engines: list[AsyncEngine] = []
        # The engine backing `sessions`, whichever identity built it — async_sessionmaker
        # has no get_bind(), so tests reach the engine through this attribute.
        self.sessions_engine: AsyncEngine | None = None

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
        """Exactly what the compose db-bootstrap service does."""
        from app.database import apply_postgresql_schema_upgrades
        from app.models import db_models  # noqa: F401  (registers every model)

        engine = create_async_engine(self.admin_url(), future=True)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(db_models.Base.metadata.create_all)
                await apply_postgresql_schema_upgrades(conn)
        finally:
            await engine.dispose()

    async def run_sql_file(self, path: Path) -> None:
        """One migration file, VERBATIM, through the simple query protocol."""
        conn = await self._admin_conn()
        try:
            await conn.execute(path.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    async def run_role_separation(self) -> None:
        await self.run_sql_file(ROLE_SEPARATION_SQL)

    async def run_ledger_install(self) -> None:
        await self.run_sql_file(LEDGER_INSTALL_SQL)

    def app_engine(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = create_async_engine(self.app_url(), future=True)
        self._engines.append(engine)
        self.sessions_engine = engine
        return async_sessionmaker(engine, expire_on_commit=False)

    def admin_engine(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = create_async_engine(self.admin_url(), future=True)
        self._engines.append(engine)
        self.sessions_engine = engine
        return async_sessionmaker(engine, expire_on_commit=False)

    async def dispose_engines(self) -> None:
        self.sessions_engine = None
        while self._engines:
            await self._engines.pop().dispose()

    async def admin_scalar(self, sql: str):
        conn = await self._admin_conn()
        try:
            return await conn.fetchval(sql)
        finally:
            await conn.close()
