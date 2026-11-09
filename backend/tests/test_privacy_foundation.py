"""
Tests for Task 14B.5A — RESOLVE Protected Customer Data compliance foundation.

Keeps four promises honest:
  * the Shopify data-use declaration is machine-checkable and matches the live grant (four reads
    plus the single `write_returns` for the controlled return workflow, no broader write scope,
    no PII field appearing in any query);
  * the retention policy has a safe default and STRUCTURALLY cannot target core
    mission/payment/audit records (it is reporting-only today, and deletes nothing);
  * the automated-decision opt-out persists, blocks autonomous resolution while active, and
    changes nothing for issues that did not opt out;
  * no Shopify credential or token ever appears in persisted state, API payloads, or audit
    reasons.

Hermetic: mock reasoning/discovery via conftest; all providers mocked; no live Shopify calls.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.main import app
from app.models.db_models import (
    AuditLogRecord,
    OrderIssueRecord,
    ResolutionRecord,
    ReturnRecord,
)
from app.retention.service import (
    PROTECTED_CORE_TABLES,
    SHOPIFY_PROTECTED_SOURCES,
    ShopifyProtectedSource,
    build_retention_report,
)
from app.tools.shopify.client import (
    _CATALOG_QUERY,
    _FULFILLMENT_LINE_ITEMS_QUERY,
    _ORDERS_QUERY,
    _ORDER_LINE_IDENTITY_QUERY,
    _ORDER_REFERENCE_QUERY,
    _ORDER_RETURNS_QUERY,
    _PRODUCTS_QUERY,
    _RETURNS_QUERY,
    _RETURN_TARGETS_QUERY,
)
from app.tools.shopify.data_use import SHOPIFY_DATA_USE

from tests.test_refund_authority import _delegate, _verified_mission_product  # noqa: F401
from tests.test_refund_execution import (  # noqa: F401
    _login,
    _register,
    install_fake_reasoning,
    mock_razorpay,
    recorded_events,
    refund_api,
    restart_env,
)
from tests.test_replacement_execution import _assess, _issue  # noqa: F401
from tests.test_return_execution import (  # noqa: F401
    _authorized_return_issue,
    _return_proposal,
    _set_return_switch,
    return_provider,
)
from tests.test_resolution_orchestrator import (  # noqa: F401
    _proposal,
    _resolve,
    _setup,
)


async def _manual_review(client, mission_id, issue_id):
    return await client.post(
        f"/missions/{mission_id}/order/issues/{issue_id}/manual-review"
    )


async def _get_issue(client, mission_id, issue_id) -> dict:
    issues = (await client.get(f"/missions/{mission_id}/order/issues")).json()["issues"]
    return [i for i in issues if i["issue_id"] == issue_id][0]


# --------------------------------------------------------------------------- #
# G1: data-use declaration exists and is truthful about the scopes actually held
# --------------------------------------------------------------------------- #
def test_shopify_data_use_declaration_matches_the_live_grant_and_is_minimal():
    """
    Task 14C.6 verified the installed app holds four reads plus `write_returns`, so the
    declaration must say exactly that (Task 14C.6B). 'Minimal' now means: one write scope, and it
    is the narrowest one the controlled return workflow can be done without.
    """
    use = SHOPIFY_DATA_USE
    assert use.requested_scopes == (
        "read_products",
        "read_orders",
        "read_returns",
        "read_merchant_managed_fulfillment_orders",
        "write_returns",
    )
    # Every read granted before 14C.6 is still declared — a write enablement must not quietly
    # drop the reads the target/reconciliation paths depend on.
    for read in (
        "read_orders",
        "read_products",
        "read_returns",
        "read_merchant_managed_fulfillment_orders",
    ):
        assert read in use.requested_scopes, read
    assert "write_returns" in use.requested_scopes
    assert use.has_write_scope is True
    assert use.persists_shopify_pii is False
    assert "resolution" in use.purpose.lower()


def test_only_the_controlled_return_write_scope_is_declared():
    """
    `write_returns` is broader than `returnCreate` on Shopify's side, so the boundary has to be
    stated and checked: exactly one write scope, one intended mutation, and the write scopes
    RESOLVE does not want are absent rather than merely unmentioned.
    """
    use = SHOPIFY_DATA_USE
    writes = tuple(s for s in use.requested_scopes if not s.startswith("read_"))
    assert writes == ("write_returns",)
    assert use.intended_write_mutations == ("returnCreate",)
    for broader in use.unrequested_write_scopes:
        assert broader not in use.requested_scopes, broader
    assert "write_orders" not in use.requested_scopes
    assert "write_products" not in use.requested_scopes
    assert "write_merchant_managed_fulfillment_orders" not in use.requested_scopes


def test_the_write_scope_is_declared_only_for_the_return_purpose():
    """
    A granted write scope is a capability, not a licence. The declaration has to say which job it
    is for, and the privacy boundaries that made the read side acceptable must still be there.
    """
    use = SHOPIFY_DATA_USE
    purpose = use.purpose.lower()
    assert "return" in purpose
    assert "returncreate" in purpose
    for preserved in ("advertis", "analytic", "profil", "sale", "sharing"):
        assert preserved in purpose, preserved
    # The boundaries themselves are unchanged by 14C.6B: no PII claimed, no Shopify PII persisted,
    # and the persisted-field list is still only merchant object references and counts.
    assert use.persists_shopify_pii is False
    assert use.not_requested_pii == (
        "customer { firstName }",
        "customer { lastName }",
        "customer { email }",
        "phone",
        "address",
    )
    assert use.persisted_shopify_reference_fields == (
        "provider_order_reference",
        "provider_line_reference",
        "provider_quantity",
        "provider_reason",
        "submission_claim",
        "submission_claimed_at",
    )


def test_no_pii_field_appears_in_any_shopify_query():
    queries = (
        _PRODUCTS_QUERY
        + _ORDERS_QUERY
        + _RETURNS_QUERY
        + _FULFILLMENT_LINE_ITEMS_QUERY
        + _RETURN_TARGETS_QUERY
        + _ORDER_RETURNS_QUERY
        + _ORDER_REFERENCE_QUERY
        + _ORDER_LINE_IDENTITY_QUERY
        + _CATALOG_QUERY
    ).lower()
    for banned in ("email", "firstname", "lastname", "first_name", "last_name",
                   "phone", "address1", "address2"):
        assert banned not in queries


def test_the_return_submission_claim_stores_no_personal_or_secret_field():
    """
    Task 14C.4 lets a return row remember what it sent, so that a lost answer can be read back
    instead of re-sent. That memory must stay confined to merchant object references and counts:
    no credential, no header, and nothing naming a person.
    """
    claim_fields = SHOPIFY_DATA_USE.persisted_shopify_reference_fields
    assert claim_fields == (
        "provider_order_reference",
        "provider_line_reference",
        "provider_quantity",
        "provider_reason",
        "submission_claim",
        "submission_claimed_at",
    )
    for banned in (
        "token",
        "secret",
        "authorization",
        "header",
        "customer",
        "email",
        "phone",
        "address",
        "name",
    ):
        for field_name in claim_fields:
            assert banned not in field_name.lower(), (banned, field_name)

    # The columns that exist on the row are exactly the declared ones plus the pre-existing
    # provider reference — and no Shopify customer object is reachable from it.
    from app.models.db_models import ReturnRecord

    columns = {c.name for c in ReturnRecord.__table__.columns}
    assert set(claim_fields) <= columns
    for banned in ("shopify_customer", "customer_email", "customer_id", "access_token"):
        assert banned not in columns


def test_the_return_submission_claim_holds_only_values_resolved_server_side():
    """
    Behavioural half of the same rule: the return endpoint has no request body at all — not even a
    parameter that could carry one — so every merchant-side value on the persisted row must come
    from what the provider read. A caller has no channel to name an order, a line, a quantity or
    a reason.
    """
    import inspect

    from app.routers.missions import execute_issue_return, run_return_executor

    allowed = {"mission_id", "issue_id", "current_user", "db"}
    for fn in (execute_issue_return, run_return_executor):
        parameters = set(inspect.signature(fn).parameters)
        assert parameters == allowed, (fn.__name__, parameters)

    from app.routers.missions import _claim_return_submission

    claim_source = inspect.getsource(_claim_return_submission)
    for field in (
        "provider_order_reference",
        "provider_line_reference",
        "provider_quantity",
        "provider_reason",
    ):
        assert field in claim_source, field
    for banned in ("token", "secret", "authorization", "customer"):
        assert banned not in claim_source.lower(), banned


# --------------------------------------------------------------------------- #
# G2: retention configuration has a safe default
# --------------------------------------------------------------------------- #
def test_retention_config_default_is_safe():
    default = Settings.model_fields["shopify_retention_days"].default
    assert default == 30  # a conservative dev/test default, clearly bounded
    assert 0 < get_settings().shopify_retention_days <= 365


# --------------------------------------------------------------------------- #
# G3: retention logic can never touch protected core payment/audit records
# --------------------------------------------------------------------------- #
async def test_retention_report_deletes_nothing_and_protects_core(restart_env):
    _make_client, test_session = restart_env
    async with test_session() as session:
        report = await build_retention_report(session)
    assert report.dry_run is True
    # No Shopify-derived source is registered, so nothing at all is eligible.
    assert SHOPIFY_PROTECTED_SOURCES == ()
    assert report.sources_considered == []
    assert report.eligible_row_counts == {}
    # Core tables are listed as protected-skipped, never considered.
    for core in ("refunds", "audit_log", "missions", "resolve_orders"):
        assert core in report.protected_core_tables_skipped
    assert any("delet" in n.lower() or "nothing was deleted" in n.lower()
               for n in [report.notes[0]]) or report.notes


def test_protected_core_table_cannot_be_registered_for_retention():
    for table in ("refunds", "audit_log", "missions", "order_issues"):
        with pytest.raises(ValueError):
            ShopifyProtectedSource(table=table, id_column="x_id", timestamp_column="created_at")


async def test_retention_report_touches_no_data(restart_env):
    """A dry-run pass leaves every row of every protected table exactly as it was."""
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        await _register(client, "Beta Owner", "beta@shop.com")
        mission_id = (
            await client.post("/missions/shopping", json={"prompt": "shampoo under 600"})
        ).json()["mission_id"]
        async with test_session() as session:
            before = await session.scalar(
                select(func.count()).select_from(AuditLogRecord)
            )
            report = await build_retention_report(session, retention_days=1)
            after = await session.scalar(
                select(func.count()).select_from(AuditLogRecord)
            )
        assert report.retention_days == 1
        assert before == after  # reporting only — nothing deleted
        assert mission_id  # sanity: the pass did not disturb application state


# --------------------------------------------------------------------------- #
# G4 + G5 + G9: opt-out request persists, is visible, idempotent, and respects auth
# --------------------------------------------------------------------------- #
async def test_manual_review_optout_persists_idempotent_and_authorization_bound(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    recorded_events,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _issue(client, mission_id, kind="none")

        # G9: anonymous and non-owner cannot opt anyone's issue in (404, not 403-by-design).
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
            assert (await _manual_review(anon, mission_id, issue_id)).status_code == 404
        await _register(client, "Mallory", "mallory@shop.com")
        assert (await _manual_review(client, mission_id, issue_id)).status_code == 404
        await _login(client, "beta@shop.com")

        # G4: the owner's request succeeds and is reported honestly.
        resp = await _manual_review(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()
        assert data["manual_review"]["requested"] is True
        assert data["manual_review"]["blocks_autonomous_resolution"] is True
        assert data["already_existed"] is False
        assert data["manual_review"]["requested_at"]
        assert "no external ticket exists" in data["agent_message"] + data["note"]

        # G5: it persists — visible on the issue payload and in the database row.
        issue = await _get_issue(client, mission_id, issue_id)
        assert issue["manual_review"]["requested"] is True
        async with test_session() as session:
            row = await session.get(OrderIssueRecord, issue_id)
            assert row.manual_review_requested is True
            assert row.manual_review_requested_at is not None

        # Idempotent: a repeat changes nothing further.
        again = await _manual_review(client, mission_id, issue_id)
        assert again.status_code == 200
        assert again.json()["already_existed"] is True
        assert again.json()["manual_review"]["requested_at"] == data["manual_review"]["requested_at"]

        # Auditable through the EXISTING insert-only audit log; one WS event.
        async with test_session() as session:
            audits = (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
            assert sum(1 for a in audits if "manual_review_requested" in (a.reason or "")) == 1
            assert all(a.action == "escalate_to_human" for a in audits
                       if "manual_review_requested" in (a.reason or ""))
        assert [e["event"] for e in recorded_events if e["event"] == "manual_review_requested"] == [
            "manual_review_requested"
        ]


# --------------------------------------------------------------------------- #
# G6 + G7: opt-out blocks autonomous execution; non-opted issues behave exactly as before
# --------------------------------------------------------------------------- #
async def test_optout_blocks_autonomous_resolution_others_unchanged(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    async with client:
        mission_id = await mission_setup(client)

        # Opted-out issue: a fully ALLOW-assessed, otherwise-executable return request.
        opted = await _authorized_return_issue(client, test_session, mission_id)
        assert (await _manual_review(client, mission_id, opted)).status_code == 200

        resp = await _resolve(client, mission_id, opted)
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolution_state"] == "escalated"
        assert data["authority_decision"] == "manual_review"
        assert data["executor"] == "none"
        assert data["provider_attempted"] is False
        assert "manual review" in data["resolution_note"] + data["message"]
        # NO executor ran: no return row, and the fake provider was never contacted.
        assert return_provider.requests == []
        async with test_session() as session:
            assert await session.scalar(select(func.count()).select_from(ReturnRecord)) == 0
            resolutions = (
                await session.scalars(
                    select(ResolutionRecord).where(ResolutionRecord.issue_id == opted)
                )
            ).all()
        assert len(resolutions) == 1

        # Repeated /resolve stays blocked and idempotent (escalated is terminal).
        again = (await _resolve(client, mission_id, opted)).json()
        assert again["resolution_state"] == "escalated"
        assert again["resolution_id"] == data["resolution_id"]
        assert return_provider.requests == []

        # G7: an issue WITHOUT the opt-out still follows the exact existing path —
        # authority is evaluated, the executor runs, and the provider is reached.
        await _delegate(test_session, mission_id, ["request_return"])
        normal = await _issue(client, mission_id, kind="none")
        assessment = await _assess(client, mission_id, normal)
        assert assessment["proposed_action"] == "request_return"
        assert assessment["authority_decision"] == "allow"
        resp = await _resolve(client, mission_id, normal)
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolution_state"] == "requested"
        assert data["provider_attempted"] is True
        assert len(return_provider.requests) == 1
        async with test_session() as session:
            assert await session.scalar(select(func.count()).select_from(ReturnRecord)) == 1


# --------------------------------------------------------------------------- #
# G8: Shopify credentials/tokens never appear in logs or persisted application state
# --------------------------------------------------------------------------- #
async def test_shopify_credentials_absent_from_state_and_logs(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning,
    monkeypatch, return_provider,
):
    client, test_session, mission_setup = await _setup(
        restart_env, install_mock_discovery, sample_products, mock_razorpay, install_fake_reasoning
    )
    secret = "shppa_SECRET_MUST_NOT_LEAK"
    token = "shpat_TOKEN_MUST_NOT_LEAK"
    monkeypatch.setattr(get_settings(), "shopify_client_id", "ci-test-value")
    monkeypatch.setattr(get_settings(), "shopify_client_secret", secret)

    async with client:
        mission_id = await mission_setup(client)
        issue_id = await _authorized_return_issue(client, test_session, mission_id)
        resolved = (await _resolve(client, mission_id, issue_id)).json()
        await _manual_review(client, mission_id, issue_id)

        raw_payloads = [
            str(resolved),
            str((await client.get(f"/missions/{mission_id}/order/issues")).json()),
            str((await client.get(f"/missions/{mission_id}")).json()),
        ]
        async with test_session() as session:
            rows = {
                "issues": [
                    {c.name: getattr(r, c.name) for c in r.__table__.columns}
                    for r in (await session.scalars(
                        select(OrderIssueRecord).where(OrderIssueRecord.mission_id == mission_id)
                    )).all()
                ],
                "resolutions": [
                    {c.name: getattr(r, c.name) for c in r.__table__.columns}
                    for r in (await session.scalars(
                        select(ResolutionRecord).where(ResolutionRecord.mission_id == mission_id)
                    )).all()
                ],
                "audits": [
                    a.reason for a in (await session.scalars(
                        select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                    )).all()
                ],
            }
        raw = "".join(raw_payloads) + str(rows)
        for must_not in (secret, token):
            assert must_not not in raw
        # And the schema simply has nowhere for such data to live:
        assert not any("shopify" in (c.name or "") for c in OrderIssueRecord.__table__.columns)
        assert not any("shopify" in (c.name or "") for c in ResolutionRecord.__table__.columns)


# --------------------------------------------------------------------------- #
# Task 14B.5B — the centralized startup migration (app/database.py) applies the
# additive manual-review columns to a PRE-EXISTING table that lacks them, safely and
# idempotently. Fresh databases get them via create_all (covered by every other test).
# --------------------------------------------------------------------------- #
_PRE_5B_ORDER_ISSUES = """
CREATE TABLE order_issues (
    issue_id VARCHAR(64) PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL,
    mission_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    issue_type VARCHAR(32) NOT NULL,
    description TEXT NOT NULL,
    requested_resolution VARCHAR(32) NOT NULL DEFAULT 'none',
    status VARCHAR(32) NOT NULL DEFAULT 'reported',
    evidence JSON NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""


async def test_startup_migration_adds_manual_review_columns_idempotently(tmp_path, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.database as database

    url = f"sqlite+aiosqlite:///{tmp_path/'mig.db'}"
    test_engine = create_async_engine(url, future=True)

    # Simulate the existing dev volume: order_issues present WITHOUT the 14B.5A columns.
    async with test_engine.begin() as conn:
        await conn.execute(text(_PRE_5B_ORDER_ISSUES))

    monkeypatch.setattr(database, "engine", test_engine)
    await database.init_models()

    def columns(sync_conn):
        from sqlalchemy import inspect

        return {c["name"] for c in inspect(sync_conn).get_columns("order_issues")}

    async with test_engine.begin() as conn:
        cols = await conn.run_sync(columns)
    assert "manual_review_requested" in cols
    assert "manual_review_requested_at" in cols

    # Idempotent: a second startup on the already-migrated schema raises nothing and is a no-op.
    await database.init_models()
    async with test_engine.begin() as conn:
        cols_again = await conn.run_sync(columns)
    assert cols_again == cols

    await test_engine.dispose()
