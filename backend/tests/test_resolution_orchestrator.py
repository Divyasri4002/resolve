"""
Tests for Task 13: the autonomous resolution orchestrator (POST .../resolve).

What this suite keeps honest:
  * The orchestrator decides NOTHING on its own: the action comes from the persisted Task 10
    proposal, permission comes from a FRESH Authority Engine evaluation, and execution goes
    through the SAME controlled refund/replacement/return executors the individual endpoints
    use. The reasoning model is never called from /resolve.
  * 'allowed' is never 'completed': denied -> no provider contact, provider-unavailable stays
    pending, and no resolution_* event ever claims a completion the provider did not report.
  * Idempotency at the orchestrator level: one resolution row per issue, terminal states
    short-circuit, and conflicts propagate as deterministic denials.

Hermetic: mock reasoning/discovery via conftest; Razorpay and merchant providers are fully
mocked — NO real payment, refund, replacement, or return calls are made.
"""
from decimal import Decimal  # noqa: F401  (canonical money comparisons below)

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import get_settings
from app.main import app
from app.models.db_models import (
    AuditLogRecord,
    RefundRecord,
    ReplacementRecord,
    ResolutionRecord,
    ReturnRecord,
)

from tests.test_refund_authority import (  # noqa: F401
    _delegate,
    _refund_proposal,
    _verified_mission_product,
)
from tests.test_refund_execution import (  # noqa: F401
    _confirmed_order_with_issue,
    _login,
    _register,
    _refund,
    install_fake_reasoning,
    mock_razorpay,
    recorded_events,
    refund_api,
    restart_env,
)
from tests.test_replacement_execution import (  # noqa: F401
    _assess,
    _issue,
    _replace,
    _replacement_proposal,
    replacement_provider,
)
from tests.test_return_execution import (  # noqa: F401
    _authorized_return_issue,
    _ret,
    _set_assessment,
    _set_return_switch,
    return_provider,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _proposal(action: str, **overrides) -> dict:
    base = {
        "issue_assessment": "The wrong variant was delivered; resolve it appropriately.",
        "proposed_action": action,
        "reasoning_summary": "Resolution proposed by the resolution model.",
        "requested_amount": None,
        "currency": "INR",
        "confidence": 0.8,
        "needs_escalation": False,
    }
    base.update(overrides)
    return base


async def _resolve(client, mission_id, issue_id, json=None):
    url = f"/missions/{mission_id}/order/issues/{issue_id}/resolve"
    return await client.post(url, json=json) if json is not None else await client.post(url)


async def _resolutions(test_session, mission_id) -> list[ResolutionRecord]:
    async with test_session() as session:
        return list(
            await session.scalars(
                select(ResolutionRecord).where(ResolutionRecord.mission_id == mission_id)
            )
        )


async def _get_issue(client, mission_id, issue_id) -> dict:
    issues = (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
    return [i for i in issues if i["issue_id"] == issue_id][0]


async def _setup(restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake):
    """Mission factory: verified-payment shopping mission, and ONLY THEN the controlled
    resolution proposal (mission creation itself needs the hermetic mock reasoning provider)."""
    make_client, test_session = restart_env
    client = make_client()

    async def _mission(c):
        mission_id = await _verified_mission_product(
            c, install_mock_discovery, sample_products, mock_razorpay, "item_dove", "pay_T1"
        )
        install_fake(_proposal("request_return"))
        return mission_id

    return client, test_session, _mission


# --------------------------------------------------------------------------- #
# A + E + K + M: owner allowed, return ALLOW reaches the existing executor, no provider
# connected routes to the audited manual-review escalation, repeats never duplicate
# --------------------------------------------------------------------------- #
async def test_resolve_ownership_and_return_allow_pending(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)

        # A: anonymous and non-owner rejected — before any resolution row exists.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
            assert (await _resolve(anon, mission_id, issue_id)).status_code == 404
        await _register(client, "Mallory", "mallory@shop.com")
        assert (await _resolve(client, mission_id, issue_id)).status_code == 404
        await _login(client, "beta@shop.com")
        assert (await _resolve(client, "mission_nope000000", issue_id)).status_code == 404
        assert (await _resolve(client, mission_id, "issue_nope000000")).status_code == 404
        assert await _resolutions(test_session, mission_id) == []

        # E + M: ALLOW reaches the return executor; the default provider refuses, so the
        # orchestrator reports the audited escalated routing — never a completion.
        resp = await _resolve(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "request_return"
        assert data["authority_decision"] == "allow"
        assert data["executor"] == "return"
        assert data["resolution_state"] == "escalated"
        assert data["provider"] == "none"
        assert data["provider_attempted"] is False
        assert "No merchant return provider is connected" in (
            data["execution"]["failure_reason"] or ""
        )
        assert "routed for human review" in data["resolution_note"]
        assert data["execution"]["status"] == "escalated"
        assert data["executor_record_id"] == data["execution"]["return_id"]

        # K: repeat — same resolution row, same return row, no duplicate execution anywhere.
        again = await _resolve(client, mission_id, issue_id)
        assert again.status_code == 200
        second = again.json()
        assert second["resolution_id"] == data["resolution_id"]
        assert second["resolution_state"] == "escalated"
        assert second["already_existed"] is True
        rows = await _resolutions(test_session, mission_id)
        assert len(rows) == 1
        async with test_session() as session:
            assert await session.scalar(
                select(func.count()).select_from(ReturnRecord)
            ) == 1
            assert await session.scalar(
                select(func.count()).select_from(RefundRecord)
            ) == 0
            assert await session.scalar(
                select(func.count()).select_from(ReplacementRecord)
            ) == 0


# --------------------------------------------------------------------------- #
# B: missing assessment — rejected, and the reasoning model is NOT recalled
# --------------------------------------------------------------------------- #
async def test_missing_assessment_rejected_without_calling_model(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        provider = install_fake_reasoning(_proposal("request_return"))
        issue_id = await _issue(client, mission_id, kind="none")

        resp = await _resolve(client, mission_id, issue_id)
        assert resp.status_code == 400
        assert "never calls the resolution model" in resp.json()["detail"]
        assert provider.contexts == []  # the model was not consulted for /resolve
        assert return_provider.requests == []
        assert await _resolutions(test_session, mission_id) == []


# --------------------------------------------------------------------------- #
# C: request_refund — ALLOW reaches the existing refund executor; DENY never contacts Razorpay;
# existing idempotency and the terminal-state short-circuit are preserved
# --------------------------------------------------------------------------- #
async def test_refund_allow_denied_and_terminal_idempotency(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    refund_api, monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)

        # DENY (C, second half): refund authority off -> fresh evaluation refuses, nothing runs.
        monkeypatch.setattr(get_settings(), "shopping_refund_authority_enabled", False)
        install_fake_reasoning(_refund_proposal(399))
        denied_issue = await _confirmed_order_with_issue(client, mission_id)
        await _delegate(test_session, mission_id, ["request_refund"], refund_limit="500")
        assessment = await _assess(client, mission_id, denied_issue)
        assert assessment["authority_decision"] == "deny"
        resp = await _resolve(client, mission_id, denied_issue)
        assert resp.status_code == 400
        denied = resp.json()
        assert denied["resolution_state"] == "denied"
        assert denied["authority_decision"] == "deny"
        assert denied["provider_attempted"] is False
        assert "refused" in denied["resolution_note"]
        assert set(refund_api.calls.values()) == {0}
        async with test_session() as session:
            assert await session.scalar(select(func.count()).select_from(RefundRecord)) == 0
        monkeypatch.setattr(get_settings(), "shopping_refund_authority_enabled", True)

        # ALLOW (C, first half): reaches the SAME executor the /refund endpoint uses.
        install_fake_reasoning(_refund_proposal(399))
        issue_id = await _issue(client, mission_id, kind="refund")
        assessment = await _assess(client, mission_id, issue_id)
        assert assessment["authority_decision"] == "allow"
        resp = await _resolve(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "request_refund"
        assert data["executor"] == "refund"
        assert data["resolution_state"] == "verified"
        assert data["provider"] == "razorpay_test_mode"
        assert data["provider_attempted"] is True
        assert data["provider_reference"] == "rfnd_TEST1"
        assert data["executor_record_id"] == data["execution"]["refund_id"]
        assert data["execution"]["amount_paise"] == 39900  # canonical amount, never client data
        assert refund_api.calls == {"create": 1, "fetch": 1, "list": 1}
        assert (await _get_issue(client, mission_id, issue_id))["status"] == "resolution_pending"

        # Terminal short-circuit: a repeat returns the persisted resolution, zero provider use.
        again = await _resolve(client, mission_id, issue_id)
        assert again.status_code == 200
        second = again.json()
        assert second["already_existed"] is True
        assert second["resolution_id"] == data["resolution_id"]
        assert "already resolved as verified" in second["message"]
        assert refund_api.calls == {"create": 1, "fetch": 1, "list": 1}
        rows = await _resolutions(test_session, mission_id)
        assert len(rows) == 2  # the denied attempt + this issue: one row each, never more
        assert {r.resolution_state for r in rows} == {"denied", "verified"}


# --------------------------------------------------------------------------- #
# D: request_replacement — ALLOW reaches the replacement executor; DENY never touches it
# --------------------------------------------------------------------------- #
async def test_replacement_allow_and_deny_never_touch_provider(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    replacement_provider, monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        install_fake_reasoning(_replacement_proposal())
        await _delegate(test_session, mission_id, ["request_replacement"])

        # DENY first: replacement authority off -> no provider contact at all.
        monkeypatch.setattr(get_settings(), "shopping_replacement_authority_enabled", False)
        denied_issue = await _issue(client, mission_id, kind="replacement")
        assessment = await _assess(client, mission_id, denied_issue)
        assert assessment["authority_decision"] == "deny"
        resp = await _resolve(client, mission_id, denied_issue)
        assert resp.status_code == 400
        assert resp.json()["resolution_state"] == "denied"
        assert replacement_provider.requests == []
        monkeypatch.setattr(get_settings(), "shopping_replacement_authority_enabled", True)

        # ALLOW: the fake provider accepts -> 'requested', which is explicitly NOT a completion.
        issue_id = await _issue(client, mission_id, kind="replacement")
        assessment = await _assess(client, mission_id, issue_id)
        assert assessment["authority_decision"] == "allow"
        resp = await _resolve(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolution_state"] == "requested"
        assert data["executor"] == "replacement"
        assert data["provider"] == "fake"
        assert data["provider_reference"] == "MERC-REPL-1"
        assert data["provider_attempted"] is True
        assert "acceptance only" in data["resolution_note"]
        assert len(replacement_provider.requests) == 1
        assert (await _get_issue(client, mission_id, issue_id))["status"] == "replacement_requested"


# --------------------------------------------------------------------------- #
# F, G, H: non-executing proposals contact nothing external and invent no ticket
# --------------------------------------------------------------------------- #
async def test_non_executing_actions_contact_nothing(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, replacement_provider, refund_api,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    expected = {
        "request_more_information": "more_information_required",
        "escalate_to_human": "escalated",
        "no_action": "no_action",
    }
    async with client:
        mission_id = await mission_setup(client)
        for action, state in expected.items():
            install_fake_reasoning(_proposal(action))
            issue_id = await _issue(client, mission_id, kind="none")
            assessment = await _assess(client, mission_id, issue_id)
            # Intrinsically-safe proposals are never denied; escalate_to_human is stored
            # as its own decision, which the orchestrator treats as a non-executing outcome.
            assert assessment["authority_decision"] in ("allow", "escalate")

            resp = await _resolve(client, mission_id, issue_id)
            assert resp.status_code == 200
            data = resp.json()
            assert data["action"] == action
            assert data["resolution_state"] == state
            assert data["executor"] == "none"
            assert data["provider"] is None
            assert data["provider_reference"] is None
            assert data["provider_attempted"] is False
            assert data["execution"] is None
            if action == "escalate_to_human":
                # G: routed for human review — no invented ticket anywhere in the payload.
                assert "no ticket number exists" in data["resolution_note"]
                assert "ticket" not in str(data.get("execution"))

        # Nothing external was touched: all three provider seams stayed silent.
        assert return_provider.requests == []
        assert replacement_provider.requests == []
        assert set(refund_api.calls.values()) == {0}
        async with test_session() as session:
            for model in (RefundRecord, ReplacementRecord, ReturnRecord):
                assert await session.scalar(
                    select(func.count()).select_from(model)
                ) == 0
        rows = await _resolutions(test_session, mission_id)
        assert len(rows) == 3  # one per issue


# --------------------------------------------------------------------------- #
# I: a stale stored approval cannot outrank the current Authority Engine
# --------------------------------------------------------------------------- #
async def test_stale_approval_current_deny_no_provider_call(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, monkeypatch,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)

        # Flip return authority OFF after the stored ALLOW: /resolve must re-evaluate now.
        _set_return_switch(monkeypatch, enabled=False)
        resp = await _resolve(client, mission_id, issue_id)
        assert resp.status_code == 400
        data = resp.json()
        assert data["resolution_state"] == "denied"
        assert data["provider_attempted"] is False
        assert return_provider.requests == []
        async with test_session() as session:
            assert await session.scalar(select(func.count()).select_from(ReturnRecord)) == 0
        _set_return_switch(monkeypatch, enabled=True)

        # With the switch back ON the same call proceeds to the (fake) provider.
        assert (await _resolve(client, mission_id, issue_id)).status_code == 200
        assert len(return_provider.requests) == 1


# --------------------------------------------------------------------------- #
# J: a client body cannot inject action / amount / product / order / authority / provider
# --------------------------------------------------------------------------- #
async def test_client_tampering_is_ignored(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        resp = await _resolve(
            client,
            mission_id,
            issue_id,
            json={
                "action": "request_refund",
                "proposed_action": "request_refund",
                "amount": 99999,
                "product_id": "item_premium",
                "order_id": "order_fake",
                "provider": "razorpay",
                "provider_reference": "HACK-1",
                "authority_decision": "allow",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # The persisted proposal, canonical product/order, and provider's own reference win.
        assert data["action"] == "request_return"
        assert data["provider"] == "fake"
        assert data["provider_reference"] == "RMA-FAKE-1"
        assert data["order_id"] != "order_fake"
        assert return_provider.requests[-1].product_id == "item_dove"


# --------------------------------------------------------------------------- #
# L: existing conflict guards survive orchestration
# --------------------------------------------------------------------------- #
async def test_conflicts_propagate_as_deterministic_denials(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, replacement_provider, refund_api,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        await _delegate(
            test_session, mission_id,
            ["request_refund", "request_replacement", "request_return"], refund_limit="500",
        )

        # Verified refund (via its own endpoint) blocks an orchestrated return.
        refund_issue = await _confirmed_order_with_issue(client, mission_id)
        install_fake_reasoning(_refund_proposal(399))
        assessment = await _assess(client, mission_id, refund_issue)
        assert assessment["authority_decision"] == "allow"
        assert (await _refund(client, mission_id, refund_issue)).status_code == 200
        install_fake_reasoning(_proposal("request_return"))
        await _set_assessment(
            test_session, assessment["assessment_id"],
            proposed_action="request_return", authority_decision="allow",
        )
        resp = await _resolve(client, mission_id, refund_issue)
        assert resp.status_code == 400
        assert "verified refund" in resp.json()["detail"]
        assert resp.json()["resolution_state"] == "denied"
        assert return_provider.requests == []

        # A confirmed replacement blocks an orchestrated return on the same issue.
        install_fake_reasoning(_replacement_proposal())
        repl_issue = await _issue(client, mission_id, kind="replacement")
        assessment = await _assess(client, mission_id, repl_issue)
        assert (await _replace(client, mission_id, repl_issue)).json()["status"] == "requested"
        replacement_provider.outcome = "confirmed"
        assert (await _replace(client, mission_id, repl_issue)).json()["status"] == "confirmed"
        await _set_assessment(
            test_session, assessment["assessment_id"],
            proposed_action="request_return", authority_decision="allow",
        )
        resp = await _resolve(client, mission_id, repl_issue)
        assert resp.status_code == 400 and "replacement" in resp.json()["detail"]
        assert return_provider.requests == []

        # A confirmed return blocks an orchestrated replacement.
        install_fake_reasoning(_proposal("request_return"))
        ret_issue = await _issue(client, mission_id, kind="none")
        assessment = await _assess(client, mission_id, ret_issue)
        assert (await _ret(client, mission_id, ret_issue)).json()["status"] == "requested"
        return_provider.outcome = "confirmed"
        assert (await _ret(client, mission_id, ret_issue)).json()["status"] == "confirmed"
        await _set_assessment(
            test_session, assessment["assessment_id"],
            proposed_action="request_replacement", authority_decision="allow",
        )
        resp = await _resolve(client, mission_id, ret_issue)
        assert resp.status_code == 400 and "return" in resp.json()["detail"]
        assert len(replacement_provider.requests) == 1  # no extra replacement attempted

        # The orchestrator never re-routes a conflict into another action: each blocked
        # attempt got its own row, and only the genuinely executed ones created records.
        rows = await _resolutions(test_session, mission_id)
        assert len(rows) == 3
        assert all(r.resolution_state in {"denied", "verified", "confirmed"} for r in rows)


async def test_terminal_resolution_short_circuits_conflicting_proposal(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        assert (await _resolve(client, mission_id, issue_id)).json()["resolution_state"] == "requested"
        return_provider.outcome = "confirmed"
        assert (await _resolve(client, mission_id, issue_id)).json()["resolution_state"] == "confirmed"

        # Confirmed is terminal: even a flipped stored proposal cannot trigger anything new.
        install_fake_reasoning(_proposal("request_more_information"))
        resp = await _resolve(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()
        assert data["already_existed"] is True
        assert data["resolution_state"] == "confirmed"
        assert len(return_provider.requests) == 1
        assert return_provider.status_reads == ["RMA-FAKE-1"]  # only the honest confirmation read


# --------------------------------------------------------------------------- #
# N + secret hygiene
# --------------------------------------------------------------------------- #
async def test_audit_records_orchestration_without_secrets(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    secret = get_settings().razorpay_key_secret
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        body = str((await _resolve(client, mission_id, issue_id)).json())
        assert secret not in body

        async with test_session() as session:
            audits = (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
        orchestrator_rows = [a for a in audits if f"resolve {issue_id}:" in (a.reason or "")]
        assert orchestrator_rows, "the orchestrator must leave its own audit trail"
        latest = orchestrator_rows[-1].reason
        assert "action=request_return" in latest
        assert "decision=allow" in latest
        assert "executor=return" in latest
        assert "state=requested" in latest
        assert "provider_attempted=True" in latest
        assert secret not in str([a.reason for a in audits])


# --------------------------------------------------------------------------- #
# O: WebSocket sequence — one event per real outcome, never a false completion
# --------------------------------------------------------------------------- #
async def test_websocket_events_never_claim_false_completion(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider, recorded_events,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        await _resolve(client, mission_id, issue_id)  # requested via fake provider
        return_provider.outcome = "confirmed"
        await _resolve(client, mission_id, issue_id)  # confirmation read
        await _resolve(client, mission_id, issue_id)  # terminal: returns persisted state
    resolution_events = [
        e["event"]
        for e in recorded_events
        if e["event"].startswith("resolution_") and e["event"] != "resolution_assessment_created"
    ]
    assert resolution_events == ["resolution_requested", "resolution_confirmed"]
    assert all("completed" not in e for e in resolution_events)
    exec_events = [e["event"] for e in recorded_events if e["event"].startswith("return_")]
    assert exec_events == ["return_pending", "return_requested", "return_confirmed"]
    last = [e for e in recorded_events if e["event"] == "resolution_confirmed"][-1]
    assert last["executor"] == "return" and last["provider"] == "fake"
    assert last["provider_attempted"] is True


# --------------------------------------------------------------------------- #
# P: resolution state survives a restart
# --------------------------------------------------------------------------- #
async def test_resolution_survives_restart(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        data = (await _resolve(client, mission_id, issue_id)).json()

    async with restart_env[0]() as second_client:
        await _login(second_client, "beta@shop.com")
        issue = await _get_issue(second_client, mission_id, issue_id)
        assert issue["resolution"]["resolution_id"] == data["resolution_id"]
        assert issue["resolution"]["resolution_state"] == "requested"
        assert issue["resolution"]["provider_reference"] == "RMA-FAKE-1"
        # The restarted orchestrator still does not re-submit: the row is the claim.
        repeat = await _resolve(second_client, mission_id, issue_id)
        assert repeat.json()["resolution_id"] == data["resolution_id"]
    assert len(return_provider.requests) == 1
