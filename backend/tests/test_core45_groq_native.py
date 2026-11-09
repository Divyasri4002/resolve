"""
Task CORE-4.5 — Groq as a second real reasoning provider behind the existing abstraction.

What this file proves, hermetically:

* Selection: `REASONING_PROVIDER=groq` resolves through the SAME factory to a provider carrying the
  provider-specific credential and model — never the Gemini key.
* Wire contract: the step chooser speaks Groq's OpenAI-compatible NATIVE function calling — a
  `tools` array built from the one declared surface, `tool_choice` forced, the credential in the
  Authorization header and nowhere else.
* Parsing: a `tool_calls` answer becomes a strict `AgentStepDecision`; prose, an undeclared name, or
  arguments that are not valid JSON each become a refusal that runs nothing.
* Retry: the bounded transport behaviour is shared, not re-implemented — the Groq call inherits the
  same budget, waits, and credential-free exhaustion as the Gemini call.
* Composition: a full agent turn over a scripted Groq wire runs the same loop, the same registry,
  the same canonical context, and the same transcript as Gemini — and the key never reaches the
  context, the transcript, or any request body.

No test in this file can reach Groq, Gemini, SerpApi, or Razorpay.
"""
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents import reasoning_provider as provider_module
from app.agents.reasoning_provider import (
    RESPOND_TOOL_NAME,
    GeminiReasoningProvider,
    GroqReasoningProvider,
    ReasoningUnavailable,
    get_reasoning_provider,
)
from app.agents.tools import declarations_for_model, tool_names
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.db_models import AgentConversationRecord
from app.routers import missions as missions_module

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
GROQ_KEY = "gsk_groq-key-DO-NOT-LEAK"
PASSWORD = "SecurePassword123!"
SHIRT_PROMPT = "Find me white shirts under ₹1000."


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
    settings = get_settings()
    monkeypatch.setattr(settings, "reasoning_max_retries", 2, raising=False)
    monkeypatch.setattr(settings, "reasoning_retry_base_seconds", 0.5, raising=False)
    return {"retries": 2, "base": 0.5}


@pytest.fixture
def no_sleeping(monkeypatch):
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(provider_module, "_pause", record)
    return waits


class _ScriptedGroq:
    """The provider's HTTP client, serving a fixed sequence of answers; the last one repeats."""

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
                    }
                )
                index = len(outer.sent) - 1
                return outer._responses[min(index, len(outer._responses) - 1)]

        return _Client()


def _refused(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"error": {"message": "nope"}},
        request=httpx.Request("POST", GroqReasoningProvider.BASE_URL),
    )


def _text_answer(content: str) -> httpx.Response:
    """A plain chat-completion answer: intent parses, decisions, and summaries all look like this."""
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        request=httpx.Request("POST", GroqReasoningProvider.BASE_URL),
    )


def _tool_call_answer(name: str, arguments: str) -> httpx.Response:
    """A native function-calling answer: the chosen function and its JSON-string arguments."""
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ],
                    }
                }
            ]
        },
        request=httpx.Request("POST", GroqReasoningProvider.BASE_URL),
    )


def _intent_json(state: str = "discovery_completed") -> str:
    return json.dumps(
        {
            "category": "shirt",
            "budget": 1000,
            "currency": "INR",
            "preferences": ["white"],
            "brand": None,
            "size_or_variant": None,
            "missing_info": [],
            "clarification_prompt": None,
            "state": state,
        }
    )


def _decide_search_json() -> str:
    return json.dumps(
        {
            "action": "search",
            "query": "white formal shirts for men",
            "budget": 1000,
            "currency": "INR",
            "preferences": ["white"],
            "brand": None,
            "size_or_variant": None,
            "clarification_prompt": None,
        }
    )


def _chooser_requests(scripted) -> list[dict]:
    """The step chooser's requests, which are the only ones that carry `tools`."""
    return [sent for sent in scripted.sent if "tools" in sent["json"]]


def _provider() -> GroqReasoningProvider:
    return GroqReasoningProvider(api_key=GROQ_KEY, model="test-groq-model")


def _install(monkeypatch, provider) -> None:
    monkeypatch.setattr(missions_module, "get_reasoning_provider", lambda name, key: provider)


async def _transcript(test_session, mission_id: str) -> list[AgentConversationRecord]:
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


# --------------------------------------------------------------------------- #
# 1. Selection: the factory hands Groq its own credential and model.
# --------------------------------------------------------------------------- #


def test_selecting_groq_reads_the_provider_specific_credential_and_model(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "reasoning_groq_api_key", GROQ_KEY, raising=False)
    monkeypatch.setattr(settings, "reasoning_groq_model", "test-groq-model", raising=False)

    provider = get_reasoning_provider("groq", "gemini-key-DO-NOT-USE")
    assert isinstance(provider, GroqReasoningProvider)
    assert provider.api_key == GROQ_KEY, "the provider-specific key wins over the shared one"
    assert provider.model == "test-groq-model"

    # The shared REASONING_API_KEY still works for deployments that predate the specific name.
    monkeypatch.setattr(settings, "reasoning_groq_api_key", "", raising=False)
    monkeypatch.setattr(settings, "reasoning_groq_model", "", raising=False)
    fallback = get_reasoning_provider("groq", GROQ_KEY)
    assert isinstance(fallback, GroqReasoningProvider)
    assert fallback.api_key == GROQ_KEY
    assert fallback.model == GroqReasoningProvider.DEFAULT_MODEL

    # Gemini selection is untouched by any of this.
    monkeypatch.setattr(settings, "reasoning_model", "gemini-test", raising=False)
    assert isinstance(get_reasoning_provider("gemini", GROQ_KEY), GeminiReasoningProvider)


def test_a_groq_provider_without_a_credential_refuses_to_construct():
    with pytest.raises(ValueError, match="Groq API key is empty"):
        GroqReasoningProvider(api_key="")


# --------------------------------------------------------------------------- #
# 2. The wire: native function calling over the OpenAI-compatible surface.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_step_chooser_speaks_native_function_calling_on_the_wire(
    monkeypatch, budget, no_sleeping
):
    scripted = _ScriptedGroq(
        _tool_call_answer("search_products", json.dumps({"query": "white shirts under 1000"}))
    )
    monkeypatch.setattr("httpx.AsyncClient", scripted)

    decision = await _provider().choose_agent_action(
        {"mission": {"mission_id": "m1"}}, declarations_for_model()
    )

    assert decision.action == "tool" and decision.tool_name == "search_products"
    assert decision.arguments == {"query": "white shirts under 1000"}
    assert len(scripted.sent) == 1

    sent = scripted.sent[0]
    assert sent["url"] == GroqReasoningProvider.BASE_URL
    assert sent["headers"]["Authorization"] == f"Bearer {GROQ_KEY}"
    body = sent["json"]
    assert body["model"] == "test-groq-model"
    assert body["tool_choice"] == "required", "a step must be one of the declared functions"
    assert GROQ_KEY not in sent["url"]
    assert GROQ_KEY not in json.dumps(body)

    # The declared surface went out intact: every registry tool plus the one non-tool step.
    functions = {tool["function"]["name"]: tool for tool in body["tools"]}
    assert {name for name in functions if name != RESPOND_TOOL_NAME} == set(tool_names())
    assert functions[RESPOND_TOOL_NAME]["type"] == "function"
    for tool in body["tools"]:
        assert tool["type"] == "function"
        assert tool["function"]["description"]
        parameters = tool["function"].get("parameters") or {}
        if parameters:
            assert parameters["type"] == "object"

    # The context reached the model as the user message — the same canonical dict, serialised.
    assert '"mission_id"' in body["messages"][1]["content"]
    assert body["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_a_groq_429_is_retried_inside_the_call_and_exhaustion_is_credential_free(
    monkeypatch, budget, no_sleeping
):
    scripted = _ScriptedGroq(
        _refused(429), _tool_call_answer(RESPOND_TOOL_NAME, json.dumps({"message": "hi"}))
    )
    monkeypatch.setattr("httpx.AsyncClient", scripted)
    decision = await _provider().choose_agent_action({}, declarations_for_model())
    assert decision.action == "respond" and decision.message == "hi"
    assert len(scripted.sent) == 2 and len(no_sleeping) == 1

    refusing = _ScriptedGroq(_refused(503))
    monkeypatch.setattr("httpx.AsyncClient", refusing)
    with pytest.raises(ReasoningUnavailable) as raised:
        await _provider().choose_agent_action({}, declarations_for_model())
    assert len(refusing.sent) == budget["retries"] + 1
    assert GROQ_KEY not in str(raised.value)
    assert all(s["headers"]["Authorization"] == f"Bearer {GROQ_KEY}" for s in refusing.sent)
    assert all(GROQ_KEY not in s["url"] and GROQ_KEY not in json.dumps(s["json"]) for s in refusing.sent)


# --------------------------------------------------------------------------- #
# 3. Composition: the same loop, registry, context and transcript, on a Groq wire.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_full_shopping_turn_runs_over_a_scripted_groq_wire(
    async_client, install_mock_discovery, sample_products, session_factory, monkeypatch, budget,
    no_sleeping,
):
    """
    One mission, one discovery, one conversational turn — every model answer on the Groq wire shape.

    The creation flow's two calls and the discovery summary are plain-text answers; the turn's step
    chooser answers with a native respond_to_user call. What comes out the other side is the same
    transcript, the same state, and no credential anywhere.
    """
    _engine, test_session = session_factory

    async def override_get_db():
        async with test_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    scripted = _ScriptedGroq(
        _text_answer(_intent_json(state="ready_for_discovery")),
        _text_answer(_decide_search_json()),
        # The scripted model answers quote the canonical feed (grounded responses only carry
        # verified product facts), not the pre-grounding shirt fiction.
        _text_answer("Two shampoos came back: L'Oreal Paris Total Repair 5 Shampoo at ₹549 "
                     "from Amazon.in and Dove Hair Fall Rescue Shampoo at ₹399 from Nykaa."),
        _text_answer(_intent_json(state="discovery_completed")),
        _tool_call_answer(
            RESPOND_TOOL_NAME,
            json.dumps(
                {
                    "message": "For office wear the L'Oreal Paris Total Repair 5 Shampoo at "
                               "₹549 from Amazon.in is the strongest pick of the two."
                }
            ),
        ),
    )
    monkeypatch.setattr("httpx.AsyncClient", scripted)
    install_mock_discovery(sample_results=sample_products)
    _install(monkeypatch, _provider())

    assert (
        await async_client.post(
            "/auth/register",
            json={"name": "Groq Owner", "email": "groq@core45.test", "password": PASSWORD},
        )
    ).status_code == 201

    created = await async_client.post("/missions/shopping", json={"prompt": SHIRT_PROMPT})
    assert created.status_code == 200, created.text
    mission_id = created.json()["mission_id"]
    intent = created.json().get("shopping_intent") or {}
    assert intent.get("category") == "shirt"
    assert str(intent.get("budget")) == "1000"
    assert "white" in (intent.get("preferences") or [])
    assert intent.get("discovered_products")

    turn = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "Which one is best for office use?"}
    )
    assert turn.status_code == 200, turn.text
    data = turn.json()
    assert data["agent_tools"] == [], "a respond decision runs no tool"
    assert "office" in data["agent_message"]

    # The chooser's one request carried the canonical context and the forced tool surface.
    chooser = _chooser_requests(scripted)
    assert len(chooser) == 1
    context_body = json.loads(chooser[0]["json"]["messages"][1]["content"])
    assert "mission" in context_body and "shopping" in context_body
    assert chooser[0]["json"]["tool_choice"] == "required"

    rows = await _transcript(test_session, mission_id)
    assert [row.event_type for row in rows] == ["user_message", "agent_message"]
    stored = json.dumps([row.content for row in rows])
    assert GROQ_KEY not in stored


@pytest.mark.asyncio
async def test_a_groq_outage_parks_the_turn_without_a_500(
    async_client, install_mock_discovery, sample_products, monkeypatch, budget, no_sleeping
):
    """The degradation contract is provider-agnostic: exhaustion degrades honestly, in a 200."""
    scripted = _ScriptedGroq(_refused(503))
    monkeypatch.setattr("httpx.AsyncClient", scripted)
    install_mock_discovery(sample_results=sample_products)
    _install(monkeypatch, _provider())

    assert (
        await async_client.post(
            "/auth/register",
            json={"name": "Groq Owner", "email": "groq.down@core45.test", "password": PASSWORD},
        )
    ).status_code == 201
    created = await async_client.post("/missions/shopping", json={"prompt": SHIRT_PROMPT})
    assert created.status_code == 200, created.text
    mission_id = created.json()["mission_id"]
    assert created.json().get("shopping_intent", {}).get("state") == "needs_clarification", (
        "creation itself degrades when the parse cannot be reached"
    )

    turn = await async_client.post(
        f"/missions/{mission_id}/agent", json={"user_input": "white shirts please"}
    )
    assert turn.status_code == 200, turn.text
    assert "temporarily unavailable" in turn.json()["agent_message"].lower()
    assert turn.json()["mission_status"] == "needs_clarification"
    assert turn.json()["agent_tools"] == []
