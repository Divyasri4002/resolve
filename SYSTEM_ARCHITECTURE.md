# SYSTEM ARCHITECTURE

RESOLVE runs as four containers under Docker Compose on a single host (local deployment):

```
┌──────────────┐     HTTP/WS      ┌───────────────┐     SQL       ┌────────────┐
│  frontend    │ ───────────────► │   backend     │ ────────────► │  Postgres  │
│  Next.js     │                  │   FastAPI     │               │  (PG16)    │
│  :3000       │                  │   :8000       │               └────────────┘
└──────────────┘                  └──────┬────────┘
                                         │ SigV4 HTTP
                                  ┌──────▼────────┐
                                  │    MinIO      │  evidence bucket
                                  │  (S3 API)     │  resolve-evidence-conformance
                                  └───────────────┘
```

External services (all TEST/free-tier): Razorpay (payments, TEST mode), SerpAPI (product
discovery), Google/Groq (reasoning providers with failover), Shopify Admin API (optional,
read-only catalog / separate merchant channel).

## Backend (`backend/app/`)

- **routers/** — the HTTP surface. `missions.py` (36 endpoints) owns the shopping journey and
  post-purchase lifecycle; `auth.py` register/login/logout/me; `webhooks.py` Razorpay
  signature-verified webhook; `ws.py` authenticated WebSocket event stream; `integrations.py`
  service-authenticated Shopify order-linkage receiver.
- **mission_engine/** — the agent loop: canonical sanitized context builder, bounded tool
  loop, lifecycle state machine, conversation persistence.
- **authority/engine.py** — the **deterministic Authority Engine**: a pure function with no
  model or network calls. It is the only component allowed to approve spend. Execution paths
  re-run it at execution time (the assessment verdict alone never authorizes anything).
- **resolution/** — resolution assessment (reasoning-model proposal + structured Layer A
  remedy eligibility) and the resolution orchestrator (executor selection, provider attempts,
  honest outcome recording).
- **payments/** — Razorpay order creation, HMAC-SHA256 signature verification, refund
  execution and provider-status reconciliation. TEST-mode only.
- **evidence_store.py** — dedicated S3-compatible evidence object store (reuses only the
  ledger's SigV4 signing core). Streaming multi-part validation (type sniffing, size caps,
  batch limits), sha256 pinning, authenticated retrieval, orphan cleanup on mid-batch failure.
- **body_limit.py** — pure-ASGI request-body limit middleware (content-length pre-check +
  counting receiver) guarding the multipart supplement path.
- **ledger/** — append-only event ledger with PG-trigger-enforced completeness and an
  S3-compatible head-object CAS publisher. Implemented and tested against real MinIO
  conformance suites; **flagged off** in this deployment.
- **audit/** — append-only `audit_log` writer used by every state-changing path.
- **auth/** — session cookies for users; client-credentials service auth for the
  merchant-channel trigger.
- **db_enforcement.py / bootstrap_database.py** — startup verification of DB role separation
  (`resolve_owner` NOLOGIN / `resolve_app` runtime role); startup refuses to self-heal.
- **retention/** — privacy inventory / erasure policy support.

## Frontend (`frontend/`)

Next.js (App Router) single-workspace page with three panes:

- **Sidebar** — mission list (per-user, in-app selection).
- **Conversation center** — chat with the agent, tool-activity steps, product result cards
  (`ProductResults.tsx`), payment panel (`PaymentPanel.tsx`), order lifecycle
  (`OrderPanel.tsx`), issue panel (`IssuePanel.tsx`) with intake questions, evidence upload
  (native file picker, XHR multipart with progress), assessment/resolution cards.
- **MissionContextPanel** — server-derived mission context.

Design decisions: **the server is the only writer of mission state**; no WebSocket client by
decision (polling/refresh from RESOLVE; the WS endpoint exists server-side); one shared
currency-aware formatter (the frontend never converts currency — it surfaces the server's
reason); storage keys/buckets/credentials never reach the client.

## Trust boundaries

1. **Client → backend**: nothing the client sends is trusted for money or state. The checkout
   review is server-derived; the payment signature is verified server-side; the order record
   is created server-side; evidence metadata is built exclusively server-side after bytes are
   validated ("no client ever asserts what is in the bucket").
2. **Assessment → execution**: Layer A (per-remedy eligibility, independent checks) and
   Layer B (delegated authority + ceilings) are evaluated independently of the model's
   proposal; **every execution re-runs the Authority Engine**.
3. **App → database**: the app connects as `resolve_app`, which cannot create/alter/drop
   schema or delete protected rows; migrations run as the separate `resolve_owner` role.
4. **Audit**: every authority decision, execution attempt, refusal, and evidence change is
   appended to `audit_log` with actor, decision and reason.

## Persistence

PostgreSQL: 14 tables live (see DATABASE_SCHEMA.md). MinIO holds evidence objects under
`evidence/{mission_id}/{issue_id}/{hash}.bin`; the DB row stores only the reference metadata
(kind, mime, sha256, size, storage key, bucket) and is the single source of truth.

## Failure philosophy

- Unknown outcomes are recorded as unknown, never guessed (provider calls that time out
  persist `outcome_unknown` and never claim success).
- Provider-absent actions record intent as pending with an honest message; nothing is claimed.
- Orchestrator 200-with-escalation (replaces a misleading 503) records authorized-but-pending.
- The reasoning provider fails over (Gemini → Groq) with a cooldown circuit breaker; defect
  providers never open the breaker.
