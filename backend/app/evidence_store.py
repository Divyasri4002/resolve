"""
Evidence object store — private, deletable, non-WORM storage for user-uploaded
issue evidence (Stage 1 of native file/photo uploads).

Deliberately separate from the ledger store (app/ledger/store.py). The ledger
bucket is WORM (Object Lock COMPLIANCE, 365-day floor): nothing written there can
ever be erased, which is the point for tamper-evident purchase evidence and the
exact opposite of what user photos need — a privacy erasure path must remain
possible. Evidence therefore gets its own private, deletable, NON-WORM bucket and
its own credentials; the ledger keys authorize exactly one head-key path and must
never gain put/get on arbitrary keys, so sharing them would widen their authority
for no benefit.

This module REUSES the ledger client's proven SigV4 signing core (_signed_headers,
canonical query handling, empty-body hashing, transport/error taxonomy) so the
wire behavior stays the one proven against real S3-compatible servers, but exposes
only generic keyed PUT/GET. None of the parent's head-scoped public APIs
(get_head / put_head / get_version / list_head_versions / bucket-policy queries)
are reachable from an evidence code path: those exist for the ledger's single-head
WORM contract, and an evidence bucket must never carry or be addressed as a WORM
head.

Disable-by-default (the ledger rule again): empty endpoint, bucket, or credentials
make EvidenceStore.from_settings return None. Callers treat None as "evidence
storage unavailable" and refuse honestly — there is deliberately NO fallback to
container-local filesystem, which is not durable and is a privacy liability for
user uploads.

Uploads are untrusted bytes. Validation runs on the WHOLE batch BEFORE any object
is stored (validate-all-then-store), so a rejected batch leaves nothing behind:
  - per-batch: file count, total byte budget;
  - per-file: empty, oversized, unrecognized content signature, non-allowlisted
    type, or a client-declared MIME that disagrees with the sniffed signature.
Keys are server-generated UUID paths (`evidence/{mission}/{issue}/{uuid}.bin`);
user filenames never touch a storage key (no traversal, no collisions, no
filename-derived behavior) and never appear in an error message. Error messages
never contain file bytes either.

Storage failures mid-batch are recovery-aware: already-stored objects are deleted
best-effort, and anything whose deletion cannot be confirmed is returned to the
caller as orphan candidates (EvidenceStoreFailure.orphaned) for durable tracking —
never silently assumed gone, never silently left behind without a record.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence
from urllib.parse import quote

import httpx

from app.ledger.store import (
    _EMPTY_BODY_SHA256,
    ObjectNotFound,
    ObjectStoreError,
    PutResult,
    S3ObjectStore,
    ServerError,
    TransportFailure,
    _classify_get_error,
    _classify_put_error,
    _header,
)

_MIME_JPEG = "image/jpeg"
_MIME_PNG = "image/png"
_MIME_WEBP = "image/webp"

_PNG_IEND_TAIL = b"\x00\x00\x00\x00IEND\xaeB`\x82"


class EvidenceValidationError(ValueError):
    """An upload the store refuses BEFORE touching the bucket, or an integrity
    failure on read. `code` is stable for tests and future endpoint mapping; the
    message never contains file content, file bytes, or a user-supplied filename
    (all untrusted text — XSS / log-injection surface)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sniff_image_mime(data: bytes) -> str | None:
    """Content-signature detection for the ONLY formats the evidence store accepts.
    Returns None for everything else — SVG is deliberately unrecognized here (it is
    scriptable content; a stored-SVG fetch would be an XSS vector), GIF neither.
    Hand-rolled on purpose: no new dependency, exact bytes checked."""
    if data.startswith(b"\xff\xd8\xff"):
        return _MIME_JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _MIME_PNG
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _MIME_WEBP
    return None


def _structure_ok(data: bytes, mime: str) -> bool:
    """Deeper-than-signature structural checks: a file whose first bytes LOOK like
    an image but whose body is truncated or corrupt is refused too. Deliberately
    minimal hand-rolled checks — full decoding would add a dependency and a
    decode-amplification surface, and the goal is refusing obvious garbage, not
    certifying renderability."""
    if mime == _MIME_JPEG:
        # SOI ... EOI: a truncated scan cannot end with the EOI marker.
        return len(data) >= 4 and data.endswith(b"\xff\xd9")
    if mime == _MIME_PNG:
        # Signature + IHDR present, and the stream terminates with the IEND chunk
        # (whose CRC is the fixed constant AE 42 60 82 for every valid PNG).
        return len(data) >= 16 and data[12:16] == b"IHDR" and data.endswith(_PNG_IEND_TAIL)
    if mime == _MIME_WEBP:
        # RIFF size field must equal the actual body length, and the first chunk
        # must be one of the three VP8 bitstream fourccs.
        if len(data) < 16 or int.from_bytes(data[4:8], "little") != len(data) - 8:
            return False
        return data[12:16] in (b"VP8 ", b"VP8L", b"VP8X")
    return False


def _normalize_declared_mime(declared: str | None) -> str | None:
    """The client-declared type is untrusted: strip parameters ("image/jpeg;
    charset=binary"), lowercase, trim. None means the part declared nothing, in
    which case the sniffed signature alone decides."""
    if declared is None:
        return None
    base = declared.partition(";")[0].strip().lower()
    return base or None


_KEY_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EVIDENCE_KEY_PATTERN = re.compile(
    r"^evidence/[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}/[0-9a-f]{32}\.bin$"
)


def evidence_key(mission_id: str, issue_id: str) -> str:
    """Server-owned storage key. The leaf is a UUID4 — never a filename."""
    for component in (mission_id, issue_id):
        if not _KEY_COMPONENT.match(component):
            raise EvidenceValidationError(
                "invalid_key_component",
                "mission/issue identifier is not usable in a storage key",
            )
    return f"evidence/{mission_id}/{issue_id}/{uuid.uuid4().hex}.bin"


_DISPLAY_NAME_FORBIDDEN = re.compile(r"[^A-Za-z0-9._ -]")
_DISPLAY_NAME_MAX = 128


def sanitize_display_name(raw: str | None) -> str:
    """Reduce an untrusted client filename to a bounded, ASCII-only display string:
    basename (both separators), conservative character set, no leading dots, no
    control characters, hard length cap. The result is safe to place inside a
    quoted Content-Disposition header value and is NEVER used to build a storage
    path — keys are server-generated UUIDs."""
    if not raw:
        return "upload.bin"
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _DISPLAY_NAME_FORBIDDEN.sub("_", name).lstrip(".")
    name = name[:_DISPLAY_NAME_MAX].strip()
    if not any(c.isalnum() for c in name):
        return "upload.bin"
    return name or "upload.bin"


@dataclass(frozen=True)
class IncomingEvidence:
    """One untrusted upload as received at the request boundary: raw bytes plus
    whatever MIME the client declared (None when the part declared nothing). No
    filename field exists — filenames are never consumed by this module."""

    data: bytes
    declared_mime: str | None


@dataclass(frozen=True)
class ValidatedEvidence:
    data: bytes
    mime: str
    sha256: str


@dataclass(frozen=True)
class StoredEvidence:
    """What the caller must persist (P2): the key/bucket pair is the ONLY handle to
    the object; mime/sha256 are the recorded truth to verify against on read."""

    storage_key: str
    bucket: str
    size_bytes: int
    mime: str
    sha256: str


@dataclass(frozen=True)
class EvidenceBatchResult:
    """Outcome of a validated-then-stored batch. `stored` is the caller-persistable
    record; `orphan_candidates` is normally empty and non-empty only when a mid-batch
    failure could not confirm deletion of already-written objects."""

    stored: list[StoredEvidence]
    orphan_candidates: list[StoredEvidence]


class EvidenceStoreFailure(ObjectStoreError):
    """A storage failure AFTER part of the batch may have been written. `orphaned`
    lists objects whose deletion could NOT be confirmed — the caller must record
    them for reconciliation (durable orphan tracking), never assume they vanished.
    An empty `orphaned` means cleanup succeeded and nothing from the batch remains."""

    def __init__(self, message: str, orphaned: list[StoredEvidence]) -> None:
        # ObjectStoreError.__init__ formats its own message; this failure carries a
        # richer one, so the taxonomy attributes are set directly.
        self.status_code = None
        self.operation = "PUT"
        Exception.__init__(self, message)
        self.orphaned = orphaned


@dataclass(frozen=True)
class EvidenceObject:
    """A retrieved evidence object. sha256 is computed over the RETRIEVED bytes so a
    caller holding the upload-time digest can verify the object never changed."""

    body: bytes
    mime: str | None
    etag: str | None
    version_id: str | None
    sha256: str


@dataclass(frozen=True)
class EvidenceLimits:
    max_file_bytes: int
    max_request_bytes: int
    max_files_per_request: int
    max_evidence_per_issue: int
    allowed_mime_types: frozenset[str]

    @classmethod
    def from_settings(cls, settings: object) -> "EvidenceLimits":
        """Reads ONLY evidence_* settings fields — by construction it cannot see the
        ledger store's configuration, which is the structural config separation."""
        allowed = {
            token.strip().lower()
            for token in settings.evidence_allowed_mime_types.split(",")
            if token.strip()
        }
        return cls(
            max_file_bytes=settings.evidence_max_file_bytes,
            max_request_bytes=settings.evidence_max_request_bytes,
            max_files_per_request=settings.evidence_max_files_per_request,
            max_evidence_per_issue=settings.evidence_max_evidence_per_issue,
            allowed_mime_types=frozenset(allowed),
        )


def validate_files(
    files: Sequence[IncomingEvidence], limits: EvidenceLimits
) -> list[ValidatedEvidence]:
    """Whole-batch validation BEFORE any storage. Batch-shape reasons first, then
    each file; the first failure raises and nothing has been stored."""
    if len(files) > limits.max_files_per_request:
        raise EvidenceValidationError(
            "too_many_files",
            f"a supplement carries at most {limits.max_files_per_request} file(s)",
        )
    if sum(len(f.data) for f in files) > limits.max_request_bytes:
        raise EvidenceValidationError(
            "request_too_large",
            f"the supplement exceeds the {limits.max_request_bytes}-byte batch limit",
        )
    validated: list[ValidatedEvidence] = []
    for f in files:
        if len(f.data) == 0:
            raise EvidenceValidationError("empty_file", "an evidence file is empty")
        if len(f.data) > limits.max_file_bytes:
            raise EvidenceValidationError(
                "too_large",
                f"an evidence file exceeds the {limits.max_file_bytes}-byte limit",
            )
        sniffed = sniff_image_mime(f.data)
        if sniffed is None:
            raise EvidenceValidationError(
                "unsupported_type",
                "file content is not a recognized JPEG, PNG, or WebP image",
            )
        if sniffed not in limits.allowed_mime_types:
            raise EvidenceValidationError(
                "unsupported_type",
                f"{sniffed} is not an allowed evidence type",
            )
        if not _structure_ok(f.data, sniffed):
            raise EvidenceValidationError(
                "malformed_image",
                "file content does not parse as a complete image of its detected type",
            )
        declared = _normalize_declared_mime(f.declared_mime)
        if declared is not None and declared != sniffed:
            raise EvidenceValidationError(
                "mime_mismatch",
                "the declared content type does not match the file's actual content",
            )
        validated.append(
            ValidatedEvidence(
                data=f.data, mime=sniffed, sha256=hashlib.sha256(f.data).hexdigest()
            )
        )
    return validated


class EvidenceObjectStore(S3ObjectStore):
    """Generic keyed PUT/GET against the dedicated evidence bucket.

    Inherits ONLY the SigV4 signing machinery from S3ObjectStore (and its
    endpoint/region/bucket/credentials/timeout state). The parent's head-scoped
    public methods are deliberately not used and not re-exposed: an evidence code
    path must never be able to address the ledger's WORM head contract. Every
    request here targets an arbitrary key in the SAME bucket as this client was
    built with — which is the evidence bucket, never the ledger bucket, because
    from_settings builds this client exclusively from evidence_* configuration.
    """

    @property
    def bucket(self) -> str:
        # Public read-only view of the inherited private bucket name; the
        # EvidenceStoreClient protocol exposes `bucket` and StoredEvidence records it.
        return self._bucket

    async def _keyed_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        key: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        # Keys are server-generated and URL-safe by construction; anything read back
        # from a persisted record is still quoted before it touches a URL path.
        quoted = quote(key, safe="/")
        payload_hash = hashlib.sha256(body).hexdigest() if body else _EMPTY_BODY_SHA256
        path = f"/{self._bucket}/{quoted}"
        signed = self._signed_headers(method, path, "", headers or {}, payload_hash)
        try:
            return await client.request(
                method,
                f"{self._endpoint}/{self._bucket}/{quoted}",
                content=body if body else None,
                headers=signed,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransportFailure(None, method) from exc

    async def put_object(self, key: str, body: bytes, content_type: str) -> PutResult:
        """One unconditional PUT of an already-validated upload. Content-Type is the
        SNIFFED mime (never the client's declaration) — it is what the retrieval
        endpoint will serve as the object's type."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._keyed_request(
                client, "PUT", key, body=body, headers={"Content-Type": content_type}
            )
        if response.status_code == 200:
            return PutResult(
                etag=_header(response, "etag") or "",
                version_id=_header(response, "x-amz-version-id"),
            )
        raise _classify_put_error(response.status_code, "PUT")

    async def get_object(self, key: str) -> EvidenceObject:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._keyed_request(client, "GET", key)
        if response.status_code != 200:
            raise _classify_get_error(response.status_code, "GET")
        body = response.content
        return EvidenceObject(
            body=body,
            mime=_header(response, "content-type"),
            etag=_header(response, "etag"),
            version_id=_header(response, "x-amz-version-id"),
            sha256=hashlib.sha256(body).hexdigest(),
        )

    async def delete_object(self, key: str) -> bool:
        """DELETE one object. 404 counts as success — cleanup is idempotent by
        design. Any OTHER non-2xx, or a transport failure, RAISES: the caller cannot
        confirm the object is gone and must treat it as possibly still stored
        (orphan tracking), never as deleted."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._keyed_request(client, "DELETE", key)
        if response.status_code in (200, 204, 404):
            return True
        raise _classify_get_error(response.status_code, "DELETE")


class EvidenceStoreClient(Protocol):
    """Structural seam for hermetic tests: the facade needs exactly this surface."""

    bucket: str

    async def put_object(self, key: str, body: bytes, content_type: str) -> PutResult: ...

    async def get_object(self, key: str) -> EvidenceObject: ...

    async def delete_object(self, key: str) -> bool: ...


class EvidenceStore:
    """Validate-then-store facade. Production construction goes through
    from_settings ONLY — an unconfigured deployment has no store at all."""

    def __init__(self, client: EvidenceStoreClient, limits: EvidenceLimits) -> None:
        self._client = client
        self.limits = limits

    @classmethod
    def from_settings(
        cls, settings: object, client: EvidenceStoreClient | None = None
    ) -> "EvidenceStore | None":
        """None when evidence storage is not configured: empty endpoint, bucket, OR
        any credential is disable-by-default (never half-configure an upload target).
        An INJECTED client skips the credential check by design — the seam exists so
        hermetic tests exercise the full facade without a network, while production
        builds a real client only from fully present evidence_* configuration."""
        limits = EvidenceLimits.from_settings(settings)
        if client is None:
            endpoint = settings.evidence_object_store_endpoint
            bucket = settings.evidence_object_store_bucket
            access_key = settings.evidence_object_store_access_key
            secret_key = settings.evidence_object_store_secret_key
            if not (endpoint and bucket and access_key and secret_key):
                return None
            client = EvidenceObjectStore(
                endpoint_url=endpoint,
                region=settings.evidence_object_store_region,
                bucket=bucket,
                access_key=access_key,
                secret_key=secret_key,
                timeout_seconds=settings.evidence_object_store_timeout_seconds,
            )
        return cls(client, limits)

    async def store_files(
        self, mission_id: str, issue_id: str, files: Sequence[IncomingEvidence]
    ) -> EvidenceBatchResult:
        """Validate the WHOLE batch, then store each file. A validation failure
        leaves nothing stored (nothing has been written when validate_files raises).
        Keys are generated here — the caller never supplies one, and filenames never
        reach this layer. An empty batch stores nothing.

        Recovery semantics: if a PUT fails after earlier files succeeded, every
        already-stored object is deleted best-effort. Objects whose deletion could
        NOT be confirmed come back in EvidenceStoreFailure.orphaned — the caller
        must track them durably instead of pretending the batch vanished. A fully
        cleaned-up failure raises with an empty orphan list."""
        validated = validate_files(files, self.limits)
        if not validated:
            return EvidenceBatchResult(stored=[], orphan_candidates=[])
        stored: list[StoredEvidence] = []
        for item in validated:
            key = evidence_key(mission_id, issue_id)
            try:
                await self._client.put_object(key, item.data, item.mime)
            except ObjectStoreError as exc:
                orphans = await self._cleanup_after_failure(stored)
                if orphans:
                    raise EvidenceStoreFailure(
                        f"evidence storage failed mid-batch; {len(orphans)} stored "
                        "object(s) could not be confirmed deleted and require "
                        "reconciliation",
                        orphans,
                    ) from exc
                raise EvidenceStoreFailure(
                    "evidence storage failed mid-batch; already-stored objects were "
                    "deleted, nothing from this batch remains",
                    [],
                ) from exc
            stored.append(
                StoredEvidence(
                    storage_key=key,
                    bucket=self._client.bucket,
                    size_bytes=len(item.data),
                    mime=item.mime,
                    sha256=item.sha256,
                )
            )
        return EvidenceBatchResult(stored=stored, orphan_candidates=[])

    async def _cleanup_after_failure(
        self, stored: list[StoredEvidence]
    ) -> list[StoredEvidence]:
        """Best-effort delete of everything written so far. Returns the subset whose
        deletion could NOT be confirmed. Cleanup exceptions are swallowed ON PURPOSE:
        re-raising here would mask the original storage failure that triggered it."""
        orphans: list[StoredEvidence] = []
        for obj in stored:
            try:
                await self._client.delete_object(obj.storage_key)
            except ObjectStoreError:
                orphans.append(obj)
        return orphans

    async def read(self, key: str, *, expected_sha256: str | None = None) -> EvidenceObject:
        """Fetch one stored object. The key must be a key THIS store issued (the
        persisted-record shape) — a foreign or record-tampered key is refused before
        any request is made. expected_sha256, when given, pins integrity: a mismatch
        means the stored bytes changed since upload, which the caller must surface
        as an integrity failure, never repair silently."""
        if not _EVIDENCE_KEY_PATTERN.match(key):
            raise EvidenceValidationError("invalid_key", "storage key is not an evidence key")
        obj = await self._client.get_object(key)
        if expected_sha256 is not None and obj.sha256 != expected_sha256:
            raise EvidenceValidationError(
                "sha_mismatch", "stored bytes no longer match the recorded digest"
            )
        return obj
