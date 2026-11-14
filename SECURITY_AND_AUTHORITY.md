# SECURITY AND AUTHORITY

This document describes the security and authority model as **implemented and tested**.
Where a protection is design-only or disabled, it says so.

## Authentication & access control

- **Users**: register/login with bcrypt-hashed passwords; HTTP-only session cookie
  (`/auth/*`). Passwords are never logged or returned.
- **Service credentials**: the merchant-channel trigger authenticates with a client
  credential (bearer token) against `/integrations/shopify/order-linkage`. In-process import
  of the trigger is banned by an env-boundary guard.
- **Ownership**: every mission-, order-, issue- and evidence-scoped endpoint resolves the
  mission by id AND user and returns 404/403 on mismatch. Unknown ids and other users'
  resources are indistinguishable.
- **WebSocket**: authenticated + ownership-checked before events flow.

## The authority model (the core safety property)

1. **Layer A — remedy eligibility** (assessment time): each remedy (refund / replacement /
   return) is judged **independently** of the others and of the model's proposed action.
   Verdicts: `eligible` / `needs_review` / `not_eligible` with reasons.
2. **Layer B — Authority Engine** (deterministic, pure function, no model/network calls):
   checks the requested action against the mission's **delegated mandate** and deployment
   ceilings. Verdicts: `allow` / `escalate` / `deny` with an explicit reason.
   - Missing delegation is an **authority gap → ESCALATE**, never a silent DENY (so the
     remedy path stays recoverable when the owner delegates later).
3. **Execution-time re-check**: the assessment verdict alone never authorizes anything.
   Every execution endpoint (`resolve`, `refund`, `replacement`, `return`) re-runs the
   Authority Engine against the **current** mandate before acting. Verified live: with both
   remedies ELIGIBLE and no delegation, the refund path stays blocked and the audit records
   the refusal.
4. **Ceilings**: refund authority is bounded by a server-derived ceiling
   (`SHOPPING_REFUND_MAX_INR`; the mission's mandate ceiling is derived from it). Verified:
   a ceiling misconfiguration was found and corrected (unset → default 500 < ₹799 item);
   refunds above the ceiling are refused.
5. **Requested amounts are server-derived** from the canonical order record — a client or
   model cannot inflate a refund amount.

## Payment security

- **Razorpay TEST mode only.** No real money moves; real purchases are out of scope by
  configuration and by honest UI copy.
- Signature verification is **server-side HMAC-SHA256** over Razorpay's order/payment ids
  with the key secret; the client never asserts success — the payment panel displays the
  server's verification record and timestamp.
- Webhook: signature verified before processing.
- Refund execution: only when the Authority Engine explicitly allows; "verified" means the
  provider itself reported the refund processed (`verified_at`).

## Evidence (file upload) security

- Storage: dedicated S3-compatible (MinIO) bucket, separate from everything else; the
  evidence code path reuses only the SigV4 signing machinery and cannot address any other
  bucket.
- Validation pipeline (server-side, before any storage): extension/MIME allow-list
  (JPEG/PNG/WEBP), per-file ≤5 MB, ≤5 files and ≤25 MB per supplement, ≤10 uploads per
  issue, structural image validation (magic-byte sniffing; malformed images rejected),
  declared-mime vs sniffed-mime mismatch rejected.
- Integrity: sha256 computed server-side at upload and re-verified on every retrieval
  (digest-of-retrieved-bytes must match the DB-pinned digest; verified live end-to-end).
- Retrieval: authenticated, ownership-checked, `Content-Disposition: inline`,
  `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`.
- **No client assertions**: evidence metadata is built exclusively server-side; pydantic
  ignores extra fields, so a JSON caller cannot inject storage truth; no client ever asserts
  what is in the bucket.
- **No internals leak**: storage keys, bucket names, digests, sizes and credentials are not
  part of public API responses (verified against the live UI).
- Request-size guard: pure-ASGI body-limit middleware (content-length pre-check + counting
  receiver → 413) protects the multipart path; registered before CORS.
- Failure safety: mid-batch storage failures compensate (stored objects deleted) and surface
  502 with an audit event (`evidence_orphan_tracked`); retry is safe.
- Documented limitation: the counting receiver counts bytes the app reads — requests the
  gates reject before reading never spool the body (an unauthenticated 404 does not consume
  the stream). This is disclosed, not hidden.

## Data protection

- **Role separation** (live on the dev volume): runtime app role `resolve_app` cannot create/
  alter/drop schema, cannot delete protected rows; migrations run as `resolve_owner`
  (NOLOGIN, operator-run). Startup verifies the topology and refuses to self-heal (503 gate).
- **Append-only audit**: `audit_log` records actor/action/decision/reason for every state
  change; role separation prevents deletion.
- **Event ledger** (flag-off): append-only `purchase_events` with PG-trigger-enforced
  completeness, WORM-style head CAS in object storage, deletion refused at DB and storage
  layers. Implemented and tested (hermetic + real-MinIO conformance); **not installed on the
  dev database and disabled by default** — treat as designed-not-deployed.
- **Logging redaction**: secrets are redacted from logs by a dedicated module; credentials
  are never printed or committed.
- **Privacy**: retention module carries the privacy inventory/erasure policy; manual-review
  opt-out persists and is authorization-bound (tested).

## Threat-model notes (honest scope)

- Threats covered: cross-user access, forged payment success (signature), client-injected
  evidence truth, unauthorized spend (mandate + ceilings + execution re-check), silent
  fabrication of outcomes (unknowns recorded as unknown), evidence tampering (sha256 pin),
  body-oversize abuse (413 middleware), privilege escalation at the DB role level.
- Threats NOT claimed as covered: production hardening (TLS, rate limiting, horizontal
  scaling), multi-tenant isolation beyond the single-deployment model, operator-level
  compromise (a DBA holding owner credentials can bypass application-layer controls —
  disclosed residual).
