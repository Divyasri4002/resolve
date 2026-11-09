"""
Async SQLAlchemy setup.

Three tables carry the whole system's state (see app/models/db_models.py):
  - missions    : one row per Mission, current state + the Recovery Mandate
  - messages    : the negotiation protocol log (claim / evidence / offer / counter / accept)
  - audit_log   : INSERT-ONLY. One row per Authority Engine decision. Never updated, never
                  deleted. This is what makes an "objective restored" claim auditable after
                  the fact instead of just trusted.
"""
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=False, future=True)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


# Additive schema upgrades applied at startup for tables that already exist. Every name here is a
# literal written in this file — nothing is derived from request data. Postgres gets
# ADD COLUMN IF NOT EXISTS; SQLite has no such clause, so _add_missing consults the inspector.
_SCHEMA_UPGRADES: dict[str, tuple[tuple[str, str], ...]] = {
    # Task 14C.4: the merchant-side linkage an executor may act on, and the claim recording what
    # was (or may have been) sent. All nullable: an existing row simply has no claim.
    "resolve_orders": (
        ("provider_order_reference", "provider_order_reference VARCHAR(128)"),
        # Task 14C.7D: the Shopify catalog identity of what was bought. Nullable, additive, and
        # never backfilled — an existing row simply has no Shopify identity, which is the truth for
        # anything discovered outside this store.
        ("shopify_product_gid", "shopify_product_gid VARCHAR(128)"),
        ("shopify_variant_gid", "shopify_variant_gid VARCHAR(128)"),
        ("shopify_store_domain", "shopify_store_domain VARCHAR(255)"),
    ),
    "returns": (
        ("provider_order_reference", "provider_order_reference VARCHAR(128)"),
        ("provider_line_reference", "provider_line_reference VARCHAR(128)"),
        ("provider_quantity", "provider_quantity INTEGER"),
        ("provider_reason", "provider_reason VARCHAR(64)"),
        ("submission_claim", "submission_claim VARCHAR(64)"),
        ("submission_claimed_at", "submission_claimed_at TIMESTAMP"),
    ),
    # Task CORE-2 (Phase 13): the outstanding more-information question for an issue. Nullable, so
    # every existing row simply has no pending question — which is the truth for it.
    "order_issues": (
        ("more_information_question", "more_information_question TEXT"),
    ),
    # Phase 4.4: the submission lease. Every row that predates the lease has no submission
    # in flight, so 'idle' is the honest backfill; submission_owner is nullable because an
    # idle row has no owner.
    "purchase_authorizations": (
        ("submission_state", "submission_state VARCHAR(16) NOT NULL DEFAULT 'idle'"),
        ("submission_owner", "submission_owner VARCHAR(64)"),
    ),
}

# Task 14C.5: the merchant-order linkage is 1:1 in both directions, and that is a storage
# invariant rather than a convention. NULLs are distinct in Postgres and SQLite, so every
# unlinked order keeps its own NULL row and nothing about existing data changes.
_SCHEMA_UPGRADE_INDEXES: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_resolve_orders_provider_order_reference "
    "ON resolve_orders (provider_order_reference);",
)


def _add_missing(sync_conn, inspector, table: str, columns) -> None:
    """Add any not-yet-existing nullable column to an existing SQLite table."""
    present = {c["name"] for c in inspector.get_columns(table)}
    for name, ddl in columns:
        if name not in present:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl};"))


async def apply_postgresql_schema_upgrades(conn) -> None:
    """
    The additive PostgreSQL schema upgrades (the former PG branch of init_models).

    Phase 4.6.1 moved every bit of runtime DDL out of the application's startup: this is
    now executed ONLY by the owner-run bootstrap (`python -m app.bootstrap_database`,
    connected with a controlled owner/superuser identity) — never by the backend's
    runtime role, which owns nothing and cannot ALTER anything.
    """
    await conn.execute(
        text(
            "ALTER TABLE missions ADD COLUMN IF NOT EXISTS mission_type VARCHAR(32) DEFAULT 'recovery';"
        )
    )
    await conn.execute(
        text(
            "ALTER TABLE missions ADD COLUMN IF NOT EXISTS shopping_intent JSON;"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_missions_mission_type ON missions (mission_type);"
        )
    )
    # Task 14B.5B — manual-review opt-out columns (additive, idempotent, non-destructive).
    # Fresh databases get these from create_all; this only upgrades the existing dev volume.
    await conn.execute(
        text(
            "ALTER TABLE order_issues ADD COLUMN IF NOT EXISTS "
            "manual_review_requested BOOLEAN NOT NULL DEFAULT FALSE;"
        )
    )
    await conn.execute(
        text(
            "ALTER TABLE order_issues ADD COLUMN IF NOT EXISTS "
            "manual_review_requested_at TIMESTAMP;"
        )
    )
    # Task 14C.4 — the return submission claim (additive, nullable, non-destructive).
    for table, columns in _SCHEMA_UPGRADES.items():
        for column, ddl in columns:
            await conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {ddl};")
            )
    for statement in _SCHEMA_UPGRADE_INDEXES:
        await conn.execute(text(statement))


async def init_models() -> None:
    """
    Startup schema handling.

    Phase 4.6.1: on PostgreSQL the runtime role owns nothing and must not execute
    owner-level DDL — this function performs NO DDL there. Schema creation and the
    additive upgrades are owner-run operations (app.bootstrap_database +
    db/migrations/role_separation.sql); startup verification of the resulting topology
    lives in app.db_enforcement and is called from the app lifespan.

    SQLite (the hermetic test dialect, with no roles to separate) keeps the previous
    behavior unchanged.
    """
    from app.models import db_models  # noqa: F401  (ensures models are registered)
    from sqlalchemy import text

    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            return
        await conn.run_sync(Base.metadata.create_all)

        def _upgrade_sqlite(sync_conn):
            from sqlalchemy import inspect
            inspector = inspect(sync_conn)
            if "missions" in inspector.get_table_names():
                cols = {c["name"] for c in inspector.get_columns("missions")}
                if "mission_type" not in cols:
                    sync_conn.execute(
                        text("ALTER TABLE missions ADD COLUMN mission_type VARCHAR(32) DEFAULT 'recovery';")
                    )
                if "shopping_intent" not in cols:
                    sync_conn.execute(
                        text("ALTER TABLE missions ADD COLUMN shopping_intent JSON;")
                    )
            # Task 14B.5B — manual-review opt-out columns for existing SQLite databases.
            # SQLite lacks ADD COLUMN IF NOT EXISTS, so guard via the inspector. A NOT NULL
            # boolean added to an existing table needs a non-null DEFAULT (0 == FALSE).
            if "order_issues" in inspector.get_table_names():
                issue_cols = {c["name"] for c in inspector.get_columns("order_issues")}
                if "manual_review_requested" not in issue_cols:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE order_issues ADD COLUMN manual_review_requested "
                            "BOOLEAN NOT NULL DEFAULT 0;"
                        )
                    )
                if "manual_review_requested_at" not in issue_cols:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE order_issues ADD COLUMN manual_review_requested_at "
                            "TIMESTAMP;"
                        )
                    )
                _add_missing(sync_conn, inspector, "order_issues", _SCHEMA_UPGRADES["order_issues"])
            if "resolve_orders" in inspector.get_table_names():
                _add_missing(sync_conn, inspector, "resolve_orders", _SCHEMA_UPGRADES["resolve_orders"])
                for statement in _SCHEMA_UPGRADE_INDEXES:
                    sync_conn.execute(text(statement))
            if "returns" in inspector.get_table_names():
                _add_missing(sync_conn, inspector, "returns", _SCHEMA_UPGRADES["returns"])
            if "purchase_authorizations" in inspector.get_table_names():
                _add_missing(
                    sync_conn,
                    inspector,
                    "purchase_authorizations",
                    _SCHEMA_UPGRADES["purchase_authorizations"],
                )
        await conn.run_sync(_upgrade_sqlite)
