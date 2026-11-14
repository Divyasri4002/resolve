# MODULE DESCRIPTION

Backend modules under `backend/app/`, frontend modules under `frontend/`. Every module's
current behavior is described as implemented; flagged-off or partial states are stated
explicitly.

## Backend

### routers/missions.py (36 endpoints)
The core surface: mission creation + chat/agent turn; shopping journey (select product,
clarify variant, checkout review, checkout confirm, payment order-create + verify); order
lifecycle (create, confirm, purchase via merchant channel); issue intake (report, category
questions, answers); evidence supplement (multipart, validated, batch-stored, compensating
cleanup on failure) and authenticated evidence retrieval; assessment (assess/reassess with
Layer A remedy eligibility + Layer B authority decision); resolution execution (resolve,
refund, replacement, return — each re-runs the Authority Engine); manual review; authority
delegate/revoke; agent conversation and tool listing.
Notable gates: supplement accepts only issues whose live assessment has a `needs_review`
remedy and status `reported`/`under_review`; answers accepted only while `reported`.

### routers/auth.py (4)
`/auth/register`, `/auth/login`, `/auth/logout`, `/auth/me` — bcrypt password hashing,
HTTP-only session cookie.

### routers/webhooks.py (1)
`/webhooks/razorpay` — HMAC signature verification before processing.

### routers/ws.py (1)
`/ws/missions/{mission_id}` — WebSocket broadcast of lifecycle events (authenticated,
ownership-checked). The frontend deliberately does not use a WS client; it refreshes from
RESOLVE (polling).

### routers/integrations.py (1)
`/integrations/shopify/order-linkage` — service-credential-protected receiver for the
merchant-channel trigger (separate process; env-boundary guard bans in-process import).

### mission_engine/
Agent loop: canonical sanitized context (no secrets, labeled server lifecycle state), typed
tool registry with strict schemas, bounded loop, response grounding verification, reference
resolution (product/issue references), append-only conversation persistence.

### authority/engine.py
Pure deterministic function. Evaluates requested actions against the mission's delegated
mandate: `allow` / `escalate` / `deny` with reasons. Ceilings (e.g. `SHOPPING_REFUND_MAX_INR`,
server-derived mandate ceiling) enforced here and re-checked at execution.

### resolution/
Assessment generation via the reasoning provider with structured output: proposed action,
remedy eligibility per remedy (Layer A), authority decision (Layer B), decision reason,
`requested_amount` (canonical order amount), `proposal_source`. Reassessment updates the same
row in place (unique per issue), preserving `first_assessed_at`. Orchestrator: selects the
executor per remedy; refund executes against Razorpay TEST mode; replacement/return execute
only through a connected provider; otherwise records pending intent honestly.

### payments/
Razorpay client wrappers (order create, signature verify, refund create + fetch) and
TEST-mode guards. Signature verification is server-side HMAC-SHA256.

### evidence_store.py
Dedicated evidence bucket store: `store_files` (streaming, per-file and batch limits,
structural image validation incl. mime sniffing, sha256 pinning, mid-batch failure cleanup
with orphan tracking), `read` (digest verification of retrieved bytes). Reuses only the
SigV4 signing machinery from the ledger's S3 client; can never address the ledger bucket.

### body_limit.py
Pure-ASGI `EvidenceBodyLimitMiddleware`: Layer 1 content-length pre-check, Layer 2 counting
receiver that raises 413 before exceeding the batch framing budget; registered before CORS.

### ledger/
Append-only event ledger: PG triggers as sole writer of `purchase_events` (gapless counter,
insert-only, payload snapshot), publisher with head-object CAS against an S3-compatible
store, verification tooling. **Implemented + tested (hermetic + real-MinIO conformance);
disabled by flags in this deployment and not installed on the dev database.**

### audit/
Append-only audit log writer: actor, action, decision, reason, requested amount. Every
state-changing path writes here.

### auth/
Session cookie issuance/validation; client-credentials service auth for the trigger
(`Authorization: Bearer <service credential>`); startup verification that the runtime DB role
matches the declared topology (503 gates when enforcement fails).

### Other
`config.py` (pydantic-settings; disabled-by-default evidence/ledger flags),
`db_enforcement.py`, `bootstrap_database.py`, `logging_redaction.py` (secret redaction),
`retention/` (privacy inventory/erasure), `tools/` (typed tool registry incl. search_products,
issue reporting), `agents/` (reference resolution), `protocol/negotiation.py` (claim →
evidence → offer → counter → accept state machine for agent-to-agent settlement),
`integrations/` (Shopify read client + merchant channel trigger service),
`models/` (SQLAlchemy records).

## Frontend

- `app/workspace/page.tsx` — the workspace: mission selection, agent turn streaming (fetch),
  issue operations (assess/reassess/resolve/refund/replacement/return/manual-review), evidence
  supplement with XHR progress + preview object URLs, honest 200-escalation handling.
- `components/workspace/`:
  - `Sidebar.tsx` — per-user missions (suffix `…{last 6 of id}`).
  - `PromptBox.tsx` — composer (Enter to send; disabled while working).
  - `ProductResults.tsx` — discovery cards with price/merchant/delivery + Select.
  - `CheckoutReview.tsx` — server-derived review; explicit confirm boundary.
  - `PaymentPanel.tsx` — Razorpay TEST-mode checkout; gated on the payment record; displays
    verification (HMAC-SHA256) and timestamps.
  - `OrderPanel.tsx` — RESOLVE order record; demo-delivery labelling; "No merchant order
    pairing on record" when none exists.
  - `IssuePanel.tsx` — issue cards; intake questions; answers; evidence upload form (native
    picker, client limits mirroring backend, per-file error messages, counter, submit gated);
    attached evidence with authenticated thumbnail; assessment display (authority decision,
    Layer A verdicts, Layer B trace, "Nothing has been executed"); resolution buttons.
  - `ResolutionCard.tsx`, `ActivityStream.tsx`, `ToolActivity.tsx`, `NegotiationLog.tsx` —
    lifecycle/activity displays.
  - `MissionContextPanel.tsx` — right-pane context.
- `lib/` — shared `formatMoney` (currency-aware; the frontend never converts), `apiRequest`.
- Tests: vitest (35 tests) + `tsc --noEmit` + production build.

## merchant-channel/
Separate trigger process (never imported in-process): runs buyer-flow triggers against the
merchant channel with purchase-authorization gates (`merchant_channel/gates.py`), outbox
submission with leases, and honest outcome classification. In this deployment no merchant
channel is configured (disclosed in the UI).
