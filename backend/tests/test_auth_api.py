"""
Integration tests for /auth API endpoints.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app

# Test database in-memory SQLite (async)
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def async_client():
    # Setup test in-memory SQLite engine
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


@pytest.mark.asyncio
async def test_auth_full_flow(async_client: AsyncClient):
    # 1. Unauthenticated /auth/me returns 401
    resp = await async_client.get("/auth/me")
    assert resp.status_code == 401

    # 2. Register user
    reg_payload = {
        "name": "Test User",
        "email": "test@example.com",
        "password": "Password123!",
    }
    resp = await async_client.post("/auth/register", json=reg_payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test User"
    assert data["email"] == "test@example.com"
    assert "password" not in data
    assert "password_hash" not in data
    assert "resolve_session" in resp.cookies

    # 3. Duplicate registration rejected
    resp = await async_client.post("/auth/register", json=reg_payload)
    assert resp.status_code == 409

    # 4. /auth/me with session cookie
    resp = await async_client.get("/auth/me")
    assert resp.status_code == 200
    me_data = resp.json()
    assert me_data["email"] == "test@example.com"

    # 5. Logout clears cookie
    resp = await async_client.post("/auth/logout")
    assert resp.status_code == 200

    # 6. Login with wrong password rejected
    resp = await async_client.post(
        "/auth/login", json={"email": "test@example.com", "password": "WrongPassword"}
    )
    assert resp.status_code == 401

    # 7. Login with valid credentials succeeds
    resp = await async_client.post(
        "/auth/login", json={"email": "test@example.com", "password": "Password123!"}
    )
    assert resp.status_code == 200
    assert "resolve_session" in resp.cookies

