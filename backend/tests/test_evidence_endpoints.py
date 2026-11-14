"""
Stage 2 (P2) endpoint battery — multipart evidence upload + authenticated retrieval.

The supplement endpoint accepts multipart/form-data carrying REAL uploaded bytes
alongside (or instead of) the JSON note path. Every scenario here proves one of the
mandated behaviors end-to-end over HTTP against the shared app:

- actual byte storage: the fake store holds the EXACT uploaded bytes, and the persisted
  metadata records the true SHA-256 of those bytes;
- whole-batch validation with ZERO partial persistence: a rejected batch (malformed
  image, wrong type, too many files, over budget, over the per-issue cap) leaves the
  fake store empty and the issue record untouched;
- size/count boundaries enforced while streaming (per-file cap cuts an oversized read
  off mid-read; the Content-Length pre-check refuses the request before parsing);
- partial storage failure: the already-stored prefix is cleaned up (and, when cleanup
  cannot be confirmed, durably tracked via the `evidence_orphan_tracked` audit);
- authenticated retrieval: ownership-checked, index-checked, bytes verified against the
  recorded SHA-256, served with nosniff + inline disposition + private no-store;
- the reassessment context sees COMPACT upload metadata — never storage keys, buckets,
  digests, or bytes;
- the existing JSON note path is untouched (the M3/T1/T2 battery runs separately).
"""
import hashlib
import re

import pytest
from sqlalchemy import func, select

from app.main import app
from app.models.db_models import AuditLogRecord, OrderIssueRecord, ResolutionAssessmentRecord
from app.routers import missions as missions_module
from app.routers.missions import get_evidence_store as missions_get_evidence_store
from tests.test_evidence_store import (
    FakeEvidenceClient,
    JPEG_BYTES,
    JPEG_TRUNCATED,
    PNG_BYTES,
    SVG_BYTES,
    make_limits_only_settings,
)
from tests.test_refund_execution import (  # noqa: F401
    _login,
    _register,
    install_fake_reasoning,
    mock_razorpay,
    restart_env,
)
from tests.test_replacement_execution import _assess, _issue, _setup

pytestmark = pytest.mark.asyncio

_KEY_SHAPE = re.compile(r"^evidence/[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}/[0-9a-f]{32}\.bin$")


def _needs_review_proposal(refund="needs_review", replacement="needs_review") -> dict:
    return {
        "issue_assessment": "The report is read; more evidence may change the verdict.",
        "proposed_action": "request_more_information",
        "reasoning_summary": "Controlled proposal for the upload battery.",
        "requested_amount": None,
        "currency": "INR",
        "confidence": 0.9,
        "needs_escalation": False,
        "remedy_eligibility": {
            "refund": {"eligibility": refund, "reason": "battery verdict"},
            "replacement": {"eligibility": replacement, "reason": "battery verdict"},
        },
    }


def install_fake_store() -> FakeEvidenceClient:
    """Swap the evidence-store dependency for an in-memory fake; returns the fake."""
    from app.evidence_store import EvidenceStore

    fake = FakeEvidenceClient()
    store = EvidenceStore.from_settings(make_limits_only_settings(), fake)
    app.dependency_overrides[missions_get_evidence_store] = lambda: store
    return fake


def _limit_overrides(**overrides):
    """A settings namespace that overrides ONLY the evidence_* limit fields and
    delegates everything else to the real cached Settings — other code paths
    (authority config, checkout gates) keep working during the same test."""
    from app.config import get_settings as real_get_settings

    real = real_get_settings()

    class _Limited:
        def __getattr__(self, name):
            if name in overrides:
                return overrides[name]
            return getattr(real, name)

    return _Limited()


async def _audits(test_session, mission_id):
    async with test_session() as session:
        return (
            await session.scalars(
                select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
            )
        ).all()


async def _issue_row(test_session, issue_id):
    async with test_session() as session:
        return await session.get(OrderIssueRecord, issue_id)


async def _supplement_multipart(client, mission_id, issue_id, note=None, description=None, files=()):
    data = {}
    if note is not None:
        data["note"] = note
    if description is not None:
        data["description"] = description
    uploads = [("files", (name, body, declared)) for name, body, declared in files]
    return await client.post(
        f"/missions/{mission_id}/order/issues/{issue_id}/supplement",
        data=data,
        files=uploads,
    )


async def _needs_review_issue(client, mission_setup, install_fake_reasoning):
    mission_id = await mission_setup(client)
    install_fake_reasoning(_needs_review_proposal())
    issue_id = await _issue(client, mission_id, kind="refund")
    await _assess(client, mission_id, issue_id)
    return mission_id, issue_id


# --------------------------------------------------------------------------- #
# Happy path: bytes stored, metadata persisted, audit names exactly what arrived
# --------------------------------------------------------------------------- #
async def test_multipart_upload_stores_actual_bytes_and_persists_metadata(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        resp = await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            note="A photo of the damaged unit is attached.",
            files=[
                ("photo.jpg", JPEG_BYTES, "image/jpeg"),
                ("scan.png", PNG_BYTES, "image/png"),
            ],
        )
        assert resp.status_code == 200, resp.text
        issue = resp.json()
        assert issue["status"] == "reported"
        assert len(issue["evidence"]) == 2

        first = issue["evidence"][0]
        assert first["kind"] == "uploaded_file"
        assert first["display_name"] == "photo.jpg"
        assert first["mime"] == "image/jpeg"
        assert first["size_bytes"] == len(JPEG_BYTES)
        assert issue["evidence"][1]["mime"] == "image/png"

        # The client-facing payload never carries storage handles — the browser sees
        # WHAT was attached, never where it lives.
        for item in issue["evidence"]:
            for forbidden in ("storage_key", "bucket", "sha256"):
                assert forbidden not in item

        # Storage truth lives in the persisted row: the store holds the EXACT upload,
        # keyed as recorded, typed by the SNIFFED signature.
        row = await _issue_row(test_session, issue_id)
        row_first = row.evidence[0]
        assert row_first["sha256"] == hashlib.sha256(JPEG_BYTES).hexdigest()
        assert _KEY_SHAPE.match(row_first["storage_key"])
        assert row_first["bucket"] == fake.bucket
        assert fake.objects[row_first["storage_key"]] == (JPEG_BYTES, "image/jpeg")

        # The audit names exactly what arrived — the number the UI reports.
        audits = await _audits(test_session, mission_id)
        supplemented = [a for a in audits if a.action == "issue_evidence_supplemented"]
        assert len(supplemented) == 1
        assert "2 evidence item(s)" in supplemented[0].reason
        assert "note=provided" in supplemented[0].reason


async def test_upload_without_note_is_accepted_and_audited_as_note_none(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        resp = await _supplement_multipart(
            client, mission_id, issue_id, files=[("only.png", PNG_BYTES, "image/png")]
        )
        assert resp.status_code == 200, resp.text
        audits = await _audits(test_session, mission_id)
        supplemented = [a for a in audits if a.action == "issue_evidence_supplemented"]
        assert "note=none" in supplemented[0].reason


# --------------------------------------------------------------------------- #
# Retrieval: authenticated, exact bytes, safety headers
# --------------------------------------------------------------------------- #
async def test_retrieval_serves_the_exact_uploaded_bytes_with_safety_headers(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            files=[("photo.jpg", JPEG_BYTES, "image/jpeg")],
        )

        resp = await client.get(f"/missions/{mission_id}/order/issues/{issue_id}/evidence/0")
        assert resp.status_code == 200, resp.text
        assert resp.content == JPEG_BYTES
        assert resp.headers["content-type"].startswith("image/jpeg")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-disposition"] == 'inline; filename="photo.jpg"'
        assert resp.headers["cache-control"] == "private, no-store"


async def test_retrieval_rejects_cross_user_invalid_index_and_non_upload_kinds(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    make_client, _ = restart_env
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        await _supplement_multipart(
            client, mission_id, issue_id, files=[("photo.jpg", JPEG_BYTES, "image/jpeg")]
        )
        base = f"/missions/{mission_id}/order/issues/{issue_id}/evidence"

        # Invalid indices: out of range.
        assert (await client.get(f"{base}/99")).status_code == 404
        assert (await client.get(f"{base}/-1")).status_code == 404

        # A JSON-seeded evidence item (external reference) is NOT a stored file: even
        # a valid index for it is refused — only upload-backed items are servable.
        async with test_session() as session:
            row = await session.get(OrderIssueRecord, issue_id)
            row.evidence = [{"kind": "photo_url", "url": "https://example.test/x.jpg"}]
            await session.commit()
        assert (await client.get(f"{base}/0")).status_code == 404

        # Restore an upload-backed record, then prove a different registered account
        # sees 404 — never the bytes.
        async with test_session() as session:
            row = await session.get(OrderIssueRecord, issue_id)
            row.evidence = [
                {
                    "kind": "uploaded_file",
                    "display_name": "photo.jpg",
                    "mime": "image/jpeg",
                    "size_bytes": len(JPEG_BYTES),
                    "sha256": hashlib.sha256(JPEG_BYTES).hexdigest(),
                    "storage_key": f"evidence/{mission_id}/{issue_id}/" + "a" * 32 + ".bin",
                    "bucket": "resolve-evidence-test",
                }
            ]
            await session.commit()
        other = make_client()
        async with other:
            await _register(other, "Other Person", "other@shop.com")
            await _login(other, "other@shop.com")
            assert (await other.get(f"{base}/0")).status_code == 404


async def test_retrieval_refuses_bytes_that_no_longer_match_the_recorded_digest(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        await _supplement_multipart(
            client, mission_id, issue_id, files=[("photo.jpg", JPEG_BYTES, "image/jpeg")]
        )

        # The stored bytes changed under us (tampered bucket): the sha pin must
        # surface as an integrity failure, never as silent wrong bytes.
        async with test_session() as session:
            row = await session.get(OrderIssueRecord, issue_id)
            key = row.evidence[0]["storage_key"]
        fake.objects[key] = (JPEG_BYTES[:-2] + b"xx", "image/jpeg")
        resp = await client.get(f"/missions/{mission_id}/order/issues/{issue_id}/evidence/0")
        assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Validation failures: zero partial persistence
# --------------------------------------------------------------------------- #
async def test_rejected_batch_leaves_zero_partial_persistence(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        before = (await _issue_row(test_session, issue_id)).evidence

        # A valid JPEG next to an SVG: the WHOLE batch is refused (validate-all-
        # then-store), nothing reaches storage, the issue record is untouched.
        resp = await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            note="mixed batch",
            files=[("ok.jpg", JPEG_BYTES, "image/jpeg"), ("evil.svg", SVG_BYTES, "image/svg+xml")],
        )
        assert resp.status_code == 415
        assert fake.objects == {}
        row = await _issue_row(test_session, issue_id)
        assert row.evidence == before
        assert row.status == "under_review"


async def test_malformed_image_is_refused_even_though_its_signature_looks_valid(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        # A truncated JPEG: correct magic bytes, no EOI marker — refused as malformed.
        resp = await _supplement_multipart(
            client, mission_id, issue_id, files=[("trunc.jpg", JPEG_TRUNCATED, "image/jpeg")]
        )
        assert resp.status_code == 415
        assert fake.objects == {}


async def test_unsupported_types_are_refused_with_415(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        resp = await _supplement_multipart(
            client, mission_id, issue_id, files=[("evil.svg", SVG_BYTES, "image/svg+xml")]
        )
        assert resp.status_code == 415
        assert fake.objects == {}


# --------------------------------------------------------------------------- #
# Size and count boundaries, enforced while streaming
# --------------------------------------------------------------------------- #
async def test_too_many_files_is_refused_before_any_bytes_are_read(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    monkeypatch.setattr(
        missions_module,
        "get_settings",
        lambda: _limit_overrides(evidence_max_files_per_request=3),
    )
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        files = [(f"p{i}.jpg", JPEG_BYTES, "image/jpeg") for i in range(4)]
        resp = await _supplement_multipart(client, mission_id, issue_id, files=files)
        assert resp.status_code == 400
        assert fake.objects == {}

        # Exactly at the cap is accepted.
        resp = await _supplement_multipart(client, mission_id, issue_id, files=files[:3])
        assert resp.status_code == 200, resp.text


async def test_oversized_file_is_cut_off_during_the_chunked_read(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    monkeypatch.setattr(
        missions_module,
        "get_settings",
        lambda: _limit_overrides(evidence_max_file_bytes=64),
    )
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        resp = await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            files=[("big.jpg", JPEG_BYTES + b"x" * 200, "image/jpeg")],
        )
        assert resp.status_code == 413
        assert fake.objects == {}


async def test_content_length_over_budget_is_refused_before_parsing(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    monkeypatch.setattr(
        missions_module,
        "get_settings",
        lambda: _limit_overrides(evidence_max_request_bytes=1024),
    )
    # Neutralize the form-overhead allowance so a small body can exercise the pre-check.
    monkeypatch.setattr(missions_module, "_MULTIPART_OVERHEAD_BYTES", 0)
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        resp = await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            files=[("p.jpg", JPEG_BYTES + b"\x00" * 4096, "image/jpeg")],
        )
        assert resp.status_code == 413
        assert fake.objects == {}


# --------------------------------------------------------------------------- #
# Per-issue evidence cap applies to uploads (10 items per issue)
# --------------------------------------------------------------------------- #
async def test_per_issue_cap_refuses_an_upload_beyond_ten_items(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        async with test_session() as session:
            row = await session.get(OrderIssueRecord, issue_id)
            row.evidence = [
                {"kind": "photo_url", "url": f"https://example.test/{i}.jpg"} for i in range(10)
            ]
            await session.commit()

        resp = await _supplement_multipart(
            client, mission_id, issue_id, files=[("one-more.jpg", JPEG_BYTES, "image/jpeg")]
        )
        assert resp.status_code == 400
        assert "at most 10 evidence item(s)" in resp.json()["detail"]
        assert fake.objects == {}


# --------------------------------------------------------------------------- #
# Storage disabled / partial storage failure recovery
# --------------------------------------------------------------------------- #
async def test_store_disabled_with_files_is_an_honest_503(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    app.dependency_overrides[missions_get_evidence_store] = lambda: None
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        resp = await _supplement_multipart(
            client, mission_id, issue_id, files=[("photo.jpg", JPEG_BYTES, "image/jpeg")]
        )
        assert resp.status_code == 503


async def test_partial_storage_failure_cleans_up_every_stored_object(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    from app.ledger.store import TransportFailure

    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    fake.fail_on_put = 2
    fake.fail_with = TransportFailure(None, "PUT")
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        before = (await _issue_row(test_session, issue_id)).evidence

        resp = await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            files=[("a.jpg", JPEG_BYTES, "image/jpeg"), ("b.png", PNG_BYTES, "image/png")],
        )
        assert resp.status_code == 502

        # Cleanup succeeded: nothing from the batch remains, nothing was attached,
        # and no orphan audit was needed.
        assert fake.objects == {}
        assert (await _issue_row(test_session, issue_id)).evidence == before
        audits = await _audits(test_session, mission_id)
        assert not [a for a in audits if a.action == "evidence_orphan_tracked"]


async def test_unconfirmable_cleanup_is_durably_tracked_as_an_orphan(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    from app.ledger.store import TransportFailure

    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    fake.fail_on_put = 2
    fake.fail_on_delete = 1
    fake.fail_with = TransportFailure(None, "PUT")
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        before = (await _issue_row(test_session, issue_id)).evidence

        resp = await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            files=[("a.jpg", JPEG_BYTES, "image/jpeg"), ("b.png", PNG_BYTES, "image/png")],
        )
        assert resp.status_code == 502

        # The delete itself failed: the orphan is durably tracked via audit, and the
        # issue still carries NO metadata for bytes that are not provably attached.
        audits = await _audits(test_session, mission_id)
        orphans = [a for a in audits if a.action == "evidence_orphan_tracked"]
        assert len(orphans) == 1
        assert orphans[0].actor == "system"
        assert "await reconciliation" in orphans[0].reason
        assert (await _issue_row(test_session, issue_id)).evidence == before


# --------------------------------------------------------------------------- #
# Reassessment: compact metadata in the model context, never storage handles
# --------------------------------------------------------------------------- #
async def test_reassessment_context_receives_compact_upload_metadata(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            note="Photo attached.",
            description="The torn seam, close up.",
            files=[("photo.jpg", JPEG_BYTES, "image/jpeg")],
        )

        reassessor = install_fake_reasoning(
            _needs_review_proposal(refund="eligible", replacement="needs_review")
        )
        second = await _assess(client, mission_id, issue_id)
        assert second["remedy_eligibility"]["refund"]["eligibility"] == "eligible"

        # The model saw WHAT the buyer attached — never where it is stored.
        assert len(reassessor.contexts) == 1
        context_evidence = reassessor.contexts[0]["issue"]["evidence"]
        [uploaded] = [e for e in context_evidence if e["kind"] == "uploaded_file"]
        assert uploaded["display_name"] == "photo.jpg"
        assert uploaded["mime"] == "image/jpeg"
        assert uploaded["size_bytes"] == len(JPEG_BYTES)
        assert uploaded["description"] == "The torn seam, close up."
        for forbidden in ("storage_key", "bucket", "sha256"):
            assert forbidden not in uploaded

        # The SAME assessment row was re-evaluated over the fuller record.
        async with test_session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ResolutionAssessmentRecord)
                .where(ResolutionAssessmentRecord.issue_id == issue_id)
            )
        assert count == 1


# --------------------------------------------------------------------------- #
# P3: client-facing issue payloads carry WHAT was attached, never WHERE it lives
# --------------------------------------------------------------------------- #
async def test_client_facing_issue_payloads_never_carry_storage_handles(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        resp = await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            note="Photo attached.",
            description="The torn seam, close up.",
            files=[("photo.jpg", JPEG_BYTES, "image/jpeg")],
        )
        assert resp.status_code == 200, resp.text
        listed = await client.get(f"/missions/{mission_id}/order/issues")
        assert listed.status_code == 200, listed.text

        for payload_issue in listed.json()["issues"]:
            for item in payload_issue["evidence"]:
                for forbidden in ("storage_key", "bucket", "sha256"):
                    assert forbidden not in item
        for item in resp.json()["evidence"]:
            for forbidden in ("storage_key", "bucket", "sha256"):
                assert forbidden not in item

        # The persisted row keeps the handles the retrieval endpoint reads server-side.
        row = await _issue_row(test_session, issue_id)
        [uploaded] = [e for e in row.evidence if e["kind"] == "uploaded_file"]
        assert uploaded["storage_key"] and uploaded["bucket"]


# --------------------------------------------------------------------------- #
# Branch coexistence: the JSON note path still works alongside multipart
# --------------------------------------------------------------------------- #
async def test_json_note_supplement_still_works(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        resp = await client.post(
            f"/missions/{mission_id}/order/issues/{issue_id}/supplement",
            json={"note": "The dupatta tore along the seam."},
        )
        assert resp.status_code == 200, resp.text
        issue = resp.json()
        assert issue["status"] == "reported"
        assert issue["evidence"] == []
        assert "Supplement: The dupatta tore along the seam." in issue["description"]


async def test_malformed_json_body_is_a_422_not_a_crash(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)

        resp = await client.post(
            f"/missions/{mission_id}/order/issues/{issue_id}/supplement",
            content=b"not json at all",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Metadata-persistence failure AFTER the bytes were stored: compensating cleanup
# removes the stored objects, the issue record survives untouched, and the same
# supplement can be retried safely from a clean slate.
# --------------------------------------------------------------------------- #
async def test_commit_failure_after_storage_compensates_and_allows_safe_retry(
    restart_env, install_mock_discovery, sample_products, mock_razorpay,
    install_fake_reasoning, monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    fake = install_fake_store()
    async with client:
        mission_id, issue_id = await _needs_review_issue(client, mission_setup, install_fake_reasoning)
        before = await _issue_row(test_session, issue_id)
        before_status = before.status
        before_evidence = list(before.evidence or [])

        from sqlalchemy.ext.asyncio import AsyncSession

        real_commit = AsyncSession.commit

        async def _exploding_commit(self):
            raise RuntimeError("forced metadata-persistence failure")

        monkeypatch.setattr(AsyncSession, "commit", _exploding_commit)
        resp = await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            note="A photo of the damaged unit.",
            files=[("photo.jpg", JPEG_BYTES, "image/jpeg")],
        )
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert "retry safely" in detail
        assert "uploaded bytes were removed" in detail

        # No bytes orphaned: the compensating cleanup deleted the stored object.
        assert fake.put_calls == 1
        assert fake.delete_calls == 1
        assert fake.objects == {}
        assert len(fake.deleted_keys) == 1
        assert _KEY_SHAPE.match(fake.deleted_keys[0])

        # Nothing persisted: the issue row is exactly as before the attempt and no
        # supplement audit survived the rollback.
        row = await _issue_row(test_session, issue_id)
        assert row.status == before_status
        assert list(row.evidence or []) == before_evidence
        audits = await _audits(test_session, mission_id)
        assert [a for a in audits if a.action == "issue_evidence_supplemented"] == []

        # Retry with commit restored: the fresh attempt stores under a NEW key and
        # succeeds; the earlier deletion is not repeated.
        monkeypatch.setattr(AsyncSession, "commit", real_commit)
        resp2 = await _supplement_multipart(
            client,
            mission_id,
            issue_id,
            note="A photo of the damaged unit.",
            files=[("photo.jpg", JPEG_BYTES, "image/jpeg")],
        )
        assert resp2.status_code == 200, resp2.text
        issue = resp2.json()
        assert issue["status"] == "reported"
        assert len(issue["evidence"]) == 1
        assert issue["evidence"][0]["kind"] == "uploaded_file"

        assert fake.put_calls == 2
        assert fake.delete_calls == 1  # no cleanup on the success path
        [(retry_key, (retry_body, retry_mime))] = fake.objects.items()
        assert retry_key != fake.deleted_keys[0]
        assert _KEY_SHAPE.match(retry_key)
        assert retry_body == JPEG_BYTES
        assert retry_mime == "image/jpeg"

        row2 = await _issue_row(test_session, issue_id)
        assert row2.evidence[0]["sha256"] == hashlib.sha256(JPEG_BYTES).hexdigest()
        audits2 = await _audits(test_session, mission_id)
        supplemented = [a for a in audits2 if a.action == "issue_evidence_supplemented"]
        assert len(supplemented) == 1
        assert "1 evidence item(s)" in supplemented[0].reason
