"""
The independent ledger verifier and the submission currency gate (Phase 4.6.2 §5).

The publisher's gates protect the write path. This module is the read path's adversarial
double-check: it re-derives every claim from raw evidence and refuses to trust either
side. It reads the PostgreSQL ledger, recomputes the chain from genesis, walks EVERY
external head version through its own versioned GET, and cross-checks the publish pin.
Nothing here writes — not the database, not the object store.

Verdicts are explicit and count-based; no boolean aggregate over an empty set can make
a check pass silently (mandate §3). An expected element that is absent or incomplete is
a failure, stated as one.

  verdict: healthy      — every check passed (publication lag is a note, not a failure)
           unhealthy    — at least one integrity check failed; findings name each one
           unverifiable — a dependency (database, object store) could not be reached;
                          absence of evidence is not evidence of integrity

CLI:  python -m app.ledger.verify   — exit 0 healthy, 2 unhealthy, 3 unverifiable.

Privacy: findings and summaries carry sequence numbers, digests, counts and object
identities only — never payload text, never credentials.

The submission currency gate (submission_currency) is the same evidence, folded to one
decision for the purchase-submission endpoint (4.5.9 outage policy: new sends need WORM
currency). It requires EXACT equality of database high-water, anchor head and pin, plus
full digest verification; anchor-ahead is a rollback suspicion and refuses. It SKIPS —
honestly, never silently passes — when the ledger is not installed or the store is not
configured.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ledger import canonical
from app.ledger.store import (
    ObjectNotFound,
    S3ObjectStore,
    ServerError,
    TransportFailure,
)

logger = logging.getLogger("app.ledger.verify")

VERDICT_HEALTHY = "healthy"
VERDICT_UNHEALTHY = "unhealthy"
VERDICT_UNVERIFIABLE = "unverifiable"

CURRENCY_CURRENT = "current"
CURRENCY_NOT_CURRENT = "not-current"
CURRENCY_SKIPPED = "skipped"


class LedgerNotInstalled(Exception):
    """The purchase_events / ledger_publish_state tables are absent (pre-migration)."""


@dataclass
class DbFacts:
    events: list[canonical.EventRecord] = field(default_factory=list)
    pin_seq: int | None = None
    pin_version_id: str | None = None
    pin_validator: str | None = None
    pin_head_hash: str | None = None
    pin_present: bool = False


@dataclass
class VerifyReport:
    verdict: str
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    db_high_water: int = 0
    db_event_count: int = 0
    external_head_seq: int | None = None
    external_version_count: int = 0
    pin_seq: int | None = None

    def summary_lines(self) -> list[str]:
        lines = [
            f"verdict: {self.verdict}",
            f"database ledger: {self.db_event_count} events, high-water seq {self.db_high_water}",
            f"external anchor: head seq {self.external_head_seq}, "
            f"{self.external_version_count} live version(s)",
            f"publish pin: seq {self.pin_seq}",
        ]
        for problem in self.problems:
            lines.append(f"PROBLEM: {problem}")
        for note in self.notes:
            lines.append(f"note: {note}")
        return lines


@dataclass(frozen=True)
class CurrencyDecision:
    state: str  # current | not-current | skipped
    reason: str


# --------------------------------------------------------------------------- #
# Database side                                                                #
# --------------------------------------------------------------------------- #

def _is_missing_table(exc: Exception) -> bool:
    if isinstance(exc, ProgrammingError):
        return getattr(exc.orig, "pgcode", None) == "42P01"
    if isinstance(exc, DBAPIError):
        return getattr(exc.orig, "pgcode", None) == "42P01"
    return False


async def collect_db_facts(sessionmaker: async_sessionmaker) -> DbFacts:
    """
    Every ledger row and the pin, read raw. Raises LedgerNotInstalled when the ledger
    schema is absent; other database failures propagate as-is (the caller decides whether
    that means unverifiable). Exception messages are never embedded in reports — a
    database error string can carry connection material.
    """
    try:
        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT event_seq, source, source_pk, source_revision, "
                        "payload::text, payload_sha256, created_at "
                        "FROM purchase_events ORDER BY event_seq"
                    )
                )
            ).all()
            pin_row = (
                await session.execute(
                    text(
                        "SELECT last_published_seq, last_version_id, last_validator, "
                        "last_head_hash FROM ledger_publish_state WHERE id = 1"
                    )
                )
            ).one_or_none()
    except DBAPIError as exc:
        if _is_missing_table(exc):
            raise LedgerNotInstalled(
                "the purchase-event ledger is not installed on this database"
            ) from None
        raise
    facts = DbFacts(
        events=[
            canonical.EventRecord(
                event_seq=row[0], source=row[1], source_pk=row[2], source_revision=row[3],
                payload_text=row[4], payload_sha256=row[5], created_at=row[6],
            )
            for row in rows
        ],
        pin_present=pin_row is not None,
    )
    if pin_row is not None:
        facts.pin_seq = pin_row[0]
        facts.pin_version_id = pin_row[1]
        facts.pin_validator = pin_row[2]
        facts.pin_head_hash = pin_row[3]
    return facts


def recompute_chain(events: list[canonical.EventRecord]) -> tuple[list[str], list[str]]:
    """
    The chain recomputed from the rows alone, and every structural problem found.

    Contiguity is an enumerated position check: event N must sit at position N. This
    catches gaps, duplicates and renumbering individually — an empty event list produces
    no problem and no silent pass beyond it: callers treat "no rows" as its own state.
    The digest check is per-row sha256 of the exact stored payload text, so any
    post-commit mutation of a payload is named with its sequence.
    """
    hashes = [canonical.GENESIS_HASH]
    problems: list[str] = []
    for position, event in enumerate(events, start=1):
        if event.event_seq != position:
            problems.append(
                f"ledger sequence break: position {position} holds event_seq "
                f"{event.event_seq} (gap, duplicate or renumbered ledger)"
            )
        if canonical.digest_of_text(event.payload_text) != event.payload_sha256:
            problems.append(
                f"event {event.event_seq}: payload does not match its stored digest — "
                "the row was modified after commit"
            )
        hashes.append(canonical.chain_hash(hashes[-1], event.payload_sha256))
    return hashes, problems


def _expected_head_hash(events: list[canonical.EventRecord]) -> str:
    hashes, _ = recompute_chain(events)
    return hashes[-1]


# --------------------------------------------------------------------------- #
# External side                                                                #
# --------------------------------------------------------------------------- #

def _parse_head(body: bytes) -> dict | None:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("format") != canonical.HEAD_FORMAT:
        return None
    if not isinstance(parsed.get("head_seq"), int) or not isinstance(parsed.get("head_hash"), str):
        return None
    return parsed


async def _walk_external_versions(
    store: S3ObjectStore,
    facts: DbFacts,
    chain_hashes: list[str],
    problems: list[str],
    notes: list[str],
) -> int:
    """
    Fetch and verify every live version of the head object, oldest first. Returns the
    count of live versions. Each version at 1-based position P must carry head_seq == P,
    a head_hash equal to the recomputed database chain hash at P, and an embedded event
    identical to the database event at P. Extra versions therefore fail against the
    database they claim to summarize, and no version is trusted because another was.
    """
    versions = await store.list_head_versions()
    delete_markers = sum(1 for v in versions if v.is_delete_marker)
    if delete_markers:
        problems.append(
            f"the head object carries {delete_markers} delete marker(s); a deleted "
            "anchor must never be recreated and is an integrity failure"
        )
    live = [v for v in versions if not v.is_delete_marker]
    for position, version in enumerate(live, start=1):
        try:
            fetched = await store.get_version(version.version_id)
        except ObjectNotFound:
            problems.append(
                f"listed head version at position {position} can no longer be fetched"
            )
            continue
        if fetched.etag and version.etag and fetched.etag != version.etag:
            problems.append(
                f"head version at position {position}: listing ETag and object ETag disagree"
            )
        parsed = _parse_head(fetched.body)
        if parsed is None:
            problems.append(
                f"head version at position {position} is not a valid "
                f"{canonical.HEAD_FORMAT} object"
            )
            continue
        if parsed["head_seq"] != position:
            problems.append(
                f"head version at position {position} carries head_seq {parsed['head_seq']}"
            )
            continue
        if parsed["head_seq"] > len(facts.events):
            problems.append(
                f"head version claims sequence {parsed['head_seq']} beyond the database "
                f"ledger ({len(facts.events)} events) — anchor ahead of the database, "
                "rollback suspicion"
            )
            continue
        if parsed["head_hash"] != chain_hashes[position]:
            problems.append(
                f"head version {position}: chain hash disagrees with the recomputed "
                "database chain"
            )
        envelope = parsed.get("event") or {}
        db_event = facts.events[position - 1]
        if envelope.get("event_seq") != position:
            problems.append(f"head version {position}: embedded event_seq mismatch")
        if envelope.get("payload_sha256") != db_event.payload_sha256:
            problems.append(
                f"head version {position}: embedded payload digest disagrees with the "
                f"database event (external {envelope.get('payload_sha256')!r} vs "
                f"database {db_event.payload_sha256!r})"
            )
        if envelope.get("prev_hash") != chain_hashes[position - 1]:
            problems.append(
                f"head version {position}: embedded prev_hash disagrees with the chain"
            )
    return len(live)


# --------------------------------------------------------------------------- #
# Full verification                                                            #
# --------------------------------------------------------------------------- #

async def verify(sessionmaker: async_sessionmaker, store: S3ObjectStore | None) -> VerifyReport:
    report = VerifyReport(verdict=VERDICT_HEALTHY)

    try:
        facts = await collect_db_facts(sessionmaker)
    except LedgerNotInstalled as exc:
        report.verdict = VERDICT_UNVERIFIABLE
        report.problems.append(str(exc))
        report.notes.append("run the owner-ledger migration, then verify again")
        return report
    except Exception as exc:  # noqa: BLE001 — type name only; messages may carry URLs
        report.verdict = VERDICT_UNVERIFIABLE
        report.problems.append(f"the database could not be read: {type(exc).__name__}")
        return report

    chain_hashes, db_problems = recompute_chain(facts.events)
    report.db_event_count = len(facts.events)
    report.db_high_water = len(facts.events)
    report.pin_seq = facts.pin_seq
    report.problems.extend(db_problems)

    if store is None:
        report.notes.append(
            "no external evidence store is configured; external verification skipped"
        )
    else:
        try:
            head = await store.get_head()
            live_count = await _walk_external_versions(
                store, facts, chain_hashes, report.problems, report.notes
            )
        except (ServerError, TransportFailure) as exc:
            report.verdict = VERDICT_UNVERIFIABLE
            report.problems.append(
                f"the external evidence store could not be reached ({exc.operation})"
            )
            return report
        report.external_version_count = live_count

        if head is None:
            if live_count == 0:
                if facts.events:
                    report.notes.append(
                        f"{len(facts.events)} database events await first publication"
                    )
                else:
                    report.notes.append("empty ledger; no anchor exists yet")
            else:
                report.problems.append(
                    "the current head is absent while live versions exist — the latest "
                    "is a delete marker or unreadable"
                )
        else:
            parsed = _parse_head(head.body)
            if parsed is None:
                report.problems.append(
                    f"the current head object is not a valid {canonical.HEAD_FORMAT} object"
                )
            else:
                report.external_head_seq = parsed["head_seq"]
                if parsed["head_seq"] > report.db_high_water:
                    report.problems.append(
                        f"the anchor ({parsed['head_seq']}) is ahead of the database "
                        f"ledger ({report.db_high_water}) — rollback suspicion; "
                        "never rebuild-over, operator reconciliation required"
                    )
                    logger.error(
                        "ledger ALARM: anchor seq %s ahead of database %s",
                        parsed["head_seq"], report.db_high_water,
                    )
                elif parsed["head_seq"] < report.db_high_water:
                    report.notes.append(
                        f"publication lag: anchor at {parsed['head_seq']} of "
                        f"{report.db_high_water}"
                    )
                if live_count and parsed["head_seq"] != live_count:
                    report.problems.append(
                        f"the current head claims seq {parsed['head_seq']} but "
                        f"{live_count} live versions exist"
                    )
                _verify_pin(report, facts, parsed, head)

    if facts.pin_present and facts.pin_seq is not None and facts.pin_seq > report.db_high_water:
        report.problems.append(
            f"the publish pin ({facts.pin_seq}) is ahead of the database ledger "
            f"({report.db_high_water}) — database rollback suspicion"
        )

    if report.problems:
        report.verdict = VERDICT_UNHEALTHY
        logger.error(
            "ledger verification: UNHEALTHY with %d problem(s)", len(report.problems)
        )
    else:
        logger.info("ledger verification: healthy (db high-water %s)", report.db_high_water)
    return report


def _verify_pin(report: VerifyReport, facts: DbFacts, parsed_head: dict, head) -> None:
    head_seq = parsed_head["head_seq"]
    if not facts.pin_present:
        report.problems.append("the publish pin row is missing while an external anchor exists")
        return
    assert facts.pin_seq is not None
    if facts.pin_seq > head_seq:
        report.problems.append(
            f"the pin claims seq {facts.pin_seq} but the anchor is at {head_seq} — the "
            "pin advanced without a surviving anchor version"
        )
        return
    if facts.pin_seq < head_seq:
        report.notes.append(
            f"the pin ({facts.pin_seq}) lags the anchor ({head_seq}); the publisher's "
            "next cycle re-observes and repairs this crash window"
        )
        return
    if facts.pin_version_id != head.version_id or facts.pin_validator != head.etag:
        report.problems.append(
            "the pin's recorded object identity does not match the current head version"
        )
    if facts.pin_head_hash != parsed_head["head_hash"]:
        report.problems.append(
            "the pin's recorded chain hash does not match the current head object"
        )


# --------------------------------------------------------------------------- #
# Submission currency gate (4.5.9 outage policy)                               #
# --------------------------------------------------------------------------- #

async def submission_currency(
    sessionmaker: async_sessionmaker, store: S3ObjectStore | None
) -> CurrencyDecision:
    """
    CURRENT requires positive proof: the database ledger verifies digest-by-digest, the
    anchor's head_seq equals the database high-water EXACTLY (anchor-ahead is a rollback
    suspicion and refuses), the chain hash matches the independent recompute, and the pin
    points at the live head version. Empty ledger AND absent anchor is current — nothing
    exists yet to be outrun. Anything else is not-current with the honest reason, or
    skipped only when the check does not apply at all.
    """
    if store is None:
        return CurrencyDecision(
            CURRENCY_SKIPPED,
            "the external evidence store is not configured for this deployment",
        )
    try:
        facts = await collect_db_facts(sessionmaker)
    except LedgerNotInstalled:
        # A configured store makes the check applicable: a database without the ledger
        # surface cannot prove currency, so this is not-current (fail closed), never
        # skipped — a half-enabled deployment must not silently pass.
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            "the purchase-event ledger is not installed on this database",
        )
    except Exception as exc:  # noqa: BLE001 — type name only
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            f"the ledger could not be read ({type(exc).__name__}); currency cannot be proven",
        )

    chain_hashes, db_problems = recompute_chain(facts.events)
    if db_problems:
        logger.error("ledger ALARM: submission refused on ledger integrity failure")
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            f"the purchase-event ledger failed integrity verification ({db_problems[0]})",
        )

    high_water = len(facts.events)
    try:
        head = await store.get_head()
    except (ServerError, TransportFailure) as exc:
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            f"the external evidence store could not be reached ({exc.operation}); "
            "WORM currency cannot be proven",
        )

    if head is None:
        if high_water == 0 and (not facts.pin_present or (facts.pin_seq or 0) == 0):
            return CurrencyDecision(
                CURRENCY_CURRENT,
                "the ledger is empty and no anchor exists yet; nothing has been published "
                "or awaits publication",
            )
        logger.error(
            "ledger ALARM: anchor absent with %d ledger event(s), pin seq %s",
            high_water, facts.pin_seq,
        )
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            "the external anchor is absent while the ledger holds events; publication has "
            "not caught up or the anchor was lost",
        )

    parsed = _parse_head(head.body)
    if parsed is None:
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            "the external anchor is not a valid ledger head object",
        )
    if parsed["head_seq"] > high_water:
        logger.error(
            "ledger ALARM: anchor seq %s ahead of database %s — rollback suspicion",
            parsed["head_seq"], high_water,
        )
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            f"the anchor ({parsed['head_seq']}) is ahead of the database ledger "
            f"({high_water}) — rollback suspicion; refusing new submissions",
        )
    if parsed["head_seq"] < high_water:
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            f"the anchor is at sequence {parsed['head_seq']} but the database ledger is "
            f"at {high_water}; publication has not caught up",
        )
    if parsed["head_hash"] != chain_hashes[high_water]:
        logger.error("ledger ALARM: anchor chain hash disagrees with the recomputed chain")
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            "the anchor's chain hash disagrees with the database ledger's recomputed chain",
        )
    if not facts.pin_present or facts.pin_seq != high_water:
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            "the publish pin does not confirm the current anchor sequence",
        )
    if facts.pin_version_id != head.version_id or facts.pin_validator != head.etag:
        return CurrencyDecision(
            CURRENCY_NOT_CURRENT,
            "the publish pin's recorded object identity does not match the live anchor "
            "version",
        )
    return CurrencyDecision(
        CURRENCY_CURRENT,
        f"the anchor is exactly current at sequence {high_water} with matching version "
        "identity and verified chain",
    )


async def submission_currency_for_app() -> CurrencyDecision:
    """The gate wired to application defaults; the purchase endpoint calls this."""
    from app.config import get_settings

    settings = get_settings()
    if not (
        settings.ledger_object_store_endpoint and settings.ledger_object_store_bucket
    ):
        return CurrencyDecision(
            CURRENCY_SKIPPED,
            "the external evidence store is not configured for this deployment",
        )
    from app.database import async_session
    from app.ledger.publisher import build_store

    return await submission_currency(async_session, build_store(settings))


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

async def _amain() -> int:
    from app.config import get_settings

    settings = get_settings()
    store = None
    if settings.ledger_object_store_endpoint and settings.ledger_object_store_bucket:
        from app.ledger.publisher import build_store

        store = build_store(settings)
    from app.database import async_session

    report = await verify(async_session, store)
    for line in report.summary_lines():
        print(line)
    return {
        VERDICT_HEALTHY: 0,
        VERDICT_UNHEALTHY: 2,
        VERDICT_UNVERIFIABLE: 3,
    }[report.verdict]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
