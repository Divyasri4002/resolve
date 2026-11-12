"""
Sprint M3 — Layer A remedy eligibility + the supplement/re-assess loop.

The assessor returns INDEPENDENT per-remedy verdicts (refund, replacement), normalized
deterministically: only the recognized vocabulary survives; malformed or missing verdicts
become `needs_review`, never a guessed eligibility; no verdicts at all stay None ("not
evaluated"). When a remedy is waiting for more information, the buyer supplements evidence
(note + photo/description items), the issue returns to `reported`, and the next /assess call
re-evaluates the SAME assessment row against the fuller record — the old verdict is never
presented again as if it had seen the supplement.
"""
import uuid

from sqlalchemy import func, select

from app.models.db_models import AuditLogRecord, ResolutionAssessmentRecord

from tests.test_refund_execution import install_fake_reasoning, mock_razorpay, restart_env  # noqa: F401
from tests.test_replacement_execution import _assess, _issue, _setup


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _proposal(remedy_eligibility=None, action: str = "request_more_information") -> dict:
    body = {
        "issue_assessment": "The report is read; more evidence may change the verdict.",
        "proposed_action": action,
        "reasoning_summary": "Controlled proposal for the M3 eligibility contract.",
        "requested_amount": None,
        "currency": "INR",
        "confidence": 0.9,
        "needs_escalation": False,
    }
    if remedy_eligibility is not None:
        body["remedy_eligibility"] = remedy_eligibility
    return body


async def _issue_payload(client, mission_id, issue_id):
    return [
        i
        for i in (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
        if i["issue_id"] == issue_id
    ][0]


async def _supplement(client, mission_id, issue_id, json):
    return await client.post(
        f"/missions/{mission_id}/order/issues/{issue_id}/supplement", json=json
    )


# --------------------------------------------------------------------------- #
# Layer A: independent per-remedy verdicts, normalized, persisted, exposed
# --------------------------------------------------------------------------- #
async def test_independent_per_remedy_verdicts_persisted_and_exposed(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(
            _proposal(
                {
                    "refund": {
                        "eligibility": "eligible",
                        "reason": "the described damage is the kind a refund addresses",
                    },
                    "replacement": {
                        "eligibility": "ineligible",
                        "reason": "a replacement cannot address a damaged consumable",
                    },
                }
            )
        )
        issue_id = await _issue(client, mission_id, kind="refund")
        assessment = await _assess(client, mission_id, issue_id)

        assert assessment["proposal_source"] == "reasoning_model"
        verdicts = assessment["remedy_eligibility"]
        assert set(verdicts) == {"refund", "replacement"}
        assert verdicts["refund"]["eligibility"] == "eligible"
        assert verdicts["refund"]["reason"]
        assert verdicts["replacement"]["eligibility"] == "ineligible"

        async with test_session() as session:
            row = await session.get(ResolutionAssessmentRecord, assessment["assessment_id"])
        assert row is not None
        assert row.remedy_eligibility == verdicts


async def test_out_of_vocabulary_verdicts_normalize_to_needs_review(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(
            _proposal(
                {
                    "refund": {"eligibility": "sure_thing", "reason": "out of vocabulary"},
                    "replacement": "eligible — but not a dict",
                }
            )
        )
        issue_id = await _issue(client, mission_id, kind="refund")
        assessment = await _assess(client, mission_id, issue_id)

        verdicts = assessment["remedy_eligibility"]
        assert verdicts["refund"]["eligibility"] == "needs_review"
        assert "vocabulary" in verdicts["refund"]["reason"]
        assert verdicts["replacement"]["eligibility"] == "needs_review"
        assert "usable verdict" in verdicts["replacement"]["reason"]


async def test_no_verdicts_at_all_stay_not_evaluated(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(_proposal())  # no remedy_eligibility key at all
        issue_id = await _issue(client, mission_id, kind="refund")
        assessment = await _assess(client, mission_id, issue_id)

        assert assessment["remedy_eligibility"] is None
        async with test_session() as session:
            row = await session.get(ResolutionAssessmentRecord, assessment["assessment_id"])
        assert row.remedy_eligibility is None


# --------------------------------------------------------------------------- #
# The supplement loop: gates, success, re-assessment of the SAME row
# --------------------------------------------------------------------------- #
async def test_supplement_requires_an_assessment_first(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _issue(client, mission_id, kind="refund")

        resp = await _supplement(client, mission_id, issue_id, {"note": "photo incoming"})
        assert resp.status_code == 400
        assert "not been assessed" in resp.json()["detail"]


async def test_supplement_rejected_when_no_remedy_waits_for_information(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(
            _proposal(
                {
                    "refund": {"eligibility": "eligible", "reason": "clear"},
                    "replacement": {"eligibility": "eligible", "reason": "clear"},
                },
                action="request_refund",
            )
        )
        issue_id = await _issue(client, mission_id, kind="refund")
        await _assess(client, mission_id, issue_id)

        resp = await _supplement(client, mission_id, issue_id, {"note": "more detail anyway"})
        assert resp.status_code == 400
        assert "waiting for more information" in resp.json()["detail"]


async def test_supplement_rejected_with_neither_note_nor_evidence(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(
            _proposal(
                {
                    "refund": {"eligibility": "needs_review", "reason": "no photo yet"},
                    "replacement": {"eligibility": "needs_review", "reason": "unclear"},
                }
            )
        )
        issue_id = await _issue(client, mission_id, kind="refund")
        await _assess(client, mission_id, issue_id)

        resp = await _supplement(client, mission_id, issue_id, {"note": None, "evidence": []})
        assert resp.status_code == 400
        assert "needs a note or at least one evidence item" in resp.json()["detail"]


async def test_supplement_rejected_for_a_deterministic_fallback_issue(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        # A malformed (not uncontactable) model response routes deterministically: source
        # safe_fallback. An outage would be a 503 that writes nothing — not this path.
        install_fake_reasoning({"proposed_action": "not_a_real_action"})
        issue_id = await _issue(client, mission_id, kind="refund")
        assessment = await _assess(client, mission_id, issue_id)
        assert assessment["proposal_source"] == "safe_fallback"

        resp = await _supplement(client, mission_id, issue_id, {"note": "extra facts"})
        assert resp.status_code == 400
        assert "deterministic fallback" in resp.json()["detail"]


async def test_supplement_appends_evidence_and_returns_the_issue_to_reported(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(
            _proposal(
                {
                    "refund": {"eligibility": "needs_review", "reason": "no photo yet"},
                    "replacement": {"eligibility": "eligible", "reason": "clear"},
                }
            )
        )
        issue_id = await _issue(client, mission_id, kind="refund")
        await _assess(client, mission_id, issue_id)
        before = await _issue_payload(client, mission_id, issue_id)
        assert before["status"] == "under_review"

        resp = await _supplement(
            client,
            mission_id,
            issue_id,
            {
                "note": "A photo of the damaged unit is attached.",
                "evidence": [
                    {"kind": "photo_url", "url": "https://example.test/damaged-unit.jpg"}
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        issue = resp.json()
        assert issue["status"] == "reported"
        assert "Supplement: A photo of the damaged unit" in issue["description"]
        assert issue["description"].startswith(before["description"])
        assert any(
            e["kind"] == "photo_url" and e["url"] == "https://example.test/damaged-unit.jpg"
            for e in issue["evidence"]
        )

        # The supplement is audited, and authorizes nothing.
        async with test_session() as session:
            audits = (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
        supplemented = [a for a in audits if a.action == "issue_evidence_supplemented"]
        assert len(supplemented) == 1
        assert supplemented[0].actor == "user"
        assert "authorizes nothing" in supplemented[0].reason


async def test_reassessment_after_supplement_updates_the_same_row(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(
            _proposal(
                {
                    "refund": {"eligibility": "needs_review", "reason": "no photo yet"},
                    "replacement": {"eligibility": "needs_review", "reason": "unclear"},
                }
            )
        )
        issue_id = await _issue(client, mission_id, kind="refund")
        first = await _assess(client, mission_id, issue_id)
        assert first["remedy_eligibility"]["refund"]["eligibility"] == "needs_review"

        resp = await _supplement(
            client, mission_id, issue_id, {"note": "The photo is now attached."}
        )
        assert resp.status_code == 200, resp.text

        reassessor = install_fake_reasoning(
            _proposal(
                {
                    "refund": {
                        "eligibility": "eligible",
                        "reason": "the attached photo shows the damage",
                    },
                    "replacement": {"eligibility": "needs_review", "reason": "still unclear"},
                }
            )
        )
        second = await _assess(client, mission_id, issue_id)

        # The SAME assessment row was re-evaluated — a new verdict over the fuller record,
        # not a second opinion alongside the old one.
        assert second["assessment_id"] == first["assessment_id"]
        assert second["remedy_eligibility"]["refund"]["eligibility"] == "eligible"
        assert second["remedy_eligibility"]["replacement"]["eligibility"] == "needs_review"
        async with test_session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ResolutionAssessmentRecord)
                .where(ResolutionAssessmentRecord.issue_id == issue_id)
            )
            assert count == 1
        # The re-assessment saw the supplement: the context the model judged carried the
        # appended description, not the narrower record the first verdict was based on.
        assert len(reassessor.contexts) == 1
        assert "Supplement: The photo is now attached." in (
            reassessor.contexts[0]["issue"]["description"]
        )


# --------------------------------------------------------------------------- #
# T1/T2 (post-purchase UX consistency): what the supplement actually carried
# must be named exactly — a note-only supplement is never evidence, and an
# evidence supplement names its item count and reaches the assessment context.
# --------------------------------------------------------------------------- #
async def test_note_only_supplement_is_never_counted_as_evidence(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(
            _proposal(
                {
                    "refund": {"eligibility": "needs_review", "reason": "no photo yet"},
                    "replacement": {"eligibility": "needs_review", "reason": "unclear"},
                }
            )
        )
        issue_id = await _issue(client, mission_id, kind="refund")
        first = await _assess(client, mission_id, issue_id)

        resp = await _supplement(
            client, mission_id, issue_id, {"note": "The dupatta tore along the seam."}
        )
        assert resp.status_code == 200, resp.text
        issue = resp.json()
        # A note-only supplement attaches NO evidence items — the record must not claim any.
        assert issue["evidence"] == []
        assert "Supplement: The dupatta tore along the seam." in issue["description"]

        # The audit names exactly what arrived: zero items, note provided.
        async with test_session() as session:
            audits = (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
        supplemented = [a for a in audits if a.action == "issue_evidence_supplemented"]
        assert len(supplemented) == 1
        assert "0 evidence item(s)" in supplemented[0].reason
        assert "note=provided" in supplemented[0].reason

        # Re-assessment re-evaluates the SAME row, bumps its updated_at, and puts the
        # issue back under review — one row, one verdict, visibly re-timed.
        install_fake_reasoning(
            _proposal(
                {
                    "refund": {
                        "eligibility": "eligible",
                        "reason": "the note describes clear damage",
                    },
                    "replacement": {"eligibility": "needs_review", "reason": "still unclear"},
                }
            )
        )
        second = await _assess(client, mission_id, issue_id)
        assert second["assessment_id"] == first["assessment_id"]

        refreshed = await _issue_payload(client, mission_id, issue_id)
        assert refreshed["status"] == "under_review"

        async with test_session() as session:
            row = await session.get(ResolutionAssessmentRecord, second["assessment_id"])
            count = await session.scalar(
                select(func.count())
                .select_from(ResolutionAssessmentRecord)
                .where(ResolutionAssessmentRecord.issue_id == issue_id)
            )
        assert count == 1
        assert row.updated_at is not None
        assert row.created_at is not None
        assert row.updated_at != row.created_at


async def test_evidence_supplement_names_its_item_count_and_reaches_the_context(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(
            _proposal(
                {
                    "refund": {"eligibility": "needs_review", "reason": "no photo yet"},
                    "replacement": {"eligibility": "needs_review", "reason": "unclear"},
                }
            )
        )
        issue_id = await _issue(client, mission_id, kind="refund")
        await _assess(client, mission_id, issue_id)

        resp = await _supplement(
            client,
            mission_id,
            issue_id,
            {
                "evidence": [
                    {"kind": "photo_url", "url": "https://example.test/tear.jpg"},
                    {"kind": "written_description", "description": "seam split on first wear"},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        issue = resp.json()
        assert len(issue["evidence"]) == 2

        # The audit's item count matches what was sent — the number the UI reports.
        async with test_session() as session:
            audits = (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
        supplemented = [a for a in audits if a.action == "issue_evidence_supplemented"]
        assert len(supplemented) == 1
        assert "2 evidence item(s)" in supplemented[0].reason
        assert "note=none" in supplemented[0].reason

        # The re-assessment context carries the evidence items themselves, not just words.
        reassessor = install_fake_reasoning(
            _proposal(
                {
                    "refund": {"eligibility": "eligible", "reason": "photo shows the tear"},
                    "replacement": {"eligibility": "needs_review", "reason": "unclear"},
                }
            )
        )
        await _assess(client, mission_id, issue_id)
        assert len(reassessor.contexts) == 1
        context_evidence = reassessor.contexts[0]["issue"]["evidence"]
        assert any(
            e["kind"] == "photo_url" and e["url"] == "https://example.test/tear.jpg"
            for e in context_evidence
        )
        assert any(
            e["kind"] == "written_description"
            and e.get("description") == "seam split on first wear"
            for e in context_evidence
        )
