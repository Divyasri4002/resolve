"""
The external evidence publisher (Phase 4.6.2, approved 4.5.6/4.5.9 design).

One lease-serialized loop that advances the WORM head object one event per conditional
PUT, in strict sequence order, classifying every outcome before any retry:

  per event, per attempt:
    re-read the pin fresh from the database          (4.5.5 fence rule)
    revalidate the lease immediately                 (stale publishers must not write)
    observe the external head                        (GET)
    validate gates G1-G5 on that fresh observation   (4.5.9 five-gate validation)
    one conditional PUT (If-Match: observed ETag)    (4.5.7: ETag, never VersionId)
    classify: 200 -> pin advance | 412/409 -> re-observe (content decides)
              5xx/timeout -> re-observe, GET decides | 404 -> REFUSE, never re-bootstrap

  gates on the fresh observation against pin sequence N:
    G1 sequence:  obs == N -> G2; obs == N+1 -> content identity (G5); obs > N+1 ->
                  REFUSE+ALARM (divergent head); obs < N -> RECONCILE (regression —
                  never rebuild-over, never retry)
    G2 hash:      obs.head_hash must equal the pin's chain hash at N, else the
                  predecessor is forged -> REFUSE+ALARM
    G3 validator: obs ETag differing while seq+hash match means the object was re-written
                  byte-identically (a lost response from a crashed publisher) — the new
                  version identity is adopted, never fought
    G5 content:   at N+1, the embedded event must be OURS (same seq + same payload
                  digest, chained to our head) -> ACK without a PUT (the classic
                  lost-response / crash-after-PUT recovery)

  lineage (before ANY pin advance): the pin's recorded version must still exist in the
                  head object's version history. Conformance finding S5: MinIO-class
                  providers accept an If-Match PUT on a MISSING object and CREATE a
                  fresh one (AWS S3 answers 404), so a head destroyed between the
                  observation and the write would otherwise land our next entry on a
                  brand-new anchor with no genesis and no history — and every later
                  gate would accept it, because the content is ours. A destroyed
                  lineage is tampering evidence: halt, never adopt, never rebuild.

Bounded retries (4.5.9 C5): at most 3 consuming attempts (412/409/5xx/timeout) per
unresolved episode; exponential backoff with full jitter capped at 30 s; every transition
persisted to ledger_publish_state BEFORE the next network call; exhaustion marks the
episode `unresolved` and requires explicit operator reconciliation. A 409 gets one
rebuild-retry beyond observation, then refuses (4.5.9 D7). The publisher never calls
merchant or payment APIs — its egress is the object store and nothing else.

Terminal states (`halted`, `reconcile`) are operator-owned: the publisher refuses to run
and logs honestly until an operator resets them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ledger import canonical
from app.ledger.store import (
    ConflictError,
    ObjectNotFound,
    PreconditionFailed,
    S3ObjectStore,
    ServerError,
    TransportFailure,
)
from app.ledger.worm import S3WormPolicy, WormPolicy

logger = logging.getLogger("app.ledger.publisher")

MAX_CAS_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
MAX_EVENTS_PER_CYCLE = 50
_LEASE_RENEW_MARGIN_SECONDS = 10.0

_STATUS_IDLE = "idle"
_STATUS_PUBLISHING = "publishing"
_STATUS_UNRESOLVED = "unresolved"
_STATUS_HALTED = "halted"
_STATUS_RECONCILE = "reconcile"
_TERMINAL_STATUSES = (_STATUS_HALTED, _STATUS_RECONCILE)


@dataclass(frozen=True)
class PinState:
    last_published_seq: int
    last_version_id: str | None
    last_validator: str | None
    last_head_hash: str
    publish_status: str
    attempts: int
    last_error: str | None
    last_head_seq: int | None
    next_eligible_at: datetime | None
    lease_epoch: int
    lease_owner: str | None
    lease_expires_at: datetime | None


@dataclass(frozen=True)
class HeadObservation:
    head_seq: int
    head_hash: str
    envelope: dict | None
    etag: str
    version_id: str | None


def utcnow() -> datetime:
    return datetime.utcnow()


def _full_jitter_backoff(attempt: int) -> float:
    ceiling = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)
    return random.uniform(0, ceiling)


class Publisher:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        store: S3ObjectStore,
        *,
        worm: WormPolicy,
        lease_seconds: float = 60.0,
        max_attempts: int = MAX_CAS_ATTEMPTS,
        sleep=asyncio.sleep,
        clock=utcnow,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._store = store
        # The WORM policy is REQUIRED, not defaulted: every construction site must
        # state its enforcement posture explicitly (4.6.3 — production wires
        # S3WormPolicy; only tests may construct PermissiveWormPolicy, and the wiring
        # test pins that).
        self._worm = worm
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._clock = clock

    # ------------------------------------------------------------------ pin I/O --
    async def _read_pin(self) -> PinState | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT last_published_seq, last_version_id, last_validator, "
                        "last_head_hash, publish_status, attempts, last_error, "
                        "last_head_seq, next_eligible_at, lease_epoch, lease_owner, "
                        "lease_expires_at FROM ledger_publish_state WHERE id = 1"
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return PinState(
            last_published_seq=row[0], last_version_id=row[1], last_validator=row[2],
            last_head_hash=row[3], publish_status=row[4], attempts=row[5], last_error=row[6],
            last_head_seq=row[7], next_eligible_at=row[8], lease_epoch=row[9],
            lease_owner=row[10], lease_expires_at=row[11],
        )

    async def _set_status(
        self, status: str, error: str | None, *, attempts: int | None = None,
        next_eligible_at: datetime | None = None, last_head_seq: int | None = None,
        last_head_validator: str | None = None,
    ) -> None:
        sets = ["publish_status = :status", "last_error = :error", "updated_at = :now"]
        params: dict[str, object] = {"status": status, "error": error, "now": self._clock()}
        if attempts is not None:
            sets.append("attempts = :attempts")
            params["attempts"] = attempts
        if next_eligible_at is not None:
            sets.append("next_eligible_at = :next_eligible_at")
            params["next_eligible_at"] = next_eligible_at
        if last_head_seq is not None:
            sets.append("last_head_seq = :last_head_seq")
            params["last_head_seq"] = last_head_seq
        if last_head_validator is not None:
            sets.append("last_head_validator = :last_head_validator")
            params["last_head_validator"] = last_head_validator
        async with self._sessionmaker() as session:
            await session.execute(
                text(f"UPDATE ledger_publish_state SET {', '.join(sets)} WHERE id = 1"), params
            )
            await session.commit()

    async def _advance_pin(self, seq: int, version_id: str | None, validator: str | None,
                           head_hash: str) -> None:
        async with self._sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE ledger_publish_state SET last_published_seq = :seq, "
                    "last_version_id = :version_id, last_validator = :validator, "
                    "last_head_hash = :head_hash, attempts = 0, last_error = NULL, "
                    "last_head_seq = NULL, next_eligible_at = NULL, updated_at = :now "
                    "WHERE id = 1"
                ),
                {"seq": seq, "version_id": version_id, "validator": validator,
                 "head_hash": head_hash, "now": self._clock()},
            )
            await session.commit()

    # ---------------------------------------------------------------- lease I/O --
    async def _acquire_lease(self) -> tuple[str, int] | None:
        token = uuid.uuid4().hex
        now = self._clock()
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE ledger_publish_state SET lease_owner = :token, "
                        "lease_expires_at = :expires, lease_epoch = lease_epoch + 1, "
                        "publish_status = :status, updated_at = :now "
                        "WHERE id = 1 AND (lease_owner IS NULL "
                        "OR lease_expires_at IS NULL OR lease_expires_at < :now) "
                        "RETURNING lease_epoch"
                    ),
                    {"token": token, "expires": now + timedelta(seconds=self._lease_seconds),
                     "status": _STATUS_PUBLISHING, "now": now},
                )
            ).one_or_none()
            if row is None:
                return None
            await session.commit()
        return token, row[0]

    async def _revalidate_lease(self, token: str, epoch: int) -> bool:
        """Renew-or-verify ownership. Returns False when the lease was lost."""
        now = self._clock()
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT lease_owner, lease_epoch, lease_expires_at "
                        "FROM ledger_publish_state WHERE id = 1"
                    )
                )
            ).one_or_none()
            if row is None or row[0] != token or row[1] != epoch:
                return False
            if row[2] is None or row[2] < now:
                return False
            if row[2] < now + timedelta(seconds=_LEASE_RENEW_MARGIN_SECONDS):
                await session.execute(
                    text(
                        "UPDATE ledger_publish_state SET lease_expires_at = :expires, "
                        "updated_at = :now WHERE id = 1 AND lease_owner = :token "
                        "AND lease_epoch = :epoch"
                    ),
                    {"expires": now + timedelta(seconds=self._lease_seconds),
                     "now": now, "token": token, "epoch": epoch},
                )
            await session.commit()
        return True

    async def _release_lease(self, token: str, epoch: int) -> None:
        # Only a still-`publishing` pin falls back to idle on release; a status the
        # cycle just persisted (halted/unresolved/reconcile) must survive it.
        async with self._sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE ledger_publish_state SET lease_owner = NULL, "
                    "lease_expires_at = NULL, publish_status = "
                    "CASE WHEN publish_status = :publishing THEN :idle "
                    "ELSE publish_status END, updated_at = :now "
                    "WHERE id = 1 AND lease_owner = :token AND lease_epoch = :epoch"
                ),
                {"publishing": _STATUS_PUBLISHING, "idle": _STATUS_IDLE,
                 "now": self._clock(), "token": token, "epoch": epoch},
            )
            await session.commit()

    # ------------------------------------------------------------- event reads --
    async def _db_head_seq(self) -> int:
        async with self._sessionmaker() as session:
            row = (await session.execute(text("SELECT max(event_seq) FROM purchase_events"))).one()
        return row[0] or 0

    async def _read_event(self, seq: int) -> canonical.EventRecord | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT event_seq, source, source_pk, source_revision, "
                        "payload::text, payload_sha256, created_at "
                        "FROM purchase_events WHERE event_seq = :seq"
                    ),
                    {"seq": seq},
                )
            ).one_or_none()
        if row is None:
            return None
        return canonical.EventRecord(
            event_seq=row[0], source=row[1], source_pk=row[2], source_revision=row[3],
            payload_text=row[4], payload_sha256=row[5], created_at=row[6],
        )

    # ------------------------------------------------------------------- cycle --
    async def run_cycle(self) -> str:
        """One lease-serialized drain. Returns the summary action for logs and tests."""
        pin = await self._read_pin()
        if pin is None:
            logger.error("ledger publisher: ledger_publish_state is missing; refusing to run")
            return "refused-no-pin"

        if pin.publish_status in _TERMINAL_STATUSES:
            logger.error(
                "ledger publisher: publication is %s and requires operator reconciliation "
                "(see the pin's last_error); no publication attempted", pin.publish_status,
            )
            return f"skipped-{pin.publish_status}"

        now = self._clock()
        if (
            pin.publish_status == _STATUS_UNRESOLVED
            and pin.next_eligible_at is not None
            and pin.next_eligible_at > now
        ):
            return "skipped-backoff"

        # WORM gate (4.6.3): before any store interaction this cycle, the bucket must
        # satisfy the evidence-protection policy — versioning Enabled, Object Lock
        # COMPLIANCE with a default retention at or above the floor, and no Enabled
        # lifecycle rule that could expire object versions. It sits after the
        # terminal-status and backoff fast paths so a halted/reconcile pin surfaces
        # ITS reason to the operator, not a store misconfiguration. On failure the
        # cycle refuses WITHOUT touching the pin or the anchor: no status write, no
        # observation, no write attempt, no bootstrap.
        verdict = await self._worm.verify(self._store)
        if not verdict.ok:
            logger.error(
                "ledger ALARM: evidence bucket fails the WORM protection policy; "
                "publication refused and the trust anchor left untouched — %s",
                "; ".join(verdict.failures),
            )
            return "refused-worm-policy"

        db_head = await self._db_head_seq()
        if db_head < pin.last_published_seq:
            await self._set_status(
                _STATUS_RECONCILE,
                f"database head ({db_head}) is behind the published head "
                f"({pin.last_published_seq}); suspected rollback — operator reconciliation "
                "required before any further publication",
            )
            logger.error(
                "ledger ALARM: database head %s behind published head %s — "
                "publication entering reconcile (never rebuild-over)", db_head,
                pin.last_published_seq,
            )
            return "reconcile-db-behind"

        lease = await self._acquire_lease()
        if lease is None:
            logger.info("ledger publisher: another publisher holds the lease; skipping cycle")
            return "lease-busy"
        token, epoch = lease

        try:
            for _ in range(MAX_EVENTS_PER_CYCLE):
                outcome = await self._publish_one(token, epoch)
                if outcome != "ack":
                    return outcome
            return "ack-batch-limit"
        finally:
            await self._release_lease(token, epoch)

    # ---------------------------------------------------------------- per event --
    async def _publish_one(self, token: str, epoch: int) -> str:
        pin = await self._read_pin()
        if pin is None:
            return "refused-no-pin"
        if pin.publish_status in _TERMINAL_STATUSES:
            return f"skipped-{pin.publish_status}"

        event = await self._read_event(pin.last_published_seq + 1)
        if event is None:
            return "idle"
        if canonical.digest_of_text(event.payload_text) != event.payload_sha256:
            await self._set_status(
                _STATUS_HALTED,
                f"event {event.event_seq} payload does not match its own digest; the ledger "
                "row was modified after commit — tampering evidence",
            )
            logger.error("ledger ALARM: event %s digest mismatch — halted", event.event_seq)
            return "refused-digest-mismatch"

        attempts_used = 0
        conflict_rebuilds = 0
        while attempts_used < self._max_attempts:
            if not await self._revalidate_lease(token, epoch):
                logger.error(
                    "ledger publisher: lease lost before publication of event %s; a stale "
                    "publisher must not write", event.event_seq,
                )
                return "lost-lease"

            pin = await self._read_pin()
            if pin is None or pin.publish_status in _TERMINAL_STATUSES:
                return "refused-terminal-pin"

            try:
                observation = await self._observe_head()
            except (ServerError, TransportFailure) as exc:
                attempts_used += 1
                if await self._consume_attempt(event, attempts_used, exc.operation):
                    return "unresolved"
                continue

            if observation is None:
                if pin.last_published_seq == 0:
                    outcome = await self._cas(
                        event, pin, token, epoch, create_only=True,
                        conflict_rebuilds=conflict_rebuilds,
                    )
                else:
                    await self._set_status(
                        _STATUS_HALTED,
                        "the external head object is missing while the pin expects sequence "
                        f"{pin.last_published_seq}; the trust anchor must never be re-created "
                        "automatically — operator investigation required",
                    )
                    logger.error(
                        "ledger ALARM: head object missing at pin seq %s — halted "
                        "(never re-bootstrap)", pin.last_published_seq,
                    )
                    return "refused-missing-head"
            else:
                gate = self._validate_gates(pin, observation, event)
                if gate == "ack-already-published":
                    lineage = await self._verify_anchor_lineage(event, pin)
                    if lineage == "ok":
                        await self._advance_pin(
                            observation.head_seq, observation.version_id, observation.etag,
                            observation.head_hash,
                        )
                        logger.info(
                            "ledger publisher: event %s already published (head at %s); pin "
                            "advanced without a PUT", event.event_seq, observation.head_seq,
                        )
                        return "ack"
                    if lineage == "retry":
                        continue
                    return lineage
                if gate == "proceed":
                    outcome = await self._cas(
                        event, pin, token, epoch, if_match=observation.etag,
                        conflict_rebuilds=conflict_rebuilds,
                    )
                elif gate.startswith("refused"):
                    await self._set_status(
                        _STATUS_HALTED,
                        f"event {event.event_seq}: {gate.replace('-', ' ')} — external head "
                        "disagrees with the database; refusing to publish",
                    )
                    return gate
                else:  # reconcile-external-regression
                    await self._set_status(
                        _STATUS_RECONCILE,
                        f"event {event.event_seq}: external head ({observation.head_seq}) is "
                        f"behind the pin ({pin.last_published_seq}); the predecessor vanished "
                        "— operator reconciliation required, never rebuild-over",
                    )
                    logger.error(
                        "ledger ALARM: external head regression at seq %s (pin %s) — "
                        "reconcile", observation.head_seq, pin.last_published_seq,
                    )
                    return gate

            if outcome == "ack":
                logger.info(
                    "ledger publisher: published event %s (head seq %s)",
                    event.event_seq, event.event_seq,
                )
                return "ack"
            if outcome in ("unresolved", "lost-lease"):
                return outcome
            if outcome.startswith("refused") or outcome.startswith("reconcile"):
                return outcome
            # "retry" — an attempt was consumed and persisted; the loop re-observes.
            if outcome == "conflict-retry":
                conflict_rebuilds += 1

        await self._set_status(
            _STATUS_UNRESOLVED,
            f"event {event.event_seq}: attempt budget exhausted without resolution; operator "
            "reconciliation required",
        )
        return "unresolved"

    # ------------------------------------------------------- anchor lineage --
    async def _verify_anchor_lineage(self, event: canonical.EventRecord,
                                     pin: PinState) -> str:
        """
        The pin's recorded version must still exist in the head object's version
        history. Runs before EVERY pin advance — on a fresh conditional PUT and on the
        ack-already-published recovery alike — because a provider that creates on a
        mismatched conditional (MinIO; conformance S5) turns a mid-race deletion into a
        silently adopted brand-new anchor. Returns "ok" to advance, a refused-*/reconcile
        outcome to stop, or "retry" when the verification itself could not be obtained.
        """
        if pin.last_published_seq == 0:
            return "ok"  # genesis: no recorded predecessor to verify
        if not pin.last_version_id:
            await self._set_status(
                _STATUS_HALTED,
                f"the pin records no version id at sequence {pin.last_published_seq}; the "
                "anchor lineage cannot be verified on a versioned bucket — operator "
                "investigation required",
            )
            logger.error(
                "ledger ALARM: pin records no version id at seq %s — lineage "
                "unverifiable, halted", pin.last_published_seq,
            )
            return "refused-unverifiable-lineage"
        try:
            versions = await self._store.list_head_versions()
        except (ServerError, TransportFailure) as exc:
            attempts_used = pin.attempts + 1
            if await self._consume_attempt(event, attempts_used, exc.operation):
                return "unresolved"
            return "retry"
        if any(v.version_id == pin.last_version_id for v in versions):
            return "ok"
        await self._set_status(
            _STATUS_HALTED,
            f"the pin's recorded version ({pin.last_version_id}) no longer exists in the "
            "head object's version history; the trust anchor was destroyed and something "
            "created a fresh object in its place — tampering evidence, publication halted, "
            "never rebuilt over the loss",
        )
        logger.error(
            "ledger ALARM: anchor lineage destroyed (pin version %s absent from the "
            "version history) — halted, never adopted the fresh object",
            pin.last_version_id,
        )
        return "refused-destroyed-lineage"

    async def _observe_head(self) -> HeadObservation | None:
        result = await self._store.get_head()
        if result is None:
            return None
        body = json.loads(result.body.decode("utf-8"))
        return HeadObservation(
            head_seq=int(body["head_seq"]),
            head_hash=body["head_hash"],
            envelope=body.get("event"),
            etag=result.etag,
            version_id=result.version_id,
        )

    def _validate_gates(self, pin: PinState, obs: HeadObservation,
                        event: canonical.EventRecord) -> str:
        n = pin.last_published_seq
        if obs.head_seq < n:
            return "reconcile-external-regression"
        if obs.head_seq > n + 1:
            logger.error(
                "ledger ALARM: external head at seq %s is ahead of the pin (%s) by more than "
                "one event — divergent head refused", obs.head_seq, n,
            )
            return "refused-divergent-head"
        if obs.head_seq == n:
            if obs.head_hash != pin.last_head_hash:
                logger.error(
                    "ledger ALARM: external head at seq %s carries chain hash %s but the pin "
                    "records %s — forged predecessor refused", n, obs.head_hash,
                    pin.last_head_hash,
                )
                return "refused-forged-predecessor"
            # G3: seq and chain hash match; a differing ETag is a byte-identical re-write
            # of our own content (lost response) — the identity is adopted, not fought.
            return "proceed"
        # obs.head_seq == n + 1: G5 content identity decides.
        envelope = obs.envelope or {}
        if (
            envelope.get("event_seq") == n + 1
            and envelope.get("payload_sha256") == event.payload_sha256
            and envelope.get("prev_hash") == pin.last_head_hash
            and obs.head_hash == canonical.chain_hash(pin.last_head_hash, event.payload_sha256)
        ):
            return "ack-already-published"
        logger.error(
            "ledger ALARM: external head at seq %s carries different content than event %s "
            "(embedded payload digest %s vs ours %s) — rollback proof or foreign writer; "
            "refused", obs.head_seq, event.event_seq, envelope.get("payload_sha256"),
            event.payload_sha256,
        )
        return "refused-divergent-content"

    async def _consume_attempt(self, event: canonical.EventRecord, attempt: int,
                               operation: str) -> bool:
        """Persist the consumed attempt BEFORE the next network call. True when exhausted."""
        exhausted = attempt >= self._max_attempts
        await self._set_status(
            _STATUS_UNRESOLVED if exhausted else _STATUS_PUBLISHING,
            f"publication attempt {attempt} of event {event.event_seq} ended in {operation}; "
            "the outcome is unknown, so the head will be re-observed before any further write",
            attempts=attempt,
            next_eligible_at=self._clock() + timedelta(seconds=_full_jitter_backoff(attempt)),
        )
        if exhausted:
            logger.error(
                "ledger ALARM: publication of event %s exhausted %s attempts — episode "
                "marked unresolved; operator reconciliation required",
                event.event_seq, self._max_attempts,
            )
        return exhausted

    async def _cas(self, event: canonical.EventRecord, pin: PinState, token: str, epoch: int, *,
                   if_match: str | None = None, create_only: bool = False,
                   conflict_rebuilds: int = 0) -> str:
        if not await self._revalidate_lease(token, epoch):
            return "lost-lease"
        envelope = canonical.build_envelope(
            event,
            prev_seq=pin.last_published_seq,
            prev_hash=pin.last_head_hash,
            fence_token=epoch,
        )
        head_body, head_hash = canonical.build_head(pin.last_head_hash, envelope)
        body = canonical.serialize(head_body).encode("utf-8")
        try:
            if create_only:
                result = await self._store.put_head(body, create_only=True)
            else:
                result = await self._store.put_head(body, if_match=if_match)
        except PreconditionFailed:
            return await self._on_conflict(event, pin, "412", conflict_rebuilds)
        except ConflictError:
            return await self._on_conflict(event, pin, "409", conflict_rebuilds)
        except ObjectNotFound:
            await self._set_status(
                _STATUS_HALTED,
                "the head object vanished between observation and the conditional write; the "
                "trust anchor must never be re-created automatically",
            )
            logger.error("ledger ALARM: head vanished mid-publication — halted")
            return "refused-missing-head"
        except (ServerError, TransportFailure) as exc:
            attempts_used = pin.attempts + 1
            if await self._consume_attempt(event, attempts_used, exc.operation):
                return "unresolved"
            return "retry"

        lineage = await self._verify_anchor_lineage(event, pin)
        if lineage != "ok":
            return lineage
        await self._advance_pin(event.event_seq, result.version_id, result.etag, head_hash)
        return "ack"

    async def _on_conflict(self, event: canonical.EventRecord, pin: PinState, kind: str,
                           conflict_rebuilds: int) -> str:
        """
        412/409: someone else moved the head. Never assume — re-observe; the next
        iteration's gates decide (our content at N+1 -> ACK, different content -> REFUSE,
        absent -> one rebuild). A 409 beyond one rebuild-retry is refused (4.5.9 D7).
        """
        if kind == "409" and conflict_rebuilds >= 1:
            await self._set_status(
                _STATUS_HALTED,
                f"event {event.event_seq}: repeated 409 conflicts beyond one rebuild retry; "
                "operator reconciliation required",
            )
            logger.error("ledger ALARM: repeated 409 on event %s — halted", event.event_seq)
            return "refused-repeated-conflict"

        attempts_used = pin.attempts + 1
        exhausted = attempts_used >= self._max_attempts
        await self._set_status(
            _STATUS_UNRESOLVED if exhausted else _STATUS_PUBLISHING,
            f"event {event.event_seq}: conditional write returned {kind}; re-observing the "
            "head before any further write",
            attempts=attempts_used,
            next_eligible_at=self._clock() + timedelta(seconds=_full_jitter_backoff(attempts_used)),
            last_head_seq=pin.last_published_seq,
            last_head_validator=pin.last_validator,
        )
        if exhausted:
            logger.error(
                "ledger ALARM: event %s exhausted %s attempts (last: %s) — unresolved; "
                "operator reconciliation required", event.event_seq, self._max_attempts, kind,
            )
            return "unresolved"
        return "conflict-retry" if kind == "409" else "retry"


def build_store(settings) -> S3ObjectStore:
    return S3ObjectStore(
        endpoint_url=settings.ledger_object_store_endpoint,
        region=settings.ledger_object_store_region,
        bucket=settings.ledger_object_store_bucket,
        access_key=settings.ledger_object_store_access_key,
        secret_key=settings.ledger_object_store_secret_key,
        head_key=settings.ledger_object_store_head_key,
        timeout_seconds=settings.ledger_object_store_timeout_seconds,
    )


def build_publisher(sessionmaker: async_sessionmaker, settings) -> Publisher:
    """
    The production wiring: the WORM enforcement policy is mandatory and non-permissive.
    main.py (and every deployment surface) must construct the publisher through this
    function so no deployment path can accidentally wire a permissive policy.
    """
    return Publisher(
        sessionmaker,
        build_store(settings),
        worm=S3WormPolicy(settings.ledger_worm_min_retention_days),
        lease_seconds=settings.ledger_publisher_lease_seconds,
    )


async def run_forever(sessionmaker: async_sessionmaker, settings) -> None:
    """
    The lifespan background loop. Persisted state is the only source of truth; a crash
    and restart resumes from the pin exactly where the previous process stopped.
    """
    publisher = build_publisher(sessionmaker, settings)
    logger.info(
        "ledger publisher: started (poll=%ss, lease=%ss)",
        settings.ledger_publisher_poll_seconds, settings.ledger_publisher_lease_seconds,
    )
    while True:
        try:
            outcome = await publisher.run_cycle()
            if outcome not in ("idle", "lease-busy", "skipped-backoff", "ack-batch-limit"):
                logger.warning("ledger publisher cycle: %s", outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ledger publisher cycle failed unexpectedly")
        await asyncio.sleep(settings.ledger_publisher_poll_seconds)
