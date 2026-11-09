"""
Runtime database-role enforcement (Phase 4.6.1).

The backend connects as `resolve_app` — a role that owns nothing, creates nothing, and
cannot write the future purchase-event ledger (approved design: Phase 4.5.8 P1, validated
Phase 4.5.9 C6). Ownership of every table belongs to the NOLOGIN role `resolve_owner`;
all schema DDL lives in two operator-run operations (python -m app.bootstrap_database and
db/migrations/role_separation.sql), and the application's startup performs NO DDL at all.

This module is the application's fail-closed verifier. At startup it checks the runtime
identity, schema presence, table ownership, and privilege posture against that topology,
then arms or refuses the PROTECTED operations — merchant-purchase submission and the
order-linkage receiver. Everything else keeps running: a deployment whose role topology
has not been migrated (or has drifted) loses purchase and linkage, not the whole app,
and the failure is loud (logged, reported through the gate's 503 detail), never silent.

SQLite (the hermetic test dialect) cannot express roles, ownership, or privileges. There
the verifier reports the checks as out of scope rather than pretending they passed; the
ROLE-1..12 battery proves the real posture against isolated PostgreSQL 16.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

RUNTIME_ROLE = "resolve_app"
OWNER_ROLE = "resolve_owner"

# The schema the runtime expects to find. A missing table means the owner-run bootstrap
# has not happened (or the database is wrong); the protected operations stay closed.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "missions",
        "messages",
        "audit_log",
        "users",
        "agent_conversation",
        "resolve_orders",
        "order_issues",
        "resolution_assessments",
        "refunds",
        "replacements",
        "returns",
        "resolutions",
        "integration_events",
        "purchase_authorizations",
    }
)

# The three trigger-source tables (Phase 4.5.6): the runtime may write rows but never
# delete or truncate them.
PROTECTED_TABLES: frozenset[str] = frozenset(
    {"resolve_orders", "purchase_authorizations", "integration_events"}
)

# The event-ledger tables (Phase 4.6.2, installed by db/migrations/ledger_install.sql).
# C6 contract: the runtime READS the event and counter tables and may advance ONLY the
# publication pin (SELECT + UPDATE; the singleton pin row is pre-created by the owner
# migration and INSERT is useless to the runtime given the id=1 primary key, so it is
# forbidden surface). Everything else is a failure, in BOTH directions: a held write
# privilege beyond the pin's, AND a missing pin privilege (a publisher that cannot
# record evidence would make publication a lie of omission).
LEDGER_TABLES: frozenset[str] = frozenset(
    {"purchase_events", "ledger_state", "ledger_publish_state"}
)
LEDGER_FORBIDDEN_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "purchase_events": ("INSERT", "UPDATE", "DELETE", "TRUNCATE"),
    "ledger_state": ("INSERT", "UPDATE", "DELETE", "TRUNCATE"),
    "ledger_publish_state": ("INSERT", "DELETE", "TRUNCATE"),
}
LEDGER_REQUIRED_PIN_PRIVILEGES: tuple[str, ...] = ("SELECT", "UPDATE")

# The exact trigger set the ledger migration installs (non-internal triggers on the six
# ledger-involved tables). Verification is per-pair against this frozen list — an empty
# or incomplete catalog fails every check it should, never silently passes.
LEDGER_EMIT_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("resolve_emit_pa_events", "purchase_authorizations"),
    ("resolve_emit_ie_events", "integration_events"),
    ("resolve_emit_ro_events", "resolve_orders"),
)
_LEDGER_INVOLVED_TABLES: tuple[str, ...] = (
    "resolve_orders",
    "purchase_authorizations",
    "integration_events",
    "purchase_events",
    "ledger_state",
    "ledger_publish_state",
)
LEDGER_GUARD_TRIGGERS: tuple[tuple[str, str], ...] = tuple(
    (name, table)
    for table in _LEDGER_INVOLVED_TABLES
    for name in ("resolve_refuse_delete", "resolve_refuse_truncate")
)
LEDGER_DEFINER_FUNCTIONS: tuple[str, ...] = (
    "resolve_emit_pa_event",
    "resolve_emit_ie_event",
    "resolve_emit_ro_event",
    "resolve_refuse_row_deletion",
    "resolve_refuse_truncation",
)
LEDGER_FUNCTION_SEARCH_PATH = "search_path=pg_catalog, public"


@dataclass
class EnforcementReport:
    """The outcome of the startup verification. `failures` names every unmet check."""

    dialect: str
    protected_ops_allowed: bool
    runtime_user: str | None = None
    failures: list[str] = field(default_factory=list)

    def as_log_line(self) -> str:
        state = "allowed" if self.protected_ops_allowed else "REFUSED"
        detail = "; ".join(self.failures) if self.failures else "all checks passed"
        return (
            f"database role enforcement ({self.dialect}, current_user="
            f"{self.runtime_user}): protected operations {state} [{detail}]"
        )


async def verify_database_enforcement(engine: AsyncEngine) -> EnforcementReport:
    """
    Verify the deployed role topology. Pure function of the database state: takes any
    engine (so the ROLE-1..12 battery can point it at isolated sandboxes) and returns the
    report without mutating module state.
    """
    async with engine.connect() as conn:
        dialect = conn.dialect.name
        if dialect != "postgresql":
            return EnforcementReport(
                dialect=dialect,
                protected_ops_allowed=True,
                runtime_user=None,
                failures=[
                    f"dialect '{dialect}' cannot express role separation; "
                    "enforcement is proven on PostgreSQL only"
                ],
            )

        runtime_user = (
            await conn.execute(text("SELECT current_user"))
        ).scalar_one()
        failures: list[str] = []

        if runtime_user != RUNTIME_ROLE:
            failures.append(
                f"runtime identity is '{runtime_user}', expected '{RUNTIME_ROLE}'"
            )

        present = {
            row.table_name: row.owner
            for row in (
                await conn.execute(
                    text(
                        "SELECT c.relname AS table_name, c.relowner::regrole::text AS owner "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
                    )
                )
            ).mappings()
        }

        for table in sorted(EXPECTED_TABLES):
            owner = present.get(table)
            if owner is None:
                failures.append(f"expected table '{table}' is missing")
            elif owner != OWNER_ROLE:
                failures.append(
                    f"table '{table}' is owned by '{owner}', expected '{OWNER_ROLE}'"
                )

        for table in sorted(PROTECTED_TABLES):
            if table not in present:
                continue
            for privilege in ("DELETE", "TRUNCATE"):
                if (
                    await conn.execute(
                        text(
                            "SELECT has_table_privilege(:table, :privilege)"
                        ),
                        {"table": table, "privilege": privilege},
                    )
                ).scalar_one():
                    failures.append(
                        f"runtime role holds {privilege} on protected table '{table}'"
                    )

        ledger_present = LEDGER_TABLES & present.keys()
        if ledger_present and ledger_present != LEDGER_TABLES:
            failures.append(
                "the ledger tables are only partially present ("
                + ", ".join(sorted(ledger_present))
                + "); the owner-run ledger migration is incomplete"
            )
        elif ledger_present == LEDGER_TABLES:
            for table, privileges in sorted(LEDGER_FORBIDDEN_PRIVILEGES.items()):
                for privilege in privileges:
                    if (
                        await conn.execute(
                            text("SELECT has_table_privilege(:table, :privilege)"),
                            {"table": table, "privilege": privilege},
                        )
                    ).scalar_one():
                        failures.append(
                            f"runtime role holds {privilege} on ledger table '{table}' "
                            "beyond the C6 contract"
                        )
            for privilege in LEDGER_REQUIRED_PIN_PRIVILEGES:
                if not (
                    await conn.execute(
                        text(
                            "SELECT has_table_privilege('ledger_publish_state', :privilege)"
                        ),
                        {"privilege": privilege},
                    )
                ).scalar_one():
                    failures.append(
                        f"runtime role lacks {privilege} on ledger_publish_state; the "
                        "publisher could not record publication evidence"
                    )

            # The exact trigger set, verified per pair in both directions. An empty or
            # incomplete catalog fails every expected-pair check below — nothing here is
            # a boolean aggregate that could pass on zero rows.
            trigger_rows = (
                await conn.execute(
                    text(
                        "SELECT t.tgname, c.relname AS table_name, t.tgenabled::text "
                        "FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE NOT t.tgisinternal AND n.nspname = 'public' "
                        "AND c.relname = ANY(:tables)"
                    ),
                    {"tables": list(_LEDGER_INVOLVED_TABLES)},
                )
            ).all()
            present_triggers = {(row[0], row[1]): row[2] for row in trigger_rows}
            expected_triggers = set(LEDGER_EMIT_TRIGGERS) | set(LEDGER_GUARD_TRIGGERS)
            for name, table in sorted(expected_triggers):
                enabled = present_triggers.get((name, table))
                if enabled is None:
                    failures.append(
                        f"expected ledger trigger '{name}' on '{table}' is missing"
                    )
                elif enabled not in ("O", "A"):
                    failures.append(
                        f"ledger trigger '{name}' on '{table}' is disabled "
                        f"(tgenabled={enabled})"
                    )
            for name, table in sorted(set(present_triggers) - expected_triggers):
                failures.append(
                    f"unexpected trigger '{name}' on '{table}' — ledger topology drift"
                )

            function_rows = (
                await conn.execute(
                    text(
                        "SELECT p.proname, p.prosecdef, p.proowner::regrole::text AS owner, "
                        "p.proconfig, has_function_privilege('public', p.oid, 'EXECUTE') "
                        "AS public_execute "
                        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'public' AND p.proname = ANY(:names)"
                    ),
                    {"names": list(LEDGER_DEFINER_FUNCTIONS)},
                )
            ).all()
            functions = {row[0]: row for row in function_rows}
            for name in LEDGER_DEFINER_FUNCTIONS:
                row = functions.get(name)
                if row is None:
                    failures.append(f"expected ledger function '{name}' is missing")
                    continue
                _, is_definer, owner, proconfig, public_execute = row
                if not is_definer:
                    failures.append(f"ledger function '{name}' is not SECURITY DEFINER")
                if owner != OWNER_ROLE:
                    failures.append(
                        f"ledger function '{name}' is owned by '{owner}', expected "
                        f"'{OWNER_ROLE}'"
                    )
                if not proconfig or LEDGER_FUNCTION_SEARCH_PATH not in proconfig:
                    failures.append(
                        f"ledger function '{name}' does not pin its search_path to "
                        "'pg_catalog, public'"
                    )
                if public_execute:
                    failures.append(
                        f"ledger function '{name}' is EXECUTE-able by PUBLIC; the "
                        "migration's REVOKE has been undone"
                    )

        if (
            await conn.execute(text("SELECT has_schema_privilege('public', 'CREATE')"))
        ).scalar_one():
            failures.append("runtime role holds CREATE on schema 'public'")

        if (
            await conn.execute(
                text("SELECT has_database_privilege(current_database(), 'CREATE')")
            )
        ).scalar_one():
            failures.append("runtime role holds CREATE on the database")

        return EnforcementReport(
            dialect=dialect,
            protected_ops_allowed=not failures,
            runtime_user=runtime_user,
            failures=failures,
        )


_report: EnforcementReport | None = None


async def initialize(engine: AsyncEngine) -> EnforcementReport:
    """Run the startup verification and arm (or refuse) the protected operations."""
    global _report
    _report = await verify_database_enforcement(engine)
    if _report.protected_ops_allowed:
        logger.info("database role enforcement: %s", _report.as_log_line())
    else:
        # Loud, not silent: a refused topology is a deployment error to fix, not an
        # expected state to swallow.
        logger.error("database role enforcement: %s", _report.as_log_line())
    return _report


def current_report() -> EnforcementReport | None:
    return _report


def require_protected_ops() -> None:
    """
    The gate for protected purchase and linkage operations. Raises 503 with the concrete
    verification failures when the runtime topology is not verified; returns silently
    when it is.
    """
    if _report is None or not _report.protected_ops_allowed:
        detail = (
            "Protected database operations are refused: database role enforcement is "
            "not verified for this deployment."
        )
        if _report is not None and _report.failures:
            detail += " Verification failures: " + "; ".join(_report.failures)
        raise HTTPException(status_code=503, detail=detail)
