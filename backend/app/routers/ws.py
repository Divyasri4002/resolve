"""
The live mission event stream (M21 Phase 2A canonical replay protocol).

Every frame a client can receive on this socket is the canonical envelope of a COMMITTED
conversation row (`app.agents.conversation.as_event_frame`) — the same shape a replay serves and
the same shape `GET /missions/{id}/agent/conversation` returns. There is no second event
vocabulary on the wire: an ad-hoc dict built next to a state change can never again be pushed,
because anything worth pushing has to be worth a durable row first.

The handshake order is load-bearing, and it is:

    authenticate -> prove ownership -> validate Origin -> enforce the connection limit ->
    accept -> REGISTER the socket -> read the latest committed sequence -> send hello

Registration MUST happen before the snapshot is read; do not reverse them. Reversing loses every
event broadcast in the gap between snapshot and registration: such an event is committed, is
pushed to sockets registered at that instant (not this one), and is absent from the snapshot this
client was about to be given. Registration-first closes the window — anything committed after
registration is either pushed live to this socket or recoverable by replay, and a client merges
both by sequence. That is the actual delivery contract: the server does not promise
exactly-once; the client applies frames idempotently by sequence and can always catch up from
its own last-applied cursor.

Control frames (hello, ping, pong, replay, replay_complete) are a separate vocabulary from event
frames, and they are the ONLY messages the server accepts from a client. This remains a mirror
of server state: no new event kinds are defined here and REST stays the source of truth.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import conversation
from app.auth.security import verify_session_token
from app.config import get_settings
from app.database import get_db
from app.models.db_models import AgentConversationRecord, MissionRecord, UserRecord

logger = logging.getLogger("resolve.ws")

settings = get_settings()

router = APIRouter(tags=["realtime"])

# mission_id -> registered clients, in registration order.
_connections: dict[str, list["_StreamClient"]] = defaultdict(list)

# One uniform denial code for every refused handshake — anonymous, invalid token, foreign or
# unknown mission, disallowed Origin, over the connection limit. A single code means a refused
# connection reveals nothing about which check refused it, and in particular nothing about
# whether a mission id exists.
DENY_CODE = 1008

# A socket pruned for slowness is closed with this code: the connection violated the stream's
# delivery policy (it stopped accepting frames within the bound). Distinct from the handshake
# refusal above — this one is sent after a connection was accepted.
SLOW_CONSUMER_CLOSE_CODE = 1008

# One replay control frame is answered with at most this many rows; a client pages through the
# rest by sending another replay frame with its advanced cursor.
REPLAY_PAGE_SIZE = 200


def _allowed_origins() -> set[str]:
    """Browser origins allowed to open the stream — the same list the CORS middleware allows for
    REST, so the socket is never an easier cross-site target than the API it mirrors."""
    return {
        settings.frontend_origin.rstrip("/"),
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    }


def _origin_allowed(websocket: WebSocket) -> bool:
    """The cross-site WebSocket handshake (CSWSH) guard. The CORS middleware does not run on the
    WebSocket handshake, so this check is the only cross-site guard the socket has.

    Policy: a browser always attaches an Origin header to a WebSocket handshake, so a PRESENT
    Origin must name an allowed origin or the handshake is refused. A MISSING Origin cannot come
    from a browser — it is how non-browser clients (TestClient, curl, native integrations)
    connect — so it is allowed; such clients still pass the same authentication and ownership
    checks as everyone else. The absence of an Origin header is therefore not an bypass: it
    grants no identity, and identity is what the next check establishes."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return origin.rstrip("/") in _allowed_origins()


class _StreamClient:
    """One registered socket.

    `send_lock` exists because two tasks can target one socket concurrently: this socket's own
    receive loop answering a replay control frame, and a live broadcast running inside whatever
    request handler committed the row. Interleaved `send_json` calls on one WebSocket corrupt
    framing — so every send to a socket, in every code path, goes through the lock."""

    __slots__ = ("websocket", "send_lock")

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.send_lock = asyncio.Lock()


async def _authorize_stream(
    websocket: WebSocket, db: AsyncSession, mission_id: str
) -> bool:
    """
    The WebSocket counterpart of the REST mission guard, resolved server-side only.

    Identity comes from the session cookie or the Bearer header — never from the client's
    payload, and there is no anonymous mode. The caller must own the mission, and a foreign
    mission id is indistinguishable from a missing one.
    """
    token = websocket.cookies.get(settings.auth_cookie_name)
    if not token:
        auth_header = websocket.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return False

    payload = verify_session_token(token, settings.auth_secret)
    user_id = payload.get("sub") if payload else None
    if not user_id:
        return False

    user = await db.get(UserRecord, user_id)
    if user is None:
        return False

    mission = await db.get(MissionRecord, mission_id)
    if mission is None or mission.user_id != user.id:
        return False

    return True


async def _send_frame(client: _StreamClient, frame: dict) -> None:
    """Send one frame under the socket's send lock, bounded by ws_send_timeout_seconds.

    The bound is what keeps ONE slow consumer from stalling everyone else: a client that stops
    reading its socket makes `send_json` block on TCP backpressure, and without a timeout the
    broadcast task — which runs inside whatever business request committed the row — would block
    on it indefinitely. Timing out prunes this socket (see broadcast_event_row); the client
    recovers by reconnecting and replaying from its own cursor."""
    async def _send() -> None:
        async with client.send_lock:
            await client.websocket.send_json(frame)

    await asyncio.wait_for(_send(), timeout=settings.ws_send_timeout_seconds)


async def _send_replay_page(
    client: _StreamClient, db: AsyncSession, mission_id: str, *, after_sequence: int
) -> None:
    """Answer one replay control frame with one bounded page of COMMITTED rows.

    Committed rows only: `load_after` reads the same table the REST cursor reads, so every frame
    served here is verifiable by the client against
    GET /missions/{id}/agent/conversation. An uncommitted row cannot be served — it does not
    exist in this query. Exactly one page is served per control frame and `replay_complete` says
    whether more exist; bounding the server's work per client message (one query, at most
    REPLAY_PAGE_SIZE sends) is what keeps a misbehaving client from turning its own socket into
    a firehose."""
    rows, has_more = await conversation.load_after(
        db, mission_id, after_sequence=after_sequence, limit=REPLAY_PAGE_SIZE
    )
    for row in rows:
        await _send_frame(client, conversation.as_event_frame(row))
    await _send_frame(
        client,
        {
            "kind": "replay_complete",
            "mission_id": mission_id,
            "after_sequence": after_sequence,
            "has_more": has_more,
        },
    )


async def _handle_client_frame(
    client: _StreamClient, db: AsyncSession, mission_id: str, raw: str
) -> None:
    """The only client-to-server messages are control frames. A malformed or unknown frame is
    ignored — never treated as data, never echoed, and never able to make the server send
    something the frame did not ask for. What is logged stays non-sensitive: the frame kind
    only, never its body."""
    try:
        message = json.loads(raw)
    except ValueError:
        logger.warning("ws: dropped malformed control frame on mission %s", mission_id)
        return
    if not isinstance(message, dict):
        logger.warning("ws: dropped non-object control frame on mission %s", mission_id)
        return
    kind = message.get("kind")
    if kind == "ping":
        await _send_frame(client, {"kind": "pong"})
    elif kind == "replay":
        after = message.get("after_sequence")
        if isinstance(after, bool) or not isinstance(after, int):
            logger.warning(
                "ws: dropped replay frame with invalid cursor on mission %s", mission_id
            )
            return
        await _send_replay_page(client, db, mission_id, after_sequence=after)
    else:
        logger.warning(
            "ws: dropped unknown control frame kind=%r on mission %s", kind, mission_id
        )


@router.websocket("/ws/missions/{mission_id}")
async def mission_stream(
    websocket: WebSocket,
    mission_id: str,
    db: AsyncSession = Depends(get_db),
):
    authorized = await _authorize_stream(websocket, db, mission_id)
    if not authorized:
        await websocket.close(code=DENY_CODE)
        return

    if not _origin_allowed(websocket):
        await websocket.close(code=DENY_CODE)
        return

    if len(_connections.get(mission_id, [])) >= settings.ws_max_connections_per_mission:
        await websocket.close(code=DENY_CODE)
        return

    await websocket.accept()
    client = _StreamClient(websocket)

    # The pre-accept limit check above has one await (accept) between it and the append below,
    # so a second handshake for the same mission could slip through that window. Re-checked
    # before anything is registered so the bound holds exactly even for concurrent handshakes.
    if len(_connections.get(mission_id, [])) >= settings.ws_max_connections_per_mission:
        await websocket.close(code=DENY_CODE)
        return

    # REGISTER FIRST. This ordering is the loss-free protocol: from here on, every broadcast for
    # this mission reaches this socket, and everything older than the snapshot below is
    # recoverable by replay from the client's own cursor. Do not move the snapshot read above
    # this line.
    _connections[mission_id].append(client)
    try:
        snapshot_sequence = await conversation.latest_sequence(db, mission_id)
        await _send_frame(
            client,
            {
                "kind": "hello",
                "mission_id": mission_id,
                "snapshot_sequence": snapshot_sequence,
            },
        )
        while True:
            raw = await websocket.receive_text()
            try:
                await _handle_client_frame(client, db, mission_id, raw)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # A control frame we could not answer (its send timed out — the client stopped
                # reading, or the socket is already gone) means this connection no longer serves
                # its purpose. End the loop; returning closes the socket and cleanup below
                # unregisters it.
                break
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError: another task (a timed-out broadcast pruning this socket) closed the
        # connection while this loop sat in receive.
        pass
    finally:
        connections = _connections.get(mission_id, [])
        if client in connections:
            connections.remove(client)


async def broadcast_event_row(row: AgentConversationRecord) -> None:
    """Push one COMMITTED conversation row to the mission's registered sockets, as the canonical
    event envelope.

    Callers commit the transaction containing `row` BEFORE calling: the frame is serialized from
    the row itself, so pushing an uncommitted row would hand a client an event that replay and
    REST cannot yet confirm — and a rolled-back transition must never have been announced. This
    is the single broadcast path for the stream; nothing else sends to these sockets.

    One slow or dead socket never stalls the caller's business request: each send is bounded by
    ws_send_timeout_seconds, a failure only prunes that socket, and the pruned socket is closed
    so the client knows the live stream ended and can reconnect and replay from its cursor.
    Delivery here is best-effort BY DESIGN — durability lives in the row, and a client that
    misses a live frame recovers it by replay. The server does not promise exactly-once."""
    frame = conversation.as_event_frame(row)
    dead: list[_StreamClient] = []
    for client in list(_connections.get(row.mission_id, [])):
        try:
            await _send_frame(client, frame)
        except Exception:  # noqa: BLE001 - a dead socket is pruned, never fatal to the caller
            dead.append(client)
    for client in dead:
        connections = _connections.get(row.mission_id, [])
        if client in connections:
            connections.remove(client)
        with contextlib.suppress(Exception):
            await client.websocket.close(code=SLOW_CONSUMER_CLOSE_CODE)
