"""
Real S3-compatible conformance battery (Phase 4.6.2 §8) — against MINIO.

The mandate is explicit: mocks and in-memory fakes cannot substitute for a real
S3-compatible server's conditional-write semantics. This battery runs the production
S3ObjectStore against a throwaway MinIO container and an isolated, VERSIONED test
bucket, and proves the exact semantics the publisher and verifier depend on:

  S1  a versioned bucket: every PUT creates a NEW version; ETag and VersionId are
      distinct validators (4.5.7's correction is a real-server property, not an assumption)
  S2  genesis: If-None-Match:* succeeds on an absent object and is REFUSED on an
      existing one (a conflict exception — the server decides which of 412/409)
  S3  If-Match carries the observed ETag and succeeds against the current version
  S4  a stale If-Match is a 412 PreconditionFailed
  S5  a conditional PUT against an absent object is a 404 — never a silent create
  S6  version preservation: the listing is oldest-first, every historical version is
      individually fetchable with its own body, and IsLatest marks exactly the newest
  S7  concurrent conditional writes: two writers racing the same If-Match yield exactly
      one 200 and one 412; the loser creates no version
  S8  transport failures (connection refused) classify as TransportFailure — the
      publisher's "outcome unknown, re-observe" trigger
  S9  end-to-end: the REAL publisher against the REAL MinIO bucket and an isolated
      PostgreSQL 16 ledger — genesis, advance, verifier healthy, currency gate current,
      publication lag refused then caught up (needs RESOLVE_PG_ADMIN_URL as well)
  S10 the observe-to-write race ARMED against the real divergent provider: MinIO
      accepts the mid-race conditional PUT and creates a fresh object (the S5
      divergence); the publisher must refuse to adopt the history-less orphan and
      halt (needs RESOLVE_PG_ADMIN_URL as well)
  S11 the FINAL-CHALLENGE deletion race, placed after a completed lineage-verified
      publication and before the next cycle: the next observation finds no head and
      refuses (refused-missing-head) BEFORE any PUT — MinIO creates no new head, the
      pin never moves (needs RESOLVE_PG_ADMIN_URL as well)
  S12 the only window where destruction lands after a successful lineage check —
      between a truthful lineage listing and the pin-advance commit: the pin DOES
      advance onto evidence destroyed moments later (the honest residual); contained
      fail-closed by the next cycle's refusal, the currency gate and the verifier
      (needs RESOLVE_PG_ADMIN_URL as well)

The FINAL-CHALLENGE mandated literal interleaving (read head, verify pinned
VersionId, delete everything, then let the conditional PUT execute) cannot be
constructed on this write path: Publisher._cas orders the conditional PUT BEFORE the
lineage verification, so no lineage success ever authorizes a later PUT. S10 covers
the destruction window between observation and PUT; S12 covers the only window after
lineage — lineage-ok to pin-commit — and reports its outcome without spin.

PROVIDER IDENTITY: this battery is evidence about MinIO. It must never be reported as
AWS S3 conformance. The bucket is a disposable test bucket; nothing here touches
production storage.

Skips unless RESOLVE_S3_CONFORMANCE_ENDPOINT / _BUCKET / _ACCESS_KEY / _SECRET_KEY are
all set (S9 additionally needs RESOLVE_PG_ADMIN_URL) — following the M21 convention that
environment-dependent batteries skip honestly rather than fake their preconditions.
"""
import asyncio
import json
import os
import uuid
from urllib.parse import quote

import pytest

from app.ledger import canonical
from app.ledger.publisher import Publisher
from app.ledger.store import (
    ConflictError,
    ObjectNotFound,
    PreconditionFailed,
    S3ObjectStore,
    TransportFailure,
)
from app.ledger.worm import PermissiveWormPolicy
from app.ledger.verify import (
    CURRENCY_CURRENT,
    VERDICT_HEALTHY,
    VERDICT_UNHEALTHY,
    submission_currency,
    verify,
)

_S3_ENV = (
    "RESOLVE_S3_CONFORMANCE_ENDPOINT",
    "RESOLVE_S3_CONFORMANCE_BUCKET",
    "RESOLVE_S3_CONFORMANCE_ACCESS_KEY",
    "RESOLVE_S3_CONFORMANCE_SECRET_KEY",
)

_env = {name: os.environ.get(name, "") for name in _S3_ENV}
S3_CONFIGURED = all(_env.values())

pytestmark = pytest.mark.skipif(
    not S3_CONFIGURED,
    reason=(
        "RESOLVE_S3_CONFORMANCE_* is not set; the real-provider conformance battery "
        "needs an explicitly configured isolated MinIO bucket"
    ),
)


def make_store(head_key: str, **overrides) -> S3ObjectStore:
    return S3ObjectStore(
        endpoint_url=overrides.get("endpoint_url", _env["RESOLVE_S3_CONFORMANCE_ENDPOINT"]),
        region="us-east-1",
        bucket=overrides.get("bucket", _env["RESOLVE_S3_CONFORMANCE_BUCKET"]),
        access_key=overrides.get("access_key", _env["RESOLVE_S3_CONFORMANCE_ACCESS_KEY"]),
        secret_key=overrides.get("secret_key", _env["RESOLVE_S3_CONFORMANCE_SECRET_KEY"]),
        head_key=head_key,
        timeout_seconds=overrides.get("timeout_seconds", 10.0),
    )


def unique_key() -> str:
    return f"conformance/{uuid.uuid4().hex}/head.json"


def body(seq: int) -> bytes:
    return canonical.serialize(
        {
            "format": canonical.HEAD_FORMAT,
            "head_seq": seq,
            "head_hash": f"{seq:064x}",
            "event": None,
        }
    ).encode("utf-8")


# --------------------------------------------------------------------------- #
# S1-S8: the store client against the real server                              #
# --------------------------------------------------------------------------- #


async def test_s1_versioned_bucket_etag_and_version_id_are_distinct_validators():
    store = make_store(unique_key())
    result = await store.put_head(body(1), create_only=True)
    assert result.etag, "the server returned no ETag"
    assert result.version_id, (
        "the server returned no VersionId: the conformance bucket must be "
        "VERSIONED (mc version enable) — conditional ledger writes cannot be "
        "proven on an unversioned bucket"
    )
    # 4.5.7: If-Match is validated against the ETag; recording the VersionId instead
    # would be a different validator. They must be distinct strings here.
    assert result.version_id != result.etag
    head = await store.get_head()
    assert head is not None
    assert head.version_id == result.version_id and head.etag == result.etag


async def test_s2_genesis_create_only_is_atomic():
    store = make_store(unique_key())
    first = await store.put_head(body(1), create_only=True)
    assert first.version_id
    with pytest.raises((PreconditionFailed, ConflictError)) as excinfo:
        await store.put_head(body(9), create_only=True)
    assert excinfo.value.status_code in (412, 409)
    head = await store.get_head()
    assert head.version_id == first.version_id  # the refusal wrote nothing


async def test_s3_if_match_advances_through_the_observed_etag():
    store = make_store(unique_key())
    await store.put_head(body(1), create_only=True)
    observed = await store.get_head()
    advanced = await store.put_head(body(2), if_match=observed.etag)
    assert advanced.version_id != observed.version_id
    assert advanced.etag != observed.etag
    head = await store.get_head()
    assert head.body == body(2)
    assert head.version_id == advanced.version_id and head.etag == advanced.etag


async def test_s4_stale_if_match_is_a_412():
    store = make_store(unique_key())
    await store.put_head(body(1), create_only=True)
    first = await store.get_head()
    await store.put_head(body(2), if_match=first.etag)
    with pytest.raises(PreconditionFailed) as excinfo:
        await store.put_head(body(3), if_match=first.etag)
    assert excinfo.value.status_code == 412


async def test_s5_conditional_put_on_a_missing_object_is_a_404():
    store = make_store(unique_key())
    with pytest.raises(ObjectNotFound) as excinfo:
        await store.put_head(body(1), if_match='"no-such-version"')
    assert excinfo.value.status_code == 404
    assert await store.get_head() is None


async def test_s6_version_preservation_oldest_first_and_individually_fetchable():
    store = make_store(unique_key())
    bodies = [body(1), body(2), body(3)]
    put_results = [await store.put_head(bodies[0], create_only=True)]
    put_results.append(await store.put_head(bodies[1], if_match=put_results[0].etag))
    put_results.append(await store.put_head(bodies[2], if_match=put_results[1].etag))

    versions = await store.list_head_versions()
    assert [v.version_id for v in versions] == [r.version_id for r in put_results], (
        "the listing must be OLDEST FIRST to match the verifier's chain walk"
    )
    assert [v.is_latest for v in versions] == [False, False, True]
    assert not any(v.is_delete_marker for v in versions)

    for version, expected in zip(versions, bodies):
        fetched = await store.get_version(version.version_id)
        assert fetched.body == expected
        assert fetched.version_id == version.version_id

    # Unconditional overwrite is not offered by the client at all.
    with pytest.raises(ValueError):
        await store.put_head(b"{}")


async def test_s7_concurrent_conditional_writes_yield_exactly_one_winner():
    key = unique_key()
    store = make_store(key)
    await store.put_head(body(1), create_only=True)
    observed = await store.get_head()

    loser = make_store(key)
    results = await asyncio.gather(
        store.put_head(body(2), if_match=observed.etag),
        loser.put_head(body(99), if_match=observed.etag),
        return_exceptions=True,
    )
    outcomes = sorted(
        ("ok" if not isinstance(r, BaseException) else f"{type(r).__name__}")
        for r in results
    )
    assert outcomes == ["PreconditionFailed", "ok"], outcomes
    # Either writer may win the race — gather promises no ordering. Exactly one
    # conditional PUT survives, and the head is the WINNER's content, whoever that is.
    winner_body = body(2) if not isinstance(results[0], BaseException) else body(99)
    head = await store.get_head()
    assert head.body == winner_body
    versions = await store.list_head_versions()
    assert len(versions) == 2  # the loser created no version


async def test_s8_connection_failure_classifies_as_transport_failure():
    # A closed port on a live host: connection refused is fast and deterministic.
    host = _env["RESOLVE_S3_CONFORMANCE_ENDPOINT"].split("//", 1)[1].split("/", 1)[0]
    authority, _, port = host.partition(":")
    blackhole = make_store(
        unique_key(), endpoint_url=f"http://{authority}:1", timeout_seconds=3.0
    )
    with pytest.raises(TransportFailure):
        await blackhole.get_head()


# --------------------------------------------------------------------------- #
# S9: the real publisher + verifier against the real MinIO bucket              #
# --------------------------------------------------------------------------- #


async def test_s9_real_publisher_end_to_end_on_real_minio():
    from tests.ledger_sandbox import ADMIN_URL, LedgerSandbox

    if not ADMIN_URL:
        pytest.skip("RESOLVE_PG_ADMIN_URL is not set; S9 needs an isolated PostgreSQL 16")

    sb = LedgerSandbox()
    await sb.prepare_roles()
    await sb.create()
    try:
        await sb.bootstrap()
        await sb.run_role_separation()
        await sb.run_ledger_install()
        sb.sessions = sb.app_engine()
        store = make_store(unique_key())
        publisher = Publisher(sb.sessions, store, worm=PermissiveWormPolicy(),
                              lease_seconds=60.0)

        from app.models.db_models import PurchaseAuthorizationRecord
        from sqlalchemy import text

        async def seed(tag: str) -> None:
            async with sb.sessions() as session:
                pa = PurchaseAuthorizationRecord(
                    order_id=f"ord_s9_{tag}", mission_id=f"m_s9_{tag}",
                    user_id="user_s9", authority_decision="approved",
                    decision_reason="seed row", status="authorized",
                )
                session.add(pa)
                await session.commit()
                await session.execute(
                    text("UPDATE purchase_authorizations SET status = 'purchase_failed' "
                         "WHERE authorization_id = :pk"),
                    {"pk": pa.authorization_id},
                )
                await session.commit()

        await seed("a")
        assert await publisher.run_cycle() == "idle"  # genesis on the real bucket
        await seed("b")
        assert await publisher.run_cycle() == "idle"

        report = await verify(sb.sessions, store)
        assert report.verdict == VERDICT_HEALTHY, report.summary_lines()
        decision = await submission_currency(sb.sessions, store)
        assert decision.state == CURRENCY_CURRENT

        # Publication lag is refused for new submissions, then drains clean.
        await seed("c")
        decision = await submission_currency(sb.sessions, store)
        assert decision.state == "not-current"
        assert "caught up" in decision.reason
        assert await publisher.run_cycle() == "idle"
        decision = await submission_currency(sb.sessions, store)
        assert decision.state == CURRENCY_CURRENT
        report = await verify(sb.sessions, store)
        assert report.verdict == VERDICT_HEALTHY, report.summary_lines()

        versions = await store.list_head_versions()
        assert len(versions) == 3 and versions[-1].is_latest
    finally:
        if hasattr(sb, "sessions"):
            await sb.dispose_engines()
        await sb.drop()


async def test_s10_observe_to_write_race_on_the_divergent_provider_halts_never_adopts():
    """
    The S5 divergence ARMED as a live incident against the REAL publisher and REAL
    MinIO. Genesis lands normally; then, after the publisher's next GET proves the
    head exists, every version is destroyed before the conditional PUT. MinIO accepts
    that PUT on the now-missing object and creates a brand-new anchor (AWS S3 answers
    404 here, where the publisher's ObjectNotFound halt already protects it). The
    publisher must refuse to advance the pin onto the history-less orphan — the
    anchor-lineage verification added for this finding — halt with tampering evidence,
    and the currency gate must refuse new submissions even though the orphan's
    sequence and chain hash match the database exactly.
    """
    import httpx
    from sqlalchemy import text

    from tests.ledger_sandbox import ADMIN_URL, LedgerSandbox

    if not ADMIN_URL:
        pytest.skip("RESOLVE_PG_ADMIN_URL is not set; S10 needs an isolated PostgreSQL 16")

    sb = LedgerSandbox()
    await sb.prepare_roles()
    await sb.create()
    try:
        await sb.bootstrap()
        await sb.run_role_separation()
        await sb.run_ledger_install()
        sb.sessions = sb.app_engine()
        inner = make_store(unique_key())

        async def read_pin() -> dict:
            async with sb.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT last_published_seq, last_version_id, publish_status "
                            "FROM ledger_publish_state WHERE id = 1"
                        )
                    )
                ).one()
            return {"seq": row[0], "version_id": row[1], "status": row[2]}

        async def destroy_every_version() -> None:
            # Signed DELETE per version, through the store's own request path — the
            # adversarial actor in this scenario has bucket-admin rights.
            async with httpx.AsyncClient(timeout=inner._timeout) as client:
                for version in await inner.list_head_versions():
                    response = await inner._request(
                        client, "DELETE", query="versionId=" + quote(version.version_id, safe="")
                    )
                    assert response.status_code in (200, 204)

        class DestroyAfterObserve:
            """Arms the race once: the publisher's next successful observation of a
            live head is followed by total version destruction, before any PUT."""

            def __init__(self, store: S3ObjectStore) -> None:
                self._store = store
                self.armed = True

            async def get_head(self):
                result = await self._store.get_head()
                if self.armed and result is not None:
                    self.armed = False
                    await destroy_every_version()
                return result

            def __getattr__(self, name):
                return getattr(self._store, name)

        publisher = Publisher(sb.sessions, DestroyAfterObserve(inner),
                              worm=PermissiveWormPolicy(), lease_seconds=60.0)

        from app.models.db_models import PurchaseAuthorizationRecord

        async def seed(tag: str) -> None:
            async with sb.sessions() as session:
                pa = PurchaseAuthorizationRecord(
                    order_id=f"ord_s10_{tag}", mission_id=f"m_s10_{tag}",
                    user_id="user_s10", authority_decision="approved",
                    decision_reason="seed row", status="authorized",
                )
                session.add(pa)
                await session.commit()
                await session.execute(
                    text("UPDATE purchase_authorizations SET status = 'purchase_failed' "
                         "WHERE authorization_id = :pk"),
                    {"pk": pa.authorization_id},
                )
                await session.commit()

        await seed("a")
        assert await publisher.run_cycle() == "idle"  # genesis publishes event 1
        pinned = await read_pin()
        assert pinned["seq"] == 1 and pinned["version_id"]

        # Event 2: the observation proves the head exists, the head is destroyed, and
        # MinIO's divergent conditional PUT then creates a brand-new anchor.
        await seed("b")
        assert await publisher.run_cycle() == "refused-destroyed-lineage"

        after = await read_pin()
        assert after["seq"] == 1 and after["version_id"] == pinned["version_id"]
        assert after["status"] == "halted"

        # The orphan anchor really exists — the divergence is real, not hypothetical —
        # and carries exactly one version whose identity is NOT the pin's. Its content
        # is OURS: absent the lineage check this PUT would have silently "advanced"
        # the ledger onto an anchor with no genesis and no history.
        head = await inner.get_head()
        assert head is not None
        assert json.loads(head.body.decode("utf-8"))["head_seq"] == 2
        versions = await inner.list_head_versions()
        assert len(versions) == 1 and versions[0].version_id != pinned["version_id"]

        # The verifier sees the broken version chain...
        report = await verify(sb.sessions, inner)
        assert report.verdict == VERDICT_UNHEALTHY, report.summary_lines()
        # ...and the currency gate refuses new submissions: the orphan's sequence and
        # chain hash line up with the database, so only the pin's refusal stands
        # between a broken anchor and new purchases.
        decision = await submission_currency(sb.sessions, inner)
        assert decision.state == "not-current"
        assert "pin" in decision.reason
    finally:
        if hasattr(sb, "sessions"):
            await sb.dispose_engines()
        await sb.drop()


# --------------------------------------------------------------------------- #
# S11/S12: the FINAL-CHALLENGE deletion race against the real publisher         #
# --------------------------------------------------------------------------- #


async def _read_pin(sb) -> dict:
    from sqlalchemy import text

    async with sb.sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT last_published_seq, last_version_id, publish_status "
                    "FROM ledger_publish_state WHERE id = 1"
                )
            )
        ).one()
    return {"seq": row[0], "version_id": row[1], "status": row[2]}


async def _seed(sb, prefix: str, tag: str) -> None:
    from sqlalchemy import text

    from app.models.db_models import PurchaseAuthorizationRecord

    async with sb.sessions() as session:
        pa = PurchaseAuthorizationRecord(
            order_id=f"ord_{prefix}_{tag}", mission_id=f"m_{prefix}_{tag}",
            user_id=f"user_{prefix}", authority_decision="approved",
            decision_reason="seed row", status="authorized",
        )
        session.add(pa)
        await session.commit()
        await session.execute(
            text("UPDATE purchase_authorizations SET status = 'purchase_failed' "
                 "WHERE authorization_id = :pk"),
            {"pk": pa.authorization_id},
        )
        await session.commit()


async def _destroy_every_version(store: S3ObjectStore) -> None:
    """Signed DELETE per version — the adversarial actor holds bucket-admin rights."""
    import httpx

    async with httpx.AsyncClient(timeout=store._timeout) as client:
        for version in await store.list_head_versions():
            response = await store._request(
                client, "DELETE", query="versionId=" + quote(version.version_id, safe="")
            )
            assert response.status_code in (200, 204)


async def _sandbox_with_publisher(store):
    """Full ledger sandbox wired to the REAL publisher over the given store."""
    from tests.ledger_sandbox import LedgerSandbox

    sb = LedgerSandbox()
    await sb.prepare_roles()
    await sb.create()
    await sb.bootstrap()
    await sb.run_role_separation()
    await sb.run_ledger_install()
    sb.sessions = sb.app_engine()
    return sb, Publisher(sb.sessions, store, worm=PermissiveWormPolicy(),
                         lease_seconds=60.0)


async def _assert_destroyed_anchor_containment(sb, store) -> None:
    """The verifier and the currency gate must both name the destroyed anchor."""
    report = await verify(sb.sessions, store)
    assert report.verdict == VERDICT_UNHEALTHY, report.summary_lines()
    assert any("anchor version survives" in problem for problem in report.problems), (
        report.problems
    )
    decision = await submission_currency(sb.sessions, store)
    assert decision.state == "not-current"
    assert "absent" in decision.reason


async def test_s11_destruction_after_a_completed_lineage_verified_publication_refuses_before_any_put():
    """
    The FINAL-CHALLENGE deletion race, placed where the write path allows it: after a
    fully completed, lineage-verified publication and before the next cycle. The next
    cycle's observation finds NO head and refuses (refused-missing-head) BEFORE any
    PUT — MinIO creates no new head, the pin never moves, and the destroyed anchor is
    named by the verifier and refused by the currency gate.
    """
    from tests.ledger_sandbox import ADMIN_URL

    if not ADMIN_URL:
        pytest.skip("RESOLVE_PG_ADMIN_URL is not set; S11 needs an isolated PostgreSQL 16")

    store = make_store(unique_key())
    sb, publisher = await _sandbox_with_publisher(store)
    try:
        await _seed(sb, "s11", "a")
        assert await publisher.run_cycle() == "idle"  # genesis on the real bucket
        await _seed(sb, "s11", "b")
        assert await publisher.run_cycle() == "idle"  # lineage-verified advance
        pinned = await _read_pin(sb)
        assert pinned["seq"] == 2 and pinned["status"] == "idle" and pinned["version_id"]
        assert pinned["version_id"] in {
            v.version_id for v in await store.list_head_versions()
        }

        # The mandated deletion, after a successful lineage verification completed a
        # publication: the pinned version and every other version are destroyed.
        await _destroy_every_version(store)
        assert await store.list_head_versions() == []

        await _seed(sb, "s11", "c")
        assert await publisher.run_cycle() == "refused-missing-head"
        after = await _read_pin(sb)
        assert after["seq"] == pinned["seq"]
        assert after["version_id"] == pinned["version_id"]
        assert after["status"] == "halted"

        # No conditional PUT executed: MinIO did NOT create a new head.
        assert await store.get_head() is None
        assert await store.list_head_versions() == []

        await _assert_destroyed_anchor_containment(sb, store)
    finally:
        if hasattr(sb, "sessions"):
            await sb.dispose_engines()
        await sb.drop()


async def test_s12_destruction_between_lineage_verification_and_pin_commit_advances_then_halts():
    """
    The ONLY window where destruction lands after a successful lineage verification:
    the lineage listing is obtained truthfully (the pinned version present, verdict
    'ok') and every version is destroyed before the pin advance commits. The pin DOES
    advance onto evidence that no longer exists — the residual this challenge mandates
    reporting honestly: the deletion-to-PUT race is closed, the lineage-to-commit
    window is not. Containment is immediate and fail-closed: the next cycle observes
    no head and halts (never re-bootstrap), the currency gate refuses new submissions,
    and the verifier names the destroyed anchor. Closing this window structurally
    requires provider-side immutability (S3 Object Lock COMPLIANCE mode); no
    conditional DELETE exists in S3 to serialize a writer against a deleter, and none
    is invented here.
    """
    from tests.ledger_sandbox import ADMIN_URL

    if not ADMIN_URL:
        pytest.skip("RESOLVE_PG_ADMIN_URL is not set; S12 needs an isolated PostgreSQL 16")

    inner = make_store(unique_key())

    class DestroyAfterTruthfulListing:
        """The first lineage listing is truthful; every version is destroyed before
        it returns, landing the deletion between lineage 'ok' and the pin commit."""

        def __init__(self, target: S3ObjectStore) -> None:
            self._target = target
            self.armed = True

        async def list_head_versions(self):
            versions = await self._target.list_head_versions()
            if self.armed:
                self.armed = False
                await _destroy_every_version(self._target)
            return versions

        def __getattr__(self, name):
            return getattr(self._target, name)

    sb, publisher = await _sandbox_with_publisher(DestroyAfterTruthfulListing(inner))
    try:
        await _seed(sb, "s12", "a")
        assert await publisher.run_cycle() == "idle"  # genesis: lineage skipped (seq 0)
        assert (await _read_pin(sb))["seq"] == 1

        await _seed(sb, "s12", "b")
        assert await publisher.run_cycle() == "idle"  # PUT ok, lineage 'ok', pin advanced
        pinned = await _read_pin(sb)
        assert pinned["seq"] == 2 and pinned["status"] == "idle"
        assert pinned["version_id"]  # the pin advanced onto the now-destroyed version

        # The destruction really happened: zero versions survive, including the one
        # the pin now records.
        assert await inner.get_head() is None
        assert await inner.list_head_versions() == []

        # The next cycle refuses on the absent head and never re-bootstraps; the pin
        # keeps its (now evidence-less) position and halts.
        await _seed(sb, "s12", "c")
        assert await publisher.run_cycle() == "refused-missing-head"
        after = await _read_pin(sb)
        assert after["seq"] == pinned["seq"]
        assert after["version_id"] == pinned["version_id"]
        assert after["status"] == "halted"
        assert await inner.get_head() is None  # still nothing was re-created

        await _assert_destroyed_anchor_containment(sb, inner)
    finally:
        if hasattr(sb, "sessions"):
            await sb.dispose_engines()
        await sb.drop()
