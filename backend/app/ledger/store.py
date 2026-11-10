"""
Minimal async S3-compatible object-store client for the evidence publisher (Phase 4.6.2).

Why hand-rolled SigV4 over httpx instead of boto3: the approved design (4.5.6/4.5.7)
advances the WORM head exclusively through conditional PUTs — `If-Match` carrying the
ETAG validator (never the VersionId) and, for genesis only, `If-None-Match: *` — and
classifies the response (200 / 412 / 409 / 404 / 5xx / transport) through the publisher's
own state machine. A minimal client gives exact control over those headers and error
semantics without adding a dependency.

Semantics proven by the conformance battery (tests/test_ledger_s3_conformance.py) against
a real S3-compatible server:
  - If-Match compares the ETag of the CURRENT version (4.5.7's S3-verified correction);
  - every successful PUT creates a NEW object VERSION with a fresh VersionId + ETag;
  - a conditional PUT against a missing object is a 404 (never silently recreated);
  - 412 and 409 are both "someone else moved the head" conflicts.

Nothing here logs credentials, payloads, or URLs with credentials. Errors carry status
codes only.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote
from xml.etree import ElementTree

import httpx


class ObjectStoreError(Exception):
    """Base class; message never contains credentials or payload content."""

    def __init__(self, status_code: int | None, operation: str) -> None:
        self.status_code = status_code
        self.operation = operation
        super().__init__(f"object store {operation} failed (status={status_code})")


class ObjectNotFound(ObjectStoreError):
    pass


class PreconditionFailed(ObjectStoreError):
    """HTTP 412 — the If-Match validator did not match the current version."""


class ConflictError(ObjectStoreError):
    """HTTP 409 — the server refused the write as conflicting."""


class ServerError(ObjectStoreError):
    """HTTP 5xx — ambiguous; the publisher must re-observe, never assume."""


class TransportFailure(ObjectStoreError):
    """Timeout / connection error — outcome unknown; re-observe decides."""


@dataclass(frozen=True)
class GetResult:
    body: bytes
    etag: str
    version_id: str | None


@dataclass(frozen=True)
class PutResult:
    etag: str
    version_id: str | None


@dataclass(frozen=True)
class VersionInfo:
    version_id: str
    etag: str
    is_latest: bool
    is_delete_marker: bool


@dataclass(frozen=True)
class ObjectLockConfiguration:
    """The bucket's Object Lock configuration (GET /?object-lock).

    `enabled` is the ObjectLockEnabled element ("Enabled"); `mode`/`days`/`years` come
    from the optional Rule/DefaultRetention — a lock-enabled bucket with no default
    retention reports enabled=True with all three None, which the WORM policy treats as
    a misconfiguration (every version written before a default is set is unprotected).
    """

    enabled: bool
    mode: str | None
    days: int | None
    years: int | None


@dataclass(frozen=True)
class LifecycleRule:
    """One bucket lifecycle rule (GET /?lifecycle). Only the deletion-relevant shape
    is surfaced: an Enabled rule carrying an Expiration or NoncurrentVersionExpiration
    element can purge object versions once their retention expires, which would break
    the verifier's oldest-first chain walk — the WORM policy refuses such rules."""

    status: str
    expires: bool
    expires_noncurrent: bool


_EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


def _canonical_query(query: str) -> str:
    """SigV4 canonical query string: URI-encode every pair, sort by name. A
    valueless subresource (e.g. `versions`) is signed as `versions=`."""
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append((quote(name, safe="-_.~"), quote(value, safe="-_.~")))
    return "&".join(f"{n}={v}" for n, v in sorted(pairs))


class S3ObjectStore:
    """Path-style S3 client with SigV4 signing, scoped to exactly one bucket + key."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        head_key: str = "head.json",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._endpoint = endpoint_url.rstrip("/")
        self._region = region
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._head_key = head_key.lstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))

    # ------------------------------------------------------------------ signing --
    def _signed_headers(self, method: str, path: str, query: str, headers: dict[str, str], payload_hash: str) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        signed = {
            "host": httpx.URL(self._endpoint).host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            **{k.lower(): v for k, v in headers.items()},
        }
        signed_names = sorted(signed)
        canonical_headers = "".join(f"{name}:{signed[name].strip()}\n" for name in signed_names)
        signed_header_list = ";".join(signed_names)

        canonical_request = "\n".join(
            [method, path, _canonical_query(query), canonical_headers, signed_header_list, payload_hash]
        )
        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        def _hmac(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        signing_key = _hmac(
            _hmac(_hmac(_hmac(f"AWS4{self._secret_key}".encode("utf-8"), date_stamp), self._region), "s3"),
            "aws4_request",
        )
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_header_list}, Signature={signature}"
        )
        return {**signed, "Authorization": authorization}

    def _url(self, query: str = "", *, bucket_level: bool = False) -> str:
        url = f"{self._endpoint}/{self._bucket}" if bucket_level else f"{self._endpoint}/{self._bucket}/{self._head_key}"
        return f"{url}?{query}" if query else url

    def _path_and_query(self, query: str = "", *, bucket_level: bool = False) -> tuple[str, str]:
        path = f"/{self._bucket}" if bucket_level else f"/{self._bucket}/{self._head_key}"
        return path, query

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        *,
        query: str = "",
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        bucket_level: bool = False,
    ) -> httpx.Response:
        payload_hash = hashlib.sha256(body).hexdigest() if body else _EMPTY_BODY_SHA256
        path, canonical_query = self._path_and_query(query, bucket_level=bucket_level)
        signed = self._signed_headers(method, path, canonical_query, headers or {}, payload_hash)
        try:
            return await client.request(
                method, self._url(query, bucket_level=bucket_level), content=body if body else None, headers=signed
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransportFailure(None, method) from exc

    # ------------------------------------------------------------------ public ---
    async def get_head(self) -> GetResult | None:
        """The current head object, or None when it does not exist (404)."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._request(client, "GET")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _classify_get_error(response.status_code, "GET")
        return GetResult(
            body=response.content,
            etag=_header(response, "etag") or "",
            version_id=_header(response, "x-amz-version-id"),
        )

    async def put_head(
        self, body: bytes, *, if_match: str | None = None, create_only: bool = False
    ) -> PutResult:
        """
        One conditional PUT. Exactly one of if_match / create_only may be set:
        if_match advances the head through the observed ETag; create_only performs the
        atomic genesis (If-None-Match: *). An unconditional overwrite is not offered.
        """
        if if_match is None and not create_only:
            raise ValueError("refusing an unconditional overwrite: pass if_match or create_only")
        if if_match is not None and create_only:
            raise ValueError("if_match and create_only are mutually exclusive")

        headers: dict[str, str] = {}
        if if_match is not None:
            headers["If-Match"] = if_match
        if create_only:
            headers["If-None-Match"] = "*"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._request(client, "PUT", body=body, headers=headers)

        if response.status_code == 200:
            return PutResult(
                etag=_header(response, "etag") or "",
                version_id=_header(response, "x-amz-version-id"),
            )
        raise _classify_put_error(response.status_code, "PUT")

    async def get_version(self, version_id: str) -> GetResult:
        """
        One specific historical version of the head object (the verifier's version-chain
        walk). 404 means the version existed in the listing but can no longer be fetched —
        an integrity failure the caller must surface, never repair.
        """
        query = "versionId=" + quote(version_id, safe="")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._request(client, "GET", query=query)
        if response.status_code == 404:
            raise ObjectNotFound(404, "GET")
        if response.status_code != 200:
            raise _classify_get_error(response.status_code, "GET")
        return GetResult(
            body=response.content,
            etag=_header(response, "etag") or "",
            version_id=_header(response, "x-amz-version-id"),
        )

    async def list_head_versions(self) -> list[VersionInfo]:
        """
        All versions of the head object, OLDEST FIRST. Delete markers are included —
        a latest delete marker means the head has been deleted, which the verifier and
        the publisher must treat as a missing trust anchor, never as an invitation to
        recreate it.
        """
        # ListObjectVersions is a BUCKET-level API: GET /bucket?versions&prefix=key.
        # Requesting it on the object path would make the provider ignore the versions
        # subresource and serve the object body instead of the version listing.
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._request(
                client, "GET", query="versions&prefix=" + self._head_key, bucket_level=True
            )
        if response.status_code != 200:
            raise _classify_get_error(response.status_code, "LIST")
        return _parse_versions_xml(response.text, self._head_key)

    # ------------------------------------------------- bucket policy queries (4.6.3) --
    async def get_bucket_versioning(self) -> str:
        """GET /?versioning — "Enabled", "Suspended", or "" when never configured."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._request(
                client, "GET", query="versioning", bucket_level=True
            )
        if response.status_code != 200:
            raise _classify_get_error(response.status_code, "VERSIONING-GET")
        return _first_text(response.text, ("Status",)) or ""

    async def get_bucket_object_lock_configuration(self) -> ObjectLockConfiguration | None:
        """GET /?object-lock — None when the bucket has no Object Lock configuration
        (AWS answers 404 NoSuchObjectLockConfiguration; the absence decision belongs
        to the caller, which must refuse publication on it)."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._request(
                client, "GET", query="object-lock", bucket_level=True
            )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _classify_get_error(response.status_code, "OBJECT-LOCK-GET")
        enabled = (_first_text(response.text, ("ObjectLockEnabled",)) or "") == "Enabled"
        return ObjectLockConfiguration(
            enabled=enabled,
            mode=_first_text(response.text, ("Mode",)),
            days=_first_int(response.text, ("Days",)),
            years=_first_int(response.text, ("Years",)),
        )

    async def get_bucket_lifecycle_rules(self) -> list[LifecycleRule]:
        """GET /?lifecycle — [] when no lifecycle configuration exists (404)."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await self._request(
                client, "GET", query="lifecycle", bucket_level=True
            )
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise _classify_get_error(response.status_code, "LIFECYCLE-GET")
        root = ElementTree.fromstring(response.text)
        namespace = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""

        def _find(node: ElementTree.Element, name: str) -> ElementTree.Element | None:
            tag = f"{{{namespace}}}{name}" if namespace else name
            return node.find(tag)

        rules: list[LifecycleRule] = []
        for node in root:
            if node.tag.split("}")[-1] != "Rule":
                continue
            status_node = _find(node, "Status")
            expiration = _find(node, "Expiration") is not None
            noncurrent = _find(node, "NoncurrentVersionExpiration") is not None
            rules.append(
                LifecycleRule(
                    status=status_node.text if status_node is not None and status_node.text else "",
                    expires=expiration,
                    expires_noncurrent=noncurrent,
                )
            )
        return rules


def _header(response: httpx.Response, name: str) -> str | None:
    value = response.headers.get(name)
    return value.strip() if value else None


def _first_text(xml_text: str, names: tuple[str, ...]) -> str | None:
    """The text of the FIRST element whose local name matches, anywhere in the document
    (namespace-agnostic — provider XML uses the S3 namespace but its form varies)."""
    root = ElementTree.fromstring(xml_text)
    for name in names:
        for node in root.iter():
            if node.tag.split("}")[-1] == name and node.text:
                return node.text.strip()
    return None


def _first_int(xml_text: str, names: tuple[str, ...]) -> int | None:
    text = _first_text(xml_text, names)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _classify_get_error(status_code: int, operation: str) -> ObjectStoreError:
    if status_code == 404:
        return ObjectNotFound(status_code, operation)
    if 500 <= status_code <= 599:
        return ServerError(status_code, operation)
    return ObjectStoreError(status_code, operation)


def _classify_put_error(status_code: int, operation: str) -> ObjectStoreError:
    if status_code == 404:
        return ObjectNotFound(status_code, operation)
    if status_code == 412:
        return PreconditionFailed(status_code, operation)
    if status_code == 409:
        return ConflictError(status_code, operation)
    if 500 <= status_code <= 599:
        return ServerError(status_code, operation)
    return ObjectStoreError(status_code, operation)


def _parse_versions_xml(xml_text: str, key: str) -> list[VersionInfo]:
    root = ElementTree.fromstring(xml_text)
    namespace = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
    versions: list[VersionInfo] = []

    def _find(node: ElementTree.Element, name: str) -> ElementTree.Element | None:
        tag = f"{{{namespace}}}{name}" if namespace else name
        return node.find(tag)

    for node in root:
        local = node.tag.split("}")[-1]
        if local not in ("Version", "DeleteMarker"):
            continue
        key_node = _find(node, "Key")
        if key_node is None or key_node.text != key:
            continue
        version_node = _find(node, "VersionId")
        is_latest = (_find(node, "IsLatest") is not None and _find(node, "IsLatest").text == "true")
        etag = ""
        etag_node = _find(node, "ETag")
        if etag_node is not None and etag_node.text:
            etag = etag_node.text
        versions.append(
            VersionInfo(
                version_id=version_node.text if version_node is not None and version_node.text else "",
                etag=etag,
                is_latest=is_latest,
                is_delete_marker=(local == "DeleteMarker"),
            )
        )
    # ListObjectVersions returns newest-first; the chain walk needs oldest-first.
    versions.reverse()
    return versions
