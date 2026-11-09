# RESOLVE — Shopify Protected Customer Data: Technical Data-Use Documentation

> **Scope & label.** This is the application's *technical data-use and privacy documentation*
> for its Shopify integration. It is **not legal advice** and makes **no claim of legal
> compliance**. It describes what RESOLVE's code actually does, so a human/legal reviewer can
> map it against Shopify's Level 1 Protected Customer Data requirements. Anything that depends
> on deployment or legal judgment is explicitly marked **"requires deployment/legal
> verification"** rather than claimed as satisfied.

## A. Data used and purpose

- **What RESOLVE accesses from Shopify:** order, fulfillment, and return-related information —
  and only the fields needed to verify a commerce order's canonical state and support an
  authorized post-purchase resolution. See `backend/app/tools/shopify/` (reads only, plus one
  controlled return mutation) for the exact GraphQL fields requested:
  - **Products:** `id`, `title`, `handle`, `status`, and price range (`priceRangeV2.minVariantPrice`).
  - **Orders:** `id`, `name`, `createdAt`, `displayFinancialStatus`, `displayFulfillmentStatus`,
    total (`currentTotalPriceSet.shopMoney`), and line items (`id`, `title`, `quantity`, `sku`,
    `product { id }`, `variant { id }`).
  - **Fulfillment order lines:** read through a separate query on one order
    (`order(id:) { fulfillmentOrders { lineItems } }`) for `id`, `remainingQuantity` and the
    parent order line item `id` only. These are Shopify object references and a unit count — no
    recipient, address, shipping or customer field is read. This read exists solely to see which
    fulfillment order line corresponds to which order line, and how many units of it are still
    outstanding. It is not a return-eligibility source: `remainingQuantity` counts units awaiting
    fulfilment, so it reads 0 on a line that has already shipped.
  - **Shipped fulfillment lines:** read through a separate query on one order
    (`order(id:) { lineItems fulfillments { fulfillmentLineItems } }`) for the order line ids and
    their product ids, plus each fulfillment line's `id`, `quantity` and parent order line `id`.
    A `FulfillmentLineItem.id` is what a Shopify return references, and its `quantity` (units
    actually shipped) is the only local bound RESOLVE applies to a return quantity. Nothing
    personal is read: no recipient, shipping method, tracking, address or customer field.
  - **Returns:** read only through their parent order (`Order.returns`: `id`, `status`,
    `createdAt`, and each return line's `quantity` and `returnReasonNote`). There is no top-level
    returns connection in Admin API 2026-07. The last two fields are read for one purpose only: to
    identify, WITHOUT re-submitting anything, whether a return whose creation response was lost
    actually exists. Correlation uses the quantity and the reason token RESOLVE itself sent —
    never a customer name, email, phone or address.
  - **Order existence (linkage verification):** one minimal query — `order(id:) { id name }` —
    used solely to confirm that a Shopify order id handed to RESOLVE by a **server-side**
    integration really exists at this store before RESOLVE records it as the order a RESOLVE order
    corresponds to (`resolve_orders.provider_order_reference`, a unique 1:1 column). The stored
    value is a Shopify object reference and the store's own order number, nothing else. **No
    endpoint, request body or client-supplied field can set or change it**, and once established
    it cannot be re-pointed.
- **What RESOLVE does NOT request:** Shopify **customer name, email, phone, or physical
  address** fields are **not** in any query, and they remain unreadable to this app regardless of
  its write capability.
- **Requested API scopes:** exactly `read_orders`, `read_products`, `read_returns`,
  **`read_merchant_managed_fulfillment_orders`**, and **`write_returns`**. The four reads verify
  canonical state; **`write_returns` exists for one job — the controlled return workflow's single
  `returnCreate` call** (Tasks 14C.4/14C.6). It is **broader than that on Shopify's side**: the
  same scope would also permit closing, reopening, or editing a return. RESOLVE's own code is the
  boundary that keeps it narrow — `app/tools/shopify/return_provider.py` exposes exactly one
  mutation document, `returnCreate`, and it can only be transmitted through the dedicated
  `ShopifyReturnWriteClient` capability (Task 14C.7O); no order, product, fulfillment or refund write
  operation is implemented anywhere (**no `write_orders`, no `write_products`, no
  `write_merchant_managed_fulfillment_orders`**). Verified against the live installation
  (`currentAppInstallation.accessScopes` returns exactly the five scopes above) and against the
  queries and mutation documents in code, both of which are asserted by tests.
- **Current runtime state of that capability:** the scope is granted, but **nothing in RESOLVE can
  use it yet** — no return provider is registered (`get_return_provider()` offers only `"none"`)
  and no RESOLVE order carries an authoritative Shopify linkage, so no merchant mutation is
  reachable from any endpoint. Declaring a scope is not the same as exercising it.
- **How that boundary is enforced (Task 14C.7N, absolute since 14C.7O):** it is no longer only a
  description of which documents the client happens to pass. `ShopifyReadOnlyClient.execute_graphql`
  classifies the document it was handed **as its first act, before the token exchange, before the
  auth header, before the request body, and before any HTTP call** (`app/tools/shopify/document_gate.py`;
  no GraphQL parser dependency). A document counts as a read only if it is exactly one root `query`
  operation; a `mutation` of any kind — including RESOLVE's own declared return write — a
  `subscription`, an empty/non-text/unbalanced/bodyless document, and a document with more than one
  root operation are refused locally with `ShopifyReadOnlyViolation`. **In plain terms: RESOLVE's
  read-only path is query-only, and it cannot create an order, cancel, close, delete or void one,
  refund or restock anything, or open a return** — those operations are rejected on RESOLVE's side of
  the wire, before Shopify is contacted, whatever a future caller tries to build. The refusal message
  names the document's kind and, for a write, its single root operation name; it never quotes the
  refused document, and it carries no token, secret or header.
- **RESOLVE's return capability is a separate, explicitly isolated object (Task 14C.7O).** The one
  declared write no longer travels on the read client. `ShopifyReturnWriteClient`
  (`app/tools/shopify/return_write_client.py`) is a separate capability whose only send method is
  `submit_return_create`, and it transmits a document only when it is a well-formed mutation with
  **exactly one root field named in `SHOPIFY_DATA_USE.intended_write_mutations`** — `returnCreate`.
  Anything else, including a read query (the capability is mutation-only, so it cannot read), is
  refused locally with `ShopifyWriteCapabilityViolation` and zero HTTP attempts. **It is a separate
  capability, not a second credential**: `get_shopify_clients()` builds one `ShopifyAdminTransport`
  from the same configured `SHOPIFY_*` values and hands it to both objects, so there is one client id,
  one secret, one short-lived token and one exchange; no scope was requested or changed, and no second
  app was installed. Only the return-provider seam (`app/tools/returns/__init__.py` →
  `ShopifyReturnProvider(client=…, write_client=…)`) obtains it, which a scan of every production
  module under `app/` asserts against each file's code, docstrings stripped.
- **The one write the declaration permits, stated honestly:** `write_returns` exists for
  `returnCreate`, and the return provider hands that document to the capability above rather than to
  the read client. The capability reads the declaration rather than a scope: `returnClose`,
  `returnApproveRequest`, `returnDeclineRequest`, `returnProcess`, `returnReopen` and `returnCancel`
  are refused even though the scope would allow them, and the provider is registered with nothing — no
  endpoint, executor or orchestrator path can reach it. A provider with no capability refused to be
  given one reports a definite `ReturnProviderUnavailable` ("nothing reached Shopify"), never an
  unknown outcome, and never falls back to the read client. **This document does not claim the
  application has zero write capability**; it claims one declared write operation, on its own object,
  unreachable today, with every other merchant-side write refused in code.
- **The merchant's own purchase channel is separate infrastructure:** the disposable-order purchase
  path in `merchant-channel/` uses a **merchant-owned app with its own token and its own write
  scopes**, and deliberately does **not** inherit RESOLVE's boundary — RESOLVE's client refuses its
  `draftOrderCreate`/`draftOrderComplete` documents, while that package keeps the capability its job
  requires. Customer PII is read by neither: the channel names only object ids, a poNumber, quantities
  and a currency.
- **Purpose limitation:** Shopify data is used **only** to verify canonical order/fulfillment
  state and to support an authorized post-purchase resolution. There is **no** advertising,
  **no** analytics, **no** profiling for marketing, **no** sale or sharing of Shopify customer
  data, and Shopify-derived data **must not** be reused for unrelated purposes.
- **Data minimization in practice:** as of this foundation, RESOLVE persists **no** Shopify-derived
  protected data. The client holds tokens and GraphQL responses **in memory only** and is not wired
  to any executor or storage. (Confirmed by code search for writes of Shopify payloads: none
  exist.) The `write_returns` scope changes what Shopify *would* accept, not what RESOLVE *stores*.
- **What a return submission stores:** the return record carries a submission claim so
  a lost response can be reconciled instead of re-sent — the merchant order id, the fulfillment
  line id, an integer unit count, a reason token from a fixed table, and an opaque attempt id with
  its timestamp. Those are Shopify object references and counts, **not** customer personal data,
  and no access token, client secret or request header is ever stored. No merchant mutation is
  reachable from RESOLVE today — the provider is not registered and no order is linked — so nothing
  populates these fields.

## B. Merchant-facing transparency notice

> **What this app does with your Shopify data.** RESOLVE reads your store's **orders, products,
> returns, and fulfillment order line references** using the Shopify Admin API (`read_orders`,
> `read_products`, `read_returns`, `read_merchant_managed_fulfillment_orders`) **only** to confirm
> an order's canonical paid/fulfilled state and to support a post-purchase resolution your
> customer has engaged. RESOLVE **does not request customer name, email, phone, or address**
> fields, does **not** use your Shopify data for advertising, analytics, or
> marketing profiling, and does **not** sell or share it. RESOLVE also holds **`write_returns`**,
> and asks for it for **one** purpose: to open a return in your store for an order your customer
> has already paid for and already reported a problem with, through RESOLVE's controlled return
> workflow. Be aware that Shopify's `write_returns` permission is **broader** than that single
> action — it would also allow closing, reopening, or editing a return — so RESOLVE bounds it in
> its own code: the application implements **exactly one** write operation, `returnCreate`, and
> nothing else — and that operation goes through a separate, single-purpose capability object, never
> through the read path, which is query-only. RESOLVE requests **no** `write_orders`, **no** `write_products`, and **no**
> `write_merchant_managed_fulfillment_orders`, and therefore **cannot create or edit an order,
> issue a fulfillment or shipping label, or refund a payment** in your store. Locally retained
> Shopify-derived protected data (if any is introduced) is governed by the retention policy in
> section C. Access is logged for accountability (section F).

## C. Retention policy

- **Configurable default:** `SHOPIFY_RETENTION_DAYS` (backend config `shopify_retention_days`),
  default **30 days** — a conservative development/testing default.
- **What the policy governs:** *Shopify-derived protected data only.* It is implemented in
  `backend/app/retention/service.py`.
- **Structural safety:**
  - RESOLVE's own **core application, payment, and audit records** (`missions`, `messages`,
    `audit_log`, `resolve_orders`, `refunds`, `replacements`, `returns`, `resolutions`,
    `resolution_assessments`, `order_issues`, `users`) are in a **hard protected allowlist**
    (`PROTECTED_CORE_TABLES`). The retention engine **cannot** be pointed at them, and they are
    **never** deleted by age. Razorpay payment/refund records and insert-only audit records are
    explicitly protected.
  - The **only** tables eligible for retention are those *explicitly registered* as Shopify-derived
    protected sources (`SHOPIFY_PROTECTED_SOURCES`), which is **empty today** because RESOLVE
    persists no such data.
- **Reporting-only today (documented limitation):** because there is no Shopify-derived store to
  clean and deleting core records would damage integrity, `build_retention_report(...)` produces a
  **DRY-RUN report** (cutoff date, sources considered, per-source eligible counts, protected
  tables skipped) and **deletes nothing**. When a real Shopify-derived snapshot table is later
  introduced, it is registered here and an explicit, separately-enabled cleanup can act on it —
  core tables never become eligible.
- No destructive migration was added. No automatic scheduler runs in this foundation.

## D. Automated-decision manual review / opt-out

RESOLVE's autonomous resolution orchestrator (`POST /…/order/issues/{id}/resolve`) can make an
automated resolution decision. Customers who do **not** want an automated decision have an
explicit opt-out:

- **Request:** `POST /missions/{mission_id}/order/issues/{issue_id}/manual-review` (no body).
  Sets a persisted `manual_review_requested` flag + timestamp on the issue; idempotent.
- **Effect:** while the flag is active, `resolve_issue()` short-circuits to a
  **manual-review path that runs no executor, contacts no provider, and calls no reasoning
  model**, and records the issue as `escalated` (a human chose to look). The existing autonomous
  path is **unchanged** for issues without the flag.
- **Honesty boundaries:** this records the *customer's request*. **No human action is simulated,
  no external support ticket is created, and no ticket number exists.** The Authority Engine's
  delegation rules are **not** modified.
- **Visibility & audit:** the flag appears on the issue payload (`manual_review`), and both the
  request and the blocked resolution are written to the **existing** insert-only audit log.

## E. Encryption / transport verification (from the current implementation)

Verified from code and configuration:

- **In transit (to Shopify):** the client calls **`https://`** URLs only
  (`ShopifyReadOnlyClient.graphql_url` / `token_url` are built with `https://`). No plaintext
  HTTP to Shopify exists in the code.
- **Token handling:** the access token is held **in memory on the client instance only**, is
  never returned by any public method, and is never persisted.
- **Credentials server-side only:** `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` are read from
  environment/config, excluded from the image via `.dockerignore`/`.gitignore`, and never sent
  to the frontend. `.env.example` holds empty placeholders only.
- **Secrets excluded from logs/errors:** `app/tools/shopify/sanitizer.py` registers secret
  values and scrubs known credential shapes from every outbound message; auth-failure and 5xx
  response bodies are deliberately not echoed. Tests assert no token/secret appears in logs or
  exception text.
- **Encryption at rest:** **requires deployment/storage-level verification.** The local
  development database is a plain Docker Postgres volume with no application-level at-rest
  encryption. RESOLVE **does not** add a fake encryption layer. This line item is **not**
  claimed satisfied by this task.

## F. Audit / data access traceability

- Uses the **existing** insert-only audit system (`app/audit/logger.py`) — no second audit
  system. Shopify protected-data-relevant events that are traceable: the manual-review opt-out
  request and each resolution attempt's chosen action, Authority Engine decision, executor,
  persisted record, and whether a provider was attempted.
- Audit rows store **decisions and RESOLVE record references only** — never Shopify credentials,
  tokens, or unnecessary customer PII. Note: because RESOLVE persists no Shopify customer PII
  today, there is no PII in these records by construction.

## Level 1 mapping summary (technical view only — not legal compliance)

| # | Requirement | Technical status in RESOLVE |
|---|---|---|
| 1 | Minimum personal data | Satisfied in code: four read scopes plus the single `write_returns` for the controlled return workflow (no `write_orders`/`write_products`/`write_merchant_managed_fulfillment_orders`), no name/email/phone/address requested; nothing persisted |
| 2 | Inform merchants | Documented (Merchant notice, §B) — publication is a deployment step |
| 3 | Purpose limitation | Enforced by §A scope and `SHOPIFY_DATA_USE.intended_write_mutations` (= `returnCreate` only); no other consumer of Shopify data is wired |
| 4 | Respect customer consent | Partially — see §E note; Shopify consent signals are not yet read (no PII fields). **Requires product/legal verification** |
| 5 | Opt-out of sale/sharing | N/A by design: RESOLVE does not sell/share data (§A) |
| 6 | Automated-decision opt-out | Implemented (§D) |
| 7 | Privacy/data-protection agreements | **Legal — requires merchant agreement; not a code claim** |
| 8 | Retention periods | Implemented as reporting-only policy with protected core (§C); real cleanup deferred by design |
| 9 | Encryption at rest & in transit | In transit: HTTPS verified. **At rest: requires deployment/storage verification (§E)** |
