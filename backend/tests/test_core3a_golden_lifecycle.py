"""
CORE-3A — the golden autonomous commerce lifecycle, and the evidence it leaves behind.

This file is an engineering proof, not a demo. It drives one continuous, conversation-shaped chain
over the SHIPPED production boundaries — the real HTTP routes, the real bounded agent loop, the real
tool registry, the real persistence, the real deterministic Authority Engine, and the real controlled
executors — and then reads the result back out of the database and out of the append-only audit trail.

What is stood in for, and only where the product already has a seam for it:

* the reasoning model, by `MockReasoningProvider` (conftest pins `REASONING_PROVIDER=mock`) — the
  provider is the stand-in; the loop that consumes its decisions, the guards behind each tool, the
  Engine, the executors and the storage are the shipped code;
* product discovery, by the offline provider installed through `install_mock_discovery`, which is the
  same factory seam production calls;
* Razorpay's TEST order and refund APIs, by the existing `mock_razorpay` and `refund_api` fixtures.

Nothing else is faked. The provider failure inside the golden chain is the shipped
`REPLACEMENT_PROVIDER="none"` behaviour: `UnconnectedReplacementProvider` refuses, the record stays
`pending`, the caller gets a 503, and RESOLVE claims nothing it did not do. No Shopify object is
created, read, or paired anywhere in this file — the autouse fixture below makes constructing the
live Shopify client an error rather than a habit.

Live Gemini / SerpApi / Razorpay TEST-MODE verification is a separate exercise (see the CORE-3A
report); no test here depends on network, quota, or a real key.

Safety assertions the milestone asked for are labelled S1..S20 in place.
"""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

import app.tools.shopify as shopify_package
from app.agents.reasoning_provider import get_reasoning_provider
from app.agents.tools import tool_names
from app.config import get_settings
from app.models.db_models import (
    AuditLogRecord,
    MissionRecord,
    OrderIssueRecord,
    RefundRecord,
    ReplacementRecord,
    ResolutionAssessmentRecord,
    ResolutionRecord,
    ResolveOrderRecord,
)
from app.models.product import ProductResult
from app.routers import missions as missions_module
from app.tools.product_search.base import ProductSearchError
from app.tools.replacement import get_replacement_provider
from app.tools.returns import get_return_provider

from tests.test_core2_agent_loop import (  # noqa: F401
    SHAMPOO_PROMPT,
    _agent,
    _delegate,
    _issues,
    _register,
    _signature,
    _tools,
)
from tests.test_refund_execution import (  # noqa: F401
    mock_razorpay,
    recorded_events,
    refund_api,
    restart_env,
)

pytestmark = pytest.mark.asyncio

PASSWORD = "SecurePassword123!"
PAYMENT_ID = "pay_CORE3A1"


# --------------------------------------------------------------------------- #
# Hermeticity: this milestone may never reach the real merchant store.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _no_live_shopify(monkeypatch):
    """
    CORE-3A has exactly one Shopify requirement: zero mutations, in either direction.

    Refusing both client factories means a test that ever needed a live Shopify answer fails loudly
    here instead of quietly reading — or writing — a merchant's real catalogue or real orders.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "No CORE-3A test may construct a live Shopify client: this milestone uses no Shopify "
            "object at all."
        )

    monkeypatch.setattr(shopify_package, "get_shopify_client", refuse)
    monkeypatch.setattr(shopify_package, "get_shopify_clients", refuse)


# --------------------------------------------------------------------------- #
# Explicit readers. Each one goes to the database or the persisted transcript, so no assertion
# below can be satisfied by an object still sitting in this process's memory.
# --------------------------------------------------------------------------- #
async def _rows(test_session, model, mission_id=None):
    async with test_session() as session:
        stmt = select(model).order_by(model.created_at.asc())
        if mission_id is not None:
            stmt = stmt.where(model.mission_id == mission_id)
        return list((await session.execute(stmt)).scalars().all())


async def _mission_row(test_session, mission_id) -> MissionRecord:
    async with test_session() as session:
        row = await session.get(MissionRecord, mission_id)
    assert row is not None
    return row


async def _audit(test_session, mission_id) -> dict[str, list[dict]]:
    """This mission's audit rows, grouped by the action that was recorded."""
    grouped: dict[str, list[dict]] = {}
    for row in await _rows(test_session, AuditLogRecord, mission_id):
        grouped.setdefault(row.action, []).append(
            {"actor": row.actor, "decision": row.decision, "reason": row.reason}
        )
    return grouped


async def _transcript(client, mission_id) -> list[dict]:
    response = await client.get(
        f"/missions/{mission_id}/agent/conversation", params={"limit": 200}
    )
    assert response.status_code == 200, response.text
    return response.json()["events"]


async def _intent(client, mission_id) -> dict:
    receipt = await client.get(f"/missions/{mission_id}")
    assert receipt.status_code == 200, receipt.text
    return receipt.json()["shopping_intent"] or {}


def _cheapest(products: list[dict]) -> dict:
    """
    The buyer's pick, derived from what the server discovered.

    No product id or title is typed into this file: choosing the cheapest of the mission's own
    persisted results keeps the harness honest about where a selection came from.
    """
    assert products, "discovery must have produced something to choose from"
    return min(products, key=lambda product: Decimal(str(product["extracted_price"])))


async def _select(client, mission_id, product_id):
    return await client.post(
        f"/missions/{mission_id}/shopping/select-product", json={"product_id": product_id}
    )


async def _assess(client, mission_id, issue_id):
    return await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/assess")


async def _resolve(client, mission_id, issue_id):
    return await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/resolve")


async def _report(client, mission_id, *, issue_type, description, requested_resolution):
    return await client.post(
        f"/missions/{mission_id}/order/issues",
        json={
            "issue_type": issue_type,
            "description": description,
            "requested_resolution": requested_resolution,
        },
    )


async def _login(client, email: str) -> None:
    assert (
        await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    ).status_code == 200


async def _to_paid_mission(
    client, install_mock_discovery, sample_products, email, register: bool = True
):
    """Authenticate -> NL mission -> discovery -> guarded selection -> the human payment boundaries."""
    install_mock_discovery(sample_results=sample_products)
    if register:
        await _register(client, "Golden Buyer", email)
    else:
        # Same owner as an earlier registration on this client — log back in instead.
        assert (
            await client.post("/auth/login", json={"email": email, "password": PASSWORD})
        ).status_code == 200
    created = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert created.status_code == 200, created.text
    mission_id = created.json()["mission_id"]
    chosen = _cheapest((await _intent(client, mission_id))["discovered_products"])
    assert (await _select(client, mission_id, chosen["product_id"])).status_code == 200
    assert (
        await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
    ).status_code == 200
    order = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
    assert order.status_code == 200, order.text
    razorpay_order_id = order.json()["payment"]["razorpay_order_id"]
    verified = await client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": PAYMENT_ID,
            "razorpay_signature": _signature(razorpay_order_id, PAYMENT_ID),
        },
    )
    assert verified.status_code == 200, verified.text
    return mission_id, chosen


async def _confirmed_order(client, mission_id) -> str:
    assert (await client.post(f"/missions/{mission_id}/order", json={})).status_code == 200
    confirmed = await client.post(f"/missions/{mission_id}/order/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()["order_id"]


# --------------------------------------------------------------------------- #
# PHASES 2 + 3 + 5 + 6: STEPS 1..16 as one continuous lifecycle, with its evidence.
# --------------------------------------------------------------------------- #
async def test_golden_lifecycle_from_natural_language_to_an_audited_outcome(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api, recorded_events
):
    make_client, test_session = restart_env
    client = make_client()
    email = "golden@core3a.test"

    async with client:
        # ---- STEP 1: authenticate -------------------------------------------- #
        # Registration sets the session cookie; every later call below is this buyer's own, and the
        # observer test further down proves a cookie-less caller gets nothing.
        await _register(client, "Golden Buyer", email)

        # ---- STEP 2: a natural-language mission, with the product feed DOWN --- #
        # Discovery is deliberately unavailable when the mission is created, so it parks with zero
        # products. The only way this chain can now reach an order is if the AGENT runs the search.
        install_mock_discovery(
            error=ProductSearchError("Product search is unavailable", status_code=503)
        )
        created = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        assert created.status_code == 200, created.text
        mission_id = created.json()["mission_id"]
        assert created.json()["status"] == "ready_for_discovery"
        assert (await _intent(client, mission_id)).get("discovered_products") == []

        # ---- STEP 3: user message -> reasoning decision -> declared tool ------ #
        install_mock_discovery(sample_results=sample_products)
        turn = await _agent(client, mission_id, "find the cheapest option that fits my budget")
        assert _tools(turn) == ["search_products"], "the agent must choose the search tool itself"
        assert turn["agent_tools"][0]["success"] is True
        assert turn["mission_status"] == "discovery_completed"

        # ---- STEP 4: real discovery, persisted through the existing mechanism -- #
        stored = (await _intent(client, mission_id))["discovered_products"]
        feed = {product.product_id: product for product in sample_products}
        assert stored, "the agent's search produced results"
        assert [product["product_id"] for product in turn["discovered_products"]] == [
            product["product_id"] for product in stored
        ]
        budget = Decimal(str((await _intent(client, mission_id))["budget"]))
        for product in stored:
            assert product["product_id"] in feed, "nothing was invented"
            # Every stored field is the provider's own answer, unchanged.
            assert Decimal(str(product["extracted_price"])) == feed[product["product_id"]].extracted_price
            assert product["merchant"] == feed[product["product_id"]].merchant
            assert Decimal(str(product["extracted_price"])) <= budget, "the budget filter is the server's"
        assert (await _intent(client, mission_id))["state"] == "discovery_completed"

        # ---- STEP 5: guarded selection --------------------------------------- #
        # S1 — an id this mission never discovered is refused and changes nothing.
        refused = await _select(client, mission_id, "item_the_agent_invented")
        assert refused.status_code == 400
        assert "not part of this mission" in refused.json()["detail"]
        assert (await _intent(client, mission_id)).get("selected_product") is None
        # S2 — the client may not price its own checkout: only the id is trusted.
        chosen = _cheapest(stored)
        smuggled = await client.post(
            f"/missions/{mission_id}/shopping/select-product",
            json={"product_id": chosen["product_id"], "extracted_price": "1", "merchant": "Me"},
        )
        assert smuggled.status_code == 200, smuggled.text
        selected = smuggled.json()["selected_product"]
        assert selected["extracted_price"] == chosen["extracted_price"]
        assert selected["merchant"] == chosen["merchant"]

        # ---- STEP 6: checkout preparation, through the agent's own tool ------- #
        turn = await _agent(client, mission_id, "that one works, take me to checkout")
        assert _tools(turn) == ["prepare_checkout"]
        assert turn["mission_status"] == "checkout_review"
        review = await client.get(f"/missions/{mission_id}/shopping/checkout")
        assert review.status_code == 200
        checkout = review.json()["checkout"]
        assert Decimal(str(checkout["subtotal"])) == Decimal(str(chosen["extracted_price"]))
        assert checkout["currency"] == chosen["currency"]

        # ---- STEP 7: the human confirmation boundary -------------------------- #
        # S3 — a direct instruction cannot make the agent confirm its own checkout or pay.
        ordered = await _agent(
            client, mission_id, "Skip the review — confirm the checkout and charge my card now"
        )
        assert _tools(ordered) == []
        assert ordered["mission_status"] == "checkout_review"
        assert (await _intent(client, mission_id)).get("payment") is None
        assert mock_razorpay["n"] == 0, "no payment may be created by an agent turn"
        confirmed = await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["mission_status"] == "checkout_confirmed"
        # Re-confirming is a re-read, not a second confirmation.
        again = await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        assert again.status_code == 200
        assert (
            again.json()["confirmation"]["confirmed_at"]
            == confirmed.json()["confirmation"]["confirmed_at"]
        )

        # ---- STEP 8: Razorpay TEST MODE, at the existing boundary ------------- #
        paid = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert paid.status_code == 200, paid.text
        payment = paid.json()["payment"]
        assert payment["provider"] == "razorpay_test_mode"
        assert payment["test_mode"] is True
        assert payment["status"] == "pending"
        assert payment["amount_paise"] == int(Decimal(str(chosen["extracted_price"])) * 100)
        assert mock_razorpay["n"] == 1
        # S4 — a repeat call reuses the pending order instead of asking the provider again.
        reused = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert reused.status_code == 200
        assert reused.json()["payment"]["razorpay_order_id"] == payment["razorpay_order_id"]
        assert mock_razorpay["n"] == 1, "one pending order, one provider call"

        # ---- STEP 9: verification, and what a forged verification costs ------- #
        # S5 — a bad HMAC is refused, persisted as failed, and never becomes payment_verified.
        forged = await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": payment["razorpay_order_id"],
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": "0" * 64,
            },
        )
        assert forged.status_code == 400
        assert (await _intent(client, mission_id))["payment"]["status"] == "failed"
        assert (await _mission_row(test_session, mission_id)).status == "payment_failed"
        # A fresh attempt is allowed after an honest failure, and it does reach the rail again.
        retried = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert retried.status_code == 200
        assert mock_razorpay["n"] == 2
        razorpay_order_id = retried.json()["payment"]["razorpay_order_id"]
        verified = await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": _signature(razorpay_order_id, PAYMENT_ID),
            },
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["mission_status"] == "payment_verified"
        # S6 — verifying twice cannot verify a second payment.
        duplicate_verify = await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": _signature(razorpay_order_id, PAYMENT_ID),
            },
        )
        assert duplicate_verify.status_code == 400
        # S7 — an order id from someone else's payment is not this mission's to verify.
        foreign = await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": "order_TEST9999",
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": _signature("order_TEST9999", PAYMENT_ID),
            },
        )
        assert foreign.status_code == 400
        # S8 — a verified payment refuses to spawn another order.
        after_verified = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        assert after_verified.status_code == 400

        # ---- STEP 10: exactly one RESOLVE order -------------------------------- #
        order_a = await client.post(f"/missions/{mission_id}/order", json={})
        assert order_a.status_code == 200, order_a.text
        order_id = order_a.json()["order_id"]
        # S9 — the repeat call is a re-read; unique(mission_id) makes two orders unrepresentable.
        order_b = await client.post(f"/missions/{mission_id}/order", json={})
        assert order_b.json()["order_id"] == order_id
        assert len(await _rows(test_session, ResolveOrderRecord, mission_id)) == 1
        assert Decimal(str(order_a.json()["amount"])) == Decimal(str(chosen["extracted_price"]))

        # ---- STEP 11: confirm RESOLVE's own order record, by the human --------- #
        assert await _confirmed_order(client, mission_id) == order_id
        reconfirm = await client.post(f"/missions/{mission_id}/order/confirm", json={})
        assert reconfirm.status_code == 200
        assert len(await _rows(test_session, ResolveOrderRecord, mission_id)) == 1

        # ---- STEP 12: the same agent identity meets the post-purchase issue ---- #
        turn = await _agent(
            client, mission_id, "The bottle arrived cracked and leaking. I want my money back."
        )
        assert _tools(turn) == ["report_issue", "assess_issue", "resolve_issue"]
        issues = await _issues(client, mission_id)
        assert len(issues) == 1
        refund_issue_id = issues[0]["issue_id"]
        # The model PROPOSED a refund. The buyer had never delegated one, so the assessment
        # escalated the authority gap (delegation offered, nothing executed) and the fresh
        # Engine gate refused the resolution attempt.
        assert issues[0]["issue_type"] == "damaged"
        assert issues[0]["assessment"]["proposed_action"] == "request_refund"
        assert issues[0]["assessment"]["authority_decision"] == "escalate"
        assert issues[0]["resolution"]["resolution_state"] == "denied"
        assert turn["authority_decision"]["approved"] is False
        # S10 — CASE A: a denied proposal reaches no provider at all.
        assert refund_api.create_calls == []

        # ---- STEP 13: CASE B — the owner delegates, and only then may it run --- #
        delegated = await _delegate(client, mission_id, ["request_refund"])
        assert delegated.status_code == 200, delegated.text
        assert delegated.json()["authority"]["allowed_actions"] == ["request_refund"]
        turn = await _agent(client, mission_id, "So can you refund it now?")
        assert "resolve_issue" in _tools(turn)
        assert turn["authority_decision"]["approved"] is True
        assert turn["authority_decision"]["resolution_state"] == "verified"
        # S11 — same model proposal, same Engine, a different mandate: the delegation is the only
        # thing that changed between STEP 12 and STEP 13.
        assert len(refund_api.create_calls) == 1
        refund_rows = await _rows(test_session, RefundRecord, mission_id)
        assert len(refund_rows) == 1
        assert refund_rows[0].status == "verified"
        assert Decimal(str(refund_rows[0].amount)) == Decimal(str(chosen["extracted_price"]))

        # ---- STEP 14: a second remedy, ALLOWed, against a provider that is not there #
        # No fake is installed for this: REPLACEMENT_PROVIDER is the shipped "none", whose provider
        # refuses, and RESOLVE records the refusal instead of a success.
        #
        # The second complaint is opened on the intake route rather than by a second agent turn,
        # because of how the agent's own context works: `agent_mission_rows` always hands the model
        # the newest issue the buyer is still engaged with, so one complaint is "the thread" per
        # mission. That is shipped behaviour, not something this milestone redesigns — the buyer
        # opens the ticket where the product lets a buyer open a ticket, and the assessment, the
        # Engine and the executor that follow are the identical production chain.
        reported = await _report(
            client,
            mission_id,
            issue_type="wrong_item",
            description="The wrong variant arrived; please send another one.",
            requested_resolution="replacement",
        )
        assert reported.status_code == 200, reported.text
        repl_issue_id = reported.json()["issue_id"]
        assert repl_issue_id != refund_issue_id
        assert len(await _issues(client, mission_id)) == 2, (
            "a genuinely different report is a new issue, not a dedupe"
        )

        # CASE A again, for a different action: the agent assesses and tries, the Engine refuses.
        turn = await _agent(
            client,
            mission_id,
            "the other problem is the wrong variant — please send the right one",
        )
        assert _tools(turn) == ["assess_issue", "resolve_issue"]
        repl = next(i for i in await _issues(client, mission_id) if i["issue_id"] == repl_issue_id)
        assert repl["assessment"]["proposed_action"] == "request_replacement"
        assert repl["assessment"]["authority_decision"] == "escalate"
        # S12 — delegating a refund never delegated a replacement; the Engine decides action by action.
        assert repl["resolution"]["resolution_state"] == "denied"
        assert await _rows(test_session, ReplacementRecord, mission_id) == []

        assert (await _delegate(client, mission_id, ["request_replacement"])).status_code == 200
        turn = await _agent(
            client, mission_id, "now that I have allowed it, send the replacement please"
        )
        assert _tools(turn)[:2] == ["assess_issue", "resolve_issue"]
        # The Engine allowed the action, and the tool genuinely ran — which is precisely why the
        # outcome has to be read from the persisted resolution, not from a call-level success flag.
        assert turn["authority_decision"]["approved"] is True
        assert turn["authority_decision"]["resolution_state"] == "pending"
        repl = next(i for i in await _issues(client, mission_id) if i["issue_id"] == repl_issue_id)
        assert repl["assessment"]["authority_decision"] == "allow"
        assert repl["resolution"]["resolution_state"] == "pending"
        # S13 — the record is pending, with no provider and no reference of any kind.
        repl_rows = await _rows(test_session, ReplacementRecord, mission_id)
        assert len(repl_rows) == 1
        assert repl_rows[0].status == "pending"
        assert repl_rows[0].provider == "none"
        assert repl_rows[0].provider_reference is None
        assert repl_rows[0].confirmed_at is None

        # ---- STEP 15: retry the failed action — one record, still no success --- #
        retry = await _resolve(client, mission_id, repl_issue_id)
        assert retry.status_code == 503, retry.text
        assert retry.json()["resolution_state"] == "pending"
        assert len(await _rows(test_session, ReplacementRecord, mission_id)) == 1
        # S14 — the refund that already verified is not issued a second time by this chain.
        assert len(refund_api.create_calls) == 1
        assert len(await _rows(test_session, RefundRecord, mission_id)) == 1

        # ---- STEP 16: the final state, re-read from the database --------------- #
        record = await _mission_row(test_session, mission_id)
        assert record.status == "awaiting_post_purchase_context"
        assert record.shopping_intent["state"] == "awaiting_post_purchase_context"
        assert record.shopping_intent["payment"]["status"] == "verified"
        assert record.shopping_intent["selected_product"]["product_id"] == chosen["product_id"]
        order_rows = await _rows(test_session, ResolveOrderRecord, mission_id)
        assert [order.status for order in order_rows] == ["awaiting_post_purchase_context"], (
            "intake moves the order into post-purchase context — one order row, still no second one"
        )
        assert order_rows[0].order_id == order_id
        assert len(await _rows(test_session, OrderIssueRecord, mission_id)) == 2
        assert len(await _rows(test_session, ResolutionAssessmentRecord, mission_id)) == 2
        assert len(await _rows(test_session, ResolutionRecord, mission_id)) == 2
        assert {row.resolution_state for row in await _rows(test_session, ResolutionRecord, mission_id)} == {
            "verified",
            "pending",
        }
        assert "request_replacement" in (record.mandate or {}).get("authority", {}).get(
            "allowed_actions", []
        )

        # ---- PHASE 3a: the transcript is ordered, complete, and says nothing it shouldn't #
        events = await _transcript(client, mission_id)
        assert [event["sequence"] for event in events] == list(range(len(events)))
        assert {event["event_type"] for event in events} >= {
            "user_message",
            "tool_call",
            "tool_result",
            "agent_message",
            "system_state_change",
        }
        assert events[0]["event_type"] == "user_message"
        assert [event["event_type"] for event in events[1:4]] == [
            "tool_call",
            "tool_result",
            "agent_message",
        ]
        called = [event["tool_name"] for event in events if event["event_type"] == "tool_call"]
        assert called[:2] == ["search_products", "prepare_checkout"]
        assert called[-2:] == ["assess_issue", "resolve_issue"]
        assert called.count("report_issue") == 1, (
            "the agent opened the complaint it was talking about; the second ticket came in through "
            "the intake route, because one mission has one current issue in the agent's context"
        )
        transitions = [event["payload"] for event in events if event["event_type"] == "system_state_change"]
        assert {"from": "ready_for_discovery", "to": "discovery_completed"} in transitions
        assert {"from": "product_selected", "to": "checkout_review"} in transitions
        # S15 — no chain-of-thought is persisted anywhere in the transcript.
        transcript_blob = str([(event["content"], event["payload"]) for event in events])
        for forbidden in ("chain_of_thought", "chain-of-thought", "reasoning_content"):
            assert forbidden not in transcript_blob

        # ---- PHASE 3b: the audit trail covers every lifecycle stage ----------- #
        audit = await _audit(test_session, mission_id)
        for stage in (
            "select_product",
            "checkout_confirmed",
            "payment_order_created",
            "payment_verification_refused",
            "payment_verified",
            "order_created",
            "order_confirmed",
            "issue_reported",
        ):
            assert stage in audit, f"no audit evidence for {stage}"
        assert audit["select_product"][0]["decision"] == "observed"
        assert audit["payment_verification_refused"][0]["decision"] == "refused"
        assert "did not match" in audit["payment_verification_refused"][0]["reason"]
        assert "TEST ORDER" in audit["payment_order_created"][0]["reason"].upper()
        assert "TEST MODE" in audit["payment_verified"][0]["reason"].upper()
        # The Engine's decisions are on the record on both sides of the delegation.
        refund_decisions = [row["decision"] for row in audit["request_refund"]]
        assert "blocked" in refund_decisions and "approved" in refund_decisions
        assert "blocked" in [row["decision"] for row in audit["request_replacement"]]
        assert len(audit["issue_reported"]) == 2, "both reports are auditable facts"
        # S16 — no credential value appears in any persisted evidence row.
        settings = get_settings()
        needles = [
            value
            for value in (
                settings.razorpay_key_secret,
                settings.razorpay_key_id,
                settings.reasoning_api_key,
                settings.serpapi_api_key,
                settings.linkage_integration_secret,
            )
            if value
        ]
        reasons = "\n".join(row["reason"] for entries in audit.values() for row in entries)
        for needle in needles:
            assert needle not in reasons
        assert "rzp_live_" not in reasons
        assert "shpat_" not in reasons

        # ---- the lifecycle's own events were all emitted ---------------------- #
        emitted = {event["event"] for event in recorded_events}
        assert {
            "product_selected",
            "checkout_confirmed",
            "payment_order_created",
            "payment_failed",
            "payment_verified",
            "order_created",
            "order_confirmed",
            "order_issue_reported",
            "authority_delegated",
        } <= emitted

    # ---- restart persistence: a different process reads exactly the same facts --- #
    async with make_client() as observer:
        # S18 — a caller with no session cannot even learn that this mission exists.
        assert (await observer.get(f"/missions/{mission_id}")).status_code == 404
    async with make_client() as owner:
        await _login(owner, email)
        after_restart = await owner.get(f"/missions/{mission_id}")
        assert after_restart.status_code == 200, after_restart.text
        assert after_restart.json()["status"] == "awaiting_post_purchase_context"
        assert len(await _transcript(owner, mission_id)) == len(events)
        # S17 — a refusal survives a restart as a refusal, in the audit table.
        assert (await _audit(test_session, mission_id))["payment_verification_refused"]


# --------------------------------------------------------------------------- #
# S19 + S20 — the payment boundary is structural: it is not a capability at all.
# --------------------------------------------------------------------------- #
async def test_payment_and_checkout_confirmation_are_not_capabilities_at_all(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, _test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "struct@core3a.test"
        )
        declared = set(tool_names())
        # S19 — the registry is closed and holds exactly the declared capabilities. The ten are
        # the nine from Phase 2 plus `recommend_product`, the one deliberate CORE-5 Phase 5
        # addition (§11: the smallest capability that lets a recommendation phrase resolve from
        # structured, persisted identity; it validates against the active set and records a
        # reference only). `check_purchase_eligibility` is the Buyer-flow Phase 3 mandated
        # read-only gate report — it authorizes nothing and submits nothing.
        assert declared == {
            "search_products",
            "clarify_variant",
            "select_product",
            "recommend_product",
            "prepare_checkout",
            "report_issue",
            "assess_issue",
            "resolve_issue",
            "request_more_information",
            "escalate_to_human",
            "check_purchase_eligibility",
        }
        assert not declared & {
            "create_payment",
            "confirm_payment",
            "confirm_checkout",
            "create_order",
            "delegate_authority",
            "grant_authority",
            "shopify_mutation",
        }
        surface = await client.get(f"/missions/{mission_id}/agent/tools")
        assert surface.status_code == 200
        assert surface.json()["not_available"] == [
            "create_payment",
            "confirm_payment",
            "confirm_checkout",
            "any_http_call",
            "any_python_function",
            "any_sql",
            "any_shopify_mutation",
        ]
        # S20 — payment confirmation is not even a delegable action, so no mandate can grant it.
        assert (await _delegate(client, mission_id, ["confirm_payment"])).status_code == 422


# --------------------------------------------------------------------------- #
# Delegation narrows the deployment's bounds; it never widens them.
# --------------------------------------------------------------------------- #
async def test_delegation_may_narrow_the_ceiling_but_never_raise_it(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "ceiling@core3a.test"
        )
        await _confirmed_order(client, mission_id)
        reported = await _report(
            client,
            mission_id,
            issue_type="damaged",
            description="Cracked in transit; refund please.",
            requested_resolution="refund",
        )
        assert reported.status_code == 200
        issue_id = reported.json()["issue_id"]

        ceiling = Decimal(str(get_settings().shopping_refund_max_inr))
        # A delegation above the deployment ceiling is refused outright.
        over = await _delegate(client, mission_id, ["request_refund"], refund_limit=str(ceiling + 1))
        assert over.status_code == 400
        assert "never raise" in over.json()["detail"]
        # An action outside the vocabulary is a schema error, never a grant.
        assert (await _delegate(client, mission_id, ["execute_payment"])).status_code == 422

        # A narrower ceiling is accepted — and it binds. The product is priced above ₹1, so the
        # Engine must escalate the same proposal it would otherwise allow: an above-ceiling
        # remedy is an authority gap the owner can cure, not a deployment refusal.
        assert Decimal(str(chosen["extracted_price"])) > Decimal("1")
        limited = await _delegate(client, mission_id, ["request_refund"], refund_limit="1")
        assert limited.status_code == 200, limited.text
        assessed = await _assess(client, mission_id, issue_id)
        assert assessed.status_code == 200, assessed.text
        assert assessed.json()["proposed_action"] == "request_refund"
        # The model still proposes the full amount; the Engine still gets the last word.
        assert Decimal(str(assessed.json()["requested_amount"])) == Decimal(
            str(chosen["extracted_price"])
        )
        assert assessed.json()["authority_decision"] == "escalate"
        denied = await _resolve(client, mission_id, issue_id)
        assert denied.status_code == 400
        assert refund_api.create_calls == []
        assert await _rows(test_session, RefundRecord, mission_id) == []

        # Revocation takes the grant back; spend history stays where it is.
        revoked = await client.post(f"/missions/{mission_id}/authority/revoke")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["authority"]["allowed_actions"] == []
        assert revoked.json()["previously_delegated"] == ["request_refund"]
        assert (await _resolve(client, mission_id, issue_id)).status_code == 400
        assert refund_api.create_calls == []


# --------------------------------------------------------------------------- #
# A price RESOLVE does not support stops at payment, before any provider call.
# --------------------------------------------------------------------------- #
async def test_a_price_in_an_unsupported_currency_never_reaches_the_payment_rail(
    restart_env, install_mock_discovery, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    usd_product = ProductResult(
        product_id="item_usd_only",
        title="Imported Shampoo 400ml",
        price="$12.00",
        extracted_price=Decimal("12.00"),
        currency="USD",
        merchant="Example Store",
    )
    install_mock_discovery(sample_results=[usd_product])
    async with client:
        await _register(client, "Currency Buyer", "currency@core3a.test")
        created = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        assert created.status_code == 200, created.text
        mission_id = created.json()["mission_id"]
        assert (await _select(client, mission_id, "item_usd_only")).status_code == 200
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        refused = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        assert refused.status_code == 400
        assert "does not convert" in refused.json()["detail"]
        assert mock_razorpay["n"] == 0, "the currency gate runs before the payment rail"
        assert await _rows(test_session, ResolveOrderRecord, mission_id) == []
        # Discovery and checkout still work in the store's own currency; only payment refuses.
        assert (await _mission_row(test_session, mission_id)).status == "checkout_confirmed"


# --------------------------------------------------------------------------- #
# A provider that cannot create the refund is recorded as a failure, never a success.
# --------------------------------------------------------------------------- #
async def test_a_refund_the_provider_cannot_create_is_recorded_failed_never_verified(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "failed-refund@core3a.test"
        )
        await _confirmed_order(client, mission_id)
        reported = await _report(
            client,
            mission_id,
            issue_type="damaged",
            description="Leaking bottle; money back.",
            requested_resolution="refund",
        )
        issue_id = reported.json()["issue_id"]
        assert (await _delegate(client, mission_id, ["request_refund"])).status_code == 200
        assert (await _assess(client, mission_id, issue_id)).json()["authority_decision"] == "allow"

        refund_api.create_error = RuntimeError("Razorpay is not accepting refunds right now")
        attempt = await _resolve(client, mission_id, issue_id)
        assert attempt.status_code == 502, attempt.text
        rows = await _rows(test_session, RefundRecord, mission_id)
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].razorpay_refund_id is None
        audit = await _audit(test_session, mission_id)
        assert any("provider call failed" in row["reason"] for row in audit["request_refund"])
        assert not any("status=verified" in row["reason"] for row in audit["request_refund"])
        assert attempt.json()["resolution_state"] in {"failed", "denied"}

        # Recovery reuses the same claim: still one row, and the retry is what finally verifies.
        refund_api.create_error = None
        retried = await _resolve(client, mission_id, issue_id)
        assert retried.status_code == 200, retried.text
        rows = await _rows(test_session, RefundRecord, mission_id)
        assert len(rows) == 1
        assert rows[0].status == "verified"
        assert refund_api.calls["create"] == 2, "one create per genuine attempt, no silent extra call"


# --------------------------------------------------------------------------- #
# Idempotency and dedupe, as the product actually behaves them.
# --------------------------------------------------------------------------- #
async def test_repeats_of_the_same_request_produce_one_record_and_an_honest_answer(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "dedupe@core3a.test"
        )
        await _confirmed_order(client, mission_id)

        body = {
            "issue_type": "missing_item",
            "description": "The box was empty.",
            "requested_resolution": "refund",
        }
        first = await _report(client, mission_id, **body)
        assert first.status_code == 200
        assert first.json()["already_existed"] is False
        repeat = await _report(client, mission_id, **body)
        assert repeat.status_code == 200
        assert repeat.json()["already_existed"] is True
        assert repeat.json()["issue_id"] == first.json()["issue_id"]
        assert len(await _rows(test_session, OrderIssueRecord, mission_id)) == 1

        issue_id = first.json()["issue_id"]
        assessed_once = await _assess(client, mission_id, issue_id)
        assert assessed_once.status_code == 200
        assessed_twice = await _assess(client, mission_id, issue_id)
        assert assessed_twice.status_code == 200
        assert assessed_twice.json()["already_existed"] is True
        assert len(await _rows(test_session, ResolutionAssessmentRecord, mission_id)) == 1

        # A second identical delegation changes no mandate — and is still its own audited fact.
        assert (await _delegate(client, mission_id, ["request_refund"])).status_code == 200
        second = await _delegate(client, mission_id, ["request_refund"])
        assert second.status_code == 200, second.text
        assert second.json()["authority"]["allowed_actions"] == ["request_refund"]
        record = await _mission_row(test_session, mission_id)
        assert (record.mandate or {})["authority"]["allowed_actions"] == ["request_refund"]
        audit = await _audit(test_session, mission_id)
        assert len([row for row in audit["request_refund"] if row["actor"] == "user"]) >= 2, (
            "each delegation is a separate entry in an append-only trail"
        )
        assert len(await _rows(test_session, MissionRecord, mission_id)) == 1


# --------------------------------------------------------------------------- #
# Ownership: a foreign id is invisible, not merely forbidden.
# --------------------------------------------------------------------------- #
async def test_another_users_mission_issue_and_order_are_invisible_not_just_forbidden(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    owner_client = make_client()
    attacker_client = make_client()
    async with owner_client, attacker_client:
        mission_id, _chosen = await _to_paid_mission(
            owner_client, install_mock_discovery, sample_products, "owner@core3a.test"
        )
        await _confirmed_order(owner_client, mission_id)
        reported = await _report(
            owner_client,
            mission_id,
            issue_type="wrong_item",
            description="Not what I ordered.",
            requested_resolution="refund",
        )
        issue_id = reported.json()["issue_id"]

        await _register(attacker_client, "Attacker", "attacker@core3a.test")
        # Each body below is a schema-valid one, so the request always reaches the ownership guard
        # instead of stopping at request validation: the question here is whose mission this is.
        for method, url, body in (
            ("get", f"/missions/{mission_id}", None),
            ("post", f"/missions/{mission_id}/shopping/select-product", {"product_id": "item_dove"}),
            ("get", f"/missions/{mission_id}/shopping/checkout", None),
            ("post", f"/missions/{mission_id}/shopping/checkout/confirm", {}),
            ("post", f"/missions/{mission_id}/shopping/payment/create-order", {}),
            (
                "post",
                f"/missions/{mission_id}/shopping/payment/verify",
                {
                    "razorpay_order_id": "order_TEST0001",
                    "razorpay_payment_id": PAYMENT_ID,
                    "razorpay_signature": "0" * 64,
                },
            ),
            ("post", f"/missions/{mission_id}/order", {}),
            ("post", f"/missions/{mission_id}/order/confirm", {}),
            (
                "post",
                f"/missions/{mission_id}/order/issues",
                {
                    "issue_type": "damaged",
                    "description": "Someone else's bottle.",
                    "requested_resolution": "refund",
                },
            ),
            ("post", f"/missions/{mission_id}/order/issues/{issue_id}/assess", {}),
            ("post", f"/missions/{mission_id}/order/issues/{issue_id}/resolve", {}),
            (
                "post",
                f"/missions/{mission_id}/authority/delegate",
                {"allowed_actions": ["request_refund"]},
            ),
            ("post", f"/missions/{mission_id}/authority/revoke", {}),
            ("post", f"/missions/{mission_id}/agent", {"user_input": "refund it now"}),
            ("get", f"/missions/{mission_id}/agent/conversation", None),
            ("get", f"/missions/{mission_id}/agent/tools", None),
        ):
            request = getattr(attacker_client, method)
            response = await request(url, json=body) if body is not None else await request(url)
            # 404 on every surface: the existence of someone else's mission is not public information.
            assert response.status_code == 404, f"{method.upper()} {url} -> {response.status_code}"

        untouched = await _mission_row(test_session, mission_id)
        # The buyer's own chain is exactly where they left it: one issue, no assessment, no resolution,
        # and the mission still in post-purchase context.
        assert untouched.status == "awaiting_post_purchase_context"
        assert len(await _rows(test_session, OrderIssueRecord, mission_id)) == 1
        assert await _rows(test_session, ResolutionAssessmentRecord, mission_id) == []
        assert await _rows(test_session, ResolutionRecord, mission_id) == []
        assert await _rows(test_session, RefundRecord, mission_id) == []
        # S18 — and none of the attacker's attempts were recorded as the buyer doing something.
        audit = await _audit(test_session, mission_id)
        assert not [
            row
            for entries in audit.values()
            for row in entries
            if "Someone else" in row["reason"] or "attacker" in row["reason"]
        ]


# --------------------------------------------------------------------------- #
# PHASE 7 — this evidence needs no live provider, and says so structurally.
# --------------------------------------------------------------------------- #
async def test_this_milestones_evidence_needs_no_live_provider():
    settings = get_settings()
    assert settings.reasoning_provider == "mock"
    assert settings.product_search_provider == "mock"
    assert settings.replacement_provider == "none"
    assert settings.return_provider == "none"
    assert get_replacement_provider().name == "none"
    assert get_return_provider().name == "none"
    # The provider the router asks for is the offline one, so no step in this file can reach a vendor.
    provider = get_reasoning_provider(settings.reasoning_provider, settings.reasoning_api_key)
    assert type(provider).__name__ == "MockReasoningProvider"


def test_no_shopify_client_factory_and_no_payment_call_live_in_the_agent_layer():
    """
    Two structural proofs, read off the shipped source rather than off a configuration.

    * The agent's capability surface is whatever `_register("...")` declares in the tool registry, and
      nothing payment-shaped is in that list.
    * The agent layer calls no payment provider and no Shopify client of its own: every consequence
     goes through a guarded production route, which is why refusing the Shopify factories at test
      level is enough to make this milestone's Shopify mutation count zero.
    """
    agents_dir = Path(missions_module.__file__).parent.parent / "agents"
    registry = ast.parse((agents_dir / "tools.py").read_text(encoding="utf-8"))
    registered: set[str] = set()
    for node in ast.walk(registry):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "_register"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            registered.add(node.args[0].value)
    assert "select_product" in registered
    assert not registered & {
        "create_payment",
        "confirm_payment",
        "confirm_checkout",
        "create_order",
        "delegate_authority",
    }

    for source in sorted(agents_dir.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for forbidden in (
            "get_shopify_client",
            "create_test_order",
            "create_payment_refund",
            "verify_payment_signature",
        ):
            assert forbidden not in text, f"{source.name} must not reach {forbidden} itself"
