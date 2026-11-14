# TESTING AND VALIDATION

This report separates **measured results** from **unverified claims**, per the regression
rules: compare against the frozen baseline, separate pre-existing from new failures, report
skipped tests and collection errors, never hide failures, never change tests to make them
pass, never claim a full pass when failures exist, never use mocks as live-integration
evidence.

## 1. Backend suite (canonical, 2026-11-14)

Full pytest run against the source-mounted backend image
(`MSYS_NO_PATHCONV=1`, `MERCHANT_CHANNEL_ROOT=/merchant-channel` mount):

```
13 failed, 1568 passed, 67 skipped, 5 warnings, 12 errors   (289s)
```

**This is name-for-name identical to the frozen-13 baseline** — zero new regressions.

- The **12 errors** are all `tests/test_role_separation.py` ROLE-1..12: they require the
  isolated role-separation PG sandbox which is not attached in the standard dev run —
  documented run-environment artifacts (the same battery passes 57/1 in its dedicated run).
- The **13 failures** are the documented frozen-13 set, dominated by the same run-env
  artifacts plus the one pre-existing `test_core3a_golden_lifecycle` KeyError and
  replacement/return/currency/privacy cases that assert against provider sandboxes not
  present in this environment. Each was reproduced before the current changes (a control
  experiment mounting the pre-change tree over the same environment produced every failure)
  — none was introduced by recent work.
- **67 skipped**: hermetic tests that require a real PostgreSQL URL (`PG_TEST_URL`) or real
  MinIO are skipped in the standard run and run in their dedicated batteries (below).

## 2. Focused batteries (same day)

- Evidence-focused suite: 112/112 passed (upload/validation/retrieval/body-limit/audit).
- Refund-authority battery: 22/22 passed.
- Ledger conformance (real MinIO, isolated bucket + restricted user): 12 passed (1
  documented divergence: If-Match on missing object — documented, not hidden).
- Frontend: **vitest 35/35**, `tsc --noEmit` clean, production build clean.

## 3. Live browser verification (2026-11-14, real deployment)

All scenarios executed through the real frontend at http://localhost:3000 with a real
authenticated user. **Screenshots could not be captured** — the in-app browser surface was
hidden for the tooling (`NATIVE_BROWSER_VIEWPORT_UNAVAILABLE`); all evidence below is from
accessibility snapshots, DOM inspection, HTTP behavior and server-side DB/audit/object
verification, not images.

| Scenario | Result |
| --- | --- |
| A — discovery | Fresh NL query returned 5 real results (names, prices, merchants, delivery); selection persisted ("Selected") |
| B — Razorpay TEST checkout | Payment record live-verified on-screen: order/payment ids, "Payment verified — TEST MODE", server-side HMAC-SHA256, timestamp; "no real money moved". (Popup re-drive is a tooling limitation — disclosed) |
| C — issue + evidence E2E | Report → intake → assess (needs_review ×2, ALLOW) → attach real PNG → multipart accepted → stored in MinIO → authenticated thumbnail rendered (decoded) → audit `issue_evidence_supplemented` → reassessment: refund/replacement ELIGIBLE, ESCALATE, nothing executed. DB row `kind=uploaded_file`, sha256 `4ae1ff37…`; MinIO re-read byte-exact (1,210,859 bytes) |
| D — authority | Layer A ELIGIBLE ×2 while Layer B blocked ("never delegated"); authority decision ESCALATE; "Nothing has been executed"; audit refusal entries present |
| E — failure handling | `malicious.txt` → "is not a JPEG, PNG or WEBP image."; 6 MB `huge.png` → per-file-limit message; counters stay 0/5; submit stays disabled; missing integrations disclosed ("No merchant order pairing on record") |

## 4. Historical live proofs (real integrations, not mocks)

- Razorpay TEST refund executed and provider-verified: `rfnd_Tyvnf5oHEKqJXU` ₹799
  (TEST mode; provider reported processed).
- Razorpay TEST payments verified server-side via HMAC (e.g. `pay_TywmLFU3oGnhBo` ₹69).
- Shopify read-only catalog verification (real store identity, variant identity) and one
  authorized Shopify purchase through the separate merchant-channel trigger (exactly-once,
  reconciled) — that path is **not part of the default demo journey**.
- Reasoning providers: Gemini and Groq both live-verified at various times; Groq failover
  with a cooldown circuit breaker live (defects never open the breaker).

## 5. What is NOT evidenced

- No production deployment or load testing — none was run; none is claimed.
- Screenshot images of the live runs (tooling limitation above; the checklist relies on
  snapshots/DB/audit evidence instead).
- Ledger enforcement is not exercised by the running app (flag-off); its tests ran against
  sandboxes, not the live dev DB.
- Merchant-channel purchase in the demo journey: not exercised (no channel configured;
  UI discloses this).

## 6. Test-infra invariants (guardrails enforced by the suite)

- Tests are hermetic unless explicitly marked: mocked providers, seeded DB, no network
  beyond the configured sandboxes; conftest pins feature flags via `os.environ` so `.env`
  cannot leak flags into runs (a leak was found and fixed).
- JSONB mutation tests use `copy.deepcopy` — shallow copies silently skip UPDATEs.
- Bounded retries with durable unresolved/halted states; zero merchant calls in the publish
  loop (ledger design tests).
