"""
Task CORE-2, Phase 18 — the security regressions of the continuous agent loop.

Each test here is about one thing the agent layer must not be able to do, and most of them are proven
structurally as well as behaviourally: a guard that only exists in a test can be broken by the next
feature, while a guard that is visible in the shape of the code — no HTTP client in the tool layer, no
`evaluate()` call in the agent layer, no undeclared argument accepted — fails to hold only if someone
changes it on purpose.

Hermetic like the rest of the suite: `conftest.py` pins reasoning and discovery to offline mocks and
leaves every credential value empty, so where a test needs a value that *would* be a secret it
installs one itself and checks that it never reaches the model, the transcript, or the response.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.reasoning_provider import (
    GeminiReasoningProvider,
    GroqReasoningProvider,
    MockReasoningProvider,
    RESPOND_TOOL_NAME,
)
from app.agents.tools import (
    AgentToolError,
    InvalidToolArgumentsError,
    UnknownToolError,
    declarations_for_model,
    execute_tool,
    tool_definitions,
    tool_names,
    validate_tool_call,
)
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.agent import AgentStepDecision
from app.models.db_models import AgentConversationRecord, MissionRecord
from app.routers import missions as missions_module

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
SHAMPOO_PROMPT = "I want to order shampoo under ₹600 for anti-hairfall."
PASSWORD = "SecurePassword123!"

AGENTS_DIR = Path(__file__).resolve().parent.parent / "app" / "agents"

# The agent-facing modules. `reasoning_provider.py` is deliberately not in this list: it is the one
# file in the system allowed to speak to a model over HTTPS, and Phase 18 is about everything that
# must NOT be able to.
GUARDED_MODULES = (
    "tools.py",
    "loop.py",
    "context.py",
    "conversation.py",
    "lifecycle.py",
    "mock_step_policy.py",
)


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
    """An engine the app and the test share, so a test can read what was actually written."""
    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    test_session = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_engine, test_session
    await test_engine.dispose()


@pytest.fixture
def guarded_env(monkeypatch):
    """
    Configure the deployment the way a real one would be, with values that are unmistakably secrets.

    Nothing here is a credential of any real system: they are unmistakable *because* a test can look
    for the exact string afterwards.
    """
    settings = get_settings()
    values = {
        "razorpay_key_id": "rzp_TESTKEYID_LOOKS_LIKE_A_SECRET",
        "razorpay_key_secret": "razorpay-secret-DO-NOT-LEAK",
        "razorpay_webhook_secret": "razorpay-webhook-DO-NOT-LEAK",
        "reasoning_api_key": "gemini-key-DO-NOT-LEAK",
        "reasoning_groq_api_key": "groq-key-DO-NOT-LEAK",
        "serpapi_api_key": "serpapi-key-DO-NOT-LEAK",
        "linkage_integration_secret": "linkage-hmac-DO-NOT-LEAK",
        "auth_secret": "session-signing-DO-NOT-LEAK",
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    return values


@pytest.fixture
def mock_razorpay(monkeypatch):
    counter = {"n": 0}

    def fake_create_test_order(amount_paise, currency, receipt, notes):
        counter["n"] += 1
        return {
            "id": f"order_TEST{counter['n']:04d}",
            "amount": amount_paise,
            "currency": currency,
            "status": "created",
            "receipt": receipt,
        }

    monkeypatch.setattr(missions_module, "create_test_order", fake_create_test_order)
    return counter


class RefundApi:
    def __init__(self):
        self.refunds = {}
        self.create_calls = []

    def is_test_mode(self):
        return True

    def create(self, payment_id, amount_paise, resolve_refund_id):
        self.create_calls.append((payment_id, amount_paise, resolve_refund_id))
        refund_id = f"rfnd_TEST{len(self.refunds) + 1}"
        self.refunds[refund_id] = {
            "id": refund_id,
            "status": "processed",
            "payment_id": payment_id,
            "amount": amount_paise,
            "notes": {"resolve_refund_id": resolve_refund_id},
        }
        return dict(self.refunds[refund_id])

    def fetch(self, payment_id, razorpay_refund_id):
        stored = self.refunds.get(razorpay_refund_id, {"status": "processed"})
        return {**stored, "id": razorpay_refund_id}

    def list_for_payment(self, payment_id):
        return [r for r in self.refunds.values() if r.get("payment_id") == payment_id]


@pytest.fixture
def refund_api(monkeypatch):
    api = RefundApi()
    monkeypatch.setattr(missions_module, "is_test_mode", api.is_test_mode)
    monkeypatch.setattr(missions_module, "create_payment_refund", api.create)
    monkeypatch.setattr(missions_module, "fetch_refund", api.fetch)
    monkeypatch.setattr(missions_module, "list_refunds_for_payment", api.list_for_payment)
    return api


def _signature(order_id: str, payment_id: str) -> str:
    secret = get_settings().razorpay_key_secret.encode()
    return hmac.new(secret, f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


async def _register(client: AsyncClient, email: str, name: str = "Core Two") -> None:
    assert (
        await client.post(
            "/auth/register", json={"name": name, "email": email, "password": PASSWORD}
        )
    ).status_code == 201


async def _shopping_mission(client, install_mock_discovery, sample_products, email) -> str:
    install_mock_discovery(sample_results=sample_products)
    await _register(client, email)
    response = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert response.status_code == 200, response.text
    return response.json()["mission_id"]


async def _agent(client, mission_id: str, message: str) -> dict:
    response = await client.post(f"/missions/{mission_id}/agent", json={"user_input": message})
    assert response.status_code == 200, response.text
    return response.json()


async def _order_confirmed_mission(client, install_mock_discovery, sample_products, email) -> str:
    mission_id = await _shopping_mission(client, install_mock_discovery, sample_products, email)
    select = await client.post(
        f"/missions/{mission_id}/shopping/select-product", json={"product_id": "item_dove"}
    )
    assert select.status_code == 200, select.text
    await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
    created = await client.post(
        f"/missions/{mission_id}/shopping/payment/create-order", json={}
    )
    razorpay_order_id = created.json()["payment"]["razorpay_order_id"]
    payment_id = "pay_SECTEST1"
    verified = await client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _signature(razorpay_order_id, payment_id),
        },
    )
    assert verified.status_code == 200, verified.text
    await client.post(f"/missions/{mission_id}/order", json={})
    confirmed = await client.post(f"/missions/{mission_id}/order/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    return mission_id


class ContextCapturingProvider(MockReasoningProvider):
    """Stands where the model stands and keeps every context it was shown."""

    def __init__(self, steps: list[AgentStepDecision] | None = None):
        self.contexts: list[dict] = []
        self.declarations: list[list[str]] = []
        self.steps = list(steps or [])

    async def choose_agent_action(self, context, tool_declarations):
        self.contexts.append(json.loads(json.dumps(context, default=str)))
        self.declarations.append([decl["name"] for decl in tool_declarations])
        if self.steps:
            return self.steps.pop(0)
        return await super().choose_agent_action(context, tool_declarations)


def _install(monkeypatch, provider) -> None:
    monkeypatch.setattr(missions_module, "get_reasoning_provider", lambda name, key: provider)


def _agent_layer_sources() -> list[tuple[str, str]]:
    return [
        (name, (AGENTS_DIR / name).read_text(encoding="utf-8")) for name in GUARDED_MODULES
    ]


def _code_of(source: str) -> str:
    """
    The module's executable text, with docstrings and comment lines removed.

    A refusal documented as "there is no confirm_payment tool" must not fail a scan for
    `confirm_payment`. The prose is the point of the module; the scan is about what runs.
    """
    tree = ast.parse(source)
    dropped: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            dropped.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept = [
        line
        for index, line in enumerate(source.splitlines(), start=1)
        if index not in dropped and not line.strip().startswith("#")
    ]
    return "\n".join(kept)


# --------------------------------------------------------------------------- #
# 1-3. Ownership: a mission, its conversation, and its context belong to one caller.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_agent_surface_rejects_a_foreign_mission(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """
    The agent added four new surfaces. All four use the one existing ownership guard.

    A foreign mission id reads as "not found" rather than "forbidden": the difference would tell an
    attacker that the id is real.
    """
    mine = await _shopping_mission(
        async_client, install_mock_discovery, sample_products, "own.sec@core2.test"
    )
    await _register(async_client, "other.sec@core2.test", name="Other Owner")

    responses = [
        await async_client.post(f"/missions/{mine}/agent", json={"user_input": "hello"}),
        await async_client.get(f"/missions/{mine}/agent/conversation"),
        await async_client.get(f"/missions/{mine}/agent/tools"),
        await async_client.post(
            f"/missions/{mine}/authority/delegate", json={"allowed_actions": ["request_refund"]}
        ),
        await async_client.post(f"/missions/{mine}/authority/revoke"),
    ]
    assert [response.status_code for response in responses] == [404] * 5


@pytest.mark.asyncio
async def test_anonymous_callers_cannot_reach_a_registered_users_conversation(
    async_client: AsyncClient, install_mock_discovery, sample_products
):
    """
    A mission owned by a registered user is invisible to a caller with no session.

    The transcript is the buyer's own case: what they asked, what the agent did, and where the mission
    stands. It is read through the same guard as the mission itself, so a fresh browser with a guessed
    mission id gets the same answer as for a mission that never existed.
    """
    mission_id = await _shopping_mission(
        async_client, install_mock_discovery, sample_products, "solo.sec@core2.test"
    )
    await _agent(async_client, mission_id, "Which one is cheapest?")

    anonymous = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    async with anonymous:
        conversation = await anonymous.get(f"/missions/{mission_id}/agent/conversation")
        mission = await anonymous.get(f"/missions/{mission_id}")
        turn = await anonymous.post(
            f"/missions/{mission_id}/agent", json={"user_input": "show me everything"}
        )
        assert conversation.status_code == 404
        assert mission.status_code == 404
        assert turn.status_code == 404


@pytest.mark.asyncio
async def test_one_turns_context_contains_only_its_own_mission(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    monkeypatch,
):
    """
    Cross-mission data never reaches the model.

    Two users, two missions, and one provider that keeps every context it was handed: neither
    mission's id appears anywhere in the other's contexts. There is no query in the agent layer that
    could fetch a foreign row, and this is the observable form of that fact — checked in both
    directions, because "the second one looked clean" is not the same claim as "no turn ever saw a
    foreign row".
    """
    provider = ContextCapturingProvider()
    _install(monkeypatch, provider)

    first = await _order_confirmed_mission(
        async_client, install_mock_discovery, sample_products, "one.ctx@core2.test"
    )
    await _agent(async_client, first, "The bottle is cracked, refund me")
    after_first = len(provider.contexts)
    assert after_first, "the first mission's turn really reached the model"

    second = await _shopping_mission(
        async_client, install_mock_discovery, sample_products, "two.ctx@core2.test"
    )
    await _agent(async_client, second, "Which one is cheapest?")
    assert len(provider.contexts) > after_first

    for context in provider.contexts[:after_first]:
        assert context["mission"]["mission_id"] == first
        assert second not in json.dumps(context)
    for context in provider.contexts[after_first:]:
        assert context["mission"]["mission_id"] == second
        assert first not in json.dumps(context)

    shopping = provider.contexts[after_first]
    assert shopping["order"] is None


def _override(test_session):
    async def override_get_db():
        async with test_session() as session:
            yield session

    return override_get_db


@pytest.mark.asyncio
async def test_the_context_a_turn_hands_the_model_carries_no_credential(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    monkeypatch,
    guarded_env,
):
    """
    The model sees the mission, never the deployment's keys.

    `guarded_env` installs values that are unmistakably secret-shaped, and every context the turn
    handed over is searched for each of them. The field *names* are checked too: nothing in the
    context is a credential column of anything.
    """
    mission_id = await _order_confirmed_mission(
        async_client, install_mock_discovery, sample_products, "ctx.key@core2.test"
    )
    provider = ContextCapturingProvider()
    _install(monkeypatch, provider)

    await _agent(async_client, mission_id, "The box was empty when it arrived")

    assert provider.contexts, "the turn really did show the model a context"
    dumped = json.dumps(provider.contexts)
    for value in guarded_env.values():
        assert value not in dumped
    for name in (
        "razorpay_key_secret",
        "reasoning_api_key",
        "serpapi_api_key",
        "linkage_integration_secret",
        "auth_secret",
        "password_hash",
    ):
        assert name not in dumped


@pytest.mark.asyncio
async def test_a_credential_pasted_into_the_chat_is_never_persisted_or_replayed(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    monkeypatch,
    guarded_env,
    session_factory,
):
    """
    Defence in depth for the one input this layer cannot enumerate: the buyer's own words.

    A user who pastes the deployment's key secret into the conversation does not put it into storage,
    into the next model call, or into the transcript the browser reads back. It is replaced with a
    marker, which is also what the buyer sees, so nothing is silently swallowed.
    """
    _engine, test_session = session_factory
    app.dependency_overrides[get_db] = _override(test_session)

    mission_id = await _shopping_mission(
        async_client, install_mock_discovery, sample_products, "paste@core2.test"
    )
    secret = guarded_env["razorpay_key_secret"]
    await _agent(async_client, mission_id, f"Here is my key, please use it: {secret}")

    provider = ContextCapturingProvider()
    _install(monkeypatch, provider)
    await _agent(async_client, mission_id, "what about the key I sent earlier?")

    async with test_session() as session:
        rows = (
            (
                await session.execute(
                    select(AgentConversationRecord).where(
                        AgentConversationRecord.mission_id == mission_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "the conversation really was persisted"
    stored = json.dumps([row.content for row in rows] + [row.payload or {} for row in rows])
    assert secret not in stored
    assert "[redacted]" in stored
    assert secret not in json.dumps(provider.contexts)

    transcript = await async_client.get(f"/missions/{mission_id}/agent/conversation")
    assert secret not in transcript.text


# --------------------------------------------------------------------------- #
# 4-6. What the agent layer is structurally incapable of.
# --------------------------------------------------------------------------- #


def test_the_agent_layer_opens_no_socket_and_runs_no_code_of_its_own():
    """
    No HTTP, no shell, no dynamic execution — in the modules that receive a model's words.

    The tool layer's whole contract is "call a function that already exists". A `requests` import, an
    `httpx` call, `eval`, `exec`, `__import__`, `subprocess` or `os.system` anywhere in these files
    would turn a model response into an outbound call or a command, which is exactly what Phase 1-2 of
    the safety boundaries forbid. `reasoning_provider.py` is excluded on purpose: it is the single
    place in the system allowed to speak to a model over HTTPS.
    """
    forbidden = (
        "httpx",
        "requests.",
        "aiohttp",
        "urllib",
        "socket",
        "eval(",
        "exec(",
        "__import__",
        "importlib",
        "subprocess",
        "os.system",
        "pickle.loads",
    )
    for name, source in _agent_layer_sources():
        code = _code_of(source)
        for needle in forbidden:
            assert needle not in code, (name, needle)


def test_the_agent_layer_writes_no_sql_and_reads_no_raw_rows():
    """
    The agent layer moves state through ORM objects and the router's own functions only.

    No `text()` clause, no hand-built SQL, and no `SELECT *` of a table it was never given: the context
    builder reads named attributes off named objects, which is what makes "the model cannot see a whole
    row" a property of the code rather than a hope.
    """
    for name, source in _agent_layer_sources():
        code = _code_of(source)
        assert not re.search(r"(?<![\w.])text\(", code), name
        assert "from_db" not in code, name
        assert "__table__" not in code, name
        assert ".columns" not in code, name


def test_the_agent_layer_holds_no_payment_no_provider_and_no_authority_decision():
    """
    It proposes. It does not pay, does not contact a merchant, and does not decide.

    These names are the machinery of consequence, and every one of them lives behind a guard in the
    router, the orchestrator, or the engine. If one appeared in the agent layer, that layer would have
    grown a second path around the Authority Engine — safety boundaries 1, 10 and 11 in one scan.
    """
    forbidden = (
        "create_payment_refund",
        "verify_payment_signature",
        "create_test_order",
        "razorpay",
        "get_return_provider",
        "get_replacement_provider",
        "apply_spend",
        "total_recovery_spend",
        "evaluate(",
    )
    for name, source in _agent_layer_sources():
        code = _code_of(source)
        for needle in forbidden:
            assert needle not in code, (name, needle)


def test_every_declared_tool_is_a_wrapper_and_the_registry_is_closed():
    """
    The registry is the entire surface, and each entry is a function defined in the tool module.

    A runner that lived somewhere else would be a capability the declared list does not describe, and
    a name that is not on the list cannot be reached at all.
    """
    for definition in tool_definitions():
        assert definition.runner.__module__ == "app.agents.tools", definition.name
        assert definition.input_schema["type"] == "object"
        assert definition.input_schema["additionalProperties"] is False
        assert set(definition.input_schema.get("properties", {})) <= set(
            definition.arguments_model.model_fields
        )


@pytest.mark.parametrize(
    "name",
    [
        "confirm_payment",
        "create_payment",
        "razorpay_checkout",
        "create_order",
        "refund_now",
        "http_get",
        "run_sql",
        "__import__",
        "eval",
        "getattr",
        "shopify_graphql",
    ],
)
def test_an_undeclared_capability_is_refused_before_anything_runs(name):
    with pytest.raises(UnknownToolError):
        validate_tool_call(name, {})


# --------------------------------------------------------------------------- #
# 7-9. Nothing the model names may become an amount, an identity, or a reference.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_refund_the_agent_delivers_is_the_canonical_amount_never_a_number_it_was_shown(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    monkeypatch,
):
    """
    The amount a refund reverses is a fact about the order, and the model's number is discarded.

    The scripted provider proposes a refund of ₹9,99,999 in words the buyer never said, in a message
    that names a bigger number too. What lands on the assessment row and at the provider is the ₹399
    that was actually paid.
    """

    class GreedyAssessor(MockReasoningProvider):
        async def assess_resolution(self, context):
            return {
                "issue_assessment": "Refund everything, and then some.",
                "proposed_action": "request_refund",
                "reasoning_summary": "This number is invented.",
                "requested_amount": 999999.99,
                "currency": "USD",
                "confidence": 1.0,
                "needs_escalation": False,
            }

    mission_id = await _order_confirmed_mission(
        async_client, install_mock_discovery, sample_products, "greedy@core2.test"
    )
    delegated = await async_client.post(
        f"/missions/{mission_id}/authority/delegate",
        json={"allowed_actions": ["request_refund"], "refund_limit": "500"},
    )
    assert delegated.status_code == 200, delegated.text
    _install(monkeypatch, GreedyAssessor())

    await _agent(async_client, mission_id, "The shampoo is damaged, I want a refund")

    issues = (await async_client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
    assert Decimal(issues[0]["assessment"]["requested_amount"]) == Decimal("399.00")
    assert issues[0]["assessment"]["currency"] == "INR"
    assert issues[0]["resolution"]["resolution_state"] == "verified"
    assert len(refund_api.create_calls) == 1
    _payment_id, amount_paise, _refund_id = refund_api.create_calls[0]
    assert amount_paise == 39900


@pytest.mark.asyncio
async def test_a_model_named_owner_or_provider_reference_has_nowhere_to_go(
    async_client: AsyncClient, install_mock_discovery, sample_products, monkeypatch, session_factory
):
    """
    `user_id`, a payment id and a provider reference are not arguments to anything.

    Each call below is refused for carrying an undeclared field, and every row that does get written
    belongs to the mission's real owner — the identity in storage comes from the session and the
    mission, never from a tool call.
    """
    _engine, test_session = session_factory
    app.dependency_overrides[get_db] = _override(test_session)

    mission_id = await _shopping_mission(
        async_client, install_mock_discovery, sample_products, "id.sec@core2.test"
    )

    _install(
        monkeypatch,
        ContextCapturingProvider(
            steps=[
                AgentStepDecision.tool_call(
                    "select_product",
                    {"product_id": "item_dove", "user_id": "attacker-id"},
                ),
                AgentStepDecision.tool_call(
                    "resolve_issue",
                    {"issue_id": "issue_fake", "provider_reference": "RMA-STOLEN"},
                ),
                AgentStepDecision.respond("Nothing here is mine to choose."),
            ]
        ),
    )

    data = await _agent(async_client, mission_id, "do these things")

    assert data["agent_tools"][0]["error_code"] == "refused"
    assert data["shopping_intent"].get("selected_product") is None
    async with test_session() as session:
        rows = (
            (
                await session.execute(
                    select(AgentConversationRecord).where(
                        AgentConversationRecord.mission_id == mission_id
                    )
                )
            )
            .scalars()
            .all()
        )
        owner = (await session.get(MissionRecord, mission_id)).user_id
    assert rows, "the turn wrote conversation rows"
    assert {row.user_id for row in rows} == {owner}
    stored = json.dumps([row.content for row in rows] + [row.payload or {} for row in rows])
    assert "attacker-id" not in stored
    assert "RMA-STOLEN" not in stored


@pytest.mark.asyncio
async def test_a_refused_remedy_is_recorded_and_still_reaches_no_provider(
    async_client: AsyncClient,
    install_mock_discovery,
    sample_products,
    mock_razorpay,
    refund_api,
    monkeypatch,
):
    """
    'The model asked for it' is not a decision. Without a delegation the answer is a refusal, and a
    refusal is zero calls at the money rail — plus an audit row that says so.
    """
    mission_id = await _order_confirmed_mission(
        async_client, install_mock_discovery, sample_products, "nodel@core2.test"
    )
    await _agent(async_client, mission_id, "The bottle is cracked, refund me now")

    assert refund_api.create_calls == []
    issues = (await async_client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
    assert issues[0]["resolution"]["resolution_state"] == "denied"
    assert issues[0]["resolution"]["provider_attempted"] is False


# --------------------------------------------------------------------------- #
# 10-12. The provider protocols that feed the loop.
# --------------------------------------------------------------------------- #


class _CapturingHttpx:
    """Stands in for the HTTP client the reasoning providers use, and keeps what was sent."""

    def __init__(self, response_body):
        self.response_body = response_body
        self.sent: list[dict] = []

    def __call__(self, *args, **kwargs):
        outer = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None, params=None):
                outer.sent.append({"url": url, "headers": headers or {}, "json": json or {}})

                class _Response:
                    status_code = 200

                    def raise_for_status(self):
                        return None

                    def json(self):
                        return outer.response_body

                return _Response()

        return _Client()


def _gemini_function_call_body(name: str, arguments: dict) -> dict:
    return {
        "candidates": [
            {"content": {"parts": [{"functionCall": {"name": name, "args": arguments}}]}}
        ]
    }


@pytest.mark.asyncio
async def test_gemini_is_handed_the_declared_functions_and_no_secret_in_the_url(monkeypatch, guarded_env):
    """
    Native function calling: the model chooses among `functionDeclarations` RESOLVE wrote for it.

    The declared surface arrives as named functions with their schemas and a pinned
    `allowedFunctionNames`; the key travels only in the header Google specifies, never in a URL that
    proxies and logs would keep.
    """
    provider = GeminiReasoningProvider(api_key=guarded_env["reasoning_api_key"], model="gemini-test")
    capture = _CapturingHttpx(
        _gemini_function_call_body("select_product", {"product_id": "item_dove"})
    )
    monkeypatch.setattr("httpx.AsyncClient", capture)

    decision = await provider.choose_agent_action(
        {"mission": {"mission_id": "mission_1"}}, declarations_for_model()
    )

    assert decision.action == "tool" and decision.tool_name == "select_product"
    body = capture.sent[0]["json"]
    names = [declared["name"] for declared in body["tools"][0]["functionDeclarations"]]
    assert names == [*tool_names(), RESPOND_TOOL_NAME]
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    assert body["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] == names
    assert len(names) == len(tool_names()) + 1
    declared = {d["name"]: d for d in body["tools"][0]["functionDeclarations"]}
    assert declared["select_product"]["parameters"]["required"] == ["product_id"]
    # A tool that declares no arguments is sent with no `parameters` at all.
    assert "parameters" not in declared["prepare_checkout"]
    # …and every schema that does go out uses only fields Gemini's `Schema` accepts. This was proved
    # against the live API, where one `additionalProperties` key made the entire request a 400: a
    # declaration the provider rejects is an agent that silently stops working.
    gemini_schema_fields = {
        "type",
        "properties",
        "required",
        "enum",
        "description",
        "nullable",
        "items",
        "format",
    }
    for entry in declared.values():
        parameters = entry.get("parameters")
        if parameters is None:
            continue
        assert set(parameters) <= gemini_schema_fields, entry["name"]
        for field in parameters["properties"].values():
            assert set(field) <= gemini_schema_fields, entry["name"]
    assert guarded_env["reasoning_api_key"] not in capture.sent[0]["url"]
    assert capture.sent[0]["headers"]["x-goog-api-key"] == guarded_env["reasoning_api_key"]
    for value in guarded_env.values():
        if value == guarded_env["reasoning_api_key"]:
            continue
        assert value not in json.dumps(body)


@pytest.mark.asyncio
async def test_gemini_prose_or_an_undeclared_function_executes_nothing(monkeypatch, guarded_env):
    """A reply that is not one of the declared calls is not a step; it is a refusal."""
    provider = GeminiReasoningProvider(api_key=guarded_env["reasoning_api_key"], model="gemini-test")

    monkeypatch.setattr(
        "httpx.AsyncClient",
        _CapturingHttpx({"candidates": [{"content": {"parts": [{"text": "I paid it already."}]}}]}),
    )
    prose = await provider.choose_agent_action({}, declarations_for_model())
    assert prose.action == "respond"
    assert "declared capabilities" in prose.message

    monkeypatch.setattr(
        "httpx.AsyncClient",
        _CapturingHttpx(_gemini_function_call_body("confirm_payment", {"amount": 100})),
    )
    undeclared = await provider.choose_agent_action({}, declarations_for_model())
    assert undeclared.action == "respond"
    assert "not a capability" in undeclared.message


@pytest.mark.asyncio
async def test_the_native_tool_call_contract_rejects_a_malformed_step(guarded_env):
    """
    Groq uses native function calling (Task CORE-4.5); the fail-closed contract is the same one
    Gemini's functionCall wire enforces.

    A response without a declared call, a call to a tool RESOLVE never declared, or arguments that
    are not valid JSON each become a refusal that runs nothing. The declared surface — every
    registry tool plus respond_to_user — is what went out on the wire as the `tools` array.
    """
    provider = GroqReasoningProvider(api_key=guarded_env["reasoning_api_key"], model="test-model")

    def chat_completion(tool_calls: list[dict]) -> dict:
        return {"choices": [{"message": {"role": "assistant", "tool_calls": tool_calls}}]}

    def function_call(name: str, arguments) -> dict:
        return {"index": 0, "type": "function", "function": {"name": name, "arguments": arguments}}

    seen_declarations: list[dict] = []

    async def scripted(system, user, declarations):
        seen_declarations.extend(declarations)
        return scripted.next_response

    cases = [
        (chat_completion([function_call("search_products", '{"query": "shampoo"}')]), "tool"),
        # A forced tool call answered with prose instead: no name to run.
        (chat_completion([]), "respond"),
        ({"choices": [{"message": {"role": "assistant", "content": "I would search."}}]}, "respond"),
        # A capability RESOLVE never declared: nothing runs even if the model names it.
        (
            chat_completion([function_call("confirm_payment", '{"amount": 100}')]),
            "respond",
        ),
        # The arguments field is a JSON string on this wire; garbage is a refusal, not {}.
        (chat_completion([function_call("search_products", "{not json")]), "respond"),
        (chat_completion([function_call("respond_to_user", '{"message": "Here you go."}')]), "respond"),
    ]
    for response, expected in cases:
        provider._call_model_with_tools = scripted  # type: ignore[method-assign]
        scripted.next_response = response
        decision = await provider.choose_agent_action({"mission": {}}, declarations_for_model())
        assert decision.action == expected, response
        if expected == "respond":
            assert decision.message

    declared_names = {entry["name"] for entry in seen_declarations}
    assert declared_names == {*tool_names(), RESPOND_TOOL_NAME}
    for entry in seen_declarations:
        assert entry["description"]
        if entry.get("parameters"):
            assert entry["parameters"]["type"] == "object"


def test_a_decision_that_is_not_one_of_the_two_shapes_cannot_be_constructed():
    """
    `AgentStepDecision` is the only thing a provider may return, and it has two shapes.

    There is no field for code, no field for a URL, and no third action — which is why the loop never
    has to wonder what an unrecognised step meant.
    """
    assert AgentStepDecision.model_config["extra"] == "forbid"
    with pytest.raises(Exception):
        AgentStepDecision(action="tool")
    with pytest.raises(Exception):
        AgentStepDecision(action="respond")
    with pytest.raises(Exception):
        AgentStepDecision(action="execute", code="import os")
    with pytest.raises(Exception):
        AgentStepDecision(action="tool", tool_name="select_product", url="https://example.com")
    assert set(AgentStepDecision.model_fields) == {"action", "tool_name", "arguments", "message"}


@pytest.mark.asyncio
async def test_the_conversation_table_only_grows(
    async_client: AsyncClient, install_mock_discovery, sample_products, session_factory
):
    """
    Append-only, in the sense the application guarantees.

    Every row the turn wrote is accounted for after a second turn, sequences never repeat, and no
    earlier row changed. There is no update or delete path for this table anywhere in the app — the
    same convention that makes the audit log readable as evidence.
    """
    _engine, test_session = session_factory
    app.dependency_overrides[get_db] = _override(test_session)
    mission_id = await _shopping_mission(
        async_client, install_mock_discovery, sample_products, "append@core2.test"
    )

    async def snapshot() -> list[tuple]:
        async with test_session() as session:
            rows = (
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
            return [(r.sequence, r.event_type, r.content) for r in rows]

    await _agent(async_client, mission_id, "Which one is cheapest?")
    after_first = await snapshot()
    await _agent(async_client, mission_id, "What about the second one?")
    after_second = await snapshot()

    assert after_second[: len(after_first)] == after_first
    assert len(after_second) > len(after_first)
    assert [row[0] for row in after_second] == list(range(len(after_second)))
