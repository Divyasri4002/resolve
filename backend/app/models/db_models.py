"""
SQLAlchemy ORM models — the actual Postgres tables.

The tables RESOLVE keeps state in:
  - missions       : current state of each case (mutable — status changes as the mission runs)
  - messages       : the full negotiation log (append-only in practice, never edited)
  - audit_log      : INSERT-ONLY, enforced at the application layer. One row per Authority Engine
                     decision (approved or blocked). This is the table a human or a regulator
                     reads to answer "why was this action allowed to happen."
  - resolve_orders : the post-payment RESOLVE order lifecycle record (one per shopping mission,
                     immutable canonical snapshot of what was paid for)
  - order_issues   : post-purchase issue intake on a confirmed RESOLVE order (Task 09) — the
                     persistent context future deterministic resolution will use
  - resolution_assessments : Task 10 assessment records — one per issue (unique issue_id):
                     the validated reasoning-model proposal plus the deterministic Authority
                     Engine decision on it. Proposals and decisions only; NOTHING here is executed.
  - refunds        : Task 11A controlled Razorpay TEST-MODE refund executions — one per issue
                     (unique issue_id, the idempotency gate). Amount/currency/payment reference
                     are copied from the canonical persisted payment; the client supplies none
                     of them. `status` only reaches 'verified' after a provider status check.
  - replacements   : Task 12A controlled replacement executions — one per issue (unique
                     issue_id). Product identity comes from the canonical persisted order
                     snapshot, and `requested`/`confirmed` require a real merchant provider:
                     with no provider connected the row stays 'pending' and says so.
  - resolutions    : Task 13 orchestration summary per issue (unique issue_id) — which action
                     the Authority Engine allowed right now and which controlled executor ran.
                     Detail stays on the refund/replacement/return rows, never duplicated here.
  - returns        : Task 12B controlled return executions — one per issue (unique issue_id).
                     Same rule as replacements: `requested` needs a real provider ack and
                     `confirmed` needs that provider's later confirmation, so with no provider
                     connected the row stays 'pending' and nothing is claimed.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MissionRecord(Base):
    __tablename__ = "missions"

    mission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    mission_type: Mapped[str] = mapped_column(String(32), default="recovery", server_default="recovery", index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)

    # Stored as JSON blobs — the Pydantic models in app/models/mission.py are the source of
    # truth for shape/validation; Postgres just persists their serialized form.
    mandate: Mapped[dict] = mapped_column(JSON)
    transaction: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    shopping_intent: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    total_recovery_spend: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    resolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class MessageRecord(Base):
    __tablename__ = "messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    sender: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)  # full NegotiationMessage, serialized
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class AuditLogRecord(Base):
    """
    Insert-only by convention and by application-layer enforcement (see app/audit/logger.py,
    which exposes only an `append` method — no update, no delete). Every Authority Engine
    decision, approved or blocked, produces exactly one row here.
    """

    __tablename__ = "audit_log"

    entry_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"audit_{uuid.uuid4().hex[:12]}"
    )
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(32))  # "buyer_agent" | "merchant_agent"
    action: Mapped[str] = mapped_column(String(64))  # e.g. "pay_recovery_cost"
    requested_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    decision: Mapped[str] = mapped_column(String(16))  # "approved" | "blocked"
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ResolveOrderRecord(Base):
    """
    The persistent RESOLVE order record (Task 08) — the deterministic layer after
    payment_verified. One row per shopping mission (mission_id is unique): Razorpay TEST
    payment verification is NOT a merchant order, so this table records RESOLVE's own
    order/payment lifecycle context rather than inventing fulfillment.

    Every monetary/identity value here is copied from the mission's ALREADY-PERSISTED
    payment and selected_product state at creation time — the client never supplies
    authoritative amount, currency, product data, or Razorpay ids.
    """

    __tablename__ = "resolve_orders"

    order_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"order_{uuid.uuid4().hex[:12]}"
    )
    mission_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    # Canonical product identity, copied from the persisted selected_product
    product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    merchant: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product: Mapped[dict] = mapped_column(JSON, default=dict)  # full selected_product snapshot

    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), default="INR")

    # Razorpay TEST-MODE references, copied from the verified payment record
    razorpay_order_id: Mapped[str] = mapped_column(String(64))
    razorpay_payment_id: Mapped[str] = mapped_column(String(64))

    # Link to the merchant system's OWN order id, if one has been established server-side.
    # Nothing in any request body can set this: an executor may only submit a merchant-side
    # mutation for an order whose reference was persisted here by an integration, never for one
    # it inferred from a product title, amount, or timestamp. Absent means "cannot be returned
    # through a merchant provider", which is a statement of capability, not a failure.
    # UNIQUE (NULLs distinct): one RESOLVE order carries at most one merchant order, and one
    # merchant order backs at most one RESOLVE order. Written only by
    # `app.tools.shopify.linkage`, which verifies the reference against Shopify first.
    provider_order_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Shopify CATALOG identity, copied from the canonical persisted selected_product (Task 14C.7D).
    # Server-derived, never client-writable, and never inferred from a title, a price or a
    # timestamp: a GID on this row is a value Shopify itself said about a product in its own
    # store. They are deliberately separate from the merchant-order linkage below — knowing which
    # product was bought is not knowing which merchant order holds it. Nullable and never
    # backfilled: a Google Shopping order has no Shopify identity, and giving it one would
    # manufacture a pairing nothing verified.
    shopify_product_gid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shopify_variant_gid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shopify_store_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Demo-environment delivery facts (M22 §1). Set exactly once by the server when the
    # customer's reload persists the demo transition off order_confirmed; delivery_mode names it
    # ("demo_simulated") so a simulated delivery can never be mistaken for a carrier- or
    # merchant-confirmed one. Nullable and never backfilled: an order still awaiting its first
    # reload has neither, and a future production fulfilment integration would write a different
    # mode, not reuse this one.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivery_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index(
            "uq_resolve_orders_provider_order_reference",
            "provider_order_reference",
            unique=True,
        ),
    )

    status: Mapped[str] = mapped_column(String(32), default="order_created", index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class OrderIssueRecord(Base):
    """
    A post-purchase issue reported against a confirmed RESOLVE order (Task 09).

    This is intake-only context: the stored record plus its deterministic lifecycle
    (reported -> under_review -> resolution_pending) is what future resolution
    capabilities will reason over. It records the user's issue and their resolution
    PREFERENCE — never an authorization, so there is no refund/replacement state and
    no amount column. order_id / mission_id / user_id are resolved server-side from
    the authenticated mission; the client cannot supply authoritative identity,
    payment references, or decisions.
    """

    __tablename__ = "order_issues"

    issue_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"issue_{uuid.uuid4().hex[:12]}"
    )
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    issue_type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    requested_resolution: Mapped[str] = mapped_column(String(32), default="none")
    status: Mapped[str] = mapped_column(String(32), default="reported", index=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)

    # Task 14B.5A — automated-decision opt-out. A customer who does not want an automated
    # resolution decision can request manual review; while active the orchestrator refuses to
    # run autonomously for this issue. It records the CUSTOMER'S REQUEST only — no human action
    # is claimed, no external ticket exists, and no provider is contacted because of it.
    manual_review_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manual_review_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # Task CORE-2 (Phase 13) — the one question the agent has asked about this issue and not yet
    # received an answer to. Nullable, and cleared when the answer arrives and the issue is
    # re-assessed: it is what makes a more-information request a resumable state instead of a dead
    # end. It holds the question's text only — no credentials, no provider payload.
    more_information_question: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ResolutionAssessmentRecord(Base):
    """
    One resolution assessment per issue (Task 10).

    Holds the validated reasoning-model proposal and the deterministic Authority Engine
    decision that gated it. `authority_decision` is "allow" | "deny" | "escalate" — "allow"
    means the proposal PASSED the authority gate, NOT that a refund/replacement was
    executed (execution does not exist in this system yet). issue_id is unique: the
    DB-level idempotency gate that stops uncontrolled repeat financial proposals.
    """

    __tablename__ = "resolution_assessments"

    assessment_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"assess_{uuid.uuid4().hex[:12]}"
    )
    issue_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    proposed_action: Mapped[str] = mapped_column(String(48))
    issue_assessment: Mapped[str] = mapped_column(Text)
    reasoning_summary: Mapped[str] = mapped_column(Text)
    requested_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_escalation: Mapped[bool] = mapped_column(default=False)
    proposal_source: Mapped[str] = mapped_column(String(32))  # "reasoning_model" | "safe_fallback"

    authority_decision: Mapped[str] = mapped_column(String(16))  # allow | deny | escalate
    decision_reason: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class RefundRecord(Base):
    """
    One controlled Razorpay TEST-MODE refund execution per issue (Task 11A).

    issue_id is unique: it is the DB-level idempotency gate that makes "one refund per
    issue" a storage invariant, not a frontend convention. Every authoritative value
    (amount, currency, Razorpay payment reference) is copied from the canonical persisted
    payment at execution time — the client supplies none of them, and there is no request
    body to tamper with. `status` becomes 'verified' only after a fresh provider status
    check reports the refund processed; the create response alone is never proof.
    """

    __tablename__ = "refunds"

    refund_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"refund_{uuid.uuid4().hex[:12]}"
    )
    issue_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    razorpay_payment_id: Mapped[str] = mapped_column(String(64), index=True)
    razorpay_refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    amount_paise: Mapped[int] = mapped_column(Integer)  # integer minor units — never floats
    currency: Mapped[str] = mapped_column(String(8), default="INR")

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ReplacementRecord(Base):
    """
    One controlled replacement execution per issue (Task 12A).

    issue_id is unique: the DB-level gate that makes "one replacement per issue" a storage
    invariant. The product being replaced is copied from the canonical persisted order
    snapshot — the client never supplies a product id, merchant, quantity, or provider
    reference, and there is no request body to tamper with.

    `status` advances past 'pending' ONLY on a real provider response: 'requested' means a
    connected merchant provider accepted and referenced the replacement, 'confirmed' means that
    provider later confirmed it. With no provider connected the row correctly stays 'pending'
    with the reason recorded — which is a statement of capability, never a completed action.
    """

    __tablename__ = "replacements"

    replacement_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"repl_{uuid.uuid4().hex[:12]}"
    )
    issue_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    # Canonical original product, copied from the persisted order snapshot
    original_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_title: Mapped[str] = mapped_column(String(512))
    original_merchant: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The unit being requested as a replacement — resolved server-side, never client-supplied
    replacement_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[float] = mapped_column(Numeric(12, 2))  # canonical order value, for audit
    currency: Mapped[str] = mapped_column(String(8), default="INR")

    provider: Mapped[str] = mapped_column(String(32), default="none")
    provider_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)

    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ReturnRecord(Base):
    """
    One controlled return execution per issue (Task 12B).

    issue_id is unique: the DB-level gate that makes "one return per issue" a storage
    invariant. The product being returned is copied from the canonical persisted order
    snapshot — the client never supplies a product id, order id, amount, or provider
    reference, and there is no request body to tamper with.

    `status` advances past 'pending' ONLY on a real provider response: 'requested' means a
    connected merchant provider accepted the return and gave it a reference, 'confirmed' means
    that provider later confirmed it. With no provider connected the row correctly stays
    'pending' with the reason recorded — a statement of capability, never a completed action.

    Task 14C.4 added the submission claim: the `provider_*` columns record WHAT WAS SENT (or
    may have been sent) and `submission_claim` records WHICH ATTEMPT is allowed to send it.
    Together they let a lost answer be reconciled by reading the merchant back, instead of
    re-submitting a mutation that might already have landed. They hold merchant-side
    references, an integer unit count and a reason token — never an access token, a client
    secret, an authorization header, or customer personal data.
    """

    __tablename__ = "returns"

    return_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"ret_{uuid.uuid4().hex[:12]}"
    )
    issue_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    # Canonical product being returned, copied from the persisted order snapshot
    original_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_title: Mapped[str] = mapped_column(String(512))

    provider: Mapped[str] = mapped_column(String(32), default="none")
    provider_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # The submission claim, persisted BEFORE any mutation is attempted (Task 14C.4).
    provider_order_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_line_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submission_claim: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submission_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ResolutionRecord(Base):
    """
    One persisted orchestration outcome per issue (Task 13).

    issue_id is unique, so repeated POST /resolve updates this row instead of accumulating
    resolution attempts. It deliberately stores only the SUMMARY — which action was chosen, what
    the Authority Engine decided right now, which controlled executor was called, and which of
    that executor's records holds the authoritative detail. Amounts, provider references and
    provider statuses continue to live on the refund/replacement/return rows, not here.
    """

    __tablename__ = "resolutions"

    resolution_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"res_{uuid.uuid4().hex[:12]}"
    )
    issue_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    action: Mapped[str] = mapped_column(String(48))
    authority_decision: Mapped[str] = mapped_column(String(16))
    executor: Mapped[str] = mapped_column(String(16), default="none")
    executor_record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_state: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class UserRecord(Base):
    """
    Application-owned user account. Stores credentials securely with bcrypt hash.
    Never returns password_hash to the client.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"user_{uuid.uuid4().hex[:12]}"
    )
    name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class IntegrationEventRecord(Base):
    """
    One row per accepted-and-validated order-linkage integration event (Task 14C.7B).

    This is a narrow replay guard, not an event bus: it exists so that a signed event can be
    delivered twice — because the sender retried, or because the sender never saw our answer —
    without ever producing a second linkage. `event_id` is unique, and that constraint is the
    arbiter under concurrency; nothing here would be safe to rely on as a queue.

    Deliberately absent: the HMAC signature, the shared secret, the raw request body, the full
    Shopify payload, and any customer field. What is stored is the event's identity, a SHA-256 of
    its canonical body (so "same id, different payload" is detectable), the two identifiers the
    event was about, and the outcome. Nothing that could be replayed by someone who read this
    table, and nothing that would matter to them if they did.
    """

    __tablename__ = "integration_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    body_sha256: Mapped[str] = mapped_column(String(64))

    # What the event named, and what it resolved to. Both are references, never authorities:
    # the linkage itself is written only by app.tools.shopify.linkage.
    mission_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    resolve_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider_order_reference: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    # 'linked' | 'already_linked' | 'rejected' — recorded only after authentication, validation
    # and Shopify's own confirmation, never before.
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentConversationRecord(Base):
    """
    One turn of the agent's own conversation (Task CORE-2, Phase 6) — append-only by the same
    convention that makes `audit_log` append-only: `app.agents.conversation` exposes `append_event`
    and a read, and no update or delete anywhere in the application uses this table.

    Why a table at all: before this, the agent's memory was one HTTP request long. A reload lost
    the transcript, and the next turn had no idea what it had already asked. This is also what lets
    a bounded tool loop be *resumed* — the pending question is a row, not a hope.

    What is stored is deliberately narrow: the speaker's role, which of the seven event kinds it
    is, the text a human would recognize, the tool name when the row is about a tool, and a bounded
    safe summary. Sequence is unique per mission so the order is a fact of storage rather than a
    timestamp two rows might share. Never stored: an access token, a key secret, a HMAC secret, an
    authorization header, a password hash, a raw provider payload, or a chain of thought.
    """

    __tablename__ = "agent_conversation"

    event_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"evt_{uuid.uuid4().hex[:12]}"
    )
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    # Monotonic per mission, assigned server-side at append time.
    sequence: Mapped[int] = mapped_column(Integer)

    # "user" | "agent" | "system"
    role: Mapped[str] = mapped_column(String(16))
    # "user_message" | "agent_message" | "tool_call" | "tool_result" | "system_state_change"
    # | "product_reference" | "issue_reference" — the vocabulary lives in
    # app.agents.conversation.EVENT_TYPES and nowhere else.
    event_type: Mapped[str] = mapped_column(String(32), index=True)

    content: Mapped[str] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # A bounded, secret-free summary of the event — never a database row and never a provider body.
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("uq_agent_conversation_mission_sequence", "mission_id", "sequence", unique=True),
    )


class PurchaseAuthorizationRecord(Base):
    """
    The ONE merchant-purchase authorization per RESOLVE order (buyer-flow Phase 3).

    order_id is unique: it is the DB-level gate that makes "one purchase authorization per
    order" a storage invariant, not a convention. The record is the claim the backend
    persists BEFORE the trigger is called — so a crash, a lost answer, or a refused
    submission leaves an honest record of what was authorized and what was (or may have
    been) attempted, and no second claim can ever be created for the same order.

    The Authority Engine decision stored here was made on server-derived facts only. The
    trigger-reported fields are explicitly an UNTRUSTED REPORT of what the standalone
    merchant channel claimed: they never confer linkage, and the authoritative merchant
    linkage remains resolve_orders.provider_order_reference, written only by the linkage
    receiver. Deliberately absent: any amount, currency or payment reference (the canonical
    values live on the order row alone — nothing here duplicates them), and any merchant
    credential, URL, or token.
    """

    __tablename__ = "purchase_authorizations"

    authorization_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"purch_{uuid.uuid4().hex[:12]}"
    )
    order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mission_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    # The deterministic gate's verdict on this order at authorization time
    authority_decision: Mapped[str] = mapped_column(String(16))  # approved | blocked
    decision_reason: Mapped[str] = mapped_column(Text)

    # PurchaseAuthorizationStatus vocabulary: authorized | purchase_failed |
    # purchase_unknown | purchase_reported_linked | linkage_refused
    status: Mapped[str] = mapped_column(String(32), default="authorized", index=True)

    # The submission lease (Phase 4.4): exactly ONE backend request may have a purchase
    # submission in flight per authorization. Taking the lease is a single atomic
    # conditional UPDATE (idle -> in_flight + owner token + attempt_count increment), so
    # the database row lock — not any process-local lock — arbitrates concurrent requests.
    # Values: idle | in_flight | settled. The live owner settles the lease when it applies
    # the trigger's report; a NOT_SENT report releases it to idle (the claim stays
    # resubmittable). An in_flight lease abandoned by a crashed backend is POTENTIALLY
    # SENT — persisted state cannot prove otherwise — so recovery may only close the claim
    # as purchase_unknown (terminal): never reset to idle, never auto-retried.
    submission_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle", server_default="idle"
    )
    submission_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # How many accepted submission attempts this claim has made: each lease acquisition —
    # the single atomic gate before the trigger is called — increments it exactly once.
    # Only a claim in 'authorized' may be submitted again; terminal states never re-submit.
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_submission_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # UNTRUSTED trigger report — stored verbatim under trigger_reported_ names so no reader
    # can mistake a channel claim for verified merchant state.
    trigger_reported_outcome_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger_reported_outcome_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger_reported_correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trigger_reported_external_order_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    trigger_reported_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trigger_reported_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Why a submission did not run at all (connection refused before send, trigger
    # configuration refusing pre-execution). Cleared when a submission does run.
    error_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
