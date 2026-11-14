# API DOCUMENTATION

Base URL: `http://localhost:8000` (backend). All request/response bodies are JSON except the
multipart supplement endpoint. Authentication is a session cookie for users
(`/auth/login`), or a service bearer credential for the merchant-channel trigger.

Response conventions: errors are `{"detail": "…"}`; honest status codes are used throughout
(400 validation/gate, 401/403 auth/ownership, 404 absent/not-owned, 409 conflict/sha
mismatch, 413 too large, 415 unsupported type, 502 storage failure, 503 gate/enforcement or
authorized-but-pending). No endpoint ever returns storage keys, bucket names, credentials or
internal object metadata.

## Auth — `/auth` (4)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/auth/register` | create user (bcrypt hash) |
| POST | `/auth/login` | session cookie login |
| POST | `/auth/logout` | clear session |
| GET | `/auth/me` | current user |

## Missions — `/missions` (36)

### Mission + agent

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/missions` | create mission |
| GET | `/missions` | list own missions |
| GET | `/missions/{mission_id}` | mission detail (ownership-checked) |
| POST | `/missions/{mission_id}/agent` | agent turn (chat / tool loop) |
| GET | `/missions/{mission_id}/agent/conversation` | persisted conversation |
| GET | `/missions/{mission_id}/agent/tools` | tool registry listing |
| POST | `/missions/{mission_id}/deviation` | record deviation |
| POST | `/missions/{mission_id}/offer` | negotiation offer |
| POST | `/missions/{mission_id}/resolve` | mission-level resolve action |

### Shopping journey

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/missions/shopping` | shopping intent mission |
| POST | `/missions/{mission_id}/shopping/select-product` | persist selection |
| POST | `/missions/{mission_id}/shopping/clarify-variant` | structured variant clarification round-trip |
| GET | `/missions/{mission_id}/shopping/checkout` | server-derived checkout review (**mutates** state — user action) |
| POST | `/missions/{mission_id}/shopping/checkout/confirm` | confirm checkout |
| POST | `/missions/{mission_id}/shopping/payment/create-order` | Razorpay TEST order create |
| POST | `/missions/{mission_id}/shopping/payment/verify` | server-side HMAC-SHA256 signature verify + order record |

### Order lifecycle

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/missions/{mission_id}/order` | RESOLVE order record + stages (server-advanced) |
| POST | `/missions/{mission_id}/order` | create order record (server-side only) |
| POST | `/missions/{mission_id}/order/confirm` | confirm order |
| POST | `/missions/{mission_id}/order/purchase` | merchant-channel purchase intent (authorization-gated) |
| GET | `/missions/{mission_id}/purchase` | purchase authorization status |

### Issues + evidence

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/missions/{mission_id}/order/issues` | list issues |
| POST | `/missions/{mission_id}/order/issues` | report issue (requested resolution is a **preference only**) |
| GET | `/missions/{mission_id}/order/issues/{issue_id}/questions` | category intake questions |
| POST | `/missions/{mission_id}/order/issues/{issue_id}/answers` | record answers (only while `reported`; normalized deterministically) |
| POST | `/missions/{mission_id}/order/issues/{issue_id}/supplement` | multipart: note + evidence files (JPEG/PNG/WEBP; ≤5 MB each; 5 files/25 MB; batch-validated-then-stored; 502 + orphan cleanup on storage failure) |
| GET | `/missions/{mission_id}/order/issues/{issue_id}/evidence/{index}` | authenticated evidence retrieval (inline, `nosniff`, `private-no-store`) |

### Assessment + resolution

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/missions/{mission_id}/order/issues/{issue_id}/assess` | one-shot assessment (proposal + Layer A + Layer B) |
| POST | `/missions/{mission_id}/order/issues/{issue_id}/reassess` | re-assess after new evidence/answers (updates same row, preserves `first_assessed_at`) |
| POST | `/missions/{mission_id}/order/issues/{issue_id}/resolve` | execution attempt — **re-runs the Authority Engine at execution** |
| POST | `/missions/{mission_id}/order/issues/{issue_id}/refund` | refund execution attempt (TEST mode; mandate ceiling; provider-verified only) |
| POST | `/missions/{mission_id}/order/issues/{issue_id}/replacement` | replacement attempt (provider required; otherwise pending, nothing claimed) |
| POST | `/missions/{mission_id}/order/issues/{issue_id}/return` | return attempt (provider required) |
| POST | `/missions/{mission_id}/order/issues/{issue_id}/manual-review` | opt out of autonomous decisions |

### Authority

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/missions/{mission_id}/authority/delegate` | delegate an action to the agent (bounded by deployment ceilings) |
| POST | `/missions/{mission_id}/authority/revoke` | revoke delegation |

## Webhooks — `/webhooks` (1)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/webhooks/razorpay` | signature-verified Razorpay webhook |

## Integrations — `/integrations` (1)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/integrations/shopify/order-linkage` | service-credential receiver (channel trigger only) |

## WebSocket (1)

| Protocol | Path | Purpose |
| --- | --- | --- |
| WS | `/ws/missions/{mission_id}` | authenticated lifecycle event stream (server → client). The frontend deliberately polls instead; the endpoint exists and is tested server-side. |

## Root (1)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | service-info JSON |

## Live-verified examples (2026-11-14)

- `POST …/order/issues/{issue_id}/supplement` with 1 PNG (1.2 MB): stored in MinIO, DB row
  `kind=uploaded_file` with sha256 `4ae1ff37…`; audit event `issue_evidence_supplemented`;
  retrieval endpoint served the exact bytes (digest re-verified server-side).
- `POST …/order/issues/{issue_id}/reassess`: assessment updated in place
  (`needs_review` → `eligible` ×2, `allow` → `escalate`, proposal → `request_refund` with
  canonical amount), Layer B still blocked (delegation absent), nothing executed.
- `POST …/shopping/payment/verify`: HMAC-SHA256 verified server-side for a TEST payment
  (amount ₹69); "no real money moved".
- Unauthorized requests to mission-scoped endpoints return 401/403/404 without leaking
  existence (ownership checked).
