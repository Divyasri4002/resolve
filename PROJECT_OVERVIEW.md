# PROJECT OVERVIEW

**RESOLVE** is an agent that takes over the moment a payment completes. It holds the buyer's
objective, records problems with a delivered order, assesses remedies under an explicit
authority model, and executes only what the Authority Engine explicitly permits — with every
state change recorded in an append-only audit log.

This document describes what the system **actually does today**, as verified against the
running deployment on 2026-11-14. Anything not yet working is stated plainly in
[LIMITATIONS_AND_FUTURE_SCOPE.md](LIMITATIONS_AND_FUTURE_SCOPE.md).

## What the product is

A buyer chats with RESOLVE in a mission workspace. A mission carries a shopping intent from
product discovery through payment to post-purchase problem resolution:

1. **Discovery** — the buyer states a goal in natural language ("find me a stainless steel
   water bottle under ₹500"). RESOLVE searches live product listings (SerpAPI Google Shopping),
   presents results with prices, merchants and delivery notes, and lets the buyer select one.
2. **Checkout & payment** — a server-derived checkout review; payment runs through **Razorpay
   TEST mode only** (HMAC-SHA256 signature verified server-side; no real money ever moves).
3. **Order record** — the backend creates a permanent RESOLVE order record; delivery in this
   deployment is **demo-simulated** and labelled as such everywhere it is displayed.
4. **Post-purchase issues** — the buyer reports a problem (damaged, wrong item, etc.). RESOLVE
   asks category-specific intake questions, accepts text answers and photo evidence, and runs
   a **resolution assessment** through a reasoning model.
5. **Authority-gated resolution** — every proposed remedy passes two independent checks:
   **Layer A** (per-remedy eligibility) and **Layer B** (the deterministic Authority Engine:
   delegated mandate, ceilings, execution-time re-check). When authority is missing, the
   decision is **ESCALATE** — never a silent denial and never an execution.
6. **Execution** — only when the Authority Engine explicitly allows an action does the backend
   execute it. A permitted refund executes against Razorpay TEST mode; a replacement or return
   requires a connected fulfilment provider, and without one the intent is recorded as pending
   with nothing claimed.

## What is verified live (2026-11-14, real browser against the running deployment)

- Product discovery: natural-language query returned 5 real results with prices/merchants; selection persisted ("Selected").
- Razorpay TEST payment: order created, payment completed through Razorpay's test checkout, signature verified server-side (`pay_TywmLFU3oGnhBo`, ₹69, HMAC-SHA256 verified); a TEST-mode refund (`rfnd_Tyvnf5oHEKqJXU`, ₹799) was previously executed and reported by Razorpay as processed.
- Issue intake: report → category questions → recorded answers (persisted, "authorizes nothing").
- Evidence upload: a real PNG was attached through the browser, validated (type/size/structure), stored in MinIO (sha256-pinned), retrieved through an authenticated endpoint, and re-read from MinIO with the digest matching byte-for-byte.
- Evidence-aware reassessment: before = "needs review" (no evidence); after upload = refund/replacement **ELIGIBLE** in Layer A, proposed refund, authority **ESCALATE**, nothing executed.
- Authority trace: Layer B shows exactly which delegated action is missing; audit log records each decision and each refusal.
- Failure handling: wrong-type and over-size files rejected client-side with explicit messages; no fake counters; missing integrations disclosed in the UI.

## What is simulated or disabled (by design, disclosed in the UI)

- **Delivery is demo-simulated** — labelled "demo_simulated" on the order record; no carrier or merchant shipment is claimed.
- **No merchant order is placed** in the demo journey — the order panel states "No merchant order pairing on record".
- **The event ledger** (append-only outbox with off-host anchoring) is implemented and tested but **flagged off** in this deployment and not installed on the dev database.
- Refunds require delegated authority (see SECURITY_AND_AUTHORITY.md); without delegation the flow honestly escalates.

## Status

- Code-complete for the demo journey described above; regression suite matches its frozen baseline name-for-name (see TESTING_AND_VALIDATION.md).
- Runs locally via Docker Compose. **Not deployed to production.**
- Remaining gaps are catalogued in LIMITATIONS_AND_FUTURE_SCOPE.md.

## Document map

| Document | Contents |
| --- | --- |
| [README.md](README.md) | Repository layout, running the app |
| [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md) | Components, data flow, trust boundaries |
| [MODULE_DESCRIPTION.md](MODULE_DESCRIPTION.md) | Backend + frontend module-by-module |
| [TECHNOLOGY_STACK.md](TECHNOLOGY_STACK.md) | Pinned dependency versions |
| [API_DOCUMENTATION.md](API_DOCUMENTATION.md) | HTTP surface (43 endpoints + WebSocket) |
| [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) | The 14 live tables |
| [SECURITY_AND_AUTHORITY.md](SECURITY_AND_AUTHORITY.md) | Auth, ownership, authority model, evidence security, audit |
| [TESTING_AND_VALIDATION.md](TESTING_AND_VALIDATION.md) | Suite results, live verification, known failures |
| [LIMITATIONS_AND_FUTURE_SCOPE.md](LIMITATIONS_AND_FUTURE_SCOPE.md) | What does not work / is not claimed |
| [DEMO_GUIDE.md](DEMO_GUIDE.md) | Step-by-step demo script |
