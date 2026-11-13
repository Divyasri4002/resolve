"""
P2.1 battery — request-body resource protection for the evidence supplement endpoint.

The enforcement point is a pure-ASGI middleware (app.body_limit) that wraps `receive`
BEFORE the application runs, so body bytes are counted as they arrive and nothing over
the limit is ever handed to Starlette's multipart parser. Two layers are proven here at
two levels:

- middleware-level drivers (synthetic ASGI scope + stub app): exact chunk-by-chunk
  semantics — the Content-Length pre-check refuses without reading or calling the app;
  the counting wrapper refuses BEFORE delivering the exceeding chunk; a body exactly at
  the limit is delivered in full; non-matching requests pass through with the ORIGINAL
  receive/send (zero behavior change); a 413 is never sent after the response started.
- end-to-end through the real app: over-limit multipart and JSON are 413 before any
  gate, chunked bodies are counted without Content-Length, a body exactly at the limit
  reaches the endpoint, normal uploads below the limit are untouched, and client
  disconnect / malformed multipart produce honest 400s with zero storage writes.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

import app.body_limit as body_limit_module
from app.body_limit import (
    FRAMING_ALLOWANCE_BYTES,
    EvidenceBodyLimitMiddleware,
)
from app.main import app
from tests.test_evidence_endpoints import (
    _issue_row,
    _needs_review_issue,
    _setup,
    _supplement_multipart,
    install_fake_store,
)
from tests.test_evidence_store import JPEG_BYTES
from tests.test_refund_execution import (  # noqa: F401
    install_fake_reasoning,
    mock_razorpay,
    restart_env,
)

pytestmark = pytest.mark.asyncio

# The unit-driver threshold: evidence_max_request_bytes=1024 + the 64KiB framing
# allowance. Every middleware-level limit below is derived from this.
_UNIT_REQUEST_LIMIT = 1024
_UNIT_THRESHOLD = _UNIT_REQUEST_LIMIT + FRAMING_ALLOWANCE_BYTES


def _stub_settings(max_request_bytes: int) -> SimpleNamespace:
    return SimpleNamespace(
        evidence_max_file_bytes=10 * 1024 * 1024,
        evidence_max_request_bytes=max_request_bytes,
        evidence_max_files_per_request=5,
        evidence_max_evidence_per_issue=10,
        evidence_allowed_mime_types="image/jpeg,image/png,image/webp",
    )


def _small_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        body_limit_module, "get_settings", lambda: _stub_settings(_UNIT_REQUEST_LIMIT)
    )


def _scope(path="/missions/m1/order/issues/i1/supplement", method="POST", headers=()):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": list(headers),
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


class _StubApp:
    """Minimal inner ASGI app: drains the body through receive, records every chunk
    actually DELIVERED to it, and answers 200 once the body is exhausted."""

    def __init__(self):
        self.called = False
        self.chunks: list[bytes] = []
        self.raw_receive = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.raw_receive = receive
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            self.chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _EarlyResponseApp:
    """Starts the response BEFORE finishing the body read, then keeps reading until
    the counting wrapper raises — the middleware must not send a second response."""

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"early"})
        while True:
            await receive()


def _chunked_receive(chunks: list[bytes]):
    state = {"calls": 0, "index": 0}

    async def receive():
        state["calls"] += 1
        if state["index"] < len(chunks):
            chunk = chunks[state["index"]]
            state["index"] += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": state["index"] < len(chunks),
            }
        return {"type": "http.disconnect"}

    return receive, state


def _collect():
    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    return messages, send


async def _run_middleware(stub, scope, receive) -> list[dict]:
    messages, send = _collect()
    middleware = EvidenceBodyLimitMiddleware(stub)
    await middleware(scope, receive, send)
    return messages


def _response_starts(messages: list[dict]) -> list[int]:
    return [m["status"] for m in messages if m["type"] == "http.response.start"]


# --------------------------------------------------------------------------- #
# Layer 1: Content-Length pre-check — before the app, before any body byte
# --------------------------------------------------------------------------- #
async def test_content_length_over_limit_refuses_without_reading_or_calling_the_app(
    monkeypatch,
):
    _small_limit(monkeypatch)
    stub = _StubApp()
    receive, state = _chunked_receive([b"x" * 16])
    scope = _scope(headers=[(b"content-length", str(_UNIT_THRESHOLD + 1).encode())])

    messages = await _run_middleware(stub, scope, receive)

    assert stub.called is False
    assert state["calls"] == 0
    assert _response_starts(messages) == [413]
    assert messages[1]["body"] and messages[1]["type"] == "http.response.body"


async def test_lying_content_length_is_refused_by_the_counting_wrapper(monkeypatch):
    _small_limit(monkeypatch)
    # Declared 16 bytes; actually 80 x 1000 = 80_000 > threshold — layer 2 must catch it.
    stub = _StubApp()
    chunks = [b"x" * 1000] * 80
    receive, state = _chunked_receive(chunks)
    scope = _scope(headers=[(b"content-length", b"16")])

    messages = await _run_middleware(stub, scope, receive)

    assert _response_starts(messages) == [413]
    delivered = sum(len(c) for c in stub.chunks)
    assert delivered == 66 * 1000  # exactly 66 chunks; the 67th crossed the limit
    assert delivered <= _UNIT_THRESHOLD
    assert state["calls"] == len(stub.chunks) + 1  # the call that raised never returned
    assert state["calls"] < len(chunks)


async def test_invalid_content_length_falls_through_to_counting(monkeypatch):
    _small_limit(monkeypatch)
    stub = _StubApp()
    receive, state = _chunked_receive([b"x" * 8])
    scope = _scope(headers=[(b"content-length", b"not-a-number")])

    messages = await _run_middleware(stub, scope, receive)

    assert stub.called is True
    assert _response_starts(messages) == [200]
    assert sum(len(c) for c in stub.chunks) == 8


# --------------------------------------------------------------------------- #
# Layer 2: counting receive wrapper — chunked requests, exact-at-limit
# --------------------------------------------------------------------------- #
async def test_chunked_over_limit_refuses_while_streaming(monkeypatch):
    _small_limit(monkeypatch)
    stub = _StubApp()
    chunks = [b"x" * 1000] * 80  # 80_000 > 66_560
    receive, state = _chunked_receive(chunks)
    scope = _scope()  # no content-length header at all

    messages = await _run_middleware(stub, scope, receive)

    assert _response_starts(messages) == [413]
    delivered = sum(len(c) for c in stub.chunks)
    assert delivered == 66 * 1000
    assert delivered <= _UNIT_THRESHOLD
    assert state["calls"] < len(chunks)


async def test_chunked_exactly_at_limit_is_delivered_in_full(monkeypatch):
    _small_limit(monkeypatch)
    stub = _StubApp()
    chunks = [b"x" * 1000] * 66 + [b"x" * 560]  # sums exactly to the threshold
    receive, state = _chunked_receive(chunks)
    scope = _scope()

    messages = await _run_middleware(stub, scope, receive)

    assert stub.called is True
    assert sum(len(c) for c in stub.chunks) == _UNIT_THRESHOLD
    assert state["calls"] == len(chunks)
    assert _response_starts(messages) == [200]
    assert not any(m["type"] == "http.response.start" and m["status"] == 413 for m in messages)


async def test_content_length_exactly_at_limit_passes(monkeypatch):
    _small_limit(monkeypatch)
    stub = _StubApp()
    receive, _ = _chunked_receive([b"x" * _UNIT_THRESHOLD])
    scope = _scope(headers=[(b"content-length", str(_UNIT_THRESHOLD).encode())])

    messages = await _run_middleware(stub, scope, receive)

    assert stub.called is True
    assert sum(len(c) for c in stub.chunks) == _UNIT_THRESHOLD
    assert _response_starts(messages) == [200]


async def test_413_is_never_sent_after_the_response_has_started(monkeypatch):
    _small_limit(monkeypatch)
    chunks = [b"x" * 1000] * 80
    receive, _ = _chunked_receive(chunks)
    messages, send = _collect()
    middleware = EvidenceBodyLimitMiddleware(_EarlyResponseApp())

    await middleware(_scope(), receive, send)

    assert _response_starts(messages) == [200]


async def test_disconnect_message_passes_through_unchanged(monkeypatch):
    _small_limit(monkeypatch)
    stub = _StubApp()

    async def receive():
        return {"type": "http.disconnect"}

    messages = await _run_middleware(stub, _scope(), receive)

    assert stub.called is True
    assert messages == []  # the app chose not to answer; the middleware added nothing


# --------------------------------------------------------------------------- #
# Scope discipline: only the supplement POST is ever intercepted
# --------------------------------------------------------------------------- #
async def test_non_matching_requests_pass_through_with_the_original_receive():
    stub = _StubApp()
    middleware = EvidenceBodyLimitMiddleware(stub)
    messages, send = _collect()

    for scope in (
        _scope(method="GET", path="/health"),
        _scope(path="/missions/m1/order/issues/i1/assess"),
        _scope(method="PUT"),
    ):
        receive, _ = _chunked_receive([b"hi"])
        await middleware(scope, receive, send)
        assert stub.raw_receive is receive  # never wrapped for non-matching requests

    assert _response_starts(messages) == [200, 200, 200]


async def test_cors_wraps_the_body_limit_middleware_so_the_413_reaches_browsers():
    classes = [m.cls for m in app.user_middleware]
    assert classes.index(CORSMiddleware) < classes.index(EvidenceBodyLimitMiddleware)


# --------------------------------------------------------------------------- #
# End-to-end through the real app (middleware rejects before ANY endpoint gate)
# --------------------------------------------------------------------------- #
async def _bare_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_e2e_multipart_over_limit_is_413_before_any_gate(monkeypatch):
    _small_limit(monkeypatch)
    async with await _bare_client() as client:
        resp = await client.post(
            "/missions/no-such-mission/order/issues/no-such-issue/supplement",
            content=b"x" * (_UNIT_THRESHOLD + 1),
            headers={"content-type": "multipart/form-data; boundary=bb"},
        )
    assert resp.status_code == 413
    assert f"{_UNIT_REQUEST_LIMIT}-byte request limit" in resp.json()["detail"]


async def test_e2e_chunked_over_limit_is_413(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    monkeypatch,
):
    """Layer 2 has to be proven on a request whose body is actually parsed: the
    supplement endpoint reads the body only after every gate passes, so an
    over-limit chunked upload to a real needs-review issue is refused mid-parse."""
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    _small_limit(monkeypatch)
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        boundary = "bb"
        filler = b"\x00" * 80000  # valid multipart framing; arbitrary part bytes
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="big.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + filler + f"\r\n--{boundary}--\r\n".encode()

        async def chunks():
            yield body[:40000]
            yield body[40000:]  # this half pushes the total past the limit

        resp = await client.post(
            f"/missions/{mission_id}/order/issues/{issue_id}/supplement",
            content=chunks(),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )

        assert resp.status_code == 413
        assert f"{_UNIT_REQUEST_LIMIT}-byte request limit" in resp.json()["detail"]
        assert fake.objects == {}
        row = await _issue_row(test_session, issue_id)
        assert list(row.evidence or []) == []


async def test_e2e_json_over_limit_is_413(monkeypatch):
    _small_limit(monkeypatch)
    body = json.dumps({"note": "x" * (_UNIT_THRESHOLD + 1)}).encode()
    async with await _bare_client() as client:
        resp = await client.post(
            "/missions/no-such-mission/order/issues/no-such-issue/supplement",
            content=body,
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 413


# --------------------------------------------------------------------------- #
# End-to-end through the real app, authenticated, with the issue gates
# --------------------------------------------------------------------------- #
def _exact_note_only_body(total: int, boundary: str = "exactbb") -> bytes:
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="note"\r\n\r\n'
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + b"n" * (total - len(head) - len(tail)) + tail


async def test_e2e_chunked_exactly_at_limit_reaches_the_endpoint(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    install_fake_store()
    _small_limit(monkeypatch)
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        body = _exact_note_only_body(_UNIT_THRESHOLD)

        async def chunks():
            yield body[: _UNIT_THRESHOLD // 2]
            yield body[_UNIT_THRESHOLD // 2 :]

        resp = await client.post(
            f"/missions/{mission_id}/order/issues/{issue_id}/supplement",
            content=chunks(),
            headers={"content-type": "multipart/form-data; boundary=exactbb"},
        )

        assert resp.status_code == 200
        row = await _issue_row(test_session, issue_id)
        assert row.status == "reported"
        assert [e for e in row.evidence if e.get("kind") == "uploaded_file"] == []


async def test_e2e_content_length_exactly_at_limit_reaches_the_endpoint(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    install_fake_store()
    _small_limit(monkeypatch)
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        body = _exact_note_only_body(_UNIT_THRESHOLD)

        resp = await client.post(
            f"/missions/{mission_id}/order/issues/{issue_id}/supplement",
            content=body,
            headers={"content-type": "multipart/form-data; boundary=exactbb"},
        )

        assert resp.status_code == 200
        row = await _issue_row(test_session, issue_id)
        assert row.status == "reported"


async def test_e2e_normal_multipart_below_limit_is_completely_untouched(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
):
    """The middleware with REAL settings must not perturb the normal upload path."""
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        resp = await _supplement_multipart(
            client, mission_id, issue_id, note="a normal upload",
            files=[("photo.jpg", JPEG_BYTES, "image/jpeg")],
        )

        assert resp.status_code == 200
        assert len(fake.objects) == 1
        row = await _issue_row(test_session, issue_id)
        assert [e for e in row.evidence if e.get("kind") == "uploaded_file"]


# --------------------------------------------------------------------------- #
# Parser interruption and client disconnect behavior
# --------------------------------------------------------------------------- #
async def _drive_asgi_with_disconnect(client, mission_id, issue_id) -> list[dict]:
    """POST through the real ASGI app, but the client disappears after one chunk."""
    cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
    path = f"/missions/{mission_id}/order/issues/{issue_id}/supplement"
    scope = _scope(
        path=path,
        headers=[
            (b"content-type", b"multipart/form-data; boundary=disco"),
            (b"content-length", b"512"),
            (b"cookie", cookie.encode()),
        ],
    )
    messages, send = _collect()
    sent = {"chunk": False}

    async def receive():
        if not sent["chunk"]:
            sent["chunk"] = True
            return {"type": "http.request", "body": b"--disco\r\n", "more_body": True}
        return {"type": "http.disconnect"}

    await app(scope, receive, send)
    return messages


async def test_e2e_client_disconnect_mid_body_is_a_400_with_zero_storage(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        before = await _issue_row(test_session, issue_id)
        evidence_before = list(before.evidence or [])

        messages = await _drive_asgi_with_disconnect(client, mission_id, issue_id)

        assert _response_starts(messages) == [400]
        detail = json.loads(messages[1]["body"])["detail"]
        assert detail == "The request was aborted before the body was fully received"
        assert fake.objects == {}
        after = await _issue_row(test_session, issue_id)
        assert list(after.evidence or []) == evidence_before
        assert after.status == before.status


async def test_e2e_multipart_missing_boundary_is_a_400_with_zero_storage(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        before = await _issue_row(test_session, issue_id)
        evidence_before = list(before.evidence or [])

        resp = await client.post(
            f"/missions/{mission_id}/order/issues/{issue_id}/supplement",
            content=b"--orphan\r\nnot really a form\r\n",
            headers={"content-type": "multipart/form-data"},
        )

        assert resp.status_code == 400
        # starlette 0.38.6's Request.form() converts MultiPartException itself:
        # HTTPException(400, exc.message) — the parser's own reason is the detail.
        assert resp.json()["detail"] == "Missing boundary in multipart."
        assert fake.objects == {}
        after = await _issue_row(test_session, issue_id)
        assert list(after.evidence or []) == evidence_before


async def test_e2e_truncated_multipart_body_never_stores_partial_bytes(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
):
    """A body whose terminal boundary never arrives: starlette 0.38.6 parses the
    COMPLETED parts only (the file part is dropped whole, never partially spooled),
    so the endpoint behaves as a note-only supplement — 200, zero storage writes,
    evidence unchanged. Nothing partial is ever persisted."""
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        before = await _issue_row(test_session, issue_id)
        evidence_before = list(before.evidence or [])

        boundary = "truncbb"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="note"\r\n\r\n'
            "the note that completed\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="p.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode() + JPEG_BYTES  # terminal boundary deliberately missing
        resp = await client.post(
            f"/missions/{mission_id}/order/issues/{issue_id}/supplement",
            content=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )

        assert resp.status_code == 200
        assert fake.objects == {}
        after = await _issue_row(test_session, issue_id)
        assert list(after.evidence or []) == evidence_before
        assert [e for e in after.evidence if e.get("kind") == "uploaded_file"] == []
