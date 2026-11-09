"""
Retention policy for Shopify-derived protected order data (Task 14B.5A).

What this is: a REPORTING / DRY-RUN mechanism that identifies what a retention pass WOULD
consider eligible for cleanup, without ever deleting anything. That is deliberate.

Why not delete: as of this task RESOLVE persists NO Shopify-derived protected data. The
read-only Shopify client (app/tools/shopify/) keeps tokens and GraphQL responses in memory
only and is not wired into any executor or stored anywhere — verified by searching the code
base for writes of Shopify payloads (there are none). The only records in the database are
RESOLVE's OWN mission/order/payment/audit/resolution records, which:

  * are core application state, not Shopify-derived protected data; and
  * include insert-only audit rows and Razorpay payment/refund records that must NEVER be
    swept merely because they are old (the audit trail and the financial record are exactly
    what a human or regulator reads later).

So a real destructive cleanup has nothing safe to act on, and deleting core records would
damage mission/payment/audit integrity. Per the task, this module therefore reports and
documents the boundary instead of deleting. It enforces that boundary structurally: the only
tables the retention engine is even allowed to consider are those explicitly registered as
Shopify-derived protected sources, and the protected-core allowlist can never be removed from
protection. If/when a Shopify-derived snapshot table is introduced, it is registered here and
becomes eligible; core tables never are.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.config import get_settings

# Tables whose records are CORE application / financial / audit state. These are protected by
# construction: the retention engine will never classify a row from any of them as eligible,
# regardless of age, and they cannot be unregistered.
PROTECTED_CORE_TABLES: frozenset[str] = frozenset(
    {
        "missions",
        "messages",
        "audit_log",
        "resolve_orders",
        "refunds",
        "replacements",
        "returns",
        "resolutions",
        "resolution_assessments",
        "order_issues",
        "users",
    }
)


@dataclass(frozen=True)
class ShopifyProtectedSource:
    """
    A table that stores SHOPIFY-DERIVED protected data and therefore falls under the retention
    policy. Registering one here is the ONLY way a table becomes eligible for cleanup, and a
    protected core table can never be registered. `id_column` and `timestamp_column` describe
    how a future real cleanup would identify and age rows.
    """

    table: str
    id_column: str
    timestamp_column: str

    def __post_init__(self) -> None:
        if self.table in PROTECTED_CORE_TABLES:
            raise ValueError(
                f"'{self.table}' is a protected core table and can never be subject to "
                "Shopify-derived data retention deletion."
            )


# Shopify-derived protected sources currently registered for retention. EMPTY by design: no
# such table is persisted yet (see module docstring). This list, not a broad scan, defines the
# entire blast radius of any future real cleanup.
SHOPIFY_PROTECTED_SOURCES: tuple[ShopifyProtectedSource, ...] = ()


@dataclass
class RetentionReport:
    """The outcome of a DRY-RUN retention pass. Nothing here was deleted."""

    generated_at: datetime
    retention_days: int
    cutoff: datetime
    dry_run: bool
    sources_considered: list[str] = field(default_factory=list)
    protected_core_tables_skipped: list[str] = field(default_factory=list)
    eligible_row_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at.isoformat(),
            "retention_days": self.retention_days,
            "cutoff": self.cutoff.isoformat(),
            "dry_run": self.dry_run,
            "sources_considered": self.sources_considered,
            "protected_core_tables_skipped": self.protected_core_tables_skipped,
            "eligible_row_counts": self.eligible_row_counts,
            "notes": self.notes,
        }


async def build_retention_report(
    db,
    *,
    now: datetime | None = None,
    retention_days: int | None = None,
) -> RetentionReport:
    """
    Produce a DRY-RUN retention report. It never deletes: RESOLVE's implementation of the
    Shopify protected-data retention policy is reporting-only until a real Shopify-derived
    store exists, so there is nothing here that could touch mission/payment/audit integrity.

    `retention_days` defaults to the configured value; callers (a scheduler, an admin route)
    would pass an explicit value. The cutoff and per-source eligible counts are computed only
    for explicitly-registered Shopify-derived sources — the protected core tables are listed as
    skipped and are never counted or removed.
    """
    settings = get_settings()
    days = retention_days if retention_days is not None else settings.shopify_retention_days
    moment = now or datetime.utcnow()
    cutoff = moment - timedelta(days=days)

    report = RetentionReport(
        generated_at=moment,
        retention_days=days,
        cutoff=cutoff,
        dry_run=True,
        protected_core_tables_skipped=sorted(PROTECTED_CORE_TABLES),
    )

    if not SHOPIFY_PROTECTED_SOURCES:
        report.notes.append(
            "No Shopify-derived protected data is persisted in RESOLVE, so no rows are eligible "
            "for cleanup. Core mission/payment/audit records are intentionally protected and are "
            "never deleted by age. This is a reporting-only retention pass."
        )
        return report

    from sqlalchemy import func, select, text

    for source in SHOPIFY_PROTECTED_SOURCES:
        report.sources_considered.append(source.table)
        count = await db.scalar(
            select(func.count())
            .select_from(text(source.table))
            .where(text(f"{source.timestamp_column} < :cutoff"))
        )
        report.eligible_row_counts[source.table] = int(count or 0)

    report.notes.append(
        "Counts above are rows that a future, explicitly-enabled cleanup WOULD target; nothing "
        "was deleted. Automatic deletion is not implemented because it is not safe to run "
        "against core records and no Shopify-derived store exists to run against."
    )
    return report


__all__ = [
    "PROTECTED_CORE_TABLES",
    "SHOPIFY_PROTECTED_SOURCES",
    "RetentionReport",
    "ShopifyProtectedSource",
    "build_retention_report",
]
