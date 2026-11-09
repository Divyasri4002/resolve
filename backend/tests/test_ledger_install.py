"""
Phase 4.6.2 ledger battery — suites A (schema/ownership), B (transaction integrity),
C (backfill) — proven against ISOLATED PostgreSQL 16.

ISOLATION: RESOLVE_PG_ADMIN_URL points at a throwaway PG16 cluster (see
tests/ledger_sandbox.py). Every sandbox runs the real production sequence: bootstrap ->
role_separation.sql VERBATIM -> ledger_install.sql VERBATIM. Tests skip when the URL is
unset. The dev volume is never touched.
"""
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db_enforcement import verify_database_enforcement
from app.ledger.verify import (
    VERDICT_HEALTHY,
    VERDICT_UNHEALTHY,
    collect_db_facts,
    recompute_chain,
    submission_currency,
    verify,
)
from app.models.db_models import (
    IntegrationEventRecord,
    PurchaseAuthorizationRecord,
    ResolveOrderRecord,
)
from tests.ledger_sandbox import ADMIN_URL, LedgerSandbox

pytestmark = pytest.mark.skipif(
    not ADMIN_URL,
    reason="RESOLVE_PG_ADMIN_URL is not set; the ledger battery needs isolated PostgreSQL 16",
)


# --------------------------------------------------------------------------- #
# Fixtures and seeds                                                           #
# --------------------------------------------------------------------------- #

@pytest.fixture
async def ledger_pg():
    """Full production sequence: bootstrap -> role_separation -> ledger_install."""
    sb = LedgerSandbox()
    await sb.prepare_roles()
    await sb.create()
    try:
        await sb.bootstrap()
        await sb.run_role_separation()
        await sb.run_ledger_install()
        sb.sessions = sb.app_engine()
        yield sb
    finally:
        if hasattr(sb, "sessions"):
            await sb.dispose_engines()
        await sb.drop()


@pytest.fixture
async def bare_pg():
    """Business schema only — no migrations. Seeding runs as the ADMIN identity,
    because before role_separation.sql the runtime role holds no grants at all."""
    sb = LedgerSandbox()
    await sb.prepare_roles()
    await sb.create()
    try:
        await sb.bootstrap()
        sb.sessions = sb.admin_engine()
        yield sb
    finally:
        if hasattr(sb, "sessions"):
            await sb.dispose_engines()
        await sb.drop()


def _mk_pa(order_id: str, mission_id: str) -> PurchaseAuthorizationRecord:
    return PurchaseAuthorizationRecord(
        order_id=order_id,
        mission_id=mission_id,
        user_id="user_seed",
        authority_decision="approved",
        decision_reason="seed row",
        status="authorized",
    )


async def seed_pa(sb, *, order_id: str, mission_id: str) -> str:
    async with sb.sessions() as session:
        pa = _mk_pa(order_id, mission_id)
        session.add(pa)
        await session.commit()
        return pa.authorization_id


async def seed_ie(sb, *, event_id: str, outcome: str = "received") -> None:
    async with sb.sessions() as session:
        session.add(
            IntegrationEventRecord(
                event_id=event_id,
                event_type="order.linked",
                body_sha256="a" * 64,
                outcome=outcome,
            )
        )
        await session.commit()


async def seed_order(sb, *, order_id: str, mission_id: str, linked: bool) -> None:
    async with sb.sessions() as session:
        session.add(
            ResolveOrderRecord(
                order_id=order_id,
                mission_id=mission_id,
                user_id="user_seed",
                title="RESOLVE Test Headphones",
                amount=Decimal("799.00"),
                razorpay_order_id=f"rzp_order_{order_id}",
                razorpay_payment_id=f"rzp_pay_{order_id}",
                product={"id": "prod_1", "title": "RESOLVE Test Headphones"},
                provider_order_reference=f"gid://shopify/Order/{order_id}" if linked else None,
            )
        )
        await session.commit()


async def events(sb) -> list[dict]:
    async with sb.sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_seq, source, source_pk, source_revision, payload::text, "
                    "payload_sha256 FROM purchase_events ORDER BY event_seq"
                )
            )
        ).all()
    return [
        {
            "seq": r[0], "source": r[1], "pk": r[2], "revision": r[3],
            "payload_text": r[4], "digest": r[5],
        }
        for r in rows
    ]


async def next_seq(sb) -> int:
    async with sb.sessions() as session:
        return (
            await session.execute(text("SELECT next_seq FROM ledger_state WHERE id = 1"))
        ).scalar_one()


async def pin_row(sb) -> dict:
    async with sb.sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT last_published_seq, publish_status, attempts, lease_owner "
                    "FROM ledger_publish_state WHERE id = 1"
                )
            )
        ).one()
    return {
        "published": row[0], "status": row[1], "attempts": row[2], "lease_owner": row[3],
    }


# --------------------------------------------------------------------------- #
# Suite A — schema, ownership, privileges, enforcement                         #
# --------------------------------------------------------------------------- #

async def test_a1_ledger_schema_ownership_and_privileges(ledger_pg):
    sb = ledger_pg
    # Ownership and C6 privileges, read as the ADMIN identity.
    async with sb.sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.relname, c.relowner::regrole::text, "
                    "has_table_privilege('resolve_app', c.oid, 'SELECT'), "
                    "has_table_privilege('resolve_app', c.oid, 'INSERT'), "
                    "has_table_privilege('resolve_app', c.oid, 'UPDATE'), "
                    "has_table_privilege('resolve_app', c.oid, 'DELETE'), "
                    "has_table_privilege('resolve_app', c.oid, 'TRUNCATE') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname='public' AND c.relname IN "
                    "('purchase_events','ledger_state','ledger_publish_state')"
                )
            )
        ).all()
    table = {r[0]: r for r in rows}
    assert set(table) == {"purchase_events", "ledger_state", "ledger_publish_state"}
    for name, row in table.items():
        assert row[1] == "resolve_owner", name
        assert row[2] is True, f"{name}: app SELECT required"
        assert row[3] is False, f"{name}: app INSERT forbidden"
        if name == "ledger_publish_state":
            assert row[4] is True, "pin: app UPDATE required"
        else:
            assert row[4] is False, f"{name}: app UPDATE forbidden"
        assert row[5] is False and row[6] is False, name

    # Row-revision columns on the three source tables, with triggers in place.
    async with sb.sessions() as session:
        revs = (
            await session.execute(
                text(
                    "SELECT c.relname, count(*) FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname='public' AND a.attname='row_revision' AND NOT a.attisdropped "
                    "AND c.relname IN ('resolve_orders','purchase_authorizations','integration_events') "
                    "GROUP BY c.relname"
                )
            )
        ).all()
    assert {r[0] for r in revs} == {
        "resolve_orders", "purchase_authorizations", "integration_events",
    }


async def test_a2_runtime_enforcement_passes_on_full_topology(ledger_pg):
    report = await verify_database_enforcement(ledger_pg.sessions_engine)
    assert report.protected_ops_allowed, report.failures
    assert report.failures == []


async def test_a3_enforcement_fails_when_expected_trigger_set_is_incomplete(ledger_pg):
    sb = ledger_pg
    # Drop ONE of the three expected emit triggers: the expected set is no longer
    # complete, and the runtime verifier must refuse — not silently pass.
    await sb.admin_scalar(
        "DROP TRIGGER resolve_emit_ie_events ON public.integration_events"
    )
    report = await verify_database_enforcement(sb.sessions_engine)
    assert not report.protected_ops_allowed
    assert any("resolve_emit_ie_events" in f for f in report.failures), report.failures


async def test_a4_role_separation_rerun_strips_pin_grants_and_enforcement_catches_it(ledger_pg):
    sb = ledger_pg
    # The binding run order: re-running role_separation.sql AFTER ledger_install revokes
    # everything on the ledger tables. The runtime must fail closed and name the cause.
    await sb.run_role_separation()
    report = await verify_database_enforcement(sb.sessions_engine)
    assert not report.protected_ops_allowed
    assert any("ledger_publish_state" in f for f in report.failures), report.failures
    # The documented repair: re-run ledger_install.sql, which restores the C6 posture.
    await sb.run_ledger_install()
    report = await verify_database_enforcement(sb.sessions_engine)
    assert report.protected_ops_allowed, report.failures


async def test_a5_deletion_guards_fire_even_for_the_superuser(ledger_pg):
    sb = ledger_pg
    # Row-level guards fire per deleted row: seed one event so every DELETE below
    # actually attempts rows. The admin tier may INSERT (only DELETE/TRUNCATE are
    # refused) — exactly the bypass path this test pins.
    await sb.admin_scalar(
        "INSERT INTO public.purchase_events "
        "(event_seq, source, source_pk, source_revision, payload, payload_sha256) "
        "VALUES (1, 'purchase_authorizations', 'a5_guard_probe', 0, '{}', "
        "'" + "0" * 64 + "')"
    )
    statements = [
        "DELETE FROM public.purchase_events",
        "DELETE FROM public.ledger_state",
        "DELETE FROM public.ledger_publish_state",
        "TRUNCATE public.resolve_orders",
        "TRUNCATE public.purchase_events",
        "TRUNCATE public.ledger_publish_state",
    ]
    for sql in statements:
        with pytest.raises(Exception) as excinfo:
            await sb.admin_scalar(sql)
        assert "refuse" in str(excinfo.value).lower(), sql


async def test_a6_app_cannot_write_event_or_counter_tables(ledger_pg):
    sb = ledger_pg
    forbidden = [
        "INSERT INTO purchase_events (event_seq, source, source_pk, source_revision, payload, payload_sha256) "
        "VALUES (999, 'resolve_orders', 'x', 0, '{}', '" + "0" * 64 + "')",
        "UPDATE purchase_events SET payload = '{}'",
        "INSERT INTO ledger_state (id, next_seq) VALUES (2, 5)",
        "UPDATE ledger_state SET next_seq = 99",
        "DELETE FROM ledger_publish_state",
    ]
    for sql in forbidden:
        with pytest.raises(Exception) as excinfo:
            async with sb.sessions() as session:
                await session.execute(text(sql))
                await session.commit()
        assert "permission denied" in str(excinfo.value), sql

    # The pin's C6 contract: SELECT + INSERT + UPDATE are exactly what the app holds.
    async with sb.sessions() as session:
        await session.execute(
            text(
                "UPDATE ledger_publish_state SET last_error = 'probe' WHERE id = 1"
            )
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# Suite B — transaction integrity                                              #
# --------------------------------------------------------------------------- #

async def test_b1_single_transition_emits_one_verified_event(ledger_pg):
    sb = ledger_pg
    auth_id = await seed_pa(sb, order_id="ord_b1", mission_id="m_b1")
    async with sb.sessions() as session:
        await session.execute(
            text(
                "UPDATE purchase_authorizations SET status = 'purchase_failed' "
                "WHERE authorization_id = :pk"
            ),
            {"pk": auth_id},
        )
        await session.commit()

    rows = await events(sb)
    assert len(rows) == 1
    row = rows[0]
    assert row["seq"] == 1
    assert row["source"] == "purchase_authorizations"
    assert row["pk"] == auth_id
    assert row["revision"] == 1
    import hashlib

    assert (
        hashlib.sha256(row["payload_text"].encode("utf-8")).hexdigest() == row["digest"]
    )
    assert await next_seq(sb) == 2

    facts = await collect_db_facts(sb.sessions)
    chain, problems = recompute_chain(facts.events)
    assert problems == []
    assert chain[-1] != "0" * 64


async def test_b2_rollback_does_not_consume_a_sequence(ledger_pg):
    sb = ledger_pg
    auth_id = await seed_pa(sb, order_id="ord_b2", mission_id="m_b2")
    engine = sb.sessions_engine
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "UPDATE purchase_authorizations SET status = 'purchase_failed' "
                "WHERE authorization_id = :pk"
            ),
            {"pk": auth_id},
        )
        # Event 1 is allocated inside this open transaction...
        assert len(await events(sb)) == 0  # not visible to the other snapshot? (read-committed sees committed only)
        await conn.rollback()
    # ...and the abort gives the number back: the next transition reuses seq 1.
    async with sb.sessions() as session:
        await session.execute(
            text(
                "UPDATE purchase_authorizations SET status = 'purchase_failed' "
                "WHERE authorization_id = :pk"
            ),
            {"pk": auth_id},
        )
        await session.commit()
    rows = await events(sb)
    assert [r["seq"] for r in rows] == [1]
    assert await next_seq(sb) == 2


async def test_b3_two_transitions_in_one_transaction_take_consecutive_revisions(ledger_pg):
    sb = ledger_pg
    auth_id = await seed_pa(sb, order_id="ord_b3", mission_id="m_b3")
    engine = sb.sessions_engine
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE purchase_authorizations SET status = 'purchase_unknown' "
                "WHERE authorization_id = :pk"
            ),
            {"pk": auth_id},
        )
        await conn.execute(
            text(
                "UPDATE purchase_authorizations SET submission_state = 'settled' "
                "WHERE authorization_id = :pk"
            ),
            {"pk": auth_id},
        )
    rows = await events(sb)
    assert [(r["seq"], r["revision"]) for r in rows] == [(1, 1), (2, 2)]
    assert await next_seq(sb) == 3


async def test_b4_non_qualifying_update_emits_nothing(ledger_pg):
    sb = ledger_pg
    auth_id = await seed_pa(sb, order_id="ord_b4", mission_id="m_b4")
    async with sb.sessions() as session:
        await session.execute(
            text(
                "UPDATE purchase_authorizations SET error_note = 'probe' "
                "WHERE authorization_id = :pk"
            ),
            {"pk": auth_id},
        )
        await session.commit()
    assert await events(sb) == []
    assert await next_seq(sb) == 1


async def test_b5_concurrent_allocations_are_gapless_and_unique(ledger_pg):
    sb = ledger_pg
    pks = []
    for i in range(6):
        pks.append(await seed_pa(sb, order_id=f"ord_b5_{i}", mission_id=f"m_b5_{i}"))
    import asyncio

    async def update_one(pk: str, status: str) -> None:
        async with sb.sessions() as session:
            await session.execute(
                text(
                    "UPDATE purchase_authorizations SET status = :status "
                    "WHERE authorization_id = :pk"
                ),
                {"status": status, "pk": pk},
            )
            await session.commit()

    statuses = ["purchase_failed", "purchase_unknown", "purchase_reported_linked"] * 2
    await asyncio.gather(*(update_one(pk, st) for pk, st in zip(pks, statuses)))

    rows = await events(sb)
    assert sorted(r["seq"] for r in rows) == [1, 2, 3, 4, 5, 6]
    assert await next_seq(sb) == 7


async def test_b6_advisory_lock_serializes_allocation(ledger_pg):
    import asyncio

    sb = ledger_pg
    auth_id = await seed_pa(sb, order_id="ord_b6", mission_id="m_b6")
    admin_conn = await sb._admin_conn()
    app_task = None
    try:
        await admin_conn.execute("BEGIN")
        await admin_conn.fetchval("SELECT pg_advisory_xact_lock(7, 2)")
        # While the allocation key is held by another transaction, an emitter blocks.
        async def app_update():
            async with sb.sessions() as session:
                await session.execute(
                    text(
                        "UPDATE purchase_authorizations SET status = 'purchase_failed' "
                        "WHERE authorization_id = :pk"
                    ),
                    {"pk": auth_id},
                )
                await session.commit()

        app_task = asyncio.create_task(app_update())
        await asyncio.sleep(0.6)
        # Not committed, no event row yet: the emitter is waiting on the key.
        assert await sb.admin_scalar("SELECT count(*) FROM purchase_events") == 0
    finally:
        await admin_conn.execute("COMMIT")
        await admin_conn.close()
    assert app_task is not None
    await asyncio.wait_for(app_task, timeout=10)
    rows = await events(sb)
    assert [r["seq"] for r in rows] == [1]


async def test_b7_resolve_orders_payload_redacts_product(ledger_pg):
    sb = ledger_pg
    await seed_order(sb, order_id="ord_b7", mission_id="m_b7", linked=False)
    async with sb.sessions() as session:
        await session.execute(
            text(
                "UPDATE resolve_orders SET provider_order_reference = "
                "'gid://shopify/Order/b7' WHERE order_id = 'ord_b7'"
            )
        )
        await session.commit()
    rows = await events(sb)
    assert len(rows) == 1
    import json

    payload = json.loads(rows[0]["payload_text"])
    assert "product" not in payload
    assert payload["provider_order_reference"] == "gid://shopify/Order/b7"


async def test_b8_integration_event_update_emits_on_qualifying_columns_only(ledger_pg):
    sb = ledger_pg
    await seed_ie(sb, event_id="ie_b8")
    # The INSERT itself is in backfill scope, never a trigger event.
    assert await events(sb) == []
    async with sb.sessions() as session:
        await session.execute(
            text(
                "UPDATE integration_events SET outcome = 'linked', processed_at = now() "
                "WHERE event_id = 'ie_b8'"
            )
        )
        await session.commit()
    rows = await events(sb)
    assert len(rows) == 1 and rows[0]["pk"] == "ie_b8" and rows[0]["revision"] == 1


async def test_b9_revisions_track_each_qualifying_transition(ledger_pg):
    sb = ledger_pg
    auth_id = await seed_pa(sb, order_id="ord_b9", mission_id="m_b9")
    for status in ("purchase_failed", "purchase_unknown"):
        async with sb.sessions() as session:
            await session.execute(
                text(
                    "UPDATE purchase_authorizations SET status = :s "
                    "WHERE authorization_id = :pk"
                ),
                {"s": status, "pk": auth_id},
            )
            await session.commit()
    rows = await events(sb)
    assert [(r["revision"], r["seq"]) for r in rows] == [(1, 1), (2, 2)]
    # The latest event's payload is the row's current state.
    import json

    payload = json.loads(rows[-1]["payload_text"])
    assert payload["status"] == "purchase_unknown"


# --------------------------------------------------------------------------- #
# Suite C — backfill                                                           #
# --------------------------------------------------------------------------- #

async def test_c1_backfill_covers_exactly_the_approved_scope(bare_pg):
    sb = bare_pg
    # Seed BEFORE any migration: the backfill must pick up every committed row.
    await seed_pa(sb, order_id="ord_c1_a", mission_id="m_c1_a")
    await seed_pa(sb, order_id="ord_c1_b", mission_id="m_c1_b")
    await seed_ie(sb, event_id="ie_c1_a")
    await seed_ie(sb, event_id="ie_c1_b", outcome="rejected")
    await seed_order(sb, order_id="ord_c1_x", mission_id="m_c1_x", linked=True)
    await seed_order(sb, order_id="ord_c1_y", mission_id="m_c1_y", linked=True)
    await seed_order(sb, order_id="ord_c1_z", mission_id="m_c1_z", linked=False)

    await sb.run_role_separation()
    await sb.run_ledger_install()

    sb.sessions = sb.app_engine()
    rows = await events(sb)
    assert len(rows) == 6, f"2 PA + 2 IE + 2 linked RO = 6, got {len(rows)}"
    assert all(r["revision"] == 0 for r in rows)
    by_source = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)
    assert set(by_source) == {
        "purchase_authorizations", "integration_events", "resolve_orders",
    }
    # Declared ordering: PA by authorization_id, then IE by event_id, then RO by order_id.
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs)
    pa_pks = [r["pk"] for r in by_source["purchase_authorizations"]]
    assert pa_pks == sorted(pa_pks)
    ro_pks = [r["pk"] for r in by_source["resolve_orders"]]
    assert ro_pks == sorted(ro_pks)
    assert "ord_c1_z" not in ro_pks  # unlinked resolve_orders row is out of scope
    assert await next_seq(sb) == 7

    facts = await collect_db_facts(sb.sessions)
    _, problems = recompute_chain(facts.events)
    assert problems == []


async def test_c2_ledger_install_is_idempotent(ledger_pg):
    sb = ledger_pg
    auth_id = await seed_pa(sb, order_id="ord_c2", mission_id="m_c2")
    async with sb.sessions() as session:
        await session.execute(
            text(
                "UPDATE purchase_authorizations SET status = 'purchase_failed' "
                "WHERE authorization_id = :pk"
            ),
            {"pk": auth_id},
        )
        await session.commit()
    before = await events(sb)
    before_seq = await next_seq(sb)

    await sb.run_ledger_install()  # rerun: guarded, no new events, no counter change

    assert await events(sb) == before
    assert await next_seq(sb) == before_seq


async def test_c3_verification_failure_rolls_back_the_whole_migration(bare_pg):
    sb = bare_pg
    auth_id_holder = {}

    # The ledger migration assumes the 4.6.1 owner topology (its SET ROLE section
    # creates functions as resolve_owner), so the production sequence applies here too.
    await sb.run_role_separation()

    # Pre-install the ledger surface by hand, with a sabotaged event digest for a real
    # PA row. The backfill skips that row (it already "has" an event) and the internal
    # digest verification must catch the corruption, raise, and roll EVERYTHING back.
    async with sb.sessions() as session:
        pa = _mk_pa("ord_c3", "m_c3")
        session.add(pa)
        await session.commit()
        auth_id_holder["pk"] = pa.authorization_id

    conn = await sb._admin_conn()
    try:
        await conn.execute(
            """
            CREATE TABLE public.purchase_events (
                event_seq BIGINT PRIMARY KEY,
                source TEXT NOT NULL,
                source_pk TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                payload JSONB NOT NULL,
                payload_sha256 TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """
        )
        # The two state tables are also pre-created by hand (the migration's
        # IF NOT EXISTS leaves them untouched); the backfill UPDATE needs
        # next_seq + updated_at on ledger_state.
        await conn.execute(
            """
            CREATE TABLE public.ledger_state (
                id         INTEGER   PRIMARY KEY CHECK (id = 1),
                next_seq   BIGINT    NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
                updated_at TIMESTAMP NOT NULL DEFAULT (transaction_timestamp() AT TIME ZONE 'UTC')
            )
            """
        )
        await conn.execute(
            "CREATE TABLE public.ledger_publish_state (id INTEGER PRIMARY KEY CHECK (id = 1))"
        )
        await conn.execute(
            "INSERT INTO public.ledger_state (id, next_seq) VALUES (1, 2)"
        )
        await conn.execute("INSERT INTO public.ledger_publish_state (id) VALUES (1)")
        await conn.execute(
            """
            INSERT INTO public.purchase_events
                (event_seq, source, source_pk, source_revision, payload, payload_sha256)
            VALUES
                (1, 'purchase_authorizations', $1, 0,
                 '{"sabotaged": true}',
                 '0000000000000000000000000000000000000000000000000000000000000000')
            """,
            auth_id_holder["pk"],
        )
    finally:
        await conn.close()

    with pytest.raises(Exception) as excinfo:
        await sb.run_ledger_install()
    assert "ledger verification failed" in str(excinfo.value)

    # Full rollback: the triggers, the grants, and every counter move are gone; the
    # hand-made rows remain exactly as they were.
    assert (
        await sb.admin_scalar(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'resolve_emit_pa_events' "
            "AND NOT tgisinternal"
        )
        == 0
    )
    assert await sb.admin_scalar("SELECT next_seq FROM public.ledger_state WHERE id = 1") == 2
    assert await sb.admin_scalar("SELECT count(*) FROM public.purchase_events") == 1


async def test_c4_fresh_empty_install_is_healthy(ledger_pg):
    sb = ledger_pg
    assert await events(sb) == []
    assert await next_seq(sb) == 1
    facts = await collect_db_facts(sb.sessions)
    assert facts.events == []
    report = await verify(sb.sessions, None)
    assert report.verdict == VERDICT_HEALTHY, report.problems
    report = await verify_database_enforcement(sb.sessions_engine)
    assert report.protected_ops_allowed, report.failures


async def test_c5_rerun_repairs_a_lost_trigger(ledger_pg):
    sb = ledger_pg
    await sb.admin_scalar("DROP TRIGGER resolve_emit_ro_events ON public.resolve_orders")
    await sb.run_ledger_install()  # guarded recreation + verification
    report = await verify_database_enforcement(sb.sessions_engine)
    assert report.protected_ops_allowed, report.failures


# --------------------------------------------------------------------------- #
# Verifier and currency gate (database side)                                   #
# --------------------------------------------------------------------------- #

async def test_c6_verifier_and_gate_refuse_a_tampered_ledger(ledger_pg):
    sb = ledger_pg
    auth_id = await seed_pa(sb, order_id="ord_c6", mission_id="m_c6")
    async with sb.sessions() as session:
        await session.execute(
            text(
                "UPDATE purchase_authorizations SET status = 'purchase_failed' "
                "WHERE authorization_id = :pk"
            ),
            {"pk": auth_id},
        )
        await session.commit()
    # Tamper with the committed event payload (admin bypass of the C6 contract —
    # exactly the residual the design documents; detection is the control).
    await sb.admin_scalar(
        "UPDATE purchase_events SET payload = payload || '{\"tampered\": true}' "
        "WHERE event_seq = 1"
    )
    report = await verify(sb.sessions, None)
    assert report.verdict == VERDICT_UNHEALTHY
    assert any("digest" in p for p in report.problems), report.problems

    # The gate with a configured store: the integrity failure short-circuits BEFORE any
    # store contact, so a store that refuses to be contacted proves the ordering.
    class NeverContacted:
        async def get_head(self):
            raise AssertionError("the gate must refuse before contacting the store")

    decision = await submission_currency(sb.sessions, NeverContacted())
    assert decision.state == "not-current"
    assert "integrity" in decision.reason


async def test_c7_verifier_is_unverifiable_when_ledger_absent(bare_pg):
    sb = bare_pg
    await sb.run_role_separation()  # runtime grants exist; the ledger does not
    sb.sessions = sb.app_engine()
    report = await verify(sb.sessions, None)
    assert report.verdict == "unverifiable"
    assert "not installed" in report.problems[0]

    # The gate with a configured store: the ledger-absent error short-circuits BEFORE
    # any store contact, so a store that refuses to be contacted proves the ordering.
    class NeverContacted:
        async def get_head(self):
            raise AssertionError("the gate must refuse before contacting the store")

    decision = await submission_currency(sb.sessions, NeverContacted())
    # A configured store makes the check applicable: ledger-absent is not-current
    # (fail closed), never skipped — only an unconfigured store is skipped.
    assert decision.state == "not-current"
    assert "not installed" in decision.reason
