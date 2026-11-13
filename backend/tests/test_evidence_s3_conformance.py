"""
Real S3-compatible conformance for the evidence object store (Stage 1).

Mirrors the ledger conformance battery's honesty contract: these tests run ONLY
when a dedicated evidence conformance bucket is configured via environment
variables and skip honestly otherwise. The environment names are evidence-specific
on purpose — they can never be satisfied by the ledger store's conformance
variables, so this battery is structurally incapable of running against the WORM
ledger bucket. MinIO results here are MinIO conformance, never AWS conformance.

Env contract (all four required, any absence = honest skip):
  RESOLVE_EVIDENCE_S3_CONFORMANCE_ENDPOINT   e.g. http://127.0.0.1:9000
  RESOLVE_EVIDENCE_S3_CONFORMANCE_BUCKET     a private, deletable, NON-WORM bucket
  RESOLVE_EVIDENCE_S3_CONFORMANCE_ACCESS_KEY
  RESOLVE_EVIDENCE_S3_CONFORMANCE_SECRET_KEY
"""
import hashlib
import os
import uuid
from types import SimpleNamespace

import pytest

from app.evidence_store import (
    EvidenceObjectStore,
    EvidenceStore,
    EvidenceValidationError,
    IncomingEvidence,
)
from app.ledger.store import ObjectNotFound, TransportFailure

_EVIDENCE_S3_ENV = (
    "RESOLVE_EVIDENCE_S3_CONFORMANCE_ENDPOINT",
    "RESOLVE_EVIDENCE_S3_CONFORMANCE_BUCKET",
    "RESOLVE_EVIDENCE_S3_CONFORMANCE_ACCESS_KEY",
    "RESOLVE_EVIDENCE_S3_CONFORMANCE_SECRET_KEY",
)
_EVIDENCE_S3_CONFIG = {name: os.environ.get(name, "") for name in _EVIDENCE_S3_ENV}
EVIDENCE_S3_CONFIGURED = all(_EVIDENCE_S3_CONFIG.values())

pytestmark = pytest.mark.skipif(
    not EVIDENCE_S3_CONFIGURED,
    reason=(
        "evidence S3 conformance needs RESOLVE_EVIDENCE_S3_CONFORMANCE_"
        "{ENDPOINT,BUCKET,ACCESS_KEY,SECRET_KEY}; skipping honestly"
    ),
)

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"conformance-jpeg-payload" + b"\xff\xd9"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\x0dIHDR"
    + b"\x00" * 13
    + b"\x00\x00\x00\x00"
    + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _client() -> EvidenceObjectStore:
    return EvidenceObjectStore(
        endpoint_url=_EVIDENCE_S3_CONFIG["RESOLVE_EVIDENCE_S3_CONFORMANCE_ENDPOINT"],
        region="us-east-1",
        bucket=_EVIDENCE_S3_CONFIG["RESOLVE_EVIDENCE_S3_CONFORMANCE_BUCKET"],
        access_key=_EVIDENCE_S3_CONFIG["RESOLVE_EVIDENCE_S3_CONFORMANCE_ACCESS_KEY"],
        secret_key=_EVIDENCE_S3_CONFIG["RESOLVE_EVIDENCE_S3_CONFORMANCE_SECRET_KEY"],
        timeout_seconds=10.0,
    )


def _key() -> str:
    return f"evidence/conformance/{uuid.uuid4().hex}.bin"


@pytest.mark.asyncio
async def test_put_then_get_roundtrip_preserves_bytes_content_type_and_sha():
    client = _client()
    key = _key()
    await client.put_object(key, JPEG_BYTES, "image/jpeg")
    obj = await client.get_object(key)
    assert obj.body == JPEG_BYTES
    assert obj.mime == "image/jpeg"
    assert obj.sha256 == hashlib.sha256(JPEG_BYTES).hexdigest()
    assert obj.etag


@pytest.mark.asyncio
async def test_distinct_keys_never_collide():
    client = _client()
    key_a, key_b = _key(), _key()
    await client.put_object(key_a, JPEG_BYTES, "image/jpeg")
    await client.put_object(key_b, PNG_BYTES, "image/png")
    got_a, got_b = await client.get_object(key_a), await client.get_object(key_b)
    assert got_a.body == JPEG_BYTES and got_a.mime == "image/jpeg"
    assert got_b.body == PNG_BYTES and got_b.mime == "image/png"


@pytest.mark.asyncio
async def test_get_missing_object_raises_object_not_found():
    client = _client()
    with pytest.raises(ObjectNotFound):
        await client.get_object(_key())


@pytest.mark.asyncio
async def test_store_facade_roundtrip_with_sha_pin():
    client = _client()
    settings = SimpleNamespace(
        evidence_max_file_bytes=1024 * 1024,
        evidence_max_request_bytes=4 * 1024 * 1024,
        evidence_max_files_per_request=5,
        evidence_max_evidence_per_issue=10,
        evidence_allowed_mime_types="image/jpeg,image/png,image/webp",
    )
    store = EvidenceStore.from_settings(settings, client)
    batch = await store.store_files(
        "conformance-mission", "conformance-issue", [IncomingEvidence(data=JPEG_BYTES, declared_mime="image/jpeg")]
    )
    [item] = batch.stored
    obj = await store.read(item.storage_key, expected_sha256=item.sha256)
    assert obj.body == JPEG_BYTES
    assert obj.mime == "image/jpeg"


@pytest.mark.asyncio
async def test_delete_removes_the_object_and_is_idempotent():
    # The cleanup path the upload endpoint relies on: DELETE makes the object
    # unreadable, and deleting an already-absent key still reports success.
    client = _client()
    key = _key()
    await client.put_object(key, PNG_BYTES, "image/png")
    assert await client.delete_object(key) is True
    with pytest.raises(ObjectNotFound):
        await client.get_object(key)
    assert await client.delete_object(key) is True


@pytest.mark.asyncio
async def test_transport_failure_classified():
    # A closed local port: connection refused -> TransportFailure (outcome unknown),
    # the same taxonomy the publisher relies on.
    client = EvidenceObjectStore(
        endpoint_url="http://127.0.0.1:9",
        region="us-east-1",
        bucket=_EVIDENCE_S3_CONFIG["RESOLVE_EVIDENCE_S3_CONFORMANCE_BUCKET"],
        access_key="k",
        secret_key="s",
        timeout_seconds=3.0,
    )
    with pytest.raises(TransportFailure):
        await client.put_object(_key(), JPEG_BYTES, "image/jpeg")
