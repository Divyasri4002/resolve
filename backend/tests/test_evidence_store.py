"""
Hermetic battery for app/evidence_store.py (Stage 1 core + Stage 2 recovery semantics).

No network, no database, no real Settings — the settings namespace is built by
hand so the store's behavior is proven against explicit values, and the client
seam is a duck-typed fake (EvidenceStoreClient protocol). Scenarios: MIME
sniffing, structural image validation (truncated/corrupt files refused), whole-
batch validation (empty / oversized / unrecognized / disallowed / declared-mismatch /
count / byte budget), UUID key generation, display-name sanitization, disabled-by-
default configuration, validate-all-then-store, recovery-aware store_files (cleanup
on failure, orphan tracking when cleanup cannot be confirmed), read roundtrip,
sha pinning, foreign-key refusal, and the no-leak guarantee for error messages.
"""
import hashlib
import re
from types import SimpleNamespace

import pytest

from app.evidence_store import (
    EvidenceBatchResult,
    EvidenceLimits,
    EvidenceObject,
    EvidenceStore,
    EvidenceStoreFailure,
    EvidenceValidationError,
    IncomingEvidence,
    evidence_key,
    sanitize_display_name,
    sniff_image_mime,
    validate_files,
)
from app.ledger.store import ObjectNotFound, PutResult, TransportFailure

PNG_IEND_TAIL = b"\x00\x00\x00\x00IEND\xaeB`\x82"
# Fixtures are STRUCTURALLY VALID images (signature + complete container tail), so the
# malformed-image gate proves itself on separately built truncated variants below.
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-payload" + b"\xff\xd9"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\x0dIHDR"
    + b"\x00" * 13
    + b"\x00\x00\x00\x00"
    + PNG_IEND_TAIL
)
_WEBP_BODY = b"VP8 " + b"webp-payload"
WEBP_BYTES = b"RIFF" + (len(_WEBP_BODY) + 4).to_bytes(4, "little") + b"WEBP" + _WEBP_BODY
JPEG_TRUNCATED = b"\xff\xd8\xff\xe0" + b"jpeg-payload"  # sniffable, no EOI marker
PNG_TRUNCATED = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 13  # no IEND
WEBP_BAD_SIZE = b"RIFF" + (9999).to_bytes(4, "little") + b"WEBP" + _WEBP_BODY
GARBAGE_BYTES = b"definitely not an image, just text"
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
GIF_BYTES = b"GIF89a" + b"legacy-container"

_MISSION = "m-evidence-1"
_ISSUE = "i-evidence-1"
_KEY_SHAPE = re.compile(r"^evidence/" + _MISSION + r"/" + _ISSUE + r"/[0-9a-f]{32}\.bin$")


class FakeEvidenceClient:
    """Duck-typed EvidenceStoreClient: records puts and deletes in memory, raises the
    injected exception on the Nth put/delete (1-based), serves get_object from memory."""

    def __init__(self, bucket: str = "resolve-evidence-test") -> None:
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.put_calls = 0
        self.get_calls = 0
        self.delete_calls = 0
        self.deleted_keys: list[str] = []
        self.fail_on_put: int | None = None
        self.fail_on_delete: int | None = None
        self.fail_with: Exception | None = None

    def _fail(self, kind: str, call_no: int) -> bool:
        target = self.fail_on_put if kind == "put" else self.fail_on_delete
        return target is not None and call_no == target

    async def put_object(self, key: str, body: bytes, content_type: str) -> PutResult:
        self.put_calls += 1
        if self._fail("put", self.put_calls):
            raise self.fail_with
        self.objects[key] = (body, content_type)
        return PutResult(etag=f'"etag-{self.put_calls}"', version_id=None)

    async def get_object(self, key: str) -> EvidenceObject:
        self.get_calls += 1
        if key not in self.objects:
            raise ObjectNotFound(404, "GET")
        body, mime = self.objects[key]
        return EvidenceObject(
            body=body,
            mime=mime,
            etag='"fake-etag"',
            version_id=None,
            sha256=hashlib.sha256(body).hexdigest(),
        )

    async def delete_object(self, key: str) -> bool:
        self.delete_calls += 1
        if self._fail("delete", self.delete_calls):
            raise self.fail_with
        self.deleted_keys.append(key)
        self.objects.pop(key, None)
        return True


def make_settings(**overrides) -> SimpleNamespace:
    base = dict(
        evidence_object_store_endpoint="",
        evidence_object_store_region="us-east-1",
        evidence_object_store_bucket="",
        evidence_object_store_access_key="",
        evidence_object_store_secret_key="",
        evidence_object_store_timeout_seconds=10.0,
        evidence_max_file_bytes=1024 * 1024,
        evidence_max_request_bytes=4 * 1024 * 1024,
        evidence_max_files_per_request=5,
        evidence_max_evidence_per_issue=10,
        evidence_allowed_mime_types="image/jpeg,image/png,image/webp",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_limits_only_settings(**overrides) -> SimpleNamespace:
    """ONLY the limit fields: if any evidence code path touched the ledger store's
    configuration fields, this namespace would raise AttributeError."""
    base = dict(
        evidence_max_file_bytes=1024 * 1024,
        evidence_max_request_bytes=4 * 1024 * 1024,
        evidence_max_files_per_request=5,
        evidence_max_evidence_per_issue=10,
        evidence_allowed_mime_types="image/jpeg,image/png,image/webp",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def incoming(data: bytes, declared: str | None = None) -> IncomingEvidence:
    return IncomingEvidence(data=data, declared_mime=declared)


# --------------------------------------------------------------------- sniffing --


def test_sniff_recognizes_the_three_allowed_formats():
    assert sniff_image_mime(JPEG_BYTES) == "image/jpeg"
    assert sniff_image_mime(PNG_BYTES) == "image/png"
    assert sniff_image_mime(WEBP_BYTES) == "image/webp"


def test_sniff_rejects_everything_else():
    assert sniff_image_mime(GARBAGE_BYTES) is None
    assert sniff_image_mime(SVG_BYTES) is None
    assert sniff_image_mime(GIF_BYTES) is None
    assert sniff_image_mime(b"") is None
    assert sniff_image_mime(b"\x89PNGbroken") is None
    assert sniff_image_mime(b"RIFFnot-webp-at-all") is None


# ---------------------------------------------------------------- configuration --


def test_from_settings_returns_none_when_unconfigured():
    assert EvidenceStore.from_settings(make_settings()) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_object_store_endpoint": "http://minio:9000"},
        {
            "evidence_object_store_endpoint": "http://minio:9000",
            "evidence_object_store_bucket": "resolve-evidence",
        },
        {
            "evidence_object_store_endpoint": "http://minio:9000",
            "evidence_object_store_bucket": "resolve-evidence",
            "evidence_object_store_access_key": "k",
        },
    ],
)
def test_from_settings_returns_none_when_half_configured(overrides):
    # Disable-by-default means never half-configure an upload target: any missing
    # piece (endpoint, bucket, access key, secret key) leaves NO store.
    assert EvidenceStore.from_settings(make_settings(**overrides)) is None


def test_from_settings_builds_real_client_from_full_config():
    store = EvidenceStore.from_settings(
        make_settings(
            evidence_object_store_endpoint="http://minio:9000",
            evidence_object_store_bucket="resolve-evidence",
            evidence_object_store_access_key="k",
            evidence_object_store_secret_key="s",
            evidence_object_store_timeout_seconds=7.5,
        )
    )
    assert store is not None
    from app.evidence_store import EvidenceObjectStore

    assert isinstance(store._client, EvidenceObjectStore)
    assert store._client._bucket == "resolve-evidence"
    assert store.limits.max_file_bytes == 1024 * 1024


def test_from_settings_with_injected_client_needs_no_credentials():
    # The hermetic seam: a fake client plus ONLY limit fields — no store-endpoint or
    # credential fields exist on this namespace at all.
    store = EvidenceStore.from_settings(make_limits_only_settings(), FakeEvidenceClient())
    assert store is not None
    assert store.limits.max_files_per_request == 5


def test_limits_parse_and_normalize_allowlist():
    limits = EvidenceLimits.from_settings(
        make_limits_only_settings(evidence_allowed_mime_types=" image/JPEG , image/png ,,")
    )
    assert limits.allowed_mime_types == frozenset({"image/jpeg", "image/png"})


# ------------------------------------------------------------------ validation --


def test_validate_rejects_empty_file():
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_files([incoming(b"")], EvidenceLimits.from_settings(make_limits_only_settings()))
    assert excinfo.value.code == "empty_file"


def test_validate_rejects_oversized_file():
    limits = EvidenceLimits.from_settings(make_limits_only_settings(evidence_max_file_bytes=64))
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_files([incoming(b"\xff\xd8\xff" + b"x" * 100)], limits)
    assert excinfo.value.code == "too_large"


def test_validate_rejects_unrecognized_content():
    limits = EvidenceLimits.from_settings(make_limits_only_settings())
    for payload in (GARBAGE_BYTES, SVG_BYTES, GIF_BYTES):
        with pytest.raises(EvidenceValidationError) as excinfo:
            validate_files([incoming(payload)], limits)
        assert excinfo.value.code == "unsupported_type"


def test_validate_rejects_sniffable_type_outside_the_allowlist():
    limits = EvidenceLimits.from_settings(
        make_limits_only_settings(evidence_allowed_mime_types="image/jpeg")
    )
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_files([incoming(PNG_BYTES)], limits)
    assert excinfo.value.code == "unsupported_type"


def test_validate_rejects_declared_mime_mismatch():
    limits = EvidenceLimits.from_settings(make_limits_only_settings())
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_files([incoming(JPEG_BYTES, declared="image/png")], limits)
    assert excinfo.value.code == "mime_mismatch"


def test_validate_accepts_declared_mime_with_parameters():
    limits = EvidenceLimits.from_settings(make_limits_only_settings())
    validated = validate_files(
        [incoming(JPEG_BYTES, declared="image/jpeg; charset=binary")], limits
    )
    assert [v.mime for v in validated] == ["image/jpeg"]


def test_validate_rejects_too_many_files():
    limits = EvidenceLimits.from_settings(make_limits_only_settings(evidence_max_files_per_request=5))
    files = [incoming(JPEG_BYTES) for _ in range(6)]
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_files(files, limits)
    assert excinfo.value.code == "too_many_files"


def test_validate_rejects_batch_over_total_budget():
    limits = EvidenceLimits.from_settings(
        make_limits_only_settings(evidence_max_request_bytes=4 * 1024 * 1024)
    )
    big = JPEG_BYTES + b"\x00" * (3 * 1024 * 1024)
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_files([incoming(big), incoming(big)], limits)
    assert excinfo.value.code == "request_too_large"


@pytest.mark.parametrize(
    "payload",
    [JPEG_TRUNCATED, PNG_TRUNCATED, WEBP_BAD_SIZE],
    ids=["jpeg-no-eoi", "png-no-iend", "webp-riff-size-lies"],
)
def test_validate_rejects_malformed_images_that_pass_the_signature_check(payload):
    # A truncated or corrupt file whose FIRST bytes look like an image is refused too:
    # the structure check runs after the signature check and demands a complete image.
    limits = EvidenceLimits.from_settings(make_limits_only_settings())
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_files([incoming(payload)], limits)
    assert excinfo.value.code == "malformed_image"


def test_validate_accepts_complete_images_of_all_three_formats():
    limits = EvidenceLimits.from_settings(make_limits_only_settings())
    validated = validate_files(
        [incoming(JPEG_BYTES), incoming(PNG_BYTES), incoming(WEBP_BYTES)], limits
    )
    assert [v.mime for v in validated] == ["image/jpeg", "image/png", "image/webp"]


# ------------------------------------------------------------ display-name sanitizer --


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "upload.bin"),
        ("", "upload.bin"),
        ("   ", "upload.bin"),
        ("photo.jpg", "photo.jpg"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\evil\\contract.pdf", "contract.pdf"),
        ("..hidden.exe", "hidden.exe"),
        ("résumé.png", "r_sum_.png"),
        ('bad"quote.png', "bad_quote.png"),
        ("a" * 300, "a" * 128),
        ("\x00\x01\x02", "upload.bin"),
    ],
)
def test_sanitize_display_name_reduces_untrusted_filenames_to_safe_displays(raw, expected):
    assert sanitize_display_name(raw) == expected


def test_sanitize_display_name_output_is_header_safe():
    # The sanitizer's output must be embeddable in a quoted Content-Disposition value:
    # ASCII only, no quotes, no backslashes, no control characters.
    for raw in ("../../evil.png", 'x".exe', "back\\slash.jpg", "ünïcødé.bin", None):
        safe = sanitize_display_name(raw)
        assert safe.isascii()
        assert '"' not in safe and "\\" not in safe
        assert all(ch.isprintable() for ch in safe)


# ---------------------------------------------------------------- store_files --


@pytest.mark.asyncio
async def test_store_files_happy_path_records_stored_evidence():
    client = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    batch = await store.store_files(
        _MISSION,
        _ISSUE,
        [incoming(JPEG_BYTES), incoming(PNG_BYTES, declared="image/png")],
    )
    assert batch.orphan_candidates == []
    stored = batch.stored
    assert len(stored) == 2
    for item in stored:
        assert _KEY_SHAPE.match(item.storage_key)
        assert item.bucket == client.bucket
    assert [item.mime for item in stored] == ["image/jpeg", "image/png"]
    assert stored[0].sha256 == hashlib.sha256(JPEG_BYTES).hexdigest()
    assert stored[0].size_bytes == len(JPEG_BYTES)
    assert client.objects[stored[0].storage_key] == (JPEG_BYTES, "image/jpeg")
    assert client.objects[stored[1].storage_key] == (PNG_BYTES, "image/png")


@pytest.mark.asyncio
async def test_store_files_validates_the_whole_batch_before_storing_anything():
    client = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    with pytest.raises(EvidenceValidationError):
        await store.store_files(_MISSION, _ISSUE, [incoming(JPEG_BYTES), incoming(GARBAGE_BYTES)])
    assert client.put_calls == 0
    assert client.objects == {}


@pytest.mark.asyncio
async def test_store_files_refuses_bad_identifiers_before_any_put():
    client = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    with pytest.raises(EvidenceValidationError) as excinfo:
        await store.store_files("bad id!", _ISSUE, [incoming(JPEG_BYTES)])
    assert excinfo.value.code == "invalid_key_component"
    assert client.put_calls == 0


@pytest.mark.asyncio
async def test_store_files_empty_batch_is_a_noop():
    client = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    batch = await store.store_files(_MISSION, _ISSUE, [])
    assert isinstance(batch, EvidenceBatchResult)
    assert batch.stored == []
    assert batch.orphan_candidates == []
    assert client.objects == {}


@pytest.mark.asyncio
async def test_mid_batch_failure_cleans_up_every_object_it_stored():
    client = FakeEvidenceClient()
    client.fail_on_put = 2
    client.fail_with = TransportFailure(None, "PUT")
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    with pytest.raises(EvidenceStoreFailure) as excinfo:
        await store.store_files(_MISSION, _ISSUE, [incoming(JPEG_BYTES), incoming(PNG_BYTES)])
    # Recovery: the first file WAS stored, then deleted during cleanup — nothing of
    # the batch remains and there are no orphans to track.
    assert excinfo.value.orphaned == []
    assert client.objects == {}
    assert len(client.deleted_keys) == 1
    assert excinfo.value.__cause__ is client.fail_with


@pytest.mark.asyncio
async def test_unconfirmable_cleanup_surfaces_durable_orphan_candidates():
    client = FakeEvidenceClient()
    client.fail_on_put = 2
    client.fail_on_delete = 1
    client.fail_with = TransportFailure(None, "PUT")
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    with pytest.raises(EvidenceStoreFailure) as excinfo:
        await store.store_files(_MISSION, _ISSUE, [incoming(JPEG_BYTES), incoming(PNG_BYTES)])
    # The delete itself failed: the object may still exist, so it is returned as an
    # orphan candidate for durable tracking — never assumed gone, never silently left.
    [orphan] = excinfo.value.orphaned
    assert _KEY_SHAPE.match(orphan.storage_key)
    assert orphan.sha256 == hashlib.sha256(JPEG_BYTES).hexdigest()
    assert orphan.storage_key in client.objects
    assert excinfo.value.__cause__ is client.fail_with


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_toward_absent_objects():
    client = FakeEvidenceClient()
    client.fail_on_put = 2
    client.fail_with = TransportFailure(None, "PUT")
    # A delete of a key the bucket never had (or already deleted) still succeeds.
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    with pytest.raises(EvidenceStoreFailure) as excinfo:
        await store.store_files(_MISSION, _ISSUE, [incoming(JPEG_BYTES), incoming(PNG_BYTES)])
    assert excinfo.value.orphaned == []


@pytest.mark.asyncio
async def test_keys_are_uuid_based_and_distinct_for_identical_content():
    client = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    batch = await store.store_files(
        _MISSION, _ISSUE, [incoming(JPEG_BYTES), incoming(JPEG_BYTES)]
    )
    assert batch.stored[0].storage_key != batch.stored[1].storage_key


# --------------------------------------------------------------------- reading --


@pytest.mark.asyncio
async def test_read_roundtrip_preserves_bytes_mime_and_sha():
    client = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    [stored] = (await store.store_files(_MISSION, _ISSUE, [incoming(JPEG_BYTES)])).stored
    obj = await store.read(stored.storage_key, expected_sha256=stored.sha256)
    assert obj.body == JPEG_BYTES
    assert obj.mime == "image/jpeg"
    assert obj.sha256 == stored.sha256


@pytest.mark.asyncio
async def test_read_refuses_foreign_key_shapes_without_any_request():
    client = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    for bad_key in (
        "../../ledger/head.json",
        "evidence/m/i/not-a-uuid.bin",
        "ledger/head.json",
        "",
        "evidence/m/i/" + "z" * 32 + ".bin",
    ):
        with pytest.raises(EvidenceValidationError) as excinfo:
            await store.read(bad_key)
        assert excinfo.value.code == "invalid_key"
    assert client.get_calls == 0


@pytest.mark.asyncio
async def test_read_detects_tampered_bytes_via_sha_pin():
    client = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    [stored] = (await store.store_files(_MISSION, _ISSUE, [incoming(JPEG_BYTES)])).stored
    tampered = b"\xff\xd8\xff" + b"tampered-bytes" + b"\xff\xd9"
    client.objects[stored.storage_key] = (tampered, "image/jpeg")
    with pytest.raises(EvidenceValidationError) as excinfo:
        await store.read(stored.storage_key, expected_sha256=stored.sha256)
    assert excinfo.value.code == "sha_mismatch"


@pytest.mark.asyncio
async def test_read_missing_key_raises_object_not_found():
    client = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), client)
    with pytest.raises(ObjectNotFound):
        await store.read(f"evidence/{_MISSION}/{_ISSUE}/" + "0" * 32 + ".bin")


# ------------------------------------------------------------------- no-leak --


@pytest.mark.parametrize(
    "payload,declared",
    [
        (b"SECRET-PHOTO-BYTES-" + b"\x00" * 40, None),
        (JPEG_BYTES + b"SECRET-PHOTO-BYTES", "image/gif"),
    ],
)
def test_error_messages_never_contain_file_bytes(payload, declared):
    limits = EvidenceLimits.from_settings(
        make_limits_only_settings(
            evidence_max_file_bytes=16,
            evidence_max_request_bytes=1024 * 1024,
        )
    )
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_files([incoming(payload, declared=declared)], limits)
    assert "SECRET-PHOTO-BYTES" not in str(excinfo.value)


def test_oversized_error_message_states_only_the_limit():
    limits = EvidenceLimits.from_settings(make_limits_only_settings(evidence_max_file_bytes=16))
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_files([incoming(b"\xff\xd8\xff" + b"x" * 100)], limits)
    message = str(excinfo.value)
    assert "16" in message
    assert "\xff" not in message
