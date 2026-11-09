"""
Task CORE-2.1, Objective 2 — the three legacy mission surfaces now answer to their owner.

`GET /missions`, `POST /missions/{id}/offer` and `POST /missions/{id}/resolve` were the last routes
that took no identity: any caller who could name a mission id could read every mission in the
`demo-user` namespace, put an offer in front of a stranger's mandate (an accepted offer writes
`total_recovery_spend` and moves the mission to `executing`), or close a stranger's case as `resolved`.

The rule being enforced is not new — it is the one every other mission route already uses: no session
is a 401, somebody else's mission is a 404 that is indistinguishable from a mission that never
existed, and your own mission behaves exactly as it did before. What changed is who may ask.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
PASSWORD = "SecurePassword123!"


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


def _recovery_payload() -> dict:
    return {
        "objective": {"description": "Recover the cracked camera", "success_criteria": ["working"]},
        "authority": {"allowed_actions": ["request_refund", "negotiate"], "recovery_budget": "5000"},
        "transaction": {"merchant_name": "Test Store", "order_id": "ord_123", "amount_paid": "1000"},
    }


async def _register(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/auth/register", json={"name": "Owner", "email": email, "password": PASSWORD}
    )
    assert response.status_code == 201
    return response.cookies.get("resolve_session")


async def _mission_of(client: AsyncClient, email: str) -> str:
    cookie = await _register(client, email)
    client.cookies.set("resolve_session", cookie)
    created = await client.post("/missions", json=_recovery_payload())
    assert created.status_code == 200, created.text
    return created.json()["mission_id"]


def _surfaces(mission_id: str) -> list[tuple[str, str, dict]]:
    """(method, path, body) for every surface this task hardened."""
    return [
        ("GET", "/missions", None),
        ("POST", f"/missions/{mission_id}/offer", {"offered_remedy": "refund", "offered_amount": "50"}),
        ("POST", f"/missions/{mission_id}/resolve?summary=done", None),
    ]


@pytest.mark.asyncio
async def test_anonymous_callers_are_asked_to_sign_in(async_client: AsyncClient):
    """
    No session is a 401 on all three — before the mission id means anything.

    The answer does not depend on whether the id is real, which is what makes this a refusal rather
    than a probe.
    """
    mission_id = await _mission_of(async_client, "anon.owner@core21.test")
    async_client.cookies.clear()

    listing = await async_client.get("/missions")
    assert listing.status_code == 401

    unknown = await async_client.post("/missions/mission_does_not_exist/offer", json={
        "offered_remedy": "refund",
        "offered_amount": "50",
    })
    assert unknown.status_code == 401

    for method, path, body in _surfaces(mission_id):
        response = await (
            async_client.get(path) if method == "GET" else async_client.post(path, json=body)
        )
        assert response.status_code == 401, f"{method} {path} → {response.status_code}"


@pytest.mark.asyncio
async def test_a_stranger_cannot_reach_any_of_them_and_cannot_tell_real_from_invented(
    async_client: AsyncClient,
):
    """
    Wrong owner: refused, and refused with the same answer a made-up id gets.

    A 403 would confirm the mission exists; the project's convention is that somebody else's mission
    is simply not there. That is only a real convention if the two responses are byte-identical, so
    this compares the bodies, not just the status codes.
    """
    mine = await _mission_of(async_client, "victim@core21.test")
    intruder_cookie = await _register(async_client, "intruder@core21.test")
    async_client.cookies.set("resolve_session", intruder_cookie)

    responses = []
    for method, path, body in _surfaces(mine):
        responses.append(
            await (async_client.get(path) if method == "GET" else async_client.post(path, json=body))
        )
    # The list route is the one that cannot answer 404 — it is scoped, so the stranger gets their own
    # (empty) view instead of anyone else's.
    listing, offer, resolved = responses
    assert listing.status_code == 200
    assert [m["mission_id"] for m in listing.json()] == []
    assert offer.status_code == 404
    assert resolved.status_code == 404

    invented_offer = await async_client.post(
        "/missions/mission_never_created/offer", json={"offered_remedy": "refund", "offered_amount": "50"}
    )
    invented_resolve = await async_client.post("/missions/mission_never_created/resolve?summary=x")
    assert invented_offer.status_code == 404
    assert invented_offer.json() == offer.json()
    assert invented_resolve.status_code == 404
    assert invented_resolve.json() == resolved.json()


@pytest.mark.asyncio
async def test_a_strangers_request_leaves_the_mission_exactly_as_it_was(
    async_client: AsyncClient,
):
    """
    A refusal that is refused *before* anything is written.

    `total_recovery_spend` and `status` are the two fields an accepted offer and a resolution touch,
    so they are the ones worth comparing: a 404 that still moved the mission would be a security
    answer with a business side effect.
    """
    mission_id = await _mission_of(async_client, "keep@core21.test")
    owner_email = "keep@core21.test"
    before = (await async_client.get(f"/missions/{mission_id}")).json()

    intruder_cookie = await _register(async_client, "noke@core21.test")
    async_client.cookies.set("resolve_session", intruder_cookie)
    offer = await async_client.post(
        f"/missions/{mission_id}/offer", json={"offered_remedy": "refund", "offered_amount": "50"}
    )
    assert offer.status_code == 404

    async_client.cookies.clear()
    resolve = await async_client.post(f"/missions/{mission_id}/resolve?summary=hijacked")
    assert resolve.status_code == 401

    async_client.cookies.set("resolve_session", await _login(async_client, owner_email))
    after = (await async_client.get(f"/missions/{mission_id}")).json()
    assert after["status"] == before["status"]
    assert after["total_recovery_spend"] == before["total_recovery_spend"]


async def _login(client: AsyncClient, email: str) -> str:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return response.cookies.get("resolve_session")


@pytest.mark.asyncio
async def test_the_owner_keeps_everything_they_had(async_client: AsyncClient):
    """
    The behaviour these routes were built for is unchanged for the person who owns the mission.

    Listing shows the mission; an offer still runs the Buyer Agent and the Authority Engine and comes
    back with a receipt; resolving still records the summary. Nothing in the recovery logic moved —
    the only difference is that the caller is now known.
    """
    mission_id = await _mission_of(async_client, "real@core21.test")

    listing = await async_client.get("/missions")
    assert listing.status_code == 200
    assert mission_id in [m["mission_id"] for m in listing.json()]

    offer = await async_client.post(
        f"/missions/{mission_id}/offer",
        json={"offered_remedy": "request_refund", "offered_amount": "50"},
    )
    assert offer.status_code == 200, offer.text
    assert offer.json()["mission_id"] == mission_id

    resolved = await async_client.post(f"/missions/{mission_id}/resolve?summary=verified refund")
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "resolved"
    assert body["resolution_summary"] == "verified refund"


@pytest.mark.asyncio
async def test_a_client_supplied_user_id_still_carries_no_authority(async_client: AsyncClient):
    """
    The old route let an anonymous caller ask for somebody's missions by id. It still cannot.

    `?user_id=` is accepted and ignored — the session is the only thing that decides the scope — so a
    caller who remembers the parameter gets their own list, not the one they named.
    """
    theirs_cookie = await _register(async_client, "theirs@core21.test")
    async_client.cookies.set("resolve_session", theirs_cookie)
    theirs = (await async_client.post("/missions", json=_recovery_payload())).json()["mission_id"]
    their_id = (await async_client.get("/auth/me")).json()["id"]

    my_cookie = await _register(async_client, "mine@core21.test")
    async_client.cookies.set("resolve_session", my_cookie)
    mine = (await async_client.post("/missions", json=_recovery_payload())).json()["mission_id"]

    asked = await async_client.get(f"/missions?user_id={their_id}")
    assert asked.status_code == 200
    ids = [m["mission_id"] for m in asked.json()]
    assert mine in ids
    assert theirs not in ids


@pytest.mark.asyncio
async def test_the_three_surfaces_do_not_re_implement_ownership(async_client: AsyncClient):
    """
    Structural: all three routes ask the one guard, and none of them grows its own idea of "mine".

    A second authorization implementation is how the first one starts to drift.
    """
    import ast
    import pathlib

    from app.routers import missions as missions_module

    path = pathlib.Path(missions_module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"list_missions", "receive_offer", "mark_resolved"}

    def default_of(node, name: str) -> str:
        """
        The expression a parameter defaults to.

        `ast.unparse` on the parameter node itself gives only `current_user: UserRecord` — defaults
        live on the function, aligned to its trailing parameters — so reading the annotation would
        have found a `Depends` that was never there to read.
        """
        names = [argument.arg for argument in node.args.args]
        if name not in names:
            return ""
        index = names.index(name) - (len(names) - len(node.args.defaults))
        return "" if index < 0 else ast.unparse(node.args.defaults[index])

    def code_of(node) -> str:
        """The route's statements, without its docstring: prose is not an implementation."""
        body = list(node.body)
        if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        return "\n".join(ast.unparse(statement) for statement in body)

    nodes: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in wanted:
            nodes[node.name] = node

    assert set(nodes) == wanted
    for name, node in nodes.items():
        decorators = [ast.unparse(item) for item in node.decorator_list]
        assert any(decorator.startswith("router.") for decorator in decorators), (
            f"{name} is not reachable as a route any more"
        )
        assert default_of(node, "current_user") == "Depends(get_current_user)", (
            f"{name} does not require a session"
        )

    for name in ("receive_offer", "mark_resolved"):
        assert "_verify_mission_access(" in code_of(nodes[name]), (
            f"{name} decides ownership somewhere other than the one guard"
        )

    # A list has no single record to hand the guard, so its only safe shape is the session's own id.
    listing = code_of(nodes["list_missions"])
    assert "current_user.id" in listing, "the list is not scoped to the caller"
    assert "== user_id" not in listing, "the list still trusts a caller-supplied user_id"
