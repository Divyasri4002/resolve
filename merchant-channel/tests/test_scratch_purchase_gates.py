"""
Hermetic gate tests for the scratch purchase runner.

Every scenario the runner exists for is exercised against fakes: the order source is a scripted
loader (standing in for RESOLVE's own two endpoints), the gateway is a counting stub (no Shopify
GraphQL leaves the process), the receiver is a scripted HTTP double, and the outbox is a real
Outbox at a temporary path. No network, no credential, no live purchase — the runner's whole job
is to prove that nothing reaches ``PurchaseChannel.fulfil`` unless every gate passes, and that a
confirmed run still cannot create twice, retry blind, or reclaim after a refusal.

The real ``fetch_order_facts`` parser is exercised too, against representative bodies in the
exact shape RESOLVE's own endpoints serve, via a fake ``httpx`` module — no socket is opened.
The exclusive run lock is proven with genuinely separate processes.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from merchant_channel import gates as gate_module
from merchant_channel.channel import (
    CONFIRMED_LINKED,
    OUTCOME_UNKNOWN as OUTCOME_UNKNOWN_STATUS,
    REFUSED_LINKAGE,
)
from merchant_channel.correlation import (
    correlation_id as derive_correlation,
    payload_hash as derive_payload_hash,
)
from merchant_channel.documents import MerchantOrder, OrderLine
from merchant_channel.gateway import GatewayOutcomeUnknown
from merchant_channel.outbox import (
    LINKAGE_DELIVERY_UNKNOWN,
    LINKAGE_REFUSED,
    LINKAGE_SENT,
    Outbox,
)

import scratch_purchase as runner

# Disposable values for this process only: never credentials, never printed as configuration.
ENV = {
    "MERCHANT_SHOPIFY_STORE_DOMAIN": "resolve-merchant-test.myshopify.com",
    "MERCHANT_SHOPIFY_API_VERSION": "2026-07",
    "MERCHANT_SHOPIFY_ACCESS_TOKEN": "shpua_disposable-scratch-runner-token",
    "MERCHANT_LINKAGE_SECRET": "disposable-scratch-runner-linkage-secret",
    "MERCHANT_RESOLVE_BASE_URL": "http://host.docker.internal:8000",
    "MERCHANT_EXPECTED_CURRENCY": "INR",
    "MERCHANT_EXPECTED_SHOP_NAME": "RESOLVE Merchant Test",
    "MERCHANT_CHANNEL_SOURCE": "scratch-purchase-runner",
}

MISSION_ID = "mission_scratch0001"
ORDER_ID = "order_a1b2c3d4e5f6"
PRODUCT_GID = "gid://shopify/Product/15408674210109"
VARIANT_GID = "gid://shopify/ProductVariant/67625768649021"
STORE_DOMAIN = "resolve-merchant-test.myshopify.com"
MERCHANT_ORDER_GID = "gid://shopify/Order/9000000000000042"
EVENT_ID = "evt-scratch0000000001"


def make_facts(**overrides) -> runner.OrderFacts:
    values = dict(
        resolve_order_id=ORDER_ID,
        mission_id=MISSION_ID,
        status="order_created",
        currency="INR",
        razorpay_order_id="order_testdisposable01",
        razorpay_payment_id="pay_testdisposable01",
        merchant_order_linked=False,
        product_gid=PRODUCT_GID,
        variant_gid=VARIANT_GID,
        store_domain=STORE_DOMAIN,
        order_product_id="prod_scratch",
        selected_product_id="prod_scratch",
        title="Scratch Item",
    )
    values.update(overrides)
    return runner.OrderFacts(**values)


def make_loader(facts):
    async def loader(base_url, *, mission_id):
        assert base_url == "http://host.docker.internal:8000"
        assert mission_id == MISSION_ID
        return facts, runner.GateReport()

    return loader


class GatewayStub:
    """Counts every create; answers with one consistent order so the happy path can complete."""

    def __init__(self, *, create_raises: Exception | None = None) -> None:
        self.create_calls = 0
        self.create_quantities: list[int] = []
        self.create_raises = create_raises
        self._created: MerchantOrder | None = None

    def _order(self, correlation_id: str, quantity: int) -> MerchantOrder:
        return MerchantOrder(
            order_id=MERCHANT_ORDER_GID,
            name="#SC1",
            po_number=correlation_id,
            currency="INR",
            source_name="scratch",
            financial_status="PENDING",
            created_at=None,
            lines=(
                OrderLine(
                    line_item_id="gid://shopify/LineItem/1",
                    product_id=PRODUCT_GID,
                    variant_id=VARIANT_GID,
                    quantity=quantity,
                ),
            ),
        )

    async def create_order(self, *, correlation_id: str, variant_gid: str, quantity: int):
        self.create_calls += 1
        self.create_quantities.append(quantity)
        if self.create_raises is not None:
            raise self.create_raises
        self._created = self._order(correlation_id, quantity)
        return self._created

    async def read_back(self, correlation_id: str) -> list[MerchantOrder]:
        return [self._created] if self._created is not None else []

    async def shop_identity(self) -> tuple[str | None, str | None]:
        return ("RESOLVE Merchant Test", "INR")


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class ReceiverStub:
    """The receiver double: records what it was handed, answers with a scripted status."""

    def __init__(self, *, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self.payload = payload if payload is not None else {"outcome": "linked"}
        self.requests: list[dict] = []

    async def post(self, url, *, content=None, headers=None, **kwargs):
        self.requests.append({"url": url, "content": content, "headers": headers})
        return _Response(self.status_code, self.payload)


def run_runner(tmp_path, *, env=None, facts=None, confirm=False, gateway=None,
               http_client=None, resolve_order_id=None, quantity=1) -> runner.RunnerResult:
    return asyncio.run(
        runner.run(
            env=dict(ENV if env is None else env),
            outbox_path=str(tmp_path / "outbox.sqlite3"),
            mission_id=MISSION_ID,
            resolve_order_id=resolve_order_id,
            quantity=quantity,
            confirm=confirm,
            order_loader=make_loader(make_facts() if facts is None else facts),
            gateway=gateway,
            http_client=http_client,
        )
    )


def seed_outbox(tmp_path, mutate) -> None:
    handoff = runner.handoff_from_facts(make_facts(), quantity=1)
    correlation = derive_correlation(handoff)
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    outbox.reserve(
        correlation_id=correlation,
        resolve_order_id=ORDER_ID,
        payload_hash=derive_payload_hash(handoff),
    )
    mutate(outbox, correlation)


# --------------------------------------------------------------- 1. configuration

def test_missing_linkage_secret_fails_closed_and_never_derives_a_correlation(tmp_path):
    env = dict(ENV)
    del env["MERCHANT_LINKAGE_SECRET"]
    result = run_runner(tmp_path, env=env, gateway=GatewayStub())
    assert result.exit_code == 1
    assert any("not fully configured" in problem for problem in result.problems)
    assert result.correlation_id is None


def test_a_resolve_owned_env_name_refuses_the_whole_run(tmp_path):
    env = dict(ENV)
    env["LINKAGE_INTEGRATION_SECRET"] = "borrowed-and-never-used"
    result = run_runner(tmp_path, env=env, gateway=GatewayStub())
    assert result.exit_code == 1
    assert any("must not read RESOLVE's credentials" in problem for problem in result.problems)


def test_wrong_base_url_refuses(tmp_path):
    env = dict(ENV)
    env["MERCHANT_RESOLVE_BASE_URL"] = "http://localhost:8000"
    result = run_runner(tmp_path, env=env, gateway=GatewayStub())
    assert result.exit_code == 1
    assert any("host.docker.internal:8000" in problem for problem in result.problems)


def test_wrong_shop_name_refuses(tmp_path):
    env = dict(ENV)
    env["MERCHANT_EXPECTED_SHOP_NAME"] = "RESOLVE Commerce Lab"
    result = run_runner(tmp_path, env=env, gateway=GatewayStub())
    assert result.exit_code == 1
    assert any("RESOLVE Merchant Test" in problem for problem in result.problems)


def test_empty_api_version_refuses(tmp_path):
    env = dict(ENV)
    env["MERCHANT_SHOPIFY_API_VERSION"] = ""
    result = run_runner(tmp_path, env=env, gateway=GatewayStub())
    assert result.exit_code == 1
    assert any("MERCHANT_SHOPIFY_API_VERSION" in problem for problem in result.problems)


# --------------------------------------------------------------- 2. dry run

def test_dry_run_is_the_default_and_never_creates_or_sends(tmp_path):
    gateway, receiver = GatewayStub(), ReceiverStub()
    result = run_runner(tmp_path, gateway=gateway, http_client=receiver, confirm=False)
    assert result.exit_code == 0
    assert result.dry_run is True
    assert result.outcome is None
    assert result.correlation_id is not None
    assert gateway.create_calls == 0
    assert receiver.requests == []


# --------------------------------------------------------------- 3. identity

def test_wrong_currency_store_or_gids_are_refused_before_any_channel_exists(tmp_path):
    for label, overrides in (
        ("currency", dict(currency="USD")),
        ("store", dict(store_domain="resolve-commerce-lab.myshopify.com")),
        ("variant-is-a-product-gid", dict(variant_gid=PRODUCT_GID)),
        ("non-numeric-product-gid", dict(product_gid="gid://shopify/Product/abc")),
        ("partial-identity", dict(product_gid=None)),
    ):
        gateway = GatewayStub()
        result = run_runner(tmp_path, facts=make_facts(**overrides), gateway=gateway)
        assert result.exit_code == 1, label
        assert result.problems, label
        assert gateway.create_calls == 0, label


def test_a_mismatched_requested_order_id_refuses(tmp_path):
    result = run_runner(tmp_path, resolve_order_id="order_000000000000", gateway=GatewayStub())
    assert result.exit_code == 1
    assert any("not the one that was requested" in problem for problem in result.problems)


# --------------------------------------------------------------- 4. payment / linkage evidence

def test_missing_payment_evidence_refuses(tmp_path):
    result = run_runner(
        tmp_path,
        facts=make_facts(razorpay_order_id=None, razorpay_payment_id=None),
        gateway=GatewayStub(),
    )
    assert result.exit_code == 1
    assert any("payment-verification evidence" in problem for problem in result.problems)


def test_an_already_linked_order_refuses(tmp_path):
    result = run_runner(tmp_path, facts=make_facts(merchant_order_linked=True), gateway=GatewayStub())
    assert result.exit_code == 1
    assert any("already carries a merchant linkage" in problem for problem in result.problems)


def test_inconsistent_order_and_selection_identity_refuses(tmp_path):
    result = run_runner(
        tmp_path, facts=make_facts(order_product_id="prod_other"), gateway=GatewayStub()
    )
    assert result.exit_code == 1
    assert any("disagrees with the selected catalog item" in problem for problem in result.problems)


# --------------------------------------------------------------- 5. outbox states

def _seed_pending(outbox, cid):
    outbox.begin_create(cid)


def _seed_unknown(outbox, cid):
    outbox.mark_outcome_unknown(cid, "the create answer was lost")


def _seed_ambiguous(outbox, cid):
    outbox.mark_reconciled(cid, "ambiguous")


def _seed_sent(outbox, cid):
    outbox.record_event(cid, event_id=EVENT_ID, event_body="{}")
    outbox.mark_confirmed(cid, MERCHANT_ORDER_GID)
    outbox.set_linkage(cid, LINKAGE_SENT, event_id=EVENT_ID)


def _seed_refused(outbox, cid):
    outbox.record_event(cid, event_id=EVENT_ID, event_body="{}")
    outbox.mark_confirmed(cid, MERCHANT_ORDER_GID)
    outbox.set_linkage(cid, LINKAGE_REFUSED, event_id=EVENT_ID)


def _seed_delivery_unknown(outbox, cid):
    outbox.record_event(cid, event_id=EVENT_ID, event_body="{}")
    outbox.mark_confirmed(cid, MERCHANT_ORDER_GID)
    outbox.set_linkage(cid, LINKAGE_DELIVERY_UNKNOWN, event_id=EVENT_ID)


def test_blocked_outbox_states_refuse_without_constructing_the_channel(tmp_path):
    for label, mutate in (
        ("pending", _seed_pending),
        ("unknown", _seed_unknown),
        ("ambiguous", _seed_ambiguous),
        ("already-sent", _seed_sent),
        ("refused", _seed_refused),
        ("delivery-unknown", _seed_delivery_unknown),
    ):
        seed_outbox(tmp_path, mutate)
        gateway = GatewayStub()
        result = run_runner(tmp_path, gateway=gateway, confirm=True)
        assert result.exit_code == 1, label
        assert result.problems, label
        assert gateway.create_calls == 0, label


def test_an_outbox_row_for_a_different_purchase_refuses(tmp_path):
    handoff = runner.handoff_from_facts(make_facts(), quantity=1)
    correlation = derive_correlation(handoff)
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    outbox.reserve(
        correlation_id=correlation, resolve_order_id=ORDER_ID, payload_hash="f" * 64
    )
    gateway = GatewayStub()
    result = run_runner(tmp_path, gateway=gateway)
    assert result.exit_code == 1
    assert any("different purchase" in problem for problem in result.problems)
    assert gateway.create_calls == 0


def test_an_unwritable_outbox_location_refuses(tmp_path):
    gateway = GatewayStub()
    result = asyncio.run(
        runner.run(
            env=dict(ENV),
            outbox_path=str(tmp_path / "missing-dir" / "outbox.sqlite3"),
            mission_id=MISSION_ID,
            order_loader=make_loader(make_facts()),
            gateway=gateway,
        )
    )
    assert result.exit_code == 1
    assert any("outbox directory does not exist" in problem for problem in result.problems)
    assert gateway.create_calls == 0


# --------------------------------------------------------------- 6. duplicate invocation

def test_a_second_confirmed_run_cannot_create_or_send_again(tmp_path):
    first = run_runner(
        tmp_path, confirm=True, gateway=GatewayStub(), http_client=ReceiverStub()
    )
    assert first.exit_code == 0 and first.outcome.status == CONFIRMED_LINKED

    second_gateway, second_receiver = GatewayStub(), ReceiverStub()
    second = run_runner(
        tmp_path, confirm=True, gateway=second_gateway, http_client=second_receiver
    )
    assert second.exit_code == 1
    assert any("already linked" in problem for problem in second.problems)
    assert second_gateway.create_calls == 0
    assert second_receiver.requests == []


# --------------------------------------------------------------- 7. uncertain outcome

def test_an_unknown_create_outcome_blocks_the_blind_retry(tmp_path):
    stuck = GatewayStub(create_raises=GatewayOutcomeUnknown("the create answer was lost"))
    first = run_runner(tmp_path, confirm=True, gateway=stuck, http_client=ReceiverStub())
    assert first.exit_code == 2
    assert first.outcome.status == OUTCOME_UNKNOWN_STATUS

    retry = GatewayStub()
    second = run_runner(tmp_path, confirm=True, gateway=retry, http_client=ReceiverStub())
    assert second.exit_code == 1
    assert any("outcome is unknown" in problem for problem in second.problems)
    assert retry.create_calls == 0


# --------------------------------------------------------------- 8. refusal discipline

def test_a_linkage_refusal_is_reported_and_never_reclaimed(tmp_path):
    gateway = GatewayStub()
    receiver = ReceiverStub(status_code=409, payload={"detail": "the pairing was refused"})
    result = run_runner(tmp_path, confirm=True, gateway=gateway, http_client=receiver)
    assert result.exit_code == 2
    assert result.outcome.status == REFUSED_LINKAGE
    # Exactly one create and exactly one delivery: no redelivery, no reclaim, no second claim.
    assert gateway.create_calls == 1
    assert len(receiver.requests) == 1


# --------------------------------------------------------------- 9. the fake-store happy path

def test_a_successful_fake_store_run_confirms_the_linkage(tmp_path):
    gateway, receiver = GatewayStub(), ReceiverStub()
    result = run_runner(tmp_path, confirm=True, gateway=gateway, http_client=receiver)
    assert result.exit_code == 0
    assert result.outcome.status == CONFIRMED_LINKED
    assert result.outcome.linkage_state == "linked"
    assert gateway.create_calls == 1
    assert len(receiver.requests) == 1

    outbox = Outbox(tmp_path / "outbox.sqlite3")
    rows = outbox.all()
    assert len(rows) == 1
    record = rows[0]
    assert record.status == "CONFIRMED"
    assert record.linkage == LINKAGE_SENT
    assert record.correlation_id == result.correlation_id == result.outcome.correlation_id
    # The bytes recorded before the attempt are exactly the bytes that went out.
    assert receiver.requests[0]["content"].decode("utf-8") == record.event_body
    assert record.event_id == result.outcome.event_id


# --------------------------------------------------------------- 10. the exclusive run lock

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE_NAME = ".scratch-purchase.lock"


def _lock_child_code(outbox: Path, mission: str, *, release: bool) -> str:
    """A separate process that tries once to take the lock, prints the verdict, and exits."""
    lines = [
        "import sys",
        "from pathlib import Path",
        f"sys.path.insert(0, {str(ROOT)!r})",
        "import scratch_purchase as runner",
        f"lock, problem = runner.acquire_run_lock(Path({str(outbox)!r}), mission_id={mission!r})",
        "if lock is None:",
        "    print('REFUSED')",
        "else:",
        "    print('ACQUIRED'); runner.release_run_lock(lock)" if release else "    print('ACQUIRED')",
    ]
    return "\n".join(lines)


def _run_child(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )


def test_two_separate_processes_cannot_both_take_the_run_lock(tmp_path):
    code = _lock_child_code(tmp_path / "outbox.sqlite3", MISSION_ID, release=False)
    first = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen([sys.executable, "-c", code],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first_out, first_err = first.communicate(timeout=120)
    second_out, second_err = second.communicate(timeout=120)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    # The exclusive create is atomic: exactly one of the two processes can ever win.
    assert sorted([first_out.strip(), second_out.strip()]) == ["ACQUIRED", "REFUSED"]


def test_a_held_lock_refuses_a_second_process_until_released(tmp_path):
    outbox = tmp_path / "outbox.sqlite3"
    lock, problem = runner.acquire_run_lock(outbox, mission_id=MISSION_ID)
    assert lock is not None, problem

    while_held = _run_child(_lock_child_code(outbox, MISSION_ID, release=False))
    assert while_held.returncode == 0, while_held.stderr
    assert while_held.stdout.strip() == "REFUSED"

    assert runner.release_run_lock(lock) is None
    assert not (tmp_path / LOCK_FILE_NAME).exists()

    after = _run_child(_lock_child_code(outbox, MISSION_ID, release=True))
    assert after.returncode == 0, after.stderr
    assert after.stdout.strip() == "ACQUIRED"
    assert not (tmp_path / LOCK_FILE_NAME).exists()


def test_a_different_mission_or_outbox_name_cannot_bypass_the_lock(tmp_path):
    lock, problem = runner.acquire_run_lock(tmp_path / "outbox.sqlite3", mission_id="mission_one")
    assert lock is not None, problem
    for label, other_outbox, other_mission in (
        ("different mission", tmp_path / "outbox.sqlite3", "mission_two"),
        ("different outbox name", tmp_path / "other.sqlite3", "mission_one"),
    ):
        second, problem = runner.acquire_run_lock(other_outbox, mission_id=other_mission)
        assert second is None, label
        assert "already holds the exclusive run lock" in (problem or ""), label
    assert (tmp_path / LOCK_FILE_NAME).exists()
    assert runner.release_run_lock(lock) is None


def test_a_left_behind_lock_fails_the_run_closed_and_is_never_auto_removed(tmp_path):
    lock_path = tmp_path / LOCK_FILE_NAME
    lock_path.write_text(
        "pid=999999\ntoken=not-this-run\nmission=mission_other\n"
        "acquired_at=2026-10-27T00:00:00+00:00\n",
        encoding="utf-8",
    )
    gateway, receiver = GatewayStub(), ReceiverStub()
    result = run_runner(tmp_path, confirm=True, gateway=gateway, http_client=receiver)
    assert result.exit_code == 1
    assert any("already holds the exclusive run lock" in problem for problem in result.problems)
    assert any("never removed automatically" in problem for problem in result.problems)
    assert gateway.create_calls == 0
    assert receiver.requests == []
    # Still there: the runner does not clean up a lock it does not own, however stale it looks.
    assert lock_path.exists()


def test_release_deletes_only_the_lock_this_run_still_owns(tmp_path):
    lock_path = tmp_path / LOCK_FILE_NAME

    lock, problem = runner.acquire_run_lock(tmp_path / "outbox.sqlite3", mission_id=MISSION_ID)
    assert lock is not None, problem
    assert runner.release_run_lock(lock) is None
    assert not lock_path.exists()

    # A lock whose content no longer carries our token is not ours to delete.
    lock, problem = runner.acquire_run_lock(tmp_path / "outbox.sqlite3", mission_id=MISSION_ID)
    assert lock is not None, problem
    lock_path.write_text("pid=1\ntoken=someone-else\n", encoding="utf-8")
    note = runner.release_run_lock(lock)
    assert note is not None and "no longer owned" in note
    assert lock_path.exists()

    # A lock that vanished is reported, not recreated and not deleted blindly.
    lock_path.unlink()
    note = runner.release_run_lock(runner.RunLock(path=lock_path, token=lock.token))
    assert note is not None and "already gone" in note


def test_the_run_lock_is_released_on_exception_and_cancellation(tmp_path):
    lock_path = tmp_path / LOCK_FILE_NAME

    def failing_loader(base_url, *, mission_id):
        raise RuntimeError("loader exploded")

    with pytest.raises(RuntimeError):
        asyncio.run(
            runner.run(env=dict(ENV), outbox_path=str(tmp_path / "outbox.sqlite3"),
                       mission_id=MISSION_ID, order_loader=failing_loader)
        )
    assert not lock_path.exists()

    def cancelling_loader(base_url, *, mission_id):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runner.run(env=dict(ENV), outbox_path=str(tmp_path / "outbox.sqlite3"),
                       mission_id=MISSION_ID, order_loader=cancelling_loader)
        )
    assert not lock_path.exists()


# --------------------------------------------------------------- 11. quantity is pinned

def test_order_gates_pin_quantity_to_exactly_one():
    assert runner.order_gates(make_facts(), mission_id=MISSION_ID, quantity=1,
                              expected_currency="INR",
                              expected_store_domain=STORE_DOMAIN).passed
    for bad in (2, 0, -1, True, "2"):
        report = runner.order_gates(make_facts(), mission_id=MISSION_ID, quantity=bad,
                                    expected_currency="INR",
                                    expected_store_domain=STORE_DOMAIN)
        assert not report.passed, bad
        assert any("pinned to 1" in problem for problem in report.problems), bad


def test_a_non_unit_quantity_never_reaches_fulfil(tmp_path):
    gateway, receiver = GatewayStub(), ReceiverStub()
    result = run_runner(tmp_path, confirm=True, quantity=3, gateway=gateway, http_client=receiver)
    assert result.exit_code == 1
    assert any("pinned to 1" in problem for problem in result.problems)
    assert gateway.create_calls == 0
    assert receiver.requests == []


def test_the_cli_has_no_quantity_flag(tmp_path):
    with pytest.raises(SystemExit):
        runner.main(
            ["--mission", MISSION_ID, "--outbox", str(tmp_path / "outbox.sqlite3"),
             "--quantity", "2"]
        )


# --------------------------------------------------------------- 12. the real order-source shape

# Representative bodies in the exact shape RESOLVE's own endpoints serve: `_order_response`
# (missions.py) for GET /missions/{id}/order, and the checkout review whose
# `shopping_intent.selected_product` is the server-persisted ProductResult dump for
# GET /missions/{id}/shopping/checkout.
ORDER_PAYLOAD = {
    "order_id": ORDER_ID,
    "mission_id": MISSION_ID,
    "status": "order_created",
    "product": {
        "product_id": "prod_scratch",
        "title": "Scratch Item",
        "merchant": "RESOLVE Merchant Test",
    },
    "amount": "1299.00",
    "currency": "INR",
    "razorpay_order_id": "order_testdisposable01",
    "razorpay_payment_id": "pay_testdisposable01",
    "created_at": "2026-10-28T00:00:00",
    "updated_at": "2026-10-28T00:00:00",
    "merchant_order_linked": False,
    "merchant_order_linkage": "unlinked",
    "test_mode": True,
    "fulfillment_note": "no merchant order, shipping, or delivery exists",
    "agent_message": None,
}

CHECKOUT_PAYLOAD = {
    "mission_id": MISSION_ID,
    "checkout": {"currency": "INR", "subtotal": "1299.00"},
    "confirmed": True,
    "mission_status": "order_created",
    "shopping_intent": {
        "state": "checkout_review",
        "selected_product": {
            "product_id": "prod_scratch",
            "title": "Scratch Item",
            "price": "1299.00",
            "currency": "INR",
            "merchant": "RESOLVE Merchant Test",
            "source": "shopify",
            "shopify_product_gid": PRODUCT_GID,
            "shopify_variant_gid": VARIANT_GID,
            "shopify_store_domain": STORE_DOMAIN,
            "requires_variant_clarification": False,
            "variant_clarification": None,
        },
    },
    "total_recovery_spend": "0",
}


class _FakeHTTPError(Exception):
    pass


def make_fake_httpx(order_result, checkout_result):
    """
    A stand-in for the httpx module with exactly the surface `fetch_order_facts` uses, routing
    the two real URL shapes to canned results: (status, payload) tuples, or an exception to
    raise. Every call's (url, headers) is recorded for assertions. No socket is opened anywhere.
    """

    class _Client:
        # Fresh class per make_fake_httpx call, so the record is per-fake.
        requests: list = []

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, headers=None):
            _Client.requests.append((url, dict(headers or {})))
            if url.endswith("/order"):
                result = order_result
            elif url.endswith("/shopping/checkout"):
                result = checkout_result
            else:
                raise AssertionError(f"unexpected URL requested: {url}")
            if isinstance(result, Exception):
                raise result
            status, payload = result
            return _Response(status, payload)

    return types.SimpleNamespace(
        Timeout=lambda *args, **kwargs: None,
        HTTPError=_FakeHTTPError,
        AsyncClient=_Client,
    )


def _patch_real_fetch(monkeypatch, order_result, checkout_result) -> None:
    # The real loader machine-authenticates before it asks; give it a disposable credential so
    # every scenario below exercises its own path rather than the fail-closed-on-missing-token
    # one, which has its own dedicated test.
    monkeypatch.setenv("MERCHANT_RESOLVE_SERVICE_TOKEN", "disposable-scratch-runner-machine-token")
    # fetch_order_facts now lives in merchant_channel.gates, so the fake transport must replace
    # the httpx that THAT module resolves — patching runner.httpx would leave the real client
    # in place and the tests would open sockets.
    monkeypatch.setattr(gate_module, "httpx", make_fake_httpx(order_result, checkout_result))


def _copy(payload: dict) -> dict:
    return json.loads(json.dumps(payload))


def _mutated_order(**changes) -> dict:
    payload = _copy(ORDER_PAYLOAD)
    for key, value in changes.items():
        if value is ...:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


def _mutated_selection(**changes) -> dict:
    payload = _copy(CHECKOUT_PAYLOAD)
    selected = payload["shopping_intent"]["selected_product"]
    for key, value in changes.items():
        if value is ...:
            selected.pop(key, None)
        else:
            selected[key] = value
    return payload


def test_fetch_order_facts_parses_the_real_response_shape(monkeypatch):
    _patch_real_fetch(monkeypatch, (200, _copy(ORDER_PAYLOAD)), (200, _copy(CHECKOUT_PAYLOAD)))
    facts, report = asyncio.run(
        runner.fetch_order_facts("http://host.docker.internal:8000", mission_id=MISSION_ID)
    )
    assert report.passed
    assert facts is not None
    assert facts.resolve_order_id == ORDER_ID
    assert facts.mission_id == MISSION_ID
    assert facts.status == "order_created"
    assert facts.currency == "INR"
    assert facts.razorpay_order_id == "order_testdisposable01"
    assert facts.razorpay_payment_id == "pay_testdisposable01"
    assert facts.merchant_order_linked is False
    assert facts.product_gid == PRODUCT_GID
    assert facts.variant_gid == VARIANT_GID
    assert facts.store_domain == STORE_DOMAIN
    assert facts.order_product_id == "prod_scratch"
    assert facts.selected_product_id == "prod_scratch"
    assert facts.title == "Scratch Item"


@pytest.mark.parametrize(
    "label, order_payload, checkout_payload",
    [
        ("razorpay_order_id-missing", _mutated_order(razorpay_order_id=...), CHECKOUT_PAYLOAD),
        ("razorpay_payment_id-missing", _mutated_order(razorpay_payment_id=...), CHECKOUT_PAYLOAD),
        ("status-missing", _mutated_order(status=...), CHECKOUT_PAYLOAD),
        ("product-gid-missing", ORDER_PAYLOAD, _mutated_selection(shopify_product_gid=...)),
        ("variant-gid-missing", ORDER_PAYLOAD, _mutated_selection(shopify_variant_gid=...)),
        ("store-domain-missing", ORDER_PAYLOAD, _mutated_selection(shopify_store_domain=...)),
    ],
)
def test_a_renamed_or_missing_server_field_fails_closed(
    monkeypatch, tmp_path, label, order_payload, checkout_payload
):
    _patch_real_fetch(monkeypatch, (200, order_payload), (200, _copy(checkout_payload)))
    gateway, receiver = GatewayStub(), ReceiverStub()
    result = asyncio.run(
        runner.run(
            env=dict(ENV),
            outbox_path=str(tmp_path / "outbox.sqlite3"),
            mission_id=MISSION_ID,
            confirm=True,
            gateway=gateway,
            http_client=receiver,
        )
    )
    assert result.exit_code == 1, label
    assert result.problems, label
    assert gateway.create_calls == 0, label
    assert receiver.requests == [], label


def test_a_missing_selection_fails_the_real_loader(monkeypatch):
    checkout = _copy(CHECKOUT_PAYLOAD)
    del checkout["shopping_intent"]["selected_product"]
    _patch_real_fetch(monkeypatch, (200, _copy(ORDER_PAYLOAD)), (200, checkout))
    facts, report = asyncio.run(
        runner.fetch_order_facts("http://host.docker.internal:8000", mission_id=MISSION_ID)
    )
    assert facts is None
    assert not report.passed
    assert any("no persisted selection" in problem for problem in report.problems)


def test_a_failed_or_unreachable_order_endpoint_fails_closed(monkeypatch):
    _patch_real_fetch(monkeypatch, (404, {"detail": "not found"}), (200, _copy(CHECKOUT_PAYLOAD)))
    facts, report = asyncio.run(
        runner.fetch_order_facts("http://host.docker.internal:8000", mission_id=MISSION_ID)
    )
    assert facts is None
    assert any("HTTP 404" in problem for problem in report.problems)

    _patch_real_fetch(
        monkeypatch, _FakeHTTPError("connection refused"), (200, _copy(CHECKOUT_PAYLOAD))
    )
    facts, report = asyncio.run(
        runner.fetch_order_facts("http://host.docker.internal:8000", mission_id=MISSION_ID)
    )
    assert facts is None
    assert any("could not be asked" in problem for problem in report.problems)


def test_fetch_order_facts_fails_closed_without_a_service_token(monkeypatch):
    monkeypatch.delenv("MERCHANT_RESOLVE_SERVICE_TOKEN", raising=False)
    fake = make_fake_httpx((200, _copy(ORDER_PAYLOAD)), (200, _copy(CHECKOUT_PAYLOAD)))
    monkeypatch.setattr(gate_module, "httpx", fake)
    facts, report = asyncio.run(
        runner.fetch_order_facts("http://host.docker.internal:8000", mission_id=MISSION_ID)
    )
    assert facts is None
    assert not report.passed
    assert any(
        "MERCHANT_RESOLVE_SERVICE_TOKEN" in problem for problem in report.problems
    )
    # Failing closed means failing BEFORE asking: no request was even attempted.
    assert fake.AsyncClient.requests == []


def test_fetch_order_facts_sends_the_service_token_header(monkeypatch):
    token = "disposable-scratch-runner-machine-token"
    monkeypatch.setenv("MERCHANT_RESOLVE_SERVICE_TOKEN", token)
    fake = make_fake_httpx((200, _copy(ORDER_PAYLOAD)), (200, _copy(CHECKOUT_PAYLOAD)))
    monkeypatch.setattr(gate_module, "httpx", fake)
    facts, report = asyncio.run(
        runner.fetch_order_facts("http://host.docker.internal:8000", mission_id=MISSION_ID)
    )
    assert report.passed
    assert facts is not None
    assert [url for url, _ in fake.AsyncClient.requests] == [
        f"http://host.docker.internal:8000/missions/{MISSION_ID}/order",
        f"http://host.docker.internal:8000/missions/{MISSION_ID}/shopping/checkout",
    ]
    assert [headers for _, headers in fake.AsyncClient.requests] == [
        {"X-Resolve-Service-Token": token},
        {"X-Resolve-Service-Token": token},
    ]


def test_the_real_fetch_feeds_the_real_gates_dry_run(monkeypatch, tmp_path):
    _patch_real_fetch(monkeypatch, (200, _copy(ORDER_PAYLOAD)), (200, _copy(CHECKOUT_PAYLOAD)))
    result = asyncio.run(
        runner.run(
            env=dict(ENV),
            outbox_path=str(tmp_path / "outbox.sqlite3"),
            mission_id=MISSION_ID,
            confirm=False,
        )
    )
    assert result.exit_code == 0
    assert result.dry_run is True
    assert result.outcome is None
    # The parser's own output derives exactly the correlation the hand-built facts would.
    expected = derive_correlation(runner.handoff_from_facts(make_facts(), quantity=1))
    assert result.correlation_id == expected


def test_the_real_fetch_feeds_the_real_gates_through_a_confirmed_run(monkeypatch, tmp_path):
    _patch_real_fetch(monkeypatch, (200, _copy(ORDER_PAYLOAD)), (200, _copy(CHECKOUT_PAYLOAD)))
    gateway, receiver = GatewayStub(), ReceiverStub()
    result = asyncio.run(
        runner.run(
            env=dict(ENV),
            outbox_path=str(tmp_path / "outbox.sqlite3"),
            mission_id=MISSION_ID,
            confirm=True,
            gateway=gateway,
            http_client=receiver,
        )
    )
    assert result.exit_code == 0
    assert result.outcome.status == CONFIRMED_LINKED
    assert gateway.create_calls == 1
    assert gateway.create_quantities == [1]
    assert len(receiver.requests) == 1
