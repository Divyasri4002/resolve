"""
Owner-run database bootstrap (Phase 4.6.1).

The backend's runtime role (resolve_app) owns nothing and must not execute owner-level
DDL. Schema creation (Base.metadata.create_all) and the additive PostgreSQL upgrades are
therefore a separate, operator-invoked entrypoint that connects with a controlled owner
identity supplied through RESOLVE_DB_BOOTSTRAP_URL — never a hardcoded credential, and
never the runtime role.

Ordering that produces a correctly separated installation:

  fresh volume : up db -> run this bootstrap (as the compose POSTGRES_USER superuser) ->
                 run db/migrations/role_separation.sql (transfers ownership to
                 resolve_owner and grants the runtime set to resolve_app) -> up backend
  existing volume : run db/migrations/role_separation.sql -> up backend

Every schema change after the initial separation is executed AS resolve_owner (SET ROLE
within a psql session opened by the operator), because resolve_app can no longer create
or alter anything.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger("app.bootstrap_database")


async def _run() -> int:
    url = os.environ.get("RESOLVE_DB_BOOTSTRAP_URL", "")
    if not url:
        print(
            "bootstrap_database: RESOLVE_DB_BOOTSTRAP_URL is not set. Refusing to guess a "
            "connection identity: run this tool with the owner/superuser URL in the "
            "environment (the compose 'db-bootstrap' service does this).",
            file=sys.stderr,
        )
        return 2

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database import Base, apply_postgresql_schema_upgrades
    from app.models import db_models  # noqa: F401  (registers every model on Base)

    engine = create_async_engine(url, echo=False, future=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if conn.dialect.name == "postgresql":
                await apply_postgresql_schema_upgrades(conn)
            logger.info("bootstrap: schema created/upgraded as %s", conn.dialect.name)
    finally:
        await engine.dispose()
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
