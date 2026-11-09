"""
M21 Phase 2A, Checkpoint 1 — safe per-mission sequence allocation.

The defect this locks down: ``_next_sequence`` allocated ``max(sequence) + 1`` from committed
data with no serialization. Two concurrent writers to the SAME mission both read the same
committed maximum, both compute the same next sequence, and the unique index
``uq_agent_conversation_mission_sequence`` then destroys the LOSER's entire transaction —
including whatever state change that transaction carried. The fix is a PostgreSQL
transaction-scoped advisory lock keyed per mission, taken inside the caller's own transaction
before the max read; the unique constraint stays as the storage-level backstop.

What is proven here, and how:

* SQLite proves the behavior contract (append-only, flush-not-commit, atomic lifecycle rows,
  contiguous sequences, deterministic lock-key derivation) — NOT the Postgres locking, which
  SQLite cannot even express. The lock is a no-op on SQLite by design.
* The Postgres tests run against a THROWAWAY database named by ``RESOLVE_PG_TEST_URL``
  (never the development ``resolve`` database) and prove the concurrency claims under real
  PostgreSQL: same-mission writers serialize, different missions do not block, a rollback
  releases the lock, and the unlocked race really does destroy a transaction — which is why
  the lock exists. Barriers are events and PostgreSQL's own blocking semantics; no timing
  sleeps stand in for a race.
"""
from __future__ import annotations

import asyncio
import ast
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents import conversation as conversation_module
from app.agents.conversation import (
    _mission_lock_key,
    _next_sequence,
    append_event,
    append_lifecycle_event,
)
from app.database import Base
from app.models.db_models import AgentConversationRecord, MissionRecord

# Thrown-away PostgreSQL database for the concurrency tests only. Empty by default: those
# tests skip unless the runner explicitly provides an isolated database URL.
PG_TEST_URL = os.environ.get("RESOLVE_PG_TEST_URL", "")

MISSION = "mis_seq_m21"
OTHER_MISSION = "mis_seq_m21_other"
USER = "user_seq_m21"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
async def sqlite_env():
    tmpdir = tempfile.mkdtemp(prefix="m21_seq_")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmpdir}/seq.db", echo=False, poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
async def pg_env():
    if not PG_TEST_URL:
        pytest.skip("RESOLVE_PG_TEST_URL not set — PostgreSQL concurrency not exercised here")
    engine = create_async_engine(PG_TEST_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


# --------------------------------------------------------------------------- #
# A. SQLite: the behavior contract is unchanged.
# --------------------------------------------------------------------------- #
async def test_sequences_stay_contiguous_per_mission_on_sqlite(sqlite_env):
    factory = sqlite_env
    async with factory() as db:
        for i in range(3):
            row = await append_event(
                db,
                mission_id=MISSION,
                user_id=USER,
                role="user",
                event_type="user_message",
                content=f"turn {i}",
            )
            assert row.sequence == i
        # A second mission allocates from its own counter, not the table's.
        other = await append_event(
            db,
            mission_id=OTHER_MISSION,
            user_id=USER,
            role="user",
            event_type="user_message",
            content="other",
        )
        assert other.sequence == 0
        await db.commit()

    async with factory() as reader:
        rows = (
            (await reader.execute(
                AgentConversationRecord.__table__.select().order_by(
                    AgentConversationRecord.sequence
                )
            ))
            .mappings()
            .all()
        )
        assert len(rows) == 4


async def test_append_event_flushes_but_never_commits(sqlite_env):
    """A second session must not see the appended row until the writer commits — the event
    belongs to the caller's transaction, and the advisory-lock change did not move that."""
    factory = sqlite_env
    async with factory() as writer:
        await append_event(
            writer,
            mission_id=MISSION,
            user_id=USER,
            role="user",
            event_type="user_message",
            content="not yet visible",
        )
        async with factory() as outsider:
            seen = await outsider.execute(
                AgentConversationRecord.__table__.select()
            )
            assert seen.all() == []  # flush is not a commit
        await writer.commit()

    async with factory() as outsider:
        seen = await outsider.execute(AgentConversationRecord.__table__.select())
        assert len(seen.all()) == 1


async def test_lifecycle_row_and_state_change_are_one_atomic_fact(sqlite_env):
    factory = sqlite_env
    async with factory() as db:
        record = MissionRecord(
            mission_id=MISSION,
            user_id=USER,
            mission_type="shopping",
            status="product_selected",
            mandate={},
            transaction={},
        )
        db.add(record)
        await db.flush()
        record.status = "checkout_confirmed"
        await append_lifecycle_event(
            db,
            mission_id=MISSION,
            user_id=USER,
            state="checkout_confirmed",
            detail="Checkout confirmed.",
        )
        await db.rollback()  # the caller changed its mind: NOTHING may survive

    async with factory() as reader:
        missions = (await reader.execute(MissionRecord.__table__.select())).all()
        events = (await reader.execute(AgentConversationRecord.__table__.select())).all()
        assert missions == []
        assert events == []  # no state change, and no partially persisted event beside it

    async with factory() as db:
        record = MissionRecord(
            mission_id=MISSION,
            user_id=USER,
            mission_type="shopping",
            status="product_selected",
            mandate={},
            transaction={},
        )
        db.add(record)
        await db.flush()
        record.status = "checkout_confirmed"
        await append_lifecycle_event(
            db,
            mission_id=MISSION,
            user_id=USER,
            state="checkout_confirmed",
            detail="Checkout confirmed.",
        )
        await db.commit()

    async with factory() as reader:
        events = (await reader.execute(AgentConversationRecord.__table__.select())).all()
        assert len(events) == 1
        assert events[0].event_type == "system_state_change"


async def test_multiple_appends_in_one_transaction_reenter_the_lock(sqlite_env):
    """A turn appends several rows on one transaction; the lock path must never deadlock or
    double-allocate — on SQLite it is a no-op, on PostgreSQL it is re-entrant."""
    factory = sqlite_env
    async with factory() as db:
        first = await append_event(
            db, mission_id=MISSION, user_id=USER, role="user",
            event_type="user_message", content="one",
        )
        second = await append_event(
            db, mission_id=MISSION, user_id=USER, role="agent",
            event_type="agent_message", content="two",
        )
        await db.commit()
    assert (first.sequence, second.sequence) == (0, 1)


# --------------------------------------------------------------------------- #
# B. Structural guards: what the implementation may and may not do.
# --------------------------------------------------------------------------- #
def test_lock_key_is_deterministic_and_stable_across_processes():
    assert _mission_lock_key(MISSION) == _mission_lock_key(MISSION)
    assert _mission_lock_key(MISSION) != _mission_lock_key(OTHER_MISSION)
    assert 0 <= _mission_lock_key(MISSION) < 2**31


def test_conversation_module_never_commits_and_never_uses_python_hash():
    """AST guard on the whole module: appending must not commit (the caller owns the
    transaction), and the lock key must never come from Python's randomized hash()."""
    source = Path(conversation_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            assert name != "hash", "Python hash() must never derive a database lock key"
            assert name != "commit", (
                "app.agents.conversation must never commit — transaction ownership stays "
                "with the caller"
            )


async def test_next_sequence_helper_still_derives_from_committed_rows(sqlite_env):
    factory = sqlite_env
    async with factory() as db:
        assert await _next_sequence(db, MISSION) == 0
        db.add(AgentConversationRecord(
            event_id="evt_seed_1", mission_id=MISSION, user_id=USER,
            sequence=7, role="system", event_type="system_state_change", content="seed",
        ))
        await db.flush()
        assert await _next_sequence(db, MISSION) == 8
        await db.rollback()


# --------------------------------------------------------------------------- #
# C. PostgreSQL: the concurrency claims, against an isolated database.
# --------------------------------------------------------------------------- #
async def test_postgres_concurrent_same_mission_writers_allocate_distinct_sequences(pg_env):
    """Writer 1 takes the lock, flushes, and HOLDS its open transaction. Writer 2 then starts
    on the same mission: under the advisory lock it waits BEFORE reading max(sequence). Once
    writer 1 commits, writer 2 reads the committed maximum and allocates the next sequence —
    both transactions survive. Without the lock, writer 2 would have read the same committed
    maximum and been destroyed at the unique index (proven separately below)."""
    factory = pg_env

    async def writer(db: AsyncSession, mission: str, text: str):
        row = await append_event(
            db, mission_id=mission, user_id=USER,
            role="user", event_type="user_message", content=text,
        )
        await db.flush()
        return row

    async with factory() as s1, factory() as s2:
        t1 = asyncio.create_task(writer(s1, MISSION, "first"))
        row1 = await t1  # lock held, row flushed, transaction OPEN
        t2 = asyncio.create_task(writer(s2, MISSION, "second"))
        await asyncio.sleep(0)  # one scheduling yield: writer2 now sits on the advisory lock
        assert not t2.done(), (
            "writer2 finished while writer1's transaction was open — the per-mission "
            "advisory lock is not serializing same-mission writers"
        )
        await s1.commit()  # releases the transaction-scoped lock
        row2 = await asyncio.wait_for(t2, timeout=10)
        await s2.commit()

        assert {row1.sequence, row2.sequence} == {0, 1}

    async with factory() as reader:
        rows = (
            (await reader.execute(
                AgentConversationRecord.__table__.select().order_by(
                    AgentConversationRecord.sequence
                )
            ))
            .mappings()
            .all()
        )
        assert [r["sequence"] for r in rows] == [0, 1]


async def test_postgres_different_missions_do_not_block_each_other(pg_env):
    """Writer 1 holds mission A's lock with an open transaction; writer 2 on mission B must
    append AND commit while writer 1 is still uncommitted — no cross-mission serialization."""
    factory = pg_env
    lock_held = asyncio.Event()
    release_writer_1 = asyncio.Event()

    async def hold_mission_a():
        async with factory() as s1:
            await append_event(
                s1, mission_id=MISSION, user_id=USER,
                role="user", event_type="user_message", content="A holds",
            )
            await s1.flush()
            lock_held.set()
            await release_writer_1.wait()
            await s1.commit()

    holder = asyncio.create_task(hold_mission_a())
    await lock_held.wait()

    async with factory() as s2:
        row_b = await asyncio.wait_for(
            append_event(
                s2, mission_id=OTHER_MISSION, user_id=USER,
                role="user", event_type="user_message", content="B proceeds",
            ),
            timeout=10,
        )
        await s2.commit()
    assert row_b.sequence == 0

    release_writer_1.set()
    await asyncio.wait_for(holder, timeout=10)


async def test_postgres_rollback_releases_the_transaction_scoped_lock(pg_env):
    """Writer 1 takes the lock and rolls back. Writer 2 must proceed immediately (the lock was
    transaction-scoped, so the rollback released it) and must allocate sequence 0 — writer 1's
    rolled-back allocation never existed."""
    factory = pg_env
    async with factory() as s1:
        await append_event(
            s1, mission_id=MISSION, user_id=USER,
            role="user", event_type="user_message", content="doomed",
        )
        await s1.flush()
        await s1.rollback()

    async with factory() as s2:
        row = await asyncio.wait_for(
            append_event(
                s2, mission_id=MISSION, user_id=USER,
                role="user", event_type="user_message", content="after rollback",
            ),
            timeout=10,
        )
        await s2.commit()
        assert row.sequence == 0


async def test_postgres_unserialized_max_plus_one_race_destroys_the_loser(pg_env):
    """The failure mode the lock exists to prevent, demonstrated: two sessions that read
    max(sequence) from COMMITTED data before either commits both allocate the same next
    sequence, and the unique index destroys the loser's whole transaction. This is why
    append_event takes the advisory lock before the max read. The race is constructed
    deterministically: both allocations complete while nothing is committed (awaited in
    order), then the loser's insert is what collides."""
    factory = pg_env

    async with factory() as s1, factory() as s2:
        seq1 = await _next_sequence(s1, MISSION)
        seq2 = await _next_sequence(s2, MISSION)
        assert seq1 == seq2 == 0  # both read the same committed maximum

        s1.add(AgentConversationRecord(
            event_id="evt_race_winner", mission_id=MISSION, user_id=USER,
            sequence=seq1, role="user", event_type="user_message", content="racing",
        ))
        await s1.flush()  # winner's index entry exists, transaction still uncommitted

        loser = asyncio.create_task(_flush_loser(s2, seq2))
        await asyncio.sleep(0)  # yield once so the loser reaches its blocking insert
        await s1.commit()  # winner commits; the loser's insert must now fail
        with pytest.raises(IntegrityError):
            await asyncio.wait_for(loser, timeout=10)
        await s2.rollback()  # the destroyed transaction left nothing behind

    async with factory() as reader:
        rows = (await reader.execute(AgentConversationRecord.__table__.select())).all()
        assert len(rows) == 1  # exactly one of the two racing writes survived


async def _flush_loser(db: AsyncSession, sequence: int) -> None:
    """The loser's half of the race: insert the pre-computed duplicate and flush."""
    db.add(AgentConversationRecord(
        event_id="evt_race_loser", mission_id=MISSION, user_id=USER,
        sequence=sequence, role="user", event_type="user_message", content="racing",
    ))
    await db.flush()
