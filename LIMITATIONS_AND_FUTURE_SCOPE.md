# LIMITATIONS AND FUTURE SCOPE

Every item below is a **real, current limitation** — stated so no reader mistakes the demo
for production behavior.

## Deployment & operations

1. **Local deployment only.** The system runs via Docker Compose on one host. It is not
   deployed to any production environment; there is no TLS termination, rate limiting,
   horizontal scaling, or backup/restore drill in place.
2. **Single-host persistence.** Postgres and MinIO are local containers; a host failure
   loses both (the event ledger — which would mitigate this — is flag-off; see below).

## Commerce reality

3. **No real merchant orders in the demo journey.** Discovery surfaces real listings, but
   RESOLVE does not place a merchant order in the standard flow; the order panel states
   "No merchant order pairing on record", and delivery is **demo-simulated** (`demo_simulated`)
   — no carrier or merchant fulfillment is ever claimed.
4. **Merchant purchase exists only through the separate channel path**, which requires a
   configured merchant channel; it is not part of the default demo and the UI discloses its
   absence. The one authorized Shopify purchase previously executed was a controlled,
   exactly-once test — not an ongoing capability in the demo.
5. **Major Indian merchants have no usable purchase APIs** (Amazon/Myntra/AJIO/Nykaa/
   JioMart/Zepto seller-side constraints) — RESOLVE's honest commerce presentation reflects
   this rather than faking integrations.
6. **Replacements and returns require a connected fulfilment provider.** Without one, a
   permitted proposal is recorded as **pending** and nothing is claimed. No shipping or
   merchant-contact functionality exists.

## Payments

7. **Razorpay TEST mode only.** Refunds execute in TEST mode (one ₹799 refund provider-
   verified). No real money handling is claimed or supported.
8. **Refunds need delegated authority.** Fresh missions hold no delegation; the owner must
   delegate in the mission panel. This is by design (authority model), but it means a demo
   viewer must perform the delegation step to see an executed refund.

## Feature gaps

9. **Event ledger is flag-off.** The append-only ledger (PG-trigger completeness, WORM-style
   head CAS, off-host anchoring) is implemented and conformance-tested but disabled by
   default and **not installed on the dev database** — treat it as designed-not-deployed.
   Residuals inside its design are procedure-gated, not structural (documented in the audit
   trail): DBA-level bypass, new-table discipline, provider conformance assumptions.
10. **Evidence store is enabled here but was built disabled-by-default**; deployments that do
    not configure it run with uploads disabled (honest failure, no mock storage).
11. **No WebSocket client** by decision — the frontend polls; the server-side WS endpoint is
    implemented and tested but unused by the client. Real-time streaming UI is future scope.
12. **Legacy assessments** created before structured remedy eligibility show the supplement
    form but receive an honest 400 from the backend gate ("No remedy on this issue is waiting
    for more information"). This affects only pre-existing rows, not new flows; it is a known
    UI/backend gate mismatch on legacy data.
13. **Negotiation protocol** (agent-to-agent claim → evidence → offer → counter → accept)
    exists as code and tests but has no live counterparty in the demo.
14. **Reviews** (rating/body fields on orders) are stored and displayed but there is no
    review-submission workflow in the demo journey.
15. **Single currency display**: amounts render in the order's currency via a shared
    formatter; the frontend never converts — there is no multi-currency conversion feature.

## Verification gaps (submission-day)

16. **Screenshot capture failed** during browser acceptance testing (tooling could not get a
    visible browser surface); the acceptance evidence relies on accessibility snapshots,
    DOM inspection and server-side verification instead of images. **No screenshots are
    fabricated to fill the gap.**
17. **Full-suite failures are pre-existing and environment-bound** (13 failures + 12
    collection errors, name-for-name the frozen baseline): the role-separation battery
    requires its dedicated sandbox; the golden-lifecycle KeyError and several provider-sandbox
    tests fail in the standard dev environment. Zero regressions were introduced, but the
    suite is **not** a full green run — do not represent it as one.
18. **Free-tier provider pacing**: discovery and reasoning depend on SerpAPI/Groq/Gemini
    quotas; rate-limit outages surface honestly ("temporarily unavailable") but can interrupt
    a demo.
19. **Performance is unmeasured**: no load testing or latency benchmarking was performed;
    the only latency work (provider failover circuit breaker, live ~4s/turn observed
    informally) is anecdotal, not a benchmark.

## Future scope (ordered by leverage)

1. Wire the ledger into the deployment (operator-run migration + flags on) and extend
   coverage to all money-adjacent events.
2. Connect a fulfilment provider for replacements/returns so permitted proposals can execute
   end-to-end.
3. Production hardening: TLS, rate limiting, backups, monitoring, horizontal scale.
4. Frontend WebSocket client with replay/cursor semantics (design exists: M21 CP2+).
5. Real merchant purchase via the channel trigger for supported merchants (Shopify).
6. Review-submission workflow; negotiation counterparties for agent-to-agent settlement.
