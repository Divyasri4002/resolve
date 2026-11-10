"""
Phase 4.6.2 ledger battery — suites D (publisher state machine), E (restore/rollback),
F (crash windows) — against ISOLATED PostgreSQL 16 (see tests/ledger_sandbox.py).

Fault injection uses an in-memory store that reproduces the S3-conditional semantics
the publisher depends on (If-Match compares the ETag, which is deliberately distinct
from the VersionId; create-only genesis; listed versions individually fetchable). The
REAL-provider conformance proof is a separate battery against an isolated MinIO bucket
(test_ledger_s3_conformance.py); nothing here substitutes for it.
"""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.ledger import canonical
from app.ledger.publisher import MAX_EVENTS_PER_CYCLE, Publisher
from app.ledger.store import (
    ConflictError,
    GetResult,
    ObjectNotFound,
    PreconditionFailed,
    PutResult,
    ServerError,
    TransportFailure,
    VersionInfo,
)
from app.ledger.worm import PermissiveWormPolicy
from app.ledger.verify import (
    CURRENCY_CURRENT,
    VERDICT_HEALTHY,
    VERDICT_UNHEALTHY,
    submission_currency,
    verify,
)
from tests.ledger_sandbox import ADMIN_URL, LedgerSandbox

pytestmark = pytest.mark.skipif(
    not ADMIN_URL,
    reason="RESOLVE_PG_ADMIN_URL is not set; the ledger battery needs isolated PostgreSQL 16",
)


# --------------------------------------------------------------------------- #
# Fakes and fixtures                                                           #
# --------------------------------------------------------------------------- #

class FakeStore:
    """Head object with S3-conditional semantics; ETag is never the VersionId."""

    def __init__(self) -> None:
        self._versions: list[dict] = []
        self.put_calls: list[dict] = []
        self.get_calls = 0
        self.fail_next_put: list[Exception] = []
        self.fail_put_forever: Exception | None = None
        self.fail_get: Exception | None = None
        self.fail_next_list: list[Exception] = []

    def _current(self) -> dict | None:
        # S3 versioned-bucket visibility: the LATEST version decides — a delete
        # marker on top makes the object absent even though older versions remain.
        return self._versions[-1] if self._versions and not self._versions[-1]["delete_marker"] else None

    async def get_head(self) -> GetResult | None:
        self.get_calls += 1
        if self.fail_get is not None:
            raise self.fail_get
        v = self._current()
        if v is None:
            return None
        return GetResult(body=v["body"], etag=v["etag"], version_id=v["version_id"])

    async def put_head(self, body: bytes, *, if_match: str | None = None,
                       create_only: bool = False) -> PutResult:
        self.put_calls.append({"if_match": if_match, "create_only": create_only})
        if self.fail_put_forever is not None:
            raise self.fail_put_forever
        if self.fail_next_put:
            raise self.fail_next_put.pop(0)
        current = self._current()
        if create_only:
            if current is not None:
                raise ConflictError(409, "PUT")
        elif current is None:
            raise ObjectNotFound(404, "PUT")
        elif current["etag"] != if_match:
            raise PreconditionFailed(412, "PUT")
        n = len(self._versions) + 1
        version_id = f"vid-{n:04d}"
        etag = f'"etag-{n:04d}-not-a-version-id"'
        self._versions.append(
            {"version_id": version_id, "etag": etag, "body": bytes(body),
             "delete_marker": False}
        )
        return PutResult(etag=etag, version_id=version_id)

    async def get_version(self, version_id: str) -> GetResult:
        for v in self._versions:
            if v["version_id"] == version_id and not v["delete_marker"]:
                return GetResult(body=v["body"], etag=v["etag"], version_id=v["version_id"])
        raise ObjectNotFound(404, "GET")

    async def list_head_versions(self) -> list[VersionInfo]:
        if self.fail_next_list:
            raise self.fail_next_list.pop(0)
        return [
            VersionInfo(
                version_id=v["version_id"], etag=v["etag"],
                is_latest=(i == len(self._versions) - 1),
                is_delete_marker=v["delete_marker"],
            )
            for i, v in enumerate(self._versions)
        ]

    def delete_latest(self) -> None:
        self._versions.append(
            {"version_id": f"vid-dm-{len(self._versions) + 1}", "etag": "",
             "body": b"", "delete_marker": True}
        )

    def clear(self) -> None:
        self._versions = []

    def version_bodies(self) -> list[bytes]:
        return [v["body"] for v in self._versions if not v["delete_marker"]]


class MinioRaceStore(FakeStore):
    """
    The documented provider divergence (conformance S5): MinIO accepts a conditional
    PUT whose If-Match cannot match — INCLUDING a missing object — and CREATES a fresh
    object version (AWS S3 answers 404). Optionally destroys every version right after
    the next successful observation, producing exactly the observe-to-write race the
    publisher's lineage verification must survive. The orphan object gets a version id
    from a fresh id space, as a real provider would: a recreated object's versions are
    unrelated to the destroyed ones.
    """

    def __init__(self) -> None:
        super().__init__()
        self.destroy_after_next_observe = False

    async def get_head(self) -> GetResult | None:
        result = await super().get_head()
        if self.destroy_after_next_observe and result is not None:
            self.destroy_after_next_observe = False
            self._versions = []
        return result

    async def put_head(self, body: bytes, *, if_match: str | None = None,
                       create_only: bool = False) -> PutResult:
        if create_only or if_match is None or self._current() is not None:
            return await super().put_head(body, if_match=if_match, create_only=create_only)
        # MinIO on a missing object: no 404 — the PUT creates a brand-new object.
        self.put_calls.append({"if_match": if_match, "create_only": False})
        n = len([v for v in self._versions if v["version_id"].startswith("vid-orphan")]) + 1
        version_id = f"vid-orphan-{n:04d}"
        etag = f'"etag-orphan-{n:04d}-not-a-version-id"'
        self._versions.append(
            {"version_id": version_id, "etag": etag, "body": bytes(body),
             "delete_marker": False}
        )
        return PutResult(etag=etag, version_id=version_id)


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 11, 9, 12, 0, 0)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@pytest.fixture
async def pub_pg():
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


async def seed_pa_transition(sb, *, order_id: str, mission_id: str, status: str) -> None:
    """One PA row (INSERT is backfill scope, not a trigger event) then one qualifying
    UPDATE — exactly one ledger event."""
    from app.models.db_models import PurchaseAuthorizationRecord

    async with sb.sessions() as session:
        pa = PurchaseAuthorizationRecord(
            order_id=order_id, mission_id=mission_id, user_id="user_seed",
            authority_decision="approved", decision_reason="seed row", status="authorized",
        )
        session.add(pa)
        await session.commit()
        await session.execute(
            text("UPDATE purchase_authorizations SET status = :s "
                 "WHERE authorization_id = :pk"),
            {"s": status, "pk": pa.authorization_id},
        )
        await session.commit()


async def seed_event(sb, tag: str) -> None:
    await seed_pa_transition(
        sb, order_id=f"ord_{tag}", mission_id=f"m_{tag}", status="purchase_failed"
    )


async def set_pin(sb, **values) -> None:
    """The app's C6-writable pin surface, used to stage crash windows."""
    assignments = ", ".join(f"{k} = :{k}" for k in values)
    async with sb.sessions() as session:
        await session.execute(
            text(f"UPDATE ledger_publish_state SET {assignments} WHERE id = 1"),
            dict(values),
        )
        await session.commit()


async def read_pin(sb) -> dict:
    async with sb.sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT last_published_seq, last_version_id, last_validator, "
                    "last_head_hash, publish_status, attempts, next_eligible_at, "
                    "lease_epoch, lease_owner, lease_expires_at, last_error "
                    "FROM ledger_publish_state WHERE id = 1"
                )
            )
        ).one()
    return {
        "published": row[0], "version_id": row[1], "validator": row[2],
        "head_hash": row[3], "status": row[4], "attempts": row[5],
        "next_eligible_at": row[6], "lease_epoch": row[7], "lease_owner": row[8],
        "lease_expires_at": row[9], "last_error": row[10],
    }


def make_publisher(sb, store, clock) -> Publisher:
    # The publisher state machine under test is WORM-independent by design; enforcement
    # itself is battery-tested in test_ledger_worm.py and the conformance W battery.
    return Publisher(sb.sessions, store, worm=PermissiveWormPolicy(),
                     lease_seconds=60.0, clock=clock)


# --------------------------------------------------------------------------- #
# Suite D — the publication state machine                                      #
# --------------------------------------------------------------------------- #

async def test_d1_genesis_then_advance_with_etag_conditionals(pub_pg):
    sb, clock = pub_pg, FakeClock()
    store = FakeStore()
    publisher = make_publisher(sb, store, clock)

    await seed_event(sb, "d1a")
    assert await publisher.run_cycle() == "idle"  # published, then drained
    assert store.put_calls[0]["create_only"] is True
    pin = await read_pin(sb)
    assert pin["published"] == 1 and pin["status"] == "idle"
    assert pin["lease_owner"] is None and pin["attempts"] == 0

    await seed_event(sb, "d1b")
    assert await publisher.run_cycle() == "idle"  # drained both, nothing left
    assert len(store.put_calls) == 2
    second = store.put_calls[1]
    assert not second["create_only"]
    # 4.5.7: If-Match carries the observed ETag — never the VersionId, which is
    # deliberately a different string in this store.
    assert second["if_match"] != f"vid-{len(store._versions):04d}"
    head = await store.get_head()
    # The conditional chain: each write's If-Match is the ETag the previous write
    # returned (the observed head at that moment); the live head now carries the
    # second write's fresh ETag.
    assert second["if_match"] == store._versions[0]["etag"]
    assert head.etag == store._versions[1]["etag"]

    report = await verify(sb.sessions, store)
    assert report.verdict == VERDICT_HEALTHY, report.problems
    parsed = json.loads(head.body)
    assert parsed["head_seq"] == 2


async def test_d2_crash_after_put_is_repaired_without_a_second_write(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "d2")
    await publisher.run_cycle()
    assert len(store.put_calls) == 1

    # Crash window: the PUT landed, the pin advance did not.
    await set_pin(sb, last_published_seq=0, last_version_id=None, last_validator=None,
                  last_head_hash="0" * 64)
    outcome = await publisher.run_cycle()
    assert outcome == "idle"
    assert len(store.put_calls) == 1  # G5 recognized our own content; no second PUT
    pin = await read_pin(sb)
    assert pin["published"] == 1 and pin["status"] == "idle"


async def test_d3_concurrent_publishers_never_double_publish(pub_pg):
    import asyncio

    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    await seed_event(sb, "d3a")
    await seed_event(sb, "d3b")
    p1, p2 = make_publisher(sb, store, clock), make_publisher(sb, store, clock)
    outcomes = await asyncio.gather(p1.run_cycle(), p2.run_cycle())
    assert sorted(o == "lease-busy" for o in outcomes) == [False, True]
    assert len(store._versions) == 2
    await p1.run_cycle()  # drain whatever remains
    assert len(store._versions) == 2


async def test_d4_a_stale_publisher_never_writes(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "d4")
    lease = await publisher._acquire_lease()
    assert lease is not None
    token, epoch = lease
    # Ownership is taken away: a stolen/epoch-bumped lease invalidates the holder.
    await set_pin(sb, lease_owner="thief", lease_epoch=epoch + 1)
    outcome = await publisher._publish_one(token, epoch)
    assert outcome == "lost-lease"
    assert store.put_calls == []


async def test_d5_412_reobserves_then_publishes(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    store.fail_next_put = [PreconditionFailed(412, "PUT")]
    await seed_event(sb, "d5")
    assert await publisher.run_cycle() in ("ack", "idle")
    assert len(store.put_calls) == 2
    pin = await read_pin(sb)
    assert pin["published"] == 1 and pin["attempts"] == 0 and pin["status"] == "idle"


async def test_d6_repeated_409_halts_and_stays_halted(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    store.fail_next_put = [ConflictError(409, "PUT"), ConflictError(409, "PUT")]
    await seed_event(sb, "d6")
    outcome = await publisher.run_cycle()
    assert outcome == "refused-repeated-conflict"
    assert len(store.put_calls) == 2  # one beyond observation, then refusal
    pin = await read_pin(sb)
    assert pin["status"] == "halted" and pin["published"] == 0
    # Terminal states are operator-owned.
    assert await publisher.run_cycle() == "skipped-halted"
    assert len(store.put_calls) == 2


async def test_d7_5xx_exhausts_the_budget_then_recovers(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    store.fail_get = ServerError(503, "GET")
    await seed_event(sb, "d7")
    assert await publisher.run_cycle() == "unresolved"
    assert store.put_calls == []  # observation failures never wrote
    pin = await read_pin(sb)
    assert pin["status"] == "unresolved" and pin["attempts"] == 3
    assert pin["next_eligible_at"] is not None

    store.fail_get = None
    clock.advance(120)
    assert await publisher.run_cycle() == "idle"
    pin = await read_pin(sb)
    assert pin["published"] == 1 and pin["attempts"] == 0 and pin["status"] == "idle"


async def test_d8_missing_head_is_never_rebootstrapped(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "d8a")
    await publisher.run_cycle()
    genesis_writes = sum(1 for c in store.put_calls if c["create_only"])
    assert genesis_writes == 1

    store.clear()  # the whole anchor vanished
    await seed_event(sb, "d8b")
    assert await publisher.run_cycle() == "refused-missing-head"
    pin = await read_pin(sb)
    assert pin["status"] == "halted"
    assert sum(1 for c in store.put_calls if c["create_only"]) == 1  # no re-genesis


async def test_d9_forged_predecessor_is_refused(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "d9")
    forged = {
        "format": canonical.HEAD_FORMAT, "head_seq": 0, "head_hash": "f" * 64,
        "event": None,
    }
    await store.put_head(
        canonical.serialize(forged).encode("utf-8"), create_only=True
    )
    assert await publisher.run_cycle() == "refused-forged-predecessor"
    pin = await read_pin(sb)
    assert pin["status"] == "halted"
    assert len(store._versions) == 1


async def test_d10_divergent_head_ahead_is_refused(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "d10")
    foreign = {
        "format": canonical.HEAD_FORMAT, "head_seq": 5, "head_hash": "a" * 64,
        "event": None,
    }
    await store.put_head(canonical.serialize(foreign).encode("utf-8"), create_only=True)
    assert await publisher.run_cycle() == "refused-divergent-head"
    assert (await read_pin(sb))["status"] == "halted"


async def test_d11_external_regression_enters_reconcile(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "d11a")
    await seed_event(sb, "d11b")
    await publisher.run_cycle()
    assert (await read_pin(sb))["published"] == 2

    # The predecessor reappears as the latest head: the anchor lost its tip.
    async with sb.sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT event_seq, source, source_pk, source_revision, payload::text, "
                    "payload_sha256, created_at FROM purchase_events WHERE event_seq = 1"
                )
            )
        ).one()
    record = canonical.EventRecord(
        event_seq=row[0], source=row[1], source_pk=row[2], source_revision=row[3],
        payload_text=row[4], payload_sha256=row[5], created_at=row[6],
    )
    envelope = canonical.build_envelope(
        record, prev_seq=0, prev_hash=canonical.GENESIS_HASH, fence_token=1
    )
    body, _ = canonical.build_head(canonical.GENESIS_HASH, envelope)
    store._versions.append(
        {"version_id": "vid-rewound", "etag": '"rewound"', "body": canonical.serialize(body).encode("utf-8"),
         "delete_marker": False}
    )

    await seed_event(sb, "d11c")  # a pending event makes the publisher look at the head
    outcome = await publisher.run_cycle()
    assert outcome == "reconcile-external-regression"
    pin = await read_pin(sb)
    assert pin["status"] == "reconcile"
    assert len(store._versions) == 3  # nothing was overwritten
    assert await publisher.run_cycle() == "skipped-reconcile"


async def test_d12_pin_ahead_of_database_reconciles(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "d12")
    await publisher.run_cycle()
    await set_pin(sb, last_published_seq=5)  # simulated database rollback
    assert await publisher.run_cycle() == "reconcile-db-behind"
    assert (await read_pin(sb))["status"] == "reconcile"


async def test_d13_tampered_pending_event_halts(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "d13")
    await publisher.run_cycle()
    await seed_event(sb, "d13b")
    await sb.admin_scalar(
        "UPDATE purchase_events SET payload = payload || '{\"x\": 1}' WHERE event_seq = 2"
    )
    assert await publisher.run_cycle() == "refused-digest-mismatch"
    assert (await read_pin(sb))["status"] == "halted"


async def test_d14_put_failures_exhaust_then_recover(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    store.fail_put_forever = ServerError(500, "PUT")
    await seed_event(sb, "d14")
    assert await publisher.run_cycle() == "unresolved"
    assert len(store.put_calls) == 3  # the bounded budget
    pin = await read_pin(sb)
    assert pin["attempts"] == 3 and pin["status"] == "unresolved"

    store.fail_put_forever = None
    clock.advance(60)
    assert await publisher.run_cycle() == "idle"
    pin = await read_pin(sb)
    assert pin["published"] == 1 and pin["attempts"] == 0


async def test_d15_cycle_batch_limit_is_bounded(pub_pg, monkeypatch):
    import app.ledger.publisher as publisher_module

    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    monkeypatch.setattr(publisher_module, "MAX_EVENTS_PER_CYCLE", 2)
    assert MAX_EVENTS_PER_CYCLE == 50  # the constant itself stays bounded by default
    for i in range(5):
        await seed_event(sb, f"d15_{i}")
    assert await publisher.run_cycle() == "ack-batch-limit"
    assert (await read_pin(sb))["published"] == 2
    await publisher.run_cycle()
    await publisher.run_cycle()
    assert (await read_pin(sb))["published"] == 5


# --------------------------------------------------------------------------- #
# Suite E — restore / rollback                                                 #
# --------------------------------------------------------------------------- #

async def test_e1_a_restored_database_cannot_overwrite_the_anchor(pub_pg):
    sb_a, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher_a = make_publisher(sb_a, store, clock)
    await seed_event(sb_a, "e1a")
    await seed_event(sb_a, "e1b")
    await publisher_a.run_cycle()
    anchor_bodies = store.version_bodies()
    assert len(anchor_bodies) == 2

    # The "restored" database: an empty ledger pointed at the SAME anchor.
    sb_b = LedgerSandbox()
    await sb_b.prepare_roles()
    await sb_b.create()
    try:
        await sb_b.bootstrap()
        await sb_b.run_role_separation()
        await sb_b.run_ledger_install()
        sb_b.sessions = sb_b.app_engine()
        publisher_b = make_publisher(sb_b, store, clock)
        assert await publisher_b.run_cycle() == "idle"  # nothing pending; store untouched
        assert store.version_bodies() == anchor_bodies

        report = await verify(sb_b.sessions, store)
        assert report.verdict == VERDICT_UNHEALTHY
        assert any("ahead of the database" in p for p in report.problems)
        decision = await submission_currency(sb_b.sessions, store)
        assert decision.state == "not-current"
        assert "ahead" in decision.reason

        # With one local event, the divergent head is refused and the anchor survives.
        await seed_event(sb_b, "e1c")
        assert await publisher_b.run_cycle() == "refused-divergent-head"
        assert store.version_bodies() == anchor_bodies
    finally:
        if hasattr(sb_b, "sessions"):
            await sb_b.dispose_engines()
        await sb_b.drop()


async def test_e2_deleted_anchor_refuses_and_never_recreates(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "e2")
    await publisher.run_cycle()
    before = len(store._versions)
    store.delete_latest()

    report = await verify(sb.sessions, store)
    assert report.verdict == VERDICT_UNHEALTHY
    assert any("delete marker" in p for p in report.problems)

    # A pending event forces the publisher to observe the (now absent) head.
    await seed_event(sb, "e2b")
    assert await publisher.run_cycle() == "refused-missing-head"
    assert (await read_pin(sb))["status"] == "halted"
    assert len(store._versions) == before + 1  # only the marker; no recreation


async def test_e3_publication_lag_refuses_then_catches_up(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "e3a")
    await publisher.run_cycle()
    await seed_event(sb, "e3b")

    decision = await submission_currency(sb.sessions, store)
    assert decision.state == "not-current"
    assert "caught up" in decision.reason

    await publisher.run_cycle()
    decision = await submission_currency(sb.sessions, store)
    assert decision.state == CURRENCY_CURRENT
    report = await verify(sb.sessions, store)
    assert report.verdict == VERDICT_HEALTHY, report.problems


# --------------------------------------------------------------------------- #
# Suite F — crash windows and restart                                          #
# --------------------------------------------------------------------------- #

async def test_f1_restart_resumes_from_the_persisted_pin(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher1 = make_publisher(sb, store, clock)
    await seed_event(sb, "f1a")
    await publisher1.run_cycle()
    assert len(store._versions) == 1

    publisher2 = make_publisher(sb, store, clock)  # a fresh process, same state
    await seed_event(sb, "f1b")
    await publisher2.run_cycle()
    assert len(store._versions) == 2
    assert (await read_pin(sb))["published"] == 2
    await publisher2.run_cycle()  # nothing duplicated on a quiet cycle
    assert len(store._versions) == 2


async def test_f2_expired_lease_is_taken_over_after_expiry_only(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "f2")
    await set_pin(sb, lease_owner="ghost", lease_epoch=3, publish_status="publishing",
                  lease_expires_at=clock() + timedelta(seconds=60))

    assert await publisher.run_cycle() == "lease-busy"  # still live
    assert store.put_calls == []

    clock.advance(61)
    assert await publisher.run_cycle() == "idle"  # expired: takeover, publish, release
    pin = await read_pin(sb)
    assert pin["published"] == 1
    assert pin["lease_owner"] is None and pin["lease_epoch"] == 4


async def test_f3_crashed_publisher_leaves_no_double_publication(pub_pg):
    sb, store, clock = pub_pg, FakeStore(), FakeClock()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "f3")
    # A crashed holder: lease row present, expiry in the future, no publication yet.
    await set_pin(sb, lease_owner="ghost", lease_epoch=1, publish_status="publishing",
                  lease_expires_at=clock() + timedelta(seconds=60))
    assert await publisher.run_cycle() == "lease-busy"
    # The ghost expires without ever writing; the next publisher takes over cleanly.
    clock.advance(120)
    await publisher.run_cycle()
    assert len(store._versions) == 1
    head = await store.get_head()
    assert json.loads(head.body)["head_seq"] == 1


# --------------------------------------------------------------------------- #
# Suite G — anchor lineage (the S5 provider-divergence race)                   #
# --------------------------------------------------------------------------- #

async def test_g1_a_minio_style_midrace_create_never_adopts_a_fresh_anchor(pub_pg):
    """
    The observe-to-write race on a divergent provider: the head is destroyed after the
    observation and the conditional PUT lands on a brand-new object (MinIO creates
    where AWS S3 would answer 404). The publisher must refuse to adopt the fresh
    anchor: halted, pin never advanced, event 2 never published onto history-less
    storage — and every later cycle stays stopped.
    """
    sb, clock = pub_pg, FakeClock()
    store = MinioRaceStore()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "g1a")
    assert await publisher.run_cycle() == "idle"  # genesis: pin (1, vid-0001)
    await seed_event(sb, "g1b")
    store.destroy_after_next_observe = True
    assert await publisher.run_cycle() == "refused-destroyed-lineage"
    pin = await read_pin(sb)
    assert pin["published"] == 1 and pin["version_id"] == "vid-0001"
    assert pin["status"] == "halted"
    assert "destroyed" in (pin["last_error"] or "")
    # The divergence did happen on the wire: the orphan object exists, holding ONLY our
    # event-2 envelope with no genesis behind it — and the pin never committed to it.
    versions = await store.list_head_versions()
    assert [v.version_id for v in versions] == ["vid-orphan-0001"]
    assert json.loads(store._versions[0]["body"])["head_seq"] == 2
    # Terminal: the halted pin stops every later cycle; the orphan gains no versions.
    assert await publisher.run_cycle() == "skipped-halted"
    assert len(store._versions) == 1


async def test_g2_our_own_content_on_a_recreated_object_is_refused(pub_pg):
    """
    G5 content identity alone cannot prove lineage: a destroyed-then-recreated head
    holding exactly our lost-response write passes every content gate, because the
    content IS ours. The lineage check refuses it — the pin's version is no longer in
    the object's history.
    """
    sb, clock = pub_pg, FakeClock()
    store = FakeStore()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "g2a")
    await publisher.run_cycle()
    predecessor_hash = (await read_pin(sb))["head_hash"]
    await seed_event(sb, "g2b")
    await publisher.run_cycle()  # event 2 published normally (pin 2, vid-0002)
    # Rewind the pin to the pre-crash state and destroy the pinned version: the store
    # now holds exactly what a lost response would have left on a fresh object.
    await set_pin(sb, last_published_seq=1, last_version_id="vid-0001",
                  last_validator='"etag-0001-not-a-version-id"',
                  last_head_hash=predecessor_hash)
    store._versions = [v for v in store._versions if v["version_id"] != "vid-0001"]
    assert [v.version_id for v in await store.list_head_versions()] == ["vid-0002"]
    assert await publisher.run_cycle() == "refused-destroyed-lineage"
    pin = await read_pin(sb)
    assert pin["published"] == 1 and pin["version_id"] == "vid-0001"
    assert pin["status"] == "halted"


async def test_g3_unverifiable_lineage_consumes_an_attempt_and_recovers(pub_pg):
    """
    A lineage listing that cannot be obtained after a successful PUT is an unknown
    outcome: no pin advance, one honestly consumed attempt, and clean recovery once
    the listing works — the same discipline as a 5xx on the write itself.
    """
    sb, clock = pub_pg, FakeClock()
    store = FakeStore()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "g3a")
    await publisher.run_cycle()
    await seed_event(sb, "g3b")
    store.fail_next_list.append(TransportFailure(None, "GET"))
    assert await publisher.run_cycle() == "idle"
    pin = await read_pin(sb)
    assert pin["published"] == 2 and pin["attempts"] == 0
    assert pin["version_id"] == "vid-0002"
    assert pin["status"] == "idle"


async def test_g4_the_verifier_reads_a_destroyed_anchor_as_destroyed(pub_pg):
    """
    The verifier's blind spot, closed: a pin that records published events while the
    external store holds NOTHING is not 'awaiting first publication' — it is a
    destroyed trust anchor and must be UNHEALTHY (the S12 window's read-side twin).
    The genuine pre-publication state — pin at 0, events pending, no anchor — stays a
    healthy note.
    """
    sb, clock = pub_pg, FakeClock()
    store = FakeStore()
    publisher = make_publisher(sb, store, clock)
    await seed_event(sb, "g4a")
    await publisher.run_cycle()  # pin (1, vid-0001)
    await seed_event(sb, "g4b")
    await publisher.run_cycle()  # pin (2, vid-0002)

    # Every version destroyed (bucket-admin deletion): the store is empty while the
    # pin still claims two published events.
    store.clear()
    assert await store.get_head() is None
    assert await store.list_head_versions() == []
    report = await verify(sb.sessions, store)
    assert report.verdict == VERDICT_UNHEALTHY, report.summary_lines()
    assert any("anchor version survives" in problem for problem in report.problems), (
        report.problems
    )
    assert not any("await first publication" in note for note in report.notes)
    decision = await submission_currency(sb.sessions, store)
    assert decision.state == "not-current"
    assert "absent" in decision.reason

    # The genuine pre-publication state — pin rewound to 0, events pending, no
    # anchor — is still the healthy note, not a problem.
    await set_pin(sb, last_published_seq=0, last_version_id=None, last_validator=None,
                  last_head_hash=canonical.GENESIS_HASH)
    report = await verify(sb.sessions, store)
    assert report.verdict == VERDICT_HEALTHY, report.summary_lines()
    assert any("await first publication" in note for note in report.notes), report.notes
