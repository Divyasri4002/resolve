"""
Tests for mission authorization and multi-user isolation.

Covers:
- TEST 1: User A creates a mission while sending user_id=User B -> mission belongs to User A.
- TEST 2: User A lists missions with ?user_id=User B -> User A does NOT receive User B's missions.
- TEST 3: User A requests User B's mission ID -> User A cannot receive User B's mission data.
- TEST 4: User A can retrieve their own mission.
- TEST 5: User A can list their own missions.
- TEST 6: Existing demo-user workflow continues to work safely without cross-user leak.
- TEST 7: Agent endpoint preserves functionality for owner and blocks cross-user access.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


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


def _sample_mission_payload(user_id: str = "demo-user") -> dict:
    return {
        "user_id": user_id,
        "objective": {
            "description": "Acquire camera",
            "success_criteria": ["working"],
        },
        "authority": {
            "allowed_actions": ["request_refund", "negotiate"],
            "recovery_budget": "5000",
        },
        "transaction": {
            "merchant_name": "Test Store",
            "order_id": "ord_123",
            "amount_paid": "10000",
        },
    }


@pytest.mark.asyncio
async def test_authorization_all_checks(async_client: AsyncClient):
    # -------------------------------------------------------------
    # Setup: Register User A and User B
    # -------------------------------------------------------------
    # Register User A
    resp_a = await async_client.post(
        "/auth/register",
        json={"name": "User A", "email": "user_a@example.com", "password": "Password123!"},
    )
    assert resp_a.status_code == 201
    user_a = resp_a.json()
    cookie_a = resp_a.cookies.get("resolve_session")
    assert cookie_a is not None

    # Register User B
    resp_b = await async_client.post(
        "/auth/register",
        json={"name": "User B", "email": "user_b@example.com", "password": "Password123!"},
    )
    assert resp_b.status_code == 201
    user_b = resp_b.json()
    cookie_b = resp_b.cookies.get("resolve_session")
    assert cookie_b is not None

    # -------------------------------------------------------------
    # TEST 1: User A creates a mission while sending user_id=User B.
    # Expected: Mission belongs to User A.
    # -------------------------------------------------------------
    async_client.cookies.set("resolve_session", cookie_a)
    payload_spoof = _sample_mission_payload(user_id=user_b["id"])

    create_resp = await async_client.post("/missions", json=payload_spoof)
    assert create_resp.status_code == 200
    mission_a = create_resp.json()
    mission_a_id = mission_a["mission_id"]

    # Verify ownership in DB / direct lookup: User A can access it
    get_a = await async_client.get(f"/missions/{mission_a_id}")
    assert get_a.status_code == 200
    assert "created_at" in get_a.json()
    assert get_a.json()["created_at"] is not None

    # User B creates a legitimate mission
    async_client.cookies.set("resolve_session", cookie_b)
    payload_b = _sample_mission_payload(user_id=user_b["id"])
    create_b_resp = await async_client.post("/missions", json=payload_b)
    assert create_b_resp.status_code == 200
    mission_b = create_b_resp.json()
    mission_b_id = mission_b["mission_id"]

    # -------------------------------------------------------------
    # TEST 2: User A lists missions with ?user_id=User B.
    # Expected: User A does NOT receive User B's missions.
    # -------------------------------------------------------------
    async_client.cookies.set("resolve_session", cookie_a)
    list_resp = await async_client.get(f"/missions?user_id={user_b['id']}")
    assert list_resp.status_code == 200
    items = list_resp.json()
    returned_ids = [m["mission_id"] for m in items]
    assert mission_a_id in returned_ids
    assert mission_b_id not in returned_ids  # User B's mission must NOT be in User A's list!

    # -------------------------------------------------------------
    # TEST 3: User A requests User B's mission ID.
    # Expected: User A cannot receive User B's mission data.
    # -------------------------------------------------------------
    forbidden_resp = await async_client.get(f"/missions/{mission_b_id}")
    assert forbidden_resp.status_code == 404  # 404 prevents leaking mission existence

    # -------------------------------------------------------------
    # TEST 4: User A can still retrieve their own mission.
    # -------------------------------------------------------------
    own_resp = await async_client.get(f"/missions/{mission_a_id}")
    assert own_resp.status_code == 200
    assert own_resp.json()["mission_id"] == mission_a_id

    # -------------------------------------------------------------
    # TEST 5: User A can still list their own missions.
    # -------------------------------------------------------------
    own_list = await async_client.get("/missions")
    assert own_list.status_code == 200
    assert any(m["mission_id"] == mission_a_id for m in own_list.json())

    # -------------------------------------------------------------
    # TEST 6: Existing demo-user workflow continues to work safely.
    # -------------------------------------------------------------
    async_client.cookies.clear()  # Unauthenticated
    demo_payload = _sample_mission_payload(user_id="demo-user")
    demo_create = await async_client.post("/missions", json=demo_payload)
    assert demo_create.status_code == 200
    demo_mission_id = demo_create.json()["mission_id"]

    # Unauthenticated client can retrieve demo-user mission
    demo_get = await async_client.get(f"/missions/{demo_mission_id}")
    assert demo_get.status_code == 200

    # Unauthenticated client CANNOT access authenticated User A's mission
    unauth_cross_get = await async_client.get(f"/missions/{mission_a_id}")
    assert unauth_cross_get.status_code == 404

    # Task CORE-2.1. This used to assert that an unauthenticated client could LIST missions and be
    # handed the `demo-user` namespace. Listing is now the one place that stopped having a demo door:
    # a mission receipt carries another person's objective, budget, delegated actions and spend, and a
    # visitor who happens to know the demo id has no need for the whole list to read it. The single
    # demo mission above (`demo_get`) is still reachable, because the demo workflow that reads one
    # mission it created still works; what is gone is "collect every mission in this namespace".
    demo_list = await async_client.get("/missions")
    assert demo_list.status_code == 401

    # An authenticated stranger listing does not receive the demo namespace either — a session sees
    # exactly its own missions, which is the whole rule.
    async_client.cookies.set("resolve_session", cookie_b)
    stranger_list = await async_client.get("/missions")
    assert stranger_list.status_code == 200
    listed = [m["mission_id"] for m in stranger_list.json()]
    assert demo_mission_id not in listed
    assert mission_a_id not in listed
    assert mission_b_id in listed

    # -------------------------------------------------------------
    # TEST 7: Existing mission functionality used by Gemini agent remains intact.
    # -------------------------------------------------------------
    async_client.cookies.set("resolve_session", cookie_a)
    agent_resp = await async_client.post(
        f"/missions/{mission_a_id}/agent",
        json={"user_input": "The camera lens was damaged on arrival"},
    )
    assert agent_resp.status_code == 200
    agent_data = agent_resp.json()
    assert agent_data["mission_id"] == mission_a_id
    assert "agent_proposal" in agent_data
    assert "authority_decision" in agent_data

    # User B cannot invoke agent on User A's mission
    async_client.cookies.set("resolve_session", cookie_b)
    blocked_agent = await async_client.post(
        f"/missions/{mission_a_id}/agent",
        json={"user_input": "Malicious command"},
    )
    assert blocked_agent.status_code == 404

