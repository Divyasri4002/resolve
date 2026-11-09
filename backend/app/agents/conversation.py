"""
The agent's persistent conversation (Task CORE-2, Phase 6).

Append-only by construction: this module exposes exactly two operations — write a turn, read a
mission's own recent turns. There is no update and no delete, so "what did the agent already ask
me" survives a reload, a restart, and a second browser tab. That is the difference between a chat
that replays from a summary field and an agent that remembers the mission.

Three rules the callers of this module rely on:

* **Ownership is not this module's job.** Every read and write is scoped to a mission id the
  caller has already proven it may act on, using the same `_verify_mission_access` guard the rest
  of the API uses. A user reading another user's conversation would have to name a mission they
  cannot load.
* **Nothing secret is written.** Content and payload pass through `redact_secrets`, which removes
  any configured credential value from text — so a user pasting an API key into the chat does not
  put that key into the database, into the next model call, or into the transcript the browser
  reads back.
* **A turn is data, not an instruction.** The rows are handed to the reasoning model as a
  transcript with roles; they never carry an executable thing.
"""
from __future__ import annotations

import zlib
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.context import redact_secrets
from app.models.db_models import AgentConversationRecord

ROLES = frozenset({"user", "agent", "system"})

EVENT_TYPES = frozenset(
    {
        "user_message",
        "agent_message",
        "tool_call",
        "tool_result",
        "system_state_change",
        # CORE-5 Phase 5: the canonical product anchor — written when the server resolves a
        # product phrase ("the second one") and when the agent records a recommendation, so a
        # later "that one" resolves from structured identity rather than prose.
        "product_reference",
        # CORE-7 Phase 4: the canonical issue anchor — the same mechanism pointed at the
        # mission's own reported issues, written when the server resolves an issue phrase
        # ("the second issue", "the refund issue") to one persisted issue_id.
        "issue_reference",
    }
)

# A row of conversation is a bounded sentence, not a document. Long enough for an issue
# description, short enough that a transcript cannot become a way to bulk-store provider output.
MAX_CONTENT_CHARS = 2000

# CORE-7 Phase 2: how a server-driven lifecycle event reads to the reasoning model. The label
# travels only in the MODEL view (as_model_context); the stored content and the browser transcript
# stay the plain sentence. It marks the row as a statement of what the server did so a transcript
# line can never be mistaken for a user claim — and, being transcript, it can never override the
# canonical mission/order/issue context sections that precede it.
SERVER_LIFECYCLE_LABEL = "[SERVER LIFECYCLE STATE]"


class ConversationEventRejected(ValueError):
    """A role or event type outside the declared vocabulary. Refused before anything is written."""


async def append_event(
    db: AsyncSession,
    *,
    mission_id: str,
    user_id: str,
    role: str,
    event_type: str,
    content: str,
    tool_name: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentConversationRecord:
    """
    Write one turn. The sequence number is assigned here, from this mission's own rows, so the
    caller cannot reorder its own history.

    Flushes but does not commit: a turn belongs to whatever transaction the caller is finishing.
    """
    if role not in ROLES:
        raise ConversationEventRejected(f"Unknown conversation role: {role!r}")
    if event_type not in EVENT_TYPES:
        raise ConversationEventRejected(f"Unknown conversation event type: {event_type!r}")

    # Serialize same-mission sequence allocation before reading max(sequence). The lock is
    # transaction-scoped on the caller's own transaction — it lives until that transaction
    # commits or rolls back, exactly the window in which an unlocked max+1 read could collide
    # with a concurrent writer and destroy its state change at the unique index.
    await _acquire_mission_sequence_lock(db, mission_id)

    row = AgentConversationRecord(
        mission_id=mission_id,
        user_id=user_id,
        sequence=await _next_sequence(db, mission_id),
        role=role,
        event_type=event_type,
        content=redact_secrets(content, limit=MAX_CONTENT_CHARS),
        tool_name=tool_name,
        payload=redact_payload(payload),
    )
    db.add(row)
    await db.flush()
    return row


async def append_lifecycle_event(
    db: AsyncSession,
    *,
    mission_id: str,
    user_id: str,
    state: str,
    detail: str,
    extra_payload: dict[str, Any] | None = None,
) -> AgentConversationRecord:
    """
    Record one server-driven lifecycle transition in the conversation (CORE-7 Phase 2): a product
    was selected, checkout confirmed, a payment verified, an order created — by an endpoint, not
    by the agent loop, and exactly for that reason it belongs in the transcript the loop reads.

    The rules this helper exists to keep:

    * **Transaction-safe by construction.** Like every write here it FLUSHES and never commits —
      the caller appends it inside the same transaction as the state change it describes, so the
      event cannot survive a rolled-back transition and a committed transition cannot exist
      without its event.
    * **Identity is never accepted from a request.** mission_id and user_id arrive as
      server-derived arguments from a caller that has already enforced ownership.
    * **Data, not capability.** A lifecycle row describes what happened; it carries no provider
      references, no payment ids, no signatures, and no instruction — the same redaction and
      length bounds as every other turn apply on the way in.
    """
    payload: dict[str, Any] = {"state": state, "mission_id": mission_id}
    if extra_payload:
        payload.update(extra_payload)
    return await append_event(
        db,
        mission_id=mission_id,
        user_id=user_id,
        role="system",
        event_type="system_state_change",
        content=detail,
        payload=payload,
    )


def _mission_lock_key(mission_id: str) -> int:
    """
    The advisory-lock key for one mission. zlib.crc32 is deterministic and stable across
    processes and restarts — Python's built-in hash() is not (it is per-process randomized),
    so it must never choose a database lock key. A crc32 collision between two mission ids
    would only make those two missions serialize with each other; it can never misorder
    either mission's own sequence, which stays allocated under the lock it actually holds.
    """
    return zlib.crc32(mission_id.encode("utf-8")) & 0x7FFFFFFF


async def _acquire_mission_sequence_lock(db: AsyncSession, mission_id: str) -> None:
    """
    PostgreSQL: take the transaction-scoped per-mission advisory lock inside the caller's own
    transaction — the same session, connection and transaction the following max(sequence)
    read and the row flush run on — so two concurrent writers to one mission allocate
    sequences one at a time and a loser's transaction is never destroyed at the unique index.
    The lock is re-entrant within its transaction (a turn that appends several rows takes it
    once) and is released by that transaction's commit or rollback, whatever the caller does.

    SQLite (the test dialect) has no advisory locks and no cross-connection write concurrency
    of the kind this guards, so the lock is a no-op there rather than a pretense that SQLite
    validates Postgres behavior; the Postgres path is proven against an isolated PostgreSQL
    database in tests/test_m21_sequence_allocation.py.
    """
    if db.sync_session.get_bind().dialect.name != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _mission_lock_key(mission_id)},
    )


async def _next_sequence(db: AsyncSession, mission_id: str) -> int:
    current = await db.execute(
        select(func.max(AgentConversationRecord.sequence)).where(
            AgentConversationRecord.mission_id == mission_id
        )
    )
    highest = current.scalar()
    return 0 if highest is None else int(highest) + 1


async def latest_sequence(db: AsyncSession, mission_id: str) -> int:
    """The highest sequence this mission has COMMITTED, or -1 when it has no rows at all.

    -1 rather than 0 because sequences start at 0: a client whose applied cursor equals the
    returned value is fully caught up, and `after_sequence=-1` is the canonical way to ask for
    a mission's entire history. This reads the same table every other read here does — a row
    still inside an open transaction is not history yet and is not reported.
    """
    result = await db.execute(
        select(func.max(AgentConversationRecord.sequence)).where(
            AgentConversationRecord.mission_id == mission_id
        )
    )
    highest = result.scalar()
    return -1 if highest is None else int(highest)


async def load_after(
    db: AsyncSession,
    mission_id: str,
    *,
    after_sequence: int,
    limit: int = 200,
) -> tuple[list[AgentConversationRecord], bool]:
    """Committed rows with sequence strictly greater than `after_sequence`, oldest first, capped
    at `limit`, plus whether at least one more row exists beyond the page.

    The exclusive cursor is what makes replay resumable without double-delivery: a client that
    has applied everything up to S asks for rows after S, advances to the last row it applied,
    and asking again can never re-serve what it already has. The boolean is computed by reading
    one row past the page, so the caller knows whether to offer another page without a second
    query.
    """
    result = await db.execute(
        select(AgentConversationRecord)
        .where(
            AgentConversationRecord.mission_id == mission_id,
            AgentConversationRecord.sequence > after_sequence,
        )
        .order_by(AgentConversationRecord.sequence.asc())
        .limit(limit + 1)
    )
    rows = list(result.scalars().all())
    return rows[:limit], len(rows) > limit


async def load_recent(
    db: AsyncSession, mission_id: str, *, limit: int = 20
) -> list[AgentConversationRecord]:
    """The last `limit` turns of THIS mission's conversation, oldest first."""
    result = await db.execute(
        select(AgentConversationRecord)
        .where(AgentConversationRecord.mission_id == mission_id)
        .order_by(AgentConversationRecord.sequence.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


def as_model_context(rows: list[AgentConversationRecord]) -> list[dict[str, Any]]:
    """The transcript the reasoning model is given: who said what, in order, nothing executable.

    Server-driven lifecycle rows (CORE-7 Phase 2) are explicitly labeled so the model reads them
    as statements of what the SERVER did — never as user claims, and never as something that
    outranks the canonical structured context, which is rebuilt from the database every step."""
    return [
        {
            "role": row.role,
            "event_type": row.event_type,
            "content": (
                f"{SERVER_LIFECYCLE_LABEL} {row.content}"
                if row.event_type == "system_state_change"
                else row.content
            ),
            **({"tool": row.tool_name} if row.tool_name else {}),
        }
        for row in rows
    ]


def as_event_frame(row: AgentConversationRecord) -> dict[str, Any]:
    """The canonical event envelope (M21 Phase 2A): the one shape a client ever receives.

    Every field is read from the persisted row itself — never from a caller-supplied dict — so a
    live push, a replay page, and a transcript read back over REST are the same data by
    construction and cannot drift into three dialects. The row was already redacted and
    length-bounded at write time; this adds no second redaction and no new field the row does
    not own. `kind` separates event frames from the stream's control frames (hello, pong,
    replay_complete), which are a different vocabulary produced only by the socket layer.
    """
    return {
        "kind": "event",
        "event_id": row.event_id,
        "sequence": row.sequence,
        "mission_id": row.mission_id,
        "role": row.role,
        "event_type": row.event_type,
        "content": row.content,
        "tool_name": row.tool_name,
        "payload": row.payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def as_client_transcript(rows: list[AgentConversationRecord]) -> list[dict[str, Any]]:
    """What the browser renders when a mission is opened — the persisted turns, in order.

    M21 Phase 2A: this is exactly `as_event_frame` per row, so the transcript a client rebuilds
    from REST and the one it rebuilds from replay are indistinguishable by shape."""
    return [as_event_frame(row) for row in rows]


def redact_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Every string in a payload passes the same credential filter as the content does."""
    if payload is None:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            cleaned[key] = redact_secrets(value, limit=MAX_CONTENT_CHARS)
        elif isinstance(value, dict):
            cleaned[key] = redact_payload(value)
        elif isinstance(value, list):
            cleaned[key] = [
                redact_secrets(item, limit=500)
                if isinstance(item, str)
                else redact_payload(item)
                if isinstance(item, dict)
                else item
                for item in value[:20]
            ]
        else:
            cleaned[key] = value
    return cleaned
