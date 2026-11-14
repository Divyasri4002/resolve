# DEMO GUIDE

A step-by-step script for demonstrating RESOLVE's verified capabilities. Every step below
was executed live on 2026-11-14 against the running deployment. **Prerequisites:**
`docker compose up --build` running (backend :8000, frontend :3000, Postgres, MinIO);
Razorpay TEST credentials, SerpAPI key, and a reasoning-provider key configured in
`backend/.env`. Evidence storage requires the `RESOLVE_EVIDENCE_*` settings (this deployment
has them configured; without them uploads disable honestly).

> Honest framing for the demo: payments are Razorpay **TEST mode** (no real money), delivery
> is **demo-simulated**, and no merchant order is placed unless the separate merchant channel
> is configured. The UI says this at every step — read the labels aloud.

## Part 1 — Discovery to payment (verified)

1. **Register/login** at http://localhost:3000 (`/register`, `/login`). Sessions are
   cookie-based.
2. **Open the workspace** and type a shopping goal in the composer, e.g.
   *"Find me a stainless steel water bottle under ₹500"*.
   - RESOLVE runs discovery (SerpAPI) and replies with a shortlist; the right pane shows
     **PRODUCT DISCOVERY RESULTS** — verified live: 5 matches with real prices/merchants
     (₹285–₹475) and delivery notes.
3. **Click Select** on a card. The card flips to **Selected**; a server-derived
   **checkout review** appears (verify the product/amount/currency match the selection).
4. **Confirm checkout**, then **Pay** — Razorpay's **TEST** checkout opens. Use Razorpay's
   documented test instrument. On success the panel shows:
   - Razorpay order id / payment id,
   - **"Payment verified — TEST MODE"** with the HMAC-SHA256 verification timestamp,
   - "This was a test-mode transaction — no real money moved."
5. The **RESOLVE ORDER** record appears, created server-side ("client supplies nothing"),
   with **DELIVERY (DEMO SIMULATED)** labels and — when no channel is connected —
   **"No merchant order pairing on record"**.

## Part 2 — Post-purchase issue to authority-gated resolution (verified)

6. **Report an issue** on the order (e.g. *Damaged*, description, requested resolution
   = Refund). The panel is explicit: the request is a **preference only** and authorizes
   nothing.
7. **Answer the intake questions** — category-specific questions appear; answers persist
   ("Recorded as context for the assessment — authorizes nothing").
8. **Assess the issue** — the assessment shows three independent things:
   - Proposed resolution + canonical amount (server-derived),
   - **LAYER A — REMEDY ELIGIBILITY** per remedy,
   - **LAYER B — AUTHORITY ENGINE TRACE** (what is delegated vs blocked).
   - With no delegation, the authority decision is **ESCALATE** (an authority gap, not a
     refusal) and the chat states "Action has not been executed."
9. **Supplement evidence when asked** (the `needs_review` path): attach a real photo
   (JPEG/PNG/WEBP ≤ 5 MB; ≤ 5 files / 25 MB). Try the failure path too: a `.txt` or a 6 MB
   file is rejected with an explicit message and nothing is staged.
   - After submit: **ATTACHED EVIDENCE (n)** with an authenticated thumbnail; the backend
     stores the file in MinIO (sha256-pinned) and writes an audit event.
10. **Reassess** — the assessment updates in place: eligible remedies now cite the uploaded
    evidence; Layer B still shows exactly what authority is missing. Nothing executes.
11. **Delegate authority** (mission panel): delegate `request_refund` (bounded by the
    deployment ceiling), then **Reassess after delegating the remedy**. With authority
    granted and the remedy eligible, **Resolve issue** executes the refund **in Razorpay
    TEST mode** from canonical server-side data; the panel reports provider-verified status.
    (Without delegation the same button is honestly refused — both paths are worth showing.)
12. **Manual review**: "Don't decide automatically — request manual review" opts the issue
    out of autonomous decisions (persisted, authorization-bound).

## Part 3 — What to point out (the safety story)

- **The Authority Engine is deterministic** — no model call, no network call; it is the only
  component that can approve spend, and every execution re-runs it.
- **Eligibility ≠ authority**: both can be shown diverging live (eligible remedies, blocked
  execution, ESCALATE).
- **The audit log** records every decision/refusal with actor and reason.
- **No fabricated states**: unknown provider outcomes are recorded as unknown; provider-absent
  actions record pending intent; the UI never claims an execution that did not happen.
- **Evidence integrity**: the browser-attached photo round-trips byte-exact (sha256 verified
  server-side); storage internals never appear in the UI.

## Known demo quirks (disclose, don't hide)

- Reasoning/discovery providers are free-tier: a rate-limit outage shows an honest
  "temporarily unavailable" message — retry in a moment.
- Assessments are one-shot per issue; new information flows through **supplement + reassess**.
- Issues created before the structured-assessment feature may reject supplements with an
  honest 400 (legacy rows only).
- The full test suite has a documented frozen failure set (see TESTING_AND_VALIDATION.md §1);
  it is not a green run and should not be presented as one.
