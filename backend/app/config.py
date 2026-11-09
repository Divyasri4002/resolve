"""
Application configuration.

Everything RESOLVE needs to know to talk to Postgres, Razorpay (test mode), and a reasoning
provider is centralized here, loaded from environment variables. Nothing here touches money or
approves actions — that is the Authority Engine's job, and its job alone.
"""
from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://resolve:resolve@localhost:5432/resolve"

    # Razorpay — TEST MODE
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # Reasoning provider
    reasoning_provider: str = "mock"
    reasoning_api_key: str = ""
    reasoning_model: str = "gemini-3.6-flash"
    # Groq (the OpenAI-compatible provider selected with REASONING_PROVIDER=groq) has its own
    # credential and model names, so a Gemini key and a Groq key can coexist in one .env and
    # switching providers never means re-entering a secret. The default model must stay one the
    # live account actually serves: gpt-oss-120b was confirmed against GET /models on 2026-10-02.
    reasoning_groq_api_key: str = ""
    reasoning_groq_model: str = "openai/gpt-oss-120b"

    # Bounded retry for a transient provider refusal (Task CORE-2.1). A rate-limited or overloaded
    # model answers 429/5xx; one attempt is not enough to tell that apart from an outage, and an
    # unbounded loop is a self-inflicted denial-of-service against your own quota. So: this many
    # RETRIES after the first attempt, waiting base * 2**attempt with jitter, and never longer than
    # base * 2**max_retries. A 4xx that is not 429 (a rejected request, a bad key) is NOT retried —
    # asking again would hide a real defect and cost quota to do it. Exhaustion raises, and the agent
    # loop degrades honestly from there; nothing here switches provider or lowers a guard.
    reasoning_max_retries: int = Field(default=2, ge=0, le=4)
    reasoning_retry_base_seconds: float = Field(default=0.5, gt=0, le=5)

    # Bounded agent loop (Task CORE-2). One user message may make the agent take at most this many
    # declared tool steps before it must answer the user. It is a ceiling on reasoning, not a
    # licence: every step still passes its own operation's guards and the Authority Engine, and
    # running out of steps ends the turn with a request for the user, never with a partial action.
    agent_max_steps: int = Field(default=8, ge=1, le=24)

    # Product search provider
    product_search_provider: str = "serpapi"
    serpapi_api_key: str = ""
    serpapi_base_url: str = "https://serpapi.com/search"

    # App
    env: str = "development"
    log_level: str = "INFO"

    # Bounded refund authority (Task 11B) — server-side policy, never model- or client-controlled.
    # Off by default: with authority disabled a request_refund proposal is DENIED and no Razorpay
    # call is made. The ceiling is a per-refund maximum in INR; a mandate may only narrow it via
    # its persisted Authority.refund_limit, never raise it.
    shopping_refund_authority_enabled: bool = False
    shopping_refund_max_inr: Decimal = Field(default=Decimal("500"), gt=0)

    # Bounded replacement authority (Task 11B's sibling, Task 12A) — a replacement reverses no
    # money, so there is deliberately no amount ceiling: the gate is (a) this switch, off by
    # default, and (b) the mandate having delegated request_replacement at all.
    # replacement_provider names the connected merchant fulfilment provider; the only built-in
    # is "none", which refuses explicitly rather than pretending a merchant was contacted.
    shopping_replacement_authority_enabled: bool = False
    replacement_provider: str = "none"

    # Bounded return authority (Task 12B) — its own switch and its own provider, deliberately
    # unrelated to refund/replacement authority and to recovery_budget: a return hands goods
    # back and moves no money, so it consumes no budget and has no amount ceiling.
    # OFF by default: request_return is denied and no provider is contacted.
    shopping_return_authority_enabled: bool = False
    return_provider: str = "none"

    # Bounded merchant-purchase authority (buyer-flow Phase 3) — the user's own POST endpoint
    # is the only purchase authorization boundary; the reasoning model can never propose or
    # authorize a purchase. OFF by default: with the switch off, place_merchant_order is
    # denied for every order and no trigger is contacted. The trigger URL/token name the
    # standalone merchant-channel trigger service (a separate process) and are ENVIRONMENT
    # ONLY — never exposed through API responses, logs, frontend configuration, or agent
    # context. Empty URL or token fails closed: the endpoint refuses with 503 before any
    # authorization claim is persisted and before any HTTP call is attempted.
    merchant_trigger_url: str = ""
    merchant_trigger_token: str = ""
    shopping_purchase_authority_enabled: bool = False

    # Shopify READ-ONLY connectivity (Task 14B.3). Deliberately not connected to any executor,
    # the Authority Engine, or the Resolution Orchestrator: it can authenticate and read, and
    # nothing else. Credentials come from the environment only — never from code, logs,
    # responses, or .env.example — and the client secret / access token never leave memory.
    # The domain is the public store hostname (not a secret); the two credential values have
    # NO default on purpose, so an unconfigured deployment cannot accidentally reach Shopify.
    shopify_store_domain: str = "resolve-commerce-lab.myshopify.com"
    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    shopify_api_version: str = "2026-07"

    # Retention for SHOPIFY-DERIVED protected data (Task 14B.5A). A conservative development/
    # testing default; deployment may shorten it, never extend it silently. This governs only
    # data derived from Shopify order/return reads — NOT RESOLVE missions, payments, refunds or
    # audit records, which are protected core records with their own lifecycle and are never
    # deleted by this policy (see app/retention/service.py).
    shopify_retention_days: int = Field(default=30, gt=0)

    # Order-linkage integration (Task 14C.7B) — the HMAC key an authenticated merchant-side
    # integration signs its linkage events with, so RESOLVE can record which Shopify order
    # corresponds to which RESOLVE order. Empty by default, which DISABLES the
    # endpoint outright: an unconfigured deployment refuses with 503 rather than accepting unsigned
    # events. The value is environment-only — never stored in the database, never returned in a
    # response, never logged. Possessing it authenticates the SENDER; it authorizes nothing else:
    # every event still has to name a real RESOLVE order and a Shopify order that Shopify itself
    # confirms, and `link_order_to_shopify_order` stays the only writer of the linkage.
    linkage_integration_secret: str = ""
    # How far in the past (or clock-skew-ahead) an event may be issued and still be accepted.
    # Bounds replay of a captured signed body; the event_id unique index does the rest.
    linkage_event_max_age_seconds: int = Field(default=300, gt=0, le=86400)

    # Machine-to-machine credential for the scratch purchase runner's read-only preflight —
    # the only caller that presents it. Empty by default, which DISABLES the service read path
    # outright, the same disable-by-default rule as linkage_integration_secret: an unconfigured
    # deployment refuses machine reads instead of trusting a header nobody checks. Environment
    # only — never committed, printed, logged, or returned in a response. Possessing it
    # authenticates the machine for exactly two read operations (a store-associated mission's
    # canonical order and checkout selection); it authorizes no mutation, no payment action, no
    # listing, and it can never authenticate as a user (different header, different token
    # format, no session semantics).
    service_runner_token: str = ""

    # External evidence publisher (Phase 4.6.2, approved 4.5.6-4.5.9 design). The object
    # store holds the WORM head object the publisher advances one conditional PUT per
    # committed ledger event. Endpoint/bucket/access/secret are environment-only — never
    # stored in the database, never returned in a response, never logged. Empty endpoint
    # or bucket DISABLES the store outright (the same disable-by-default rule as
    # linkage_integration_secret): the publisher does not start and the submission
    # currency gate skips honestly, rather than publishing to a store nobody configured.
    # The credentials authorize exactly one bucket/key path; they must never be a
    # production bucket in development.
    ledger_object_store_endpoint: str = ""
    ledger_object_store_region: str = "us-east-1"
    ledger_object_store_bucket: str = ""
    ledger_object_store_access_key: str = ""
    ledger_object_store_secret_key: str = ""
    ledger_object_store_head_key: str = "head.json"
    ledger_object_store_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # Publisher loop: OFF by default. When enabled it is a single-instance asyncio task
    # whose cross-process serialization is the ledger_publish_state lease, and the
    # submission currency gate — not this switch — is what keeps purchase submissions
    # honest while the store is unreachable. Bounded values only; an unbounded poll or
    # lease is a self-inflicted denial-of-service against the store.
    ledger_publish_enabled: bool = False
    ledger_publisher_poll_seconds: float = Field(default=5.0, gt=0, le=300)
    ledger_publisher_lease_seconds: float = Field(default=60.0, gt=0, le=600)

    # Auth & CORS
    auth_secret: str = "resolve-auth-secret-key-change-in-production"
    auth_cookie_name: str = "resolve_session"
    auth_cookie_max_age_seconds: int = 604800  # 7 days
    frontend_origin: str = "http://localhost:3000"

    # M21 Phase 2A live-stream guards. The stream's delivery contract is durable conversation
    # rows plus client-side replay, so these bounds protect the SERVER, not delivery: the
    # connection cap stops one mission from accumulating unbounded sockets, and the send timeout
    # bounds how long a single consumer that stopped reading its socket can stall the request
    # that committed the event it is being handed.
    ws_max_connections_per_mission: int = Field(default=8, ge=1, le=64)
    ws_send_timeout_seconds: float = Field(default=5.0, gt=0, le=60)


@lru_cache
def get_settings() -> Settings:
    return Settings()
