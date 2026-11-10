"""
WORM policy verification for the external evidence bucket (Phase 4.6.3).

Phase 4.6.2's final security challenge proved the publisher CONTAINS anchor
destruction fail-closed (S11 refuses before any PUT; S12's advance is followed by a
permanent refusal) but does not STRUCTURALLY PREVENT deletion: a bucket administrator
can still destroy every version inside the lineage-ok-to-pin-commit window, because no
conditional DELETE exists in the S3 API. The only provider-native no-bypass mechanism
is Object Lock in COMPLIANCE mode (AWS S3 userguide, object-lock.html: "a protected
object version can't be overwritten or deleted by any user, including the root user in
your AWS account"; the only pre-expiry escape is deleting the AWS account).

Completion criterion this module enforces: RESOLVE must refuse to publish unless its
external evidence bucket meets the WORM protection policy. The publisher verifies the
bucket configuration before every cycle and refuses without touching the pin when it
fails — refuse, alarm, keep the trust anchor unchanged, never bootstrap or replace.

The required policy (no configurable substitution — the mandate fixes these):
  versioning      : Enabled. Object Lock "works only in buckets that have S3 Versioning
                    enabled" (AWS), and versioning cannot be suspended after lock is on.
  object lock     : Enabled at the bucket level.
  retention mode  : COMPLIANCE. GOVERNANCE is refused outright — it is bypassable by
                    any principal holding s3:BypassGovernanceRetention plus the
                    x-amz-bypass-governance-retention:true header.
  default retention: present and >= the configured floor. The default retention is what
                    locks every version the publisher writes (retention attaches to each
                    version AT WRITE TIME — AWS: "protected exactly as though you
                    explicitly placed an individual lock ... on the object version" — so
                    clearing the default later never unlocks existing versions, but a
                    version written while no default is set is unprotected forever).
  lifecycle       : no ENABLED rule carrying Expiration or NoncurrentVersionExpiration.
                    Locked versions cannot be deleted by lifecycle expiration (AWS:
                    "a locked version of an object cannot be deleted by a S3 Lifecycle
                    expiration policy"), but once retention expires such a rule purges
                    history, and the verifier walks ALL versions oldest-first — the
                    chain walk then fails closed. Refusing the configuration is the
                    honest posture; losing history is not.

AWS vs MinIO: the checks speak the S3 REST wire shape (GET /?versioning, GET
/?object-lock, GET /?lifecycle). AWS behavior cited above comes from the AWS S3
userguide. MinIO conformance is proven separately by the W battery on a real MinIO
bucket — MinIO results are never reported as AWS conformance.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.ledger.store import S3ObjectStore

REQUIRED_RETENTION_MODE = "COMPLIANCE"

_DAYS_PER_YEAR = 365  # AWS counts 1 year as 365 days for retention-period semantics.


@dataclass(frozen=True)
class WormVerdict:
    ok: bool
    failures: tuple[str, ...]


class WormPolicy:
    """The seam the publisher requires. Production wires S3WormPolicy; nothing else."""

    async def verify(self, store: S3ObjectStore) -> WormVerdict:  # pragma: no cover
        raise NotImplementedError


class PermissiveWormPolicy(WormPolicy):
    """
    TEST-ONLY seam for CAS-mechanics tests (the S10-S12 destruction scenarios and the
    hermetic publisher battery) so they can isolate conditional-write/lineage behavior
    from bucket provisioning. PRODUCTION MUST NEVER CONSTRUCT THIS — build_publisher
    wires S3WormPolicy, and test_ledger_worm.py pins that wiring.
    """

    async def verify(self, store: S3ObjectStore) -> WormVerdict:
        return WormVerdict(ok=True, failures=())


class S3WormPolicy(WormPolicy):
    def __init__(self, min_retention_days: int) -> None:
        if min_retention_days <= 0:
            raise ValueError("min_retention_days must be positive")
        self._min_retention_days = min_retention_days

    async def verify(self, store: S3ObjectStore) -> WormVerdict:
        failures: list[str] = []

        try:
            versioning = await store.get_bucket_versioning()
        except Exception as exc:  # noqa: BLE001 - classified below
            failures.append(
                f"bucket versioning status is unverifiable ({type(exc).__name__}); a "
                "bucket whose protection cannot be read is a bucket that cannot be "
                "trusted — refusing to publish"
            )
        else:
            if versioning != "Enabled":
                failures.append(
                    f"bucket versioning is {versioning!r}, not Enabled; Object Lock "
                    "works only on versioned buckets and the head object's version "
                    "history is the lineage evidence"
                )

        lock = None
        try:
            lock = await store.get_bucket_object_lock_configuration()
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"bucket Object Lock configuration is unverifiable "
                f"({type(exc).__name__}); refusing to publish without proof of "
                "COMPLIANCE-mode protection"
            )
        if lock is None:
            failures.append(
                "the bucket has no Object Lock configuration at all — versions are "
                "deletable by anyone with DeleteObjectVersion rights; publication "
                "refused"
            )
        elif not lock.enabled:
            failures.append(
                "the bucket's Object Lock is not Enabled; publication refused"
            )
        else:
            if lock.mode != REQUIRED_RETENTION_MODE:
                failures.append(
                    f"default retention mode is {lock.mode!r}, not "
                    f"{REQUIRED_RETENTION_MODE}; GOVERNANCE is bypassable by any "
                    "principal holding s3:BypassGovernanceRetention and is refused "
                    "outright"
                )
            retention_days = lock.days
            if retention_days is None and lock.years is not None:
                retention_days = lock.years * _DAYS_PER_YEAR
            if retention_days is None:
                failures.append(
                    "the bucket is Object-Lock enabled but sets NO default retention; "
                    "versions written while no default applies are unprotected forever"
                )
            elif retention_days < self._min_retention_days:
                failures.append(
                    f"default retention ({retention_days} days) is below the required "
                    f"evidence floor ({self._min_retention_days} days)"
                )

        try:
            lifecycle_rules = await store.get_bucket_lifecycle_rules()
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"bucket lifecycle configuration is unverifiable ({type(exc).__name__}); "
                "an unreadable purge configuration is refused"
            )
        else:
            for rule in lifecycle_rules:
                if rule.status != "Enabled":
                    continue
                if rule.expires or rule.expires_noncurrent:
                    failures.append(
                        "an Enabled lifecycle rule can expire object versions; once "
                        "retention lapses it would purge the chain history the "
                        "verifier walks — remove the rule or publication stays refused"
                    )
                    break

        return WormVerdict(ok=not failures, failures=tuple(failures))
