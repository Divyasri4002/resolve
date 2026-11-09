"""
The channel's outbox: the merchant-side memory of what it decided to do to Shopify.

This table belongs to the channel, not to RESOLVE. RESOLVE is not asked to remember a purchase it
did not make, and a RESOLVE-side outbox would be a second copy of a fact whose only authoritative
owner is the system that performs the write.

The single hard rule the schema exists to enforce is the one that keeps a timeout from becoming two
orders:

    a row in OUTCOME_UNKNOWN may not start another create.

`begin_create` is the only way in, and it raises unless the previous attempt either definitively
failed, or was reconciled to `no_match` (Shopify says the order does not exist, so creating is a
deterministic next step rather than a guess). Anything still `PENDING` — a process that died between
deciding and sending — is converted to `OUTCOME_UNKNOWN` on the way past, because the honest reading
of "we meant to send and cannot tell whether we did" is unknown, not "it did not happen".

The third hard rule, added by M20.1, closes the hole inside that exception:

    an empty search never reopens a row whose request did leave, and never reopens a verified order.

Shopify's order search is eventually consistent — a created order has been observed invisible to
`orders(query:)` immediately after its creation — so a reconciliation that finds nothing is proof of
absence only when something independent of the search vouches for it: the provider's own definitive
refusal, or `sent_at IS NULL`, the recorded fact that no request for this correlation ever left this
channel. An unknown outcome whose request did leave gets the zero-result observation recorded
(`reconciliation_state = no_match` while the row stays `OUTCOME_UNKNOWN`) and is reopened for exactly
one create only by `authorize_create` — a human decision, recorded with its reason, consumed by the
next claim. Waiting longer, or reading nothing repeatedly, licenses nothing.

What is stored is the purchase's own identifiers and digests. Never the credential, never the HMAC
secret, never a signature, never a customer field.

The second hard rule, added by Task 14C.7L, concerns the claim rather than the purchase:

    a new linkage attempt requires a settled refusal of the previous one.

`begin_linkage_attempt` is the only way in, and it raises on any other linkage state. It changes the
attempt number and nothing else — same correlation, same RESOLVE order, same merchant order, same
payload hash — because a reclaim that could reach the create path would be a duplicate order with
better paperwork. `linkage_attempt_history` is the record of those attempts: event ids, attempt
numbers, outcomes, timestamps. The row's `attempt` is the only place an attempt number exists, and
`event_id_for` reads it from here, so no caller can invent one.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

NOT_STARTED = "NOT_STARTED"
PENDING = "PENDING"
CONFIRMED = "CONFIRMED"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
FAILED = "FAILED"
RECONCILED = "RECONCILED"

STATUSES = (NOT_STARTED, PENDING, CONFIRMED, OUTCOME_UNKNOWN, FAILED, RECONCILED)

MATCHED = "matched"
NO_MATCH = "no_match"
AMBIGUOUS = "ambiguous"
NOT_RECONCILED = None

RECONCILIATION_STATES = (MATCHED, NO_MATCH, AMBIGUOUS, NOT_RECONCILED)

# `linkage` tracks the second half: whether the signed event that tells RESOLVE about this order has
# actually been acknowledged. A lost answer there is unknown too, and is never re-invented — it is
# re-SENT with the same event_id, which is what makes RESOLVE's replay guard the safe thing to lean on.
LINKAGE_NOT_SENT = "NOT_SENT"
LINKAGE_SENT = "SENT"
LINKAGE_DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
LINKAGE_REFUSED = "REFUSED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS purchase_outbox (
    correlation_id       TEXT PRIMARY KEY,
    resolve_order_id     TEXT NOT NULL,
    payload_hash         TEXT NOT NULL,
    status               TEXT NOT NULL,
    reconciliation_state TEXT,
    linkage              TEXT NOT NULL,
    external_order_id    TEXT,
    event_id             TEXT,
    event_body           TEXT,
    attempted_at         TEXT,
    resolved_at          TEXT,
    failure_reason       TEXT,
    attempt              INTEGER NOT NULL DEFAULT 1,
    previous_event_id    TEXT,
    sent_at              TEXT,
    create_authorized_at TEXT,
    create_authorization_reason TEXT
);

CREATE TABLE IF NOT EXISTS linkage_attempt_history (
    correlation_id      TEXT NOT NULL,
    attempt             INTEGER NOT NULL,
    event_id            TEXT NOT NULL,
    supersedes_event_id TEXT,
    outcome             TEXT NOT NULL,
    external_order_id   TEXT,
    recorded_at         TEXT NOT NULL,
    PRIMARY KEY (correlation_id, attempt)
);
"""

# `attempt` and `previous_event_id` arrived with Task 14C.7L; `sent_at` and the authorization pair
# arrived with M20.1. A channel that already holds an outbox file gets them added, not recreated:
# `CREATE TABLE IF NOT EXISTS` above is a no-op against an existing file, and dropping a row here
# would drop the only memory that a create already happened.
_UPGRADE_COLUMNS = (
    ("attempt", "INTEGER NOT NULL DEFAULT 1"),
    ("previous_event_id", "TEXT"),
    ("sent_at", "TEXT"),
    ("create_authorized_at", "TEXT"),
    ("create_authorization_reason", "TEXT"),
)

# A second purchase for the same RESOLVE order must be visible as a second purchase, never silently
# merged into the first: the reserve key is the correlation id, which itself covers the payload hash.
_UNIQUE_ORDER_AND_PAYLOAD = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_order_payload "
    "ON purchase_outbox (resolve_order_id, payload_hash);"
)


class AmbiguousOutcome(Exception):
    """
    A create was asked for while the previous attempt's outcome is unknown.

    Refusing is the whole point: this is the moment at which a duplicate Shopify order would be
    born.
    """


class ReclaimNotAllowed(Exception):
    """
    A second linkage claim was asked for over something other than a settled refusal.

    A reclaim exists to recover a pairing RESOLVE *answered* "no" to. Anything else — never sent,
    still awaiting an answer, already paired — has a correct response that is not a new claim, and
    minting one there would put two live claims on one purchase with no refusal in between.
    """


class CreateNotAuthorized(Exception):
    """
    A human authorization for a new create was asked for and refused.

    The authorization door exists for exactly one state — an unknown outcome whose request did
    leave, with a reconciliation that currently reports no order. Anything else either does not
    need it (a definitive refusal or a never-sent attempt may retry on their own) or must not have
    it (an order already found, several candidates, a settled purchase).
    """


class EventRecordConflict(Exception):
    """
    A delivery tried to write event bytes over a row that already persists some.

    The stored (event_id, event_body) pair is one immutable fact: it is what a redelivery must
    resend and what the signature covers. An identical pair is accepted as the no-op it is;
    anything else — a different body under the same id, a different id, or a half-persisted row
    that cannot be completed from outside — is refused, because overwriting is how a fresh payload
    ends up wearing a spent event id.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SELECT = (
    "SELECT correlation_id, resolve_order_id, payload_hash, status, reconciliation_state, "
    "linkage, external_order_id, event_id, event_body, attempted_at, resolved_at, failure_reason, "
    "attempt, previous_event_id, sent_at, create_authorized_at, create_authorization_reason "
    "FROM purchase_outbox"
)


class Outbox:
    """Durable, channel-owned record of each intended Shopify purchase."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        with self._connect() as conn:
            conn.executescript(_SCHEMA + _UNIQUE_ORDER_AND_PAYLOAD)
            present = {
                row[1] for row in conn.execute("PRAGMA table_info(purchase_outbox)").fetchall()
            }
            for name, declaration in _UPGRADE_COLUMNS:
                if name not in present:
                    conn.execute(
                        f"ALTER TABLE purchase_outbox ADD COLUMN {name} {declaration}"
                    )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def reserve(self, *, correlation_id: str, resolve_order_id: str, payload_hash: str):
        """Return the row for this correlation, creating it as NOT_STARTED when first seen."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT correlation_id FROM purchase_outbox WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
            if row is None:
                try:
                    conn.execute(
                        "INSERT INTO purchase_outbox (correlation_id, resolve_order_id, payload_hash, "
                        "status, linkage) VALUES (?, ?, ?, ?, ?)",
                        (correlation_id, resolve_order_id, payload_hash, NOT_STARTED, LINKAGE_NOT_SENT),
                    )
                except sqlite3.IntegrityError:
                    # A concurrent caller inserted this correlation between the SELECT and the
                    # INSERT. That caller won the reserve; this one reads the same row rather than
                    # crashing, and the correlation (which covers the payload hash) guarantees it is
                    # the same purchase.
                    pass
        # Read after the connection is closed: the insert is committed then, and reading inside it
        # would ask a second connection for a row the first has not released yet.
        return self.get(correlation_id)

    def get(self, correlation_id: str):
        with self._connect() as conn:
            row = conn.execute(
                _SELECT + " WHERE correlation_id = ?", (correlation_id,)
            ).fetchone()
        if row is None:
            return None
        return Record(*row)

    def begin_create(self, correlation_id: str) -> "Record":
        """
        Permission to send a create to Shopify, or a refusal.

        Allowed: never attempted; definitively FAILED; reconciled to `no_match` where the absence
        was vouched for (a definitive refusal, or nothing ever sent); OUTCOME_UNKNOWN only under a
        recorded human authorization. Refused: PENDING (converted to unknown first), CONFIRMED, a
        reconciliation that concluded `matched` or `ambiguous`, and any unknown outcome without an
        authorization.

        The permission check and the flip to PENDING are one conditional UPDATE. Reading a creatable
        row and then updating it unconditionally would let two concurrent callers both pass the check
        and both send a create — the duplicate order this table exists to prevent — so only the
        caller whose UPDATE matches the row in a creatable state may proceed; the loser re-reads what
        the winner left (PENDING) and is refused. The authorization, when there was one, is consumed
        by this same UPDATE: it licenses exactly one create.
        """
        record = self.get(correlation_id)
        if record is None:
            raise KeyError(f"no outbox row for {correlation_id}")
        if record.status == PENDING:
            # The previous attempt decided to send and we cannot see the outcome: that is unknown,
            # not absent. Say so before anything else is considered.
            self.mark_outcome_unknown(correlation_id, "previous attempt left PENDING unresolved")
        with self._connect() as conn:
            claimed = conn.execute(
                "UPDATE purchase_outbox SET status = ?, attempted_at = ?, failure_reason = NULL, "
                "create_authorized_at = NULL, create_authorization_reason = NULL "
                "WHERE correlation_id = ? AND ("
                "status IN (?, ?) OR (status = ? AND reconciliation_state = ?) "
                "OR (status = ? AND create_authorized_at IS NOT NULL))",
                (PENDING, _now(), correlation_id, NOT_STARTED, FAILED, RECONCILED, NO_MATCH,
                 OUTCOME_UNKNOWN),
            ).rowcount
        if claimed != 1:
            record = self.get(correlation_id)
            raise AmbiguousOutcome(
                f"correlation {correlation_id} is {record.status} "
                f"(reconciliation={record.reconciliation_state}); another Shopify order will not be "
                "created for it. Read back and reconcile first."
            )
        return self.get(correlation_id)

    def mark_request_sent(self, correlation_id: str) -> None:
        """
        Record, once and for the row's whole life, that a create request was initiated.

        This is the fact the recovery policy leans on: `sent_at IS NULL` means no HTTP request for
        this correlation ever left the channel, so a zero-result read-back vouches for absence all
        by itself. The write happens before the request is handed to the transport and is never
        cleared — a crash in the sliver between this write and the send errs toward "sent", which
        is the fail-closed direction; it can never under-count a request that did leave.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE purchase_outbox SET sent_at = ? "
                "WHERE correlation_id = ? AND sent_at IS NULL",
                (_now(), correlation_id),
            )

    def mark_outcome_unknown(self, correlation_id: str, reason: str) -> None:
        # A reconciliation conclusion belongs to the round it was read in. `no_match` said Shopify
        # had no such order *then*; a new attempt whose outcome is unknown says nothing about *now*,
        # and letting the stale conclusion ride along would leave the row creatable while attempt
        # N+1's fate is precisely what is unknown — the blind second create.
        #
        # The write refuses a CONFIRMED row: a verified order is a fact a later "we cannot tell"
        # may not erase. That is what keeps a recovery process that read PENDING a moment before
        # another process confirmed, from clobbering the confirmation it did not see.
        with self._connect() as conn:
            conn.execute(
                "UPDATE purchase_outbox SET status = ?, reconciliation_state = NULL, "
                "failure_reason = ? WHERE correlation_id = ? AND status != ?",
                (OUTCOME_UNKNOWN, reason[:500], correlation_id, CONFIRMED),
            )

    def mark_failed(self, correlation_id: str, reason: str) -> None:
        self._set(
            correlation_id,
            status=FAILED,
            failure_reason=reason[:500],
            reconciliation_state=NO_MATCH,
            resolved_at=_now(),
        )

    def mark_confirmed(
        self, correlation_id: str, external_order_id: str, event_id: str | None = None
    ) -> None:
        columns = {
            "status": CONFIRMED,
            "external_order_id": external_order_id,
            "reconciliation_state": MATCHED,
            "resolved_at": _now(),
            "failure_reason": None,
        }
        # An existing event_id is the identity of a claim that may already be on the wire: a
        # re-verification that does not name a replacement must leave it exactly as it is, or the
        # row loses the id its stored bytes belong to and the next delivery mints a fresh body
        # under the same id — the mismatch RESOLVE's replay guard answers 409.
        if event_id is not None:
            columns["event_id"] = event_id
        self._set(correlation_id, **columns)

    def mark_reconciled(self, correlation_id: str, state: str) -> None:
        """
        Record what a read-only reconciliation concluded.

        `matched` closes the row. `no_match` means Shopify currently reports no such order — which
        reopens the row for a create ONLY when something independent of the search vouches for the
        absence: the previous attempt ended in a definitive refusal, or no request ever left this
        channel (`sent_at IS NULL`). An unknown outcome whose request did leave keeps the row closed
        with the observation recorded — Shopify's search is eventually consistent, and empty answers
        do not accumulate into proof. `ambiguous` keeps the row unknown on purpose: more than one
        candidate is a human decision, not a coin flip.
        """
        if state not in (MATCHED, NO_MATCH, AMBIGUOUS):
            raise ValueError(f"not a reconciliation state: {state!r}")
        record = self.get(correlation_id)
        if state == NO_MATCH and record is not None:
            if record.status == CONFIRMED:
                # A verified order cannot be un-seen by an empty search. The observation is
                # recorded; the purchase it verified stands, and the row stays closed.
                self._set(
                    correlation_id,
                    reconciliation_state=NO_MATCH,
                    failure_reason=(
                        "reconciled as no_match while the order was verified and confirmed; "
                        "the confirmed purchase stands"
                    ),
                )
                return
            if record.status == OUTCOME_UNKNOWN and record.sent_at is not None:
                # The request left, so an order may exist however empty the search answers. The
                # zero-result read is recorded, and the row stays exactly as closed as it was.
                self._set(
                    correlation_id,
                    status=OUTCOME_UNKNOWN,
                    reconciliation_state=NO_MATCH,
                    failure_reason=(
                        "reconciled as no_match, but the create request did leave and Shopify's "
                        "search does not prove absence; human authorization is required before "
                        "another create"
                    ),
                )
                return
        self._set(
            correlation_id,
            status=(OUTCOME_UNKNOWN if state == AMBIGUOUS else RECONCILED),
            reconciliation_state=state,
            failure_reason=(None if state == MATCHED else f"reconciled as {state}"),
            resolved_at=_now() if state == MATCHED else None,
        )

    def authorize_create(self, correlation_id: str, *, reason: str) -> "Record":
        """
        The one door that reopens a row whose create outcome is unknown after its request left.

        It exists for exactly one state: OUTCOME_UNKNOWN with a current reconciliation that found
        no order — the operator has looked elsewhere (the store admin, the passage of time, an
        out-of-band check) and is stating, with a reason that is recorded on the row, that they
        accept the risk that the original order may still surface. The authorization licenses
        exactly one create and is consumed by the next `begin_create`; if that create's outcome is
        also unknown, a fresh human decision is required, so no chain of automatic retries can
        hide inside one approval. A found-and-verified order closes the row the honest way instead
        (`matched`), and an ambiguity is never authorizable at all.
        """
        record = self.get(correlation_id)
        if record is None:
            raise KeyError(f"no outbox row for {correlation_id}")
        if record.status != OUTCOME_UNKNOWN or record.reconciliation_state != NO_MATCH:
            raise CreateNotAuthorized(
                f"correlation {correlation_id} is {record.status} "
                f"(reconciliation={record.reconciliation_state}); a create may be authorized only "
                "for an unknown outcome whose reconciliation currently reports no order. Run "
                "reconcile (a read-only query) first."
            )
        self._set(
            correlation_id,
            create_authorized_at=_now(),
            create_authorization_reason=reason[:500],
        )
        return self.get(correlation_id)

    def record_event(self, correlation_id: str, *, event_id: str, event_body: str) -> None:
        """
        Remember the exact bytes before sending them.

        A delivery whose answer is lost has to be re-tried with *these* bytes under *this* id. A
        retry that re-stamped `issued_at` would put a different body under the same `event_id`, and
        RESOLVE would correctly answer 409 "already recorded for a different payload" — about a
        pairing that is in fact still unclaimed.

        So the pair is immutable once any part of it exists: an identical redelivery is a no-op,
        and anything else raises `EventRecordConflict` rather than overwriting.
        """
        existing = self.get(correlation_id)
        if existing is not None and (existing.event_id or existing.event_body):
            if existing.event_id == event_id and existing.event_body == event_body:
                return
            raise EventRecordConflict(
                f"correlation {correlation_id} already persists an event "
                f"(event_id={'present' if existing.event_id else 'missing'}, "
                f"event_body={'present' if existing.event_body else 'missing'}); the stored event "
                "is immutable, so delivering different bytes requires a reclaim, not a rewrite"
            )
        self._set(correlation_id, event_id=event_id, event_body=event_body)

    def set_linkage(
        self,
        correlation_id: str,
        linkage: str,
        *,
        event_id: str | None = None,
        event_body: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if linkage not in (
            LINKAGE_NOT_SENT,
            LINKAGE_SENT,
            LINKAGE_DELIVERY_UNKNOWN,
            LINKAGE_REFUSED,
        ):
            raise ValueError(f"not a linkage state: {linkage!r}")
        columns = {"linkage": linkage, "event_id": event_id, "event_body": event_body,
                   "failure_reason": failure_reason}
        self._set(correlation_id, **{k: v for k, v in columns.items() if v is not None})
        if event_id and linkage in (LINKAGE_SENT, LINKAGE_REFUSED):
            # One history row per settled attempt. This is the audit the reclaim story needs, and it
            # is deliberately the same write that changes the state, so a state with no record of
            # how it got there cannot exist.
            self._record_history(correlation_id, event_id, linkage)

    def begin_linkage_attempt(self, correlation_id: str) -> "Record":
        """
        Mint the second claim, and make it the only way to.

        Permission is one state and one state only: a linkage RESOLVE has settled as REFUSED. The
        rotation clears the stored bytes, because those bytes are attempt N's answer and reusing them
        would send a spent claim; it records which event it is superseding, because an auditor is
        entitled to follow the chain; and it touches nothing about the *purchase* — the correlation,
        the RESOLVE order, the merchant order and the payload hash are exactly as they were, which is
        the whole point of a reclaim rather than a new purchase.

        It creates nothing. A reclaim that could reach Shopify's write path would be a duplicate
        order with better paperwork.
        """
        record = self.get(correlation_id)
        if record is None:
            raise KeyError(f"no outbox row for {correlation_id}")
        if record.linkage != LINKAGE_REFUSED:
            raise ReclaimNotAllowed(
                f"correlation {correlation_id} is not a settled refusal (linkage="
                f"{record.linkage}); its previous claim was never answered no, so there is nothing "
                "to reclaim and a second identity would be a second live claim on one purchase"
            )
        with self._connect() as conn:
            conn.execute(
                "UPDATE purchase_outbox SET attempt = attempt + 1, previous_event_id = event_id, "
                "event_id = NULL, event_body = NULL, linkage = ? WHERE correlation_id = ?",
                (LINKAGE_NOT_SENT, correlation_id),
            )
        return self.get(correlation_id)

    def _record_history(self, correlation_id: str, event_id: str, outcome: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempt, previous_event_id, external_order_id FROM purchase_outbox "
                "WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
        if row is None:
            return
        attempt, previous_event_id, external_order_id = row
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO linkage_attempt_history (correlation_id, attempt, event_id, "
                "supersedes_event_id, outcome, external_order_id, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    correlation_id,
                    attempt,
                    event_id,
                    previous_event_id if attempt > 1 else None,
                    outcome,
                    external_order_id,
                    _now(),
                ),
            )

    def linkage_history(self, correlation_id: str) -> list[dict]:
        """
        The chain of claims this correlation has made, oldest first.

        Event ids, attempt numbers, outcomes and timestamps — the shape of the audit. No signature,
        no secret, no body: the bytes a claim was made of stay in `purchase_outbox`, where a resend
        can find them, and are not copied into an append-only table that nothing re-sends from.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT attempt, event_id, supersedes_event_id, outcome, external_order_id, "
                "recorded_at FROM linkage_attempt_history WHERE correlation_id = ? "
                "ORDER BY attempt",
                (correlation_id,),
            ).fetchall()
        return [
            {
                "attempt": attempt,
                "event_id": event_id,
                "supersedes_event_id": supersedes,
                "outcome": outcome,
                "external_order_id": external_order_id,
                "recorded_at": recorded_at,
            }
            for attempt, event_id, supersedes, outcome, external_order_id, recorded_at in rows
        ]

    def _set(self, correlation_id: str, **columns) -> None:
        allowed = {
            "status",
            "reconciliation_state",
            "linkage",
            "external_order_id",
            "event_id",
            "event_body",
            "attempted_at",
            "resolved_at",
            "failure_reason",
            "create_authorized_at",
            "create_authorization_reason",
        }
        unknown = set(columns) - allowed
        if unknown:
            raise KeyError(f"not outbox columns: {sorted(unknown)}")
        sets = ", ".join(f"{name} = ?" for name in columns)
        values = list(columns.values()) + [correlation_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE purchase_outbox SET {sets} WHERE correlation_id = ?", values)

    def all(self) -> list["Record"]:
        with self._connect() as conn:
            rows = conn.execute(_SELECT + " ORDER BY attempted_at").fetchall()
        return [Record(*row) for row in rows]


class Record:
    """One outbox row, read out of the channel's own storage."""

    __slots__ = (
        "correlation_id",
        "resolve_order_id",
        "payload_hash",
        "status",
        "reconciliation_state",
        "linkage",
        "external_order_id",
        "event_id",
        "event_body",
        "attempted_at",
        "resolved_at",
        "failure_reason",
        "attempt",
        "previous_event_id",
        "sent_at",
        "create_authorized_at",
        "create_authorization_reason",
    )

    def __init__(self, *values) -> None:
        for name, value in zip(self.__slots__, values):
            setattr(self, name, value)

    @property
    def may_create(self) -> bool:
        """
        Whether this row licenses sending a create to Shopify.

        Never from an outcome that is merely unknown. An unknown whose request did leave is reopened
        only by a recorded human authorization (`authorize_create`); an unknown whose request never
        left is not an unknown about an order at all, and its zero-result reconciliation reopens the
        row the ordinary way (`no_match` on a RECONCILED row).
        """
        if self.status in (PENDING, CONFIRMED):
            return False
        if self.status in (NOT_STARTED, FAILED):
            return True
        if self.status == OUTCOME_UNKNOWN:
            return self.create_authorized_at is not None
        return self.reconciliation_state == NO_MATCH

    def replace(self, **columns) -> "Record":
        values = {name: getattr(self, name) for name in self.__slots__}
        values.update(columns)
        return Record(*[values[name] for name in self.__slots__])

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self) -> str:
        # `event_body` is deliberately not rendered: it is the signed payload, and a repr ends up in
        # logs and pytest assertion headers far more easily than anyone plans.
        return (
            f"Record(correlation_id={self.correlation_id!r}, status={self.status!r}, "
            f"reconciliation_state={self.reconciliation_state!r}, linkage={self.linkage!r}, "
            f"external_order_id={self.external_order_id!r})"
        )

    def __eq__(self, other) -> bool:
        return isinstance(other, Record) and self.as_dict() == other.as_dict()
