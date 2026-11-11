"""
Task CORE-2.1, Objectives 1 and 4 — the model call survives a rate limit, and a retry never runs a
tool twice.

The quota is this deployment's real problem: `gemini-3.6-flash` answers, then answers 429 for long
stretches, and one attempt cannot tell "ask again in a second" from "this service is dead". The fix is
a bounded retry in the one place that speaks HTTP — the reasoning provider — and nowhere else. It
lives there rather than in the agent loop for a reason worth stating: at the point a model call is
retried, **no tool has been chosen yet**, so a retry can produce a second *decision* but can never
produce a second *execution*.

Hermetic like the rest of the suite: every provider call here goes through a scripted HTTP client. No
test in this file can reach Google, Groq, or Razorpay.
"""
import hashlib
import hmac
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents import reasoning_provider as provider_module
from app.agents.reasoning_provider import (
    REASONING_TIMEOUT_SECONDS,
    RESPOND_TOOL_NAME,
    FallbackReasoningProvider,
    GeminiReasoningProvider,
    GroqReasoningProvider,
    ReasoningUnavailable,
    _backoff_seconds,
    get_reasoning_provider,
)
from app.agents.tools import declarations_for_model
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.db_models import AgentConversationRecord
from app.routers import missions as missions_module

# The mock Razorpay client and the inert refund/replacement API, re-used by name rather than cloned
# (the convention the rest of the suite follows). Both fixtures are what keep these turns hermetic:
# one turn here walks an order all the way to `order_confirmed`.
from tests.test_core2_agent_security import mock_razorpay, refund_api  # noqa: E402,F401

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
SHAMPOO_PROMPT = "I want to order shampoo under ₹600 for anti-hairfall."
PASSWORD = "SecurePassword123!"
API_KEY = "gemini-key-DO-NOT-LEAK"


@pytest.fixture
async def async_client():
    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    test_session = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with test_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    await test_engine.dispose()


@pytest.fixture
async def session_factory():
    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    test_session = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_engine, test_session
    await test_engine.dispose()


@pytest.fixture
def budget(monkeypatch):
    """A named retry budget, so each test states the number it expects instead of inheriting one."""
    settings = get_settings()
    monkeypatch.setattr(settings, "reasoning_max_retries", 2, raising=False)
    monkeypatch.setattr(settings, "reasoning_retry_base_seconds", 0.5, raising=False)
    return {"retries": 2, "base": 0.5}


@pytest.fixture
def no_sleeping(monkeypatch):
    """The waits are recorded, not lived through."""
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(provider_module, "_pause", record)
    return waits


@pytest.fixture(autouse=True)
def closed_circuit():
    """The failover cooldown is module state by design — it must outlive one request — so each
    test here starts (and leaves) a closed breaker: an exhaustion scripted in one test must not
    silently reroute another test's provider calls to its stand-by."""
    provider_module._circuit_open_until.clear()
    yield
    provider_module._circuit_open_until.clear()


class _ScriptedHttpx:
    """
    The provider's HTTP client, serving a fixed sequence of answers.

    Once the sequence runs out the last answer repeats — that is how a test says "this provider is
    simply unavailable" without having to name the attempt count twice.
    """

    def __init__(self, *responses: httpx.Response):
        self._responses = list(responses)
        self.sent: list[dict] = []

    def __call__(self, *args, **kwargs):
        outer = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None, params=None):
                outer.sent.append(
                    {
                        "url": url,
                        "headers": dict(headers or {}),
                        "json": json or {},
                        "client_kwargs": dict(kwargs),
                    }
                )
                index = len(outer.sent) - 1
                answer = outer._responses[min(index, len(outer._responses) - 1)]
                if isinstance(answer, Exception):
                    raise answer
                return answer

        return _Client()


def _refused(status_code: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"error": {"message": "nope"}},
        headers=headers or {},
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/x"),
    )


def _function_call(name: str, arguments: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"functionCall": {"name": name, "args": arguments}}]}}]},
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/x"),
    )


def _answer(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
    )


def _intent_answer(state: str = "discovery_completed") -> httpx.Response:
    """
    The answer to the request that comes *before* the loop.

    A `/agent` turn is not one model call. The message is first structured by the intent parser — a
    plain text request, no `tools` in its body — and only then does the step chooser get asked. A
    script that starts at the chooser is a request short, and the parse then misreads the tool call as
    prose, parks the turn, and never runs anything.
    """
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "category": "shampoo",
                                        "budget": 600,
                                        "currency": "INR",
                                        "preferences": ["anti-hairfall"],
                                        "brand": None,
                                        "size_or_variant": None,
                                        "missing_info": [],
                                        "clarification_prompt": None,
                                        "state": state,
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        },
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/x"),
    )


def _decision_requests(scripted) -> list[dict]:
    """The step chooser's requests, which are the only ones that carry `tools`."""
    return [sent for sent in scripted.sent if "tools" in sent["json"]]


def _proposal_answer(action: str = "request_refund") -> httpx.Response:
    """
    The answer a guarded operation asks for itself.

    `assess_issue` runs the real assessment path, which puts its own untrusted proposal question to
    the model. This is that answer — a resolution proposal, no amount, because a refund's amount is
    the canonical order amount and never the model's number.
    """
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "issue_assessment": "Reported damaged item on the order.",
                                        "proposed_action": action,
                                        "reasoning_summary": "Scripted proposal for a test. Nothing executed.",
                                        "requested_amount": None,
                                        "currency": "INR",
                                        "confidence": 0.8,
                                        "needs_escalation": False,
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        },
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/x"),
    )


def _provider() -> GeminiReasoningProvider:
    return GeminiReasoningProvider(api_key=API_KEY, model="gemini-test")


# --------------------------------------------------------------------------- #
# 1. The retry itself: bounded, sequential, exponential, and honest when it runs out.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_one_transient_429_is_retried_and_the_answer_still_arrives(
    monkeypatch, budget, no_sleeping
):
    """The rate limit this deployment actually hits, survivable in one quiet retry."""
    scripted = _ScriptedHttpx(_refused(429), _function_call("search_products", {"query": "shampoo"}))
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    decision = await _provider().choose_agent_action({"mission": {}}, declarations_for_model())

    assert decision.action == "tool" and decision.tool_name == "search_products"
    assert len(scripted.sent) == 2
    assert len(no_sleeping) == 1, "one retry, not two"
    assert 0.25 <= no_sleeping[0] <= 0.5, "the first wait is base-sized, scattered inside that"


@pytest.mark.asyncio
async def test_a_rate_limit_that_will_not_lift_is_bounded_and_names_the_attempts(
    monkeypatch, budget, no_sleeping
):
    """
    Two attempts, one wait, then a refusal — and never a third request to a dead quota.

    An unbounded retry against a rate-limited service is a self-inflicted denial of service on your
    own quota. One quiet retry survives the transient burst the deployment actually hits; a SECOND
    429 means the quota window will not open within this call's budget, so the ladder stops here
    and the stand-by provider — a different quota pool — takes the call instead.
    """
    scripted = _ScriptedHttpx(_refused(429))
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    with pytest.raises(ReasoningUnavailable) as raised:
        await _provider().choose_agent_action({"mission": {}}, declarations_for_model())

    assert len(scripted.sent) == 2
    assert len(no_sleeping) == 1
    assert raised.value.attempts == 2
    assert raised.value.status_code == 429
    assert "2 attempt" in str(raised.value)


@pytest.mark.asyncio
async def test_a_zero_retry_budget_means_exactly_one_attempt(monkeypatch, no_sleeping):
    """The knob may legitimately be turned off, and off must mean one call. Nothing assumes a retry."""
    settings = get_settings()
    monkeypatch.setattr(settings, "reasoning_max_retries", 0, raising=False)
    scripted = _ScriptedHttpx(_refused(429))
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    with pytest.raises(ReasoningUnavailable) as raised:
        await _provider().choose_agent_action({"mission": {}}, declarations_for_model())

    assert len(scripted.sent) == 1
    assert no_sleeping == []
    assert raised.value.attempts == 1


@pytest.mark.asyncio
async def test_the_refusal_never_carries_the_credential(monkeypatch, budget, no_sleeping):
    """
    The agent loop quotes this sentence back to the buyer, so it has to be safe to quote.

    The key appears in exactly one place on the wire — the header Google specifies — and in no URL,
    no body, no log line and no error message.
    """
    scripted = _ScriptedHttpx(_refused(429))
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    with pytest.raises(ReasoningUnavailable) as raised:
        await _provider().choose_agent_action({"mission": {}}, declarations_for_model())

    assert API_KEY not in str(raised.value)
    wire = json.dumps(
        [{"url": s["url"], "json": s["json"], "kwargs": s["client_kwargs"]} for s in scripted.sent]
    )
    assert API_KEY not in wire
    assert all(s["headers"].get("x-goog-api-key") == API_KEY for s in scripted.sent)
    assert all(API_KEY not in s["url"] for s in scripted.sent)


@pytest.mark.asyncio
async def test_a_request_refused_on_its_merits_is_not_retried(monkeypatch, budget, no_sleeping):
    """
    A 400 is an answer, not a delay.

    Google rejecting the declared schema is precisely what happened once during CORE-2. Retrying it
    would spend quota to hide a defect; the useful failure is the one that names the request.
    """
    scripted = _ScriptedHttpx(_refused(400))
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    with pytest.raises(httpx.HTTPStatusError):
        await _provider().choose_agent_action({"mission": {}}, declarations_for_model())

    assert len(scripted.sent) == 1
    assert no_sleeping == []


@pytest.mark.asyncio
async def test_the_5xx_family_is_transient_and_a_404_is_not(monkeypatch, budget, no_sleeping):
    """
    502/503/504 say the service had a moment; 404 says you asked for something that is not there.

    Both were measured against this API — a 503 capacity event, and a 404 for a closed model id — and
    only the first is worth a second attempt.
    """
    for status_code, expected_attempts in ((503, 3), (502, 3), (404, 1), (403, 1)):
        scripted = _ScriptedHttpx(_refused(status_code))
        monkeypatch.setattr("httpx.AsyncClient", scripted)
        with pytest.raises(Exception):
            await _provider().choose_agent_action({"mission": {}}, declarations_for_model())
        assert len(scripted.sent) == expected_attempts, status_code


@pytest.mark.asyncio
async def test_a_dropped_connection_is_transient_too(monkeypatch, budget, no_sleeping):
    """
    A call that never answers is the same 'ask again in a moment' a 429 is.

    The empty-detail refusal the demo once showed arrived through exactly this path: a transport
    error escaped the transport un-retried and the loop quoted an exception with nothing in it.
    Retried here like any other transient answer; exhaustion still names the attempts, with
    'no answer' where a status code would be — and never the credential.
    """
    scripted = _ScriptedHttpx(
        httpx.ReadTimeout("timed out"), _function_call(RESPOND_TOOL_NAME, {"message": "hi"})
    )
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    decision = await _provider().choose_agent_action({"mission": {}}, declarations_for_model())

    assert decision.action == "respond"
    assert len(scripted.sent) == 2
    assert len(no_sleeping) == 1


@pytest.mark.asyncio
async def test_a_connection_that_never_open_exhausts_to_a_named_no_answer(
    monkeypatch, budget, no_sleeping
):
    """A dead network is bounded the same way a dead quota is: attempts spent, then refusal."""
    scripted = _ScriptedHttpx(httpx.ConnectError("connection refused"))
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    with pytest.raises(ReasoningUnavailable) as raised:
        await _provider().choose_agent_action({"mission": {}}, declarations_for_model())

    assert len(scripted.sent) == budget["retries"] + 1 == 3
    assert len(no_sleeping) == 2
    assert raised.value.attempts == 3
    assert raised.value.status_code is None
    assert "no answer" in str(raised.value)


# --------------------------------------------------------------------------- #
# The stand-by provider: an exhausted primary is not the end of the call.
# --------------------------------------------------------------------------- #


class _StubProvider:
    """A provider reduced to the one method a test is watching; the wrapper delegates by name."""

    def __init__(self, behavior) -> None:
        self.calls = 0
        self._behavior = behavior

    async def assess_resolution(self, context):
        self.calls += 1
        return await self._behavior(context)


@pytest.mark.asyncio
async def test_an_exhausted_primary_places_the_same_call_with_the_stand_by():
    async def exhausted(context):
        raise ReasoningUnavailable("Gemini", 429, attempts=3)

    async def standing_by(context):
        return {"proposed_action": "request_refund"}

    primary = _StubProvider(exhausted)
    standby = _StubProvider(standing_by)
    wrapper = FallbackReasoningProvider(primary, standby)

    proposal = await wrapper.assess_resolution({"issue": {}})

    assert primary.calls == 1
    assert standby.calls == 1
    assert proposal == {"proposed_action": "request_refund"}


@pytest.mark.asyncio
async def test_a_defect_is_not_hidden_behind_the_stand_by():
    """
    Failover answers 'the provider is down', never 'the code is wrong'.

    A request refused on its merits must reach the log as what it is — so the same defect is not
    quietly re-asked of another vendor and Camouflaged into a second opinion.
    """

    async def broken(context):
        raise ValueError("malformed request built upstream")

    standby = _StubProvider(lambda context: {"proposed_action": "request_refund"})
    wrapper = FallbackReasoningProvider(_StubProvider(broken), standby)

    with pytest.raises(ValueError):
        await wrapper.assess_resolution({})

    assert standby.calls == 0


@pytest.mark.asyncio
async def test_a_defect_does_not_put_the_primary_in_cooldown():
    """
    Failover answers 'the provider is down', never 'the code is wrong' — twice over.

    A refusal on its merits must not merely fail over honestly; it must not reroute the NEXT
    call either. The cooldown exists for quota windows and outages, and a defect is neither.
    """
    async def broken(context):
        raise ValueError("malformed request built upstream")

    primary = _StubProvider(broken)
    standby = _StubProvider(lambda context: {"proposed_action": "request_refund"})
    wrapper = FallbackReasoningProvider(primary, standby)

    with pytest.raises(ValueError):
        await wrapper.assess_resolution({})
    with pytest.raises(ValueError):
        await wrapper.assess_resolution({})

    assert primary.calls == 2
    assert standby.calls == 0
    assert provider_module._circuit_open_until == {}


@pytest.mark.asyncio
async def test_after_one_exhaustion_the_next_call_skips_the_dead_primary():
    """
    The quota window that just refused every attempt will refuse the next call's attempts too.

    The first call fails over to the stand-by and records a cooldown, so the SECOND model call of
    the same turn reaches the stand-by on its first request instead of re-paying the primary's
    dead ladder. An expired cooldown asks the primary again exactly as before — a recovery is
    noticed within one cooldown, and a recovery that never comes costs nothing but log lines.
    """
    exhausted = {"now": True}

    async def exhaust_then_answer(context):
        if exhausted["now"]:
            raise ReasoningUnavailable("Gemini", 429, attempts=2)
        return {"proposed_action": "request_refund"}

    primary = _StubProvider(exhaust_then_answer)

    async def standing_by(context):
        return {"proposed_action": "request_refund"}

    standby = _StubProvider(standing_by)
    wrapper = FallbackReasoningProvider(primary, standby)

    assert await wrapper.assess_resolution({}) == {"proposed_action": "request_refund"}
    assert await wrapper.assess_resolution({}) == {"proposed_action": "request_refund"}
    assert primary.calls == 1, "the cooldown skipped the primary on the second call"
    assert standby.calls == 2

    provider_module._circuit_open_until.clear()  # the cooldown has expired
    exhausted["now"] = False
    assert await wrapper.assess_resolution({}) == {"proposed_action": "request_refund"}
    assert primary.calls == 2, "an expired cooldown asks the primary again"
    assert standby.calls == 2


def test_the_gemini_provider_gets_a_groq_stand_by_only_when_a_credential_exists(monkeypatch):
    """
    The wiring contract: with REASONING_GROQ_API_KEY set, a Gemini deployment is wrapped and its
    stand-by is a real Groq provider holding exactly that credential; without one, nothing changes.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "reasoning_groq_api_key", "standby-key-DO-NOT-LEAK", raising=False)

    wrapped = get_reasoning_provider("gemini", API_KEY)
    assert isinstance(wrapped, FallbackReasoningProvider)
    assert isinstance(wrapped.primary, GeminiReasoningProvider)
    assert isinstance(wrapped.fallback, GroqReasoningProvider)
    assert wrapped.fallback.api_key == "standby-key-DO-NOT-LEAK"

    monkeypatch.setattr(settings, "reasoning_groq_api_key", "", raising=False)
    assert isinstance(get_reasoning_provider("gemini", API_KEY), GeminiReasoningProvider)
    assert not isinstance(get_reasoning_provider("mock", ""), FallbackReasoningProvider)


@pytest.mark.asyncio
async def test_the_waits_grow_exponentially_and_never_leave_the_budget(monkeypatch, no_sleeping):
    """
    Asking for more retries than the transport will grant is refused, not obeyed.

    `reasoning_max_retries` is bounded by its own field, but a `Settings` built by hand in code skips
    that validation — so the transport clamps as well, and a retry budget can never become a request
    storm. Equal jitter — half the window fixed, half scattered — is what stops the retries arriving
    all at once while never landing at zero seconds.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "reasoning_max_retries", 5, raising=False)
    monkeypatch.setattr(settings, "reasoning_retry_base_seconds", 0.5, raising=False)
    # The full ladder is exercised on the 5xx family — a persistent 429 short-circuits it
    # after two attempts (the stand-by is a different quota pool), so this property needs a
    # "service had a moment" status that is worth waiting for.
    scripted = _ScriptedHttpx(_refused(503))
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    with pytest.raises(ReasoningUnavailable) as info:
        await _provider().choose_agent_action({"mission": {}}, declarations_for_model())

    granted_retries = 4
    assert len(scripted.sent) == granted_retries + 1
    assert info.value.attempts == granted_retries + 1

    ceilings = [0.5 * (2**attempt) for attempt in range(granted_retries)]
    assert len(no_sleeping) == granted_retries
    for wait, ceiling in zip(no_sleeping, ceilings):
        assert ceiling / 2 <= wait <= ceiling
    assert sum(no_sleeping) <= sum(ceilings)


def test_retry_after_is_listened_to_but_cannot_hold_the_turn_hostage():
    """
    The provider's own advice is honoured up to the ceiling this attempt had already chosen.

    A `Retry-After: 3600` — mistaken or hostile — must not turn one bounded model call into an hour of
    a buyer's request hanging in the middle of a conversation.
    """
    assert _backoff_seconds(0, 0.5, "3600") <= 0.5
    assert _backoff_seconds(2, 0.5, None) <= 2.0
    assert _backoff_seconds(1, 0.5, "not-a-number") <= 1.0
    assert _backoff_seconds(0, 0.5, "0.4") <= 0.5
    assert _backoff_seconds(0, 0.5, "-5") <= 0.5


@pytest.mark.asyncio
async def test_a_retry_keeps_the_original_timeout_on_every_attempt(
    monkeypatch, budget, no_sleeping
):
    """
    Retrying a call is not licence to let it wait longer on the socket.

    A slow model is a different problem from a rate-limited one, and the 20 seconds this provider has
    always used still bounds each individual attempt.
    """
    scripted = _ScriptedHttpx(_refused(429), _function_call(RESPOND_TOOL_NAME, {"message": "hi"}))
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    await _provider().choose_agent_action({"mission": {}}, declarations_for_model())

    assert len(scripted.sent) == 2
    for attempt in scripted.sent:
        assert attempt["client_kwargs"].get("timeout") == REASONING_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_the_other_provider_shares_the_same_bounded_behaviour(
    monkeypatch, budget, no_sleeping
):
    """
    The retry belongs to the transport, not to one vendor.

    Groq answers the same 429 the same way: bounded attempts, then a credential-free refusal. Its key
    goes in an Authorization header, which is the thing that must never be echoed anywhere else.
    """
    scripted = _ScriptedHttpx(
        _refused(429), _answer({"choices": [{"message": {"content": "{}"}}]})
    )
    monkeypatch.setattr("httpx.AsyncClient", scripted)
    provider = GroqReasoningProvider(api_key=API_KEY, model="test-model")

    assert await provider.assess_resolution({}) == {}
    assert len(scripted.sent) == 2
    assert len(no_sleeping) == 1

    refusing = _ScriptedHttpx(_refused(503))
    monkeypatch.setattr("httpx.AsyncClient", refusing)
    with pytest.raises(ReasoningUnavailable):
        await provider.assess_resolution({})
    assert len(refusing.sent) == 3
    assert all(s["headers"]["Authorization"] == f"Bearer {API_KEY}" for s in refusing.sent)
    assert all(API_KEY not in json.dumps(s["json"]) for s in refusing.sent)


# --------------------------------------------------------------------------- #
# The same transport inside a real agent turn: a retry must never become a second execution.
# --------------------------------------------------------------------------- #


async def _register(client: AsyncClient, email: str) -> None:
    assert (
        await client.post(
            "/auth/register", json={"name": "Retry Owner", "email": email, "password": PASSWORD}
        )
    ).status_code == 201


async def _shopping_mission(client, install_mock_discovery, sample_products, email) -> str:
    install_mock_discovery(sample_results=sample_products)
    await _register(client, email)
    response = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert response.status_code == 200, response.text
    return response.json()["mission_id"]


async def _paid_and_confirmed(client, mission_id: str) -> None:
    assert (
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": "item_dove"}
        )
    ).status_code == 200
    await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
    created = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
    order_id = created.json()["payment"]["razorpay_order_id"]
    signature = hmac.new(
        get_settings().razorpay_key_secret.encode(), f"{order_id}|pay_RETRY1".encode(), hashlib.sha256
    ).hexdigest()
    assert (
        await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": "pay_RETRY1",
                "razorpay_signature": signature,
            },
        )
    ).status_code == 200
    await client.post(f"/missions/{mission_id}/order", json={})
    assert (await client.post(f"/missions/{mission_id}/order/confirm", json={})).status_code == 200


def _install(monkeypatch, provider) -> None:
    monkeypatch.setattr(missions_module, "get_reasoning_provider", lambda name, key: provider)


def _override(test_session):
    async def override_get_db():
        async with test_session() as session:
            yield session

    return override_get_db


async def _transcript(test_session, mission_id: str) -> list:
    async with test_session() as session:
        return list(
            (
                await session.execute(
                    select(AgentConversationRecord)
                    .where(AgentConversationRecord.mission_id == mission_id)
                    .order_by(AgentConversationRecord.sequence)
                )
            )
            .scalars()
            .all()
        )


class _UnreachableProvider:
    """
    Stands where the model stands and cannot be reached — the exhausted budget, at loop level.

    The intent parse that precedes every turn still answers, because this test is about the chooser
    being unreachable: a stub that failed earlier would degrade for the wrong reason and prove
    nothing about the loop.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def interpret_shopping_intent(self, prompt: str) -> dict:
        return {
            "raw_prompt": prompt,
            "category": "shampoo",
            "budget": 600,
            "currency": "INR",
            "preferences": ["anti-hairfall"],
            "brand": None,
            "size_or_variant": None,
            "missing_info": [],
            "clarification_prompt": None,
            "state": "discovery_completed",
        }

    async def choose_agent_action(self, context, tool_declarations):
        self.calls += 1
        raise ReasoningUnavailable("Gemini", 429, attempts=3)


@pytest.mark.asyncio
async def test_a_reasoning_failure_executes_no_tool_and_walks_the_mission_nowhere(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    monkeypatch,
    session_factory,
):
    """
    Degradation stays non-consequential.

    The provider is unreachable, so the request must not become an action: no tool row, no authority
    decision, no money, no remedy attempted. The mission had already selected a product — the
    protected part of the ladder — so a careless "park it back at clarification" would show up here as
    a regression, and it is not one: the park writes its own note without moving a settled mission.
    """
    _engine, test_session = session_factory
    app.dependency_overrides[get_db] = _override(test_session)

    mission_id = await _shopping_mission(
        async_client, install_mock_discovery, sample_products, "rt.park@core21.test"
    )
    assert (
        await async_client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": "item_dove"}
        )
    ).status_code == 200

    provider = _UnreachableProvider()
    _install(monkeypatch, provider)
    response = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "now refund me"}
    )
    assert response.status_code == 200, response.text
    data = response.json()

    assert provider.calls == 1
    assert data["agent_tools"] == []
    assert data["authority_decision"] is None
    assert refund_api.create_calls == []
    assert data["shopping_intent"]["selected_product"]["product_id"] == "item_dove"
    assert data["mission_status"] == "product_selected"

    rows = await _transcript(test_session, mission_id)
    # The endpoint selection above persists its own mandated system_state_change row; the
    # FAILED TURN itself still contributes exactly the user message and the honest degradation
    # reply — no tool row, no authority row.
    turn_rows = [row for row in rows if row.event_type != "system_state_change"]
    assert [row.event_type for row in turn_rows] == ["user_message", "agent_message"]
    assert "temporarily unavailable" in rows[-1].content.lower()
    assert API_KEY not in json.dumps([row.content for row in rows])


@pytest.mark.asyncio
async def test_a_retried_model_answer_executes_its_tool_exactly_once(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    monkeypatch,
    budget,
    no_sleeping,
    session_factory,
):
    """
    The retry is invisible to the loop, and that is the point of putting it there.

    Attempt one is a 429; attempt two is `select_product`; attempt three ends the turn. The guarded
    operation runs **once** — one tool_call row, one tool_result row, one selection — while the wire
    shows three attempts. Written a level higher, in the loop instead of the transport, this is the
    assertion that would fail.
    """
    _engine, test_session = session_factory
    app.dependency_overrides[get_db] = _override(test_session)

    mission_id = await _shopping_mission(
        async_client, install_mock_discovery, sample_products, "rt.once@core21.test"
    )

    scripted = _ScriptedHttpx(
        _intent_answer(),
        _refused(429),
        _function_call("select_product", {"product_id": "item_dove"}),
        _function_call(RESPOND_TOOL_NAME, {"message": "Selected the Dove bottle for you."}),
    )
    monkeypatch.setattr("httpx.AsyncClient", scripted)
    _install(monkeypatch, _provider())

    data = (
        await async_client.post(
            f"/missions/{mission_id}/agent", json={"user_input": "take the first one"}
        )
    ).json()

    assert len(scripted.sent) == 4, "one intent parse, one refusal, one decision, one closing answer"
    assert len(_decision_requests(scripted)) == 3
    assert [step["tool"] for step in data["agent_tools"]] == ["select_product"]
    assert data["shopping_intent"]["selected_product"]["product_id"] == "item_dove"

    rows = await _transcript(test_session, mission_id)
    calls = [row for row in rows if row.event_type == "tool_call"]
    results = [row for row in rows if row.event_type == "tool_result"]
    assert len(calls) == 1
    assert len(results) == 1
    assert calls[0].tool_name == "select_product"
    assert calls[0].payload["arguments"] == {"product_id": "item_dove"}


@pytest.mark.asyncio
async def test_the_second_model_request_carries_the_first_tools_result(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    monkeypatch,
    budget,
    no_sleeping,
):
    """
    What makes this an agent rather than one model call with extra steps.

    `report_issue` runs, and the database now holds an issue whose id nobody knew in advance. The
    second request sent to the model must contain that id, and the first must not — which is only
    possible if the loop re-read the server's state between the two decisions. Asserting two function
    names would prove a sequence; asserting an id the first tool created proves the hand-off, and it
    is asserted on the wire, in the body the provider actually posted.
    """
    mission_id = await _shopping_mission(
        async_client, install_mock_discovery, sample_products, "rt.chain@core21.test"
    )
    await _paid_and_confirmed(async_client, mission_id)

    scripted = _ScriptedHttpx(
        _intent_answer(),
        _function_call(
            "report_issue",
            {
                "issue_type": "damaged",
                "description": "The bottle arrived cracked",
                "requested_resolution": "refund",
            },
        ),
        _function_call("assess_issue", {}),
        _proposal_answer(),
        _function_call(RESPOND_TOOL_NAME, {"message": "Logged and assessed."}),
    )
    monkeypatch.setattr("httpx.AsyncClient", scripted)
    _install(monkeypatch, _provider())

    turn = await async_client.post(
        f"/missions/{mission_id}/agent",
        json={"user_input": "the bottle arrived cracked, refund me"},
    )
    assert turn.status_code == 200, turn.text

    issues = (await async_client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
    assert len(issues) == 1
    issue_id = issues[0]["issue_id"]

    decisions = _decision_requests(scripted)
    assert len(decisions) == 3, "the loop asked exactly three times and then stopped"
    # Five requests went out, but only three of them are the loop choosing a step: the intent parse
    # that opens every turn, those three decisions, and one call the `assess_issue` operation makes
    # for itself when it asks the model for a resolution proposal. Counting the decisions separately
    # is what keeps this honest — a tool may consult the model without the loop taking another step.
    assert len(scripted.sent) == 5
    bodies = [sent["json"]["contents"][0]["parts"][0]["text"] for sent in decisions]
    assert issue_id not in bodies[0], "the first decision could not have known this id"
    assert issue_id in bodies[1], "the second decision did not see what the first tool produced"
    assert issue_id in bodies[2]

    second = json.loads(bodies[1])
    assert second["issue"]["issue_id"] == issue_id
    assert second["issue"]["issue_type"] == "damaged"
    assert second["this_turn"]["steps_used"] == 1
    assert [run["tool"] for run in second["this_turn"]["tools_already_called"]] == ["report_issue"]
    assert second["this_turn"]["tools_already_called"][0]["success"] is True

    # And the same hand-off one step further: `assess_issue` moved the issue's own recorded status, and
    # the third request carries that moved state — so the loop rebuilds from the database after every
    # tool rather than replaying its first context.
    third = json.loads(bodies[2])
    assert [run["tool"] for run in third["this_turn"]["tools_already_called"]] == [
        "report_issue",
        "assess_issue",
    ]
    assert third["issue"]["issue_id"] == issue_id
    assert second["issue"]["status"] == "reported"
    assert third["issue"]["status"] != "reported", (
        "the second tool changed no state the third decision could see"
    )

    assert [step["tool"] for step in turn.json()["agent_tools"]] == ["report_issue", "assess_issue"]
