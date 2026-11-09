# External purchase channel and the currency boundary

Task 14C.7H (design and readiness) and Task 14C.7I (the decision, and the merchant-side channel that
follows from it). Nothing in this document was implemented as a commerce change against the connected
store: no Shopify order was created, no scope was requested, and no pairing was fabricated. §1–§10 are
the 14C.7H audit; §11 records what 14C.7I decided and built, and what still blocks it. Every numbered
claim below is either a file:line in this repository or a value measured live against the connected
store through read-only queries.

Companion document: `docs/shopify-order-linkage.md` (the signed pairing contract, the corroboration
read, and the correlation-field investigation that ended in a negative result).

## 1. The two currencies, as measured

| fact | value | where it comes from |
| --- | --- | --- |
| connected store | `resolve-commerce-lab.myshopify.com` ("RESOLVE Commerce Lab") | `shop` object, live |
| **store currency** | **USD** | `shopify.currencyCode` via `query ResolveReadCatalog` |
| catalog as published | 3 ACTIVE products, 1 variant each: 410.00 / 499.00 / 2299.00 USD; every variant's options are `("Title","Default Title")`; every `sku` is `null` | live catalog read |
| **payment currency** | **INR** | `PAYMENT_CURRENCY = "INR"`, `app/routers/missions.py:553` |
| payment currency configurable? | **No.** `Settings` has no field whose name contains `currency` — the constant is in code, deliberately | `app/config.py`, pinned by test |
| FX conversion anywhere | **None.** No rate, no converter, no FX host, no rate column | AST scan across `app/`, pinned by test |
| Razorpay mode | TEST (`key_id` prefix `rzp_test`) | deployed config |
| `PRODUCT_SEARCH_PROVIDER` / `RETURN_PROVIDER` / linkage secret | `serpapi` / `none` / unset (route answers 503) | deployed config |

The mismatch is therefore structural, not accidental: **the merchant RESOLVE can name sells in a
currency the payment rail on this deployment is not configured to charge in.**

## 2. Where money is decided, in order

1. **Shopify publishes it.** `query ResolveReadCatalog` returns the shop's `currencyCode` and each
   variant's own `price` → `ShopifyCatalog.currency` (`app/tools/shopify/models.py:280`). Store
   identity and currency travel with the products on purpose: a price without its currency is not a
   price.
2. **The provider stamps it, and refuses to compare across it.**
   `_to_product_result` (`app/tools/product_search/shopify_provider.py:340-397`):
   `currency = store_currency or wanted_currency or "INR"`, `price = _money(price, store_currency)`
   (a bare number when the store published no currency, so nothing is invented). A budget is applied
   **only** when `store_currency == wanted_currency` (line 368); otherwise the product is kept and
   the result carries `CURRENCY_NOTE` — "budget not applied: the store prices in USD and the budget
   was stated in INR" (line 384). Chosen over silently dropping or silently keeping.
3. **The SerpApi path** supplies its own currency per result (`normalize_item`), which is why the
   historical dev order is INR: Google Shopping India quotes in INR. No conversion in either path.
4. **Selection materialises the server's own result.** `select_shopping_product` copies the
   canonical `ProductResult` into `shopping_intent.selected_product`; `SelectProductRequest`
   (`app/routers/missions.py:264-271`) carries `product_id` and nothing else.
5. **Checkout derivation** (`_derive_checkout_summary`, `missions.py:556-571`):
   `subtotal = str(selected.extracted_price)`, `currency = selected.currency or "INR"`.
   `ConfirmCheckoutRequest` (`missions.py:646-653`) declares **no fields** and `extra="ignore"`, so
   a body carrying `amount`/`currency` is parsed and discarded.
6. **Confirmation persists it** as `CheckoutConfirmation(quantity, subtotal=extracted_price,
   currency=…)`, `missions.py:678-683`.
7. **Payment amount** = `_derive_payment_amount(checkout, selected)` (`missions.py:722-733`): the
   persisted confirmation's subtotal, else the selected product's server-side price, else 400.
8. **The currency gate** (`missions.py:813-822`): `if currency != PAYMENT_CURRENCY → 400`, before
   any scaling and before any Razorpay call. The refusal states both codes and says RESOLVE does not
   convert. It changes no state: `intent["payment"]` stays absent.
9. **Scaling** (`missions.py:823`): `int((amount * 100).quantize(Decimal("1"), ROUND_HALF_UP))` —
   Decimal in, integer minor units out, one rounding rule, `× 100` only. This is the *only*
   arithmetic performed on an amount anywhere in the backend, and it changes units, never currency.
10. **The provider call** (`app/payments/razorpay_client.py:123`) passes `currency` through — the
    client is not INR-locked; the *gate* is what decides. (Separate flow: recovery payouts hard-code
    `"INR"` at `razorpay_client.py:49`.)
11. **The order row** (`missions.py:1094-1102`): `amount = Decimal(str(payment_data["amount"]))`,
    `currency = (payment_data.get("currency") or selected.currency or "INR").upper()`; `payload` is
    an ignored `dict`. Column: `currency String(8) NOT NULL default "INR"`
    (`app/models/db_models.py:127`), and `amount Numeric(12,2) NOT NULL`.
12. **Downstream money keeps the row's currency**: `_canonical_refund_amount`
    (`missions.py:1806-1825`) derives amount/paise/currency from the persisted payment and order and
    caps at `min(paise, order_paise)`.

**One caveat worth stating plainly, because it is the only soft spot in the chain.**
`ProductResult.currency` falls back to the *request's* currency when a store publishes none
(step 2). It is a fallback for an absent value, not a conversion — no number is rescaled — and it is
pinned as such by `test_a_store_that_reports_no_currency_does_not_get_one_invented`. The connected
store always publishes USD, so this branch is not live here. Any future currency work should make
that case refuse instead of adopt, and it should be its own change, not a side effect.

## 3. The four architectures, judged against this code (no opinions, only consequences)

| | A. USD price → charge USD | B. USD price → convert → charge INR | C. catalog restricted to INR-priced products | D. merchant channel owns Shopify payment; RESOLVE's rail is separate |
| --- | --- | --- | --- | --- |
| source of truth for price | Shopify variant price | Shopify price **× an unverified rate** | Shopify (or Google) price as quoted | the merchant's own order total |
| source of truth for currency | store | ambiguous by construction (two numbers exist) | store == payment currency | merchant order's currency, and RESOLVE's separately |
| FX source required | none | **yes — none exists in this repo or in any config** | none | none inside RESOLVE |
| rounding rule | existing `× 100`, `ROUND_HALF_UP` | would need a second rule: round after multiply? at what precision? when is the rate as-of? | existing | the channel's own |
| authorization boundary | `PAYMENT_CURRENCY` must stop being a constant; authority ceilings are INR-named and compared as bare numbers (`app/authority/engine.py:88`, `app/config.py:45`) so they become currency-blind | the ceiling would authorize a *different amount* than the merchant quoted — RESOLVE charging an amount nobody quoted | unchanged, already enforced by the gate | RESOLVE's `payment_verified` and Shopify's order state stay two facts; only a signed event + Shopify's own reads join them |
| refund currency | must equal the original payment currency (a Razorpay constraint, not an RESOLVE choice); payouts currently hard-code INR | charged in INR against a USD price: what is "the" amount to refund? | INR, as today | the channel's refund is Shopify-side; RESOLVE's refund is its own rail — both must be visible as both |
| audit requirements | currency on every money row (already present) + a declared per-currency ceiling | rate, rate source, as-of timestamp, and the converted amount — **all of which would need persisting, and none of which exists** | none new | event rows + linkage audit already exist |
| reconciliation | per-currency payment state | rate drift makes reconciliation a moving target | none new | RESOLVE reconciles *linkage*, never price |
| failure/timeout behavior | provider refusal per currency | timeout mid-conversion leaves a charge nobody can explain | unchanged: 400 before any call | the channel's own outcome-unknown problem (§6) |
| **can this code support it today?** | **No** — it is a multi-file architectural change (config model, authority units, refund currency, frontend symbol handling) | **No**, and STEP 3 forbids attempting it: no FX source is available or verified | **Yes** — it is exactly what the gate enforces now, and it is the architecture 14C.7I selected (§11) | **Yes, and it is the design the pairing contract was built for** — but it needs a channel that can assert a merchant-collected payment, which RESOLVE has no state for |

Consequences, not preferences: **C is the only option already true of the code**, and **D is the
only option under which a Shopify-native RESOLVE purchase can exist without RESOLVE inventing an
exchange rate.** A is a real project. B is the one that must not be done quietly.

## 4. What the external purchase channel has to be

It must be a system that can truthfully say *"I created/owned this Shopify order, and this is the
RESOLVE order it belongs to."* That means, concretely:

* it **owns the Shopify order lifecycle** — it calls the create, and it holds a credential with the
  write scope to do it. That credential is **not** RESOLVE's; RESOLVE keeps exactly the five scopes
  it has (`read_products, read_orders, read_returns, read_merchant_managed_fulfillment_orders,
  write_returns`) and `write_orders` stays absent;
* it **receives `resolve_order_id` before creating** — from a RESOLVE-side handoff it can name, not
  inferred afterwards. This is the load-bearing point: 14C.7F §3 measured that no field on an
  existing order can be used to correlate after the fact. The schema says the same thing back:
  `DraftOrderInput` accepts `metafields`, `customAttributes`, `poNumber`, `note`, `tags`,
  `presentmentCurrencyCode` — so a channel that **stamps the correlation at creation** is the
  design, and one that hopes to find it later is not. Which field to stamp in was measured, not
  assumed (14C.7I, read-only against 2026-07): `orders(query: "po_number:<value>")` is a filter
  Shopify honours (an impossible value answers zero orders), `metafield:` needs a provisioned
  definition of type `id` that RESOLVE cannot create, and `custom_attribute:` is **ignored** — an
  impossible value answers with *every* order. So the correlation lives in `poNumber`;
* it **creates through the path the schema actually offers.** Measured 14C.7I: `orderCreate(order,
  options)` takes an `OrderInput` whose `id` is **non-null** and which has **no `lineItems`** — it
  finalizes an order Shopify already has, it cannot sell a variant. The order-creating pair is
  `draftOrderCreate(input: DraftOrderInput!)` → `draftOrderComplete(id)`; the completed order is
  read from `DraftOrderCompletePayload.draftOrder.order`, because a DraftOrder GID is not an order;
* it **signs `resolve.order_linkage.v1` over the raw JSON bytes** with the shared
  `LINKAGE_INTEGRATION_SECRET` and posts `x-resolve-signature` — the contract 14C.7B defined and
  14C.7G exercised;
* it **deduplicates and survives its own timeouts** (§6, §7). It cannot delegate that to RESOLVE:
  the linkage event arrives *after* the order exists, so it is a record of an outcome, never a
  guard against creating a second one.

Options considered, with the verdict each got:

| option | runs | credential / scopes | creates the order? | can know `resolve_order_id`? | how it signs | dedupe | timeout/unknown | compatible with RESOLVE's payment semantics? | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Merchant-side backend / separate merchant app** | merchant infrastructure | its own custom-app token, with whatever write capability the merchant grants it — **held by it, never by RESOLVE**, and `write_orders` stays absent from RESOLVE's own app | yes — `draftOrderCreate` then `draftOrderComplete` (14C.7I §11: `orderCreate` cannot sell a variant) | yes, if RESOLVE hands the id over at purchase time and the channel stamps it as `poNumber` | HMAC with the shared secret, outside RESOLVE's repo | its own correlation key, checked by read-back (`orders(query:"po_number:…")`) before creating | must reconcile by reading its stamped id back | yes — and it is the only option that produces a *real* order | **The only viable channel. Implemented as `merchant-channel/` in 14C.7I; never run against a live store.** |
| Shopify Flow / Admin Functions automation | Shopify side | app-internal, no `write_orders` surface exposed to RESOLVE | `cartTransformCreate(functionHandle, …)` transforms a **cart**, not an order; no order-creating operation is available to RESOLVE's read-only app | no — it never sees a RESOLVE id | — | — | — | — | **Not a purchase channel.** |
| Storefront API cart + buyer-completed checkout | the shopper's browser + Storefront API | a Storefront access token (`storefrontAccessTokenCreate` exists in 2026-07; this deployment holds none, and creating one is a write RESOLVE does not make) | the *buyer* creates it, not RESOLVE | only via a cart/checkout attribute the buyer carries | whatever watches the resulting order | Shopify-side | Shopify-side | it makes the merchant the payment authority — which then makes RESOLVE's Razorpay charge a *second* charge unless the two rails are reconciled | **A design question, not a path today.** Needs an explicit answer about which rail the money moves on. |
| "Test purchase-channel fixture backed by a legitimately created Shopify-native order" (the spec's third acceptable form) | test harness | — | the fixture can only stand in for the **channel**, not for the **order** | yes | yes | n/a | n/a | n/a | **Cannot produce STATE A.** 14C.7G already implements this shape: it proves the boundary, not a purchase. |

No scope was requested or installed for any of these, and `write_orders` remains absent from RESOLVE's
app (re-read live in 14C.7I: `read_merchant_managed_fulfillment_orders, read_orders, read_products,
read_returns, write_returns` — five, unchanged).

## 5. Payment authority vs merchant authority

Two independent truths, and neither proves the other:

```
RESOLVE / Razorpay          →  payment_verified        "money moved on RESOLVE's rail, TEST mode"
Shopify (merchant side)     →  order exists, its own payment/fulfillment state
joined only by              →  a signed resolve.order_linkage.v1 event
                              + Shopify's own existence read + Product/Variant corroboration
```

Any design that treats one as evidence of the other is the bug this section exists to prevent:
a verified test payment is not a merchant order, and a merchant order is not a payment RESOLVE made.

| case | authoritative state | retry behavior | correlation key | reconciliation | money double-charged? | Shopify inventory duplicated? |
| --- | --- | --- | --- | --- | --- | --- |
| **1.** Razorpay verified, Shopify order not yet created | `payment_verified`; no order row until RESOLVE creates it; linkage `awaiting_linkage` | RESOLVE retries nothing — it waits for a signed event | `resolve_order_id` (the channel got it at handoff) | the derived state says *awaiting*; the UI never claims fulfillment; a real Shopify read only happens when an event arrives | **No.** `create-order` reuses a pending order and refuses after `verified`; `verify` requires the order id the server created plus an HMAC over `order|payment` | No — RESOLVE cannot touch inventory |
| **2.** Shopify order created, Razorpay verification failed | merchant order exists; RESOLVE has **no** `payment_verified` and no order row | the channel owns voiding/cancelling its order; RESOLVE owns its failed-payment state (a fresh attempt is allowed) | `resolve_order_id` stamped in the order | an event naming that RESOLVE order is refused `404` (`No RESOLVE order matches this event`) and *recorded* — a refusal is auditable | No | No (but the channel must not leave a stranded paid order; that is its reconciliation) |
| **3.** Payment verified, Shopify creation **times out** | RESOLVE: verified + `awaiting_linkage`. Channel: **`OUTCOME_UNKNOWN`** — it does not know whether an order exists | **Never a blind re-create.** Read back first (query by the stamped correlation id / `poNumber`), then act | `resolve_order_id`, stamped at creation time by the channel | the channel reconciles; RESOLVE keeps saying *awaiting* until an event with a Shopify GID it can corroborate arrives | No | **This is the real inventory risk**, and the schema makes it sharper, not softer: verified live on 2026-07, `orderCreate(order, options)`, `draftOrderCreate(input)` and `draftOrderComplete(id, paymentGatewayId, sourceName)` expose **no** idempotency-key argument (only `appUsageRecordCreate` and `orderCreateMandatePayment` do) — and of that pair, only the draft-order pair can create a selling order at all (§4) |
| **4.** Shopify order created, RESOLVE crashes before storing the GID | nothing is lost: the order row and `payment_verified` are already committed; only the linkage is missing | the channel redelivers with the **same `event_id`** — the `integration_events` primary key makes redelivery harmless, and a settled replay performs **zero** Shopify reads (measured in 14C.7G) | `event_id` + `resolve_order_id` | RESOLVE is the passive side; the event is the reconciliation instrument | No | No |
| **5.** The same purchase request is retried | one RESOLVE order row per mission (`mission_id` unique); payment state reused or refused as described | RESOLVE: idempotent. Channel: must dedupe **before** creating, by reading its stamped id back | `resolve_order_id` for the purchase, `event_id` for the pairing | both directions of the pairing are unique-indexed, so a duplicate lands on a conflict instead of a second record | No | Only if the channel skips its read-back. The signed event cannot prevent it — it arrives after the order exists |

## 6. Outbox and outcome-unknown

**RESOLVE itself still has no outbox, and none was added to it.** Every action RESOLVE takes against
Shopify is a read; it sends nothing outbound that could land twice. The outbox belongs to the
**channel**, which is the thing that creates orders — and in 14C.7I that channel exists, so this
section is no longer a design for a future task: the shape below is what
`merchant-channel/merchant_channel/outbox.py` implements, in the channel's own SQLite file, with no
new RESOLVE table, column or status.

```
correlation_id          the channel's own idempotency key — a pure function of the purchase, never
                        a timestamp, never random; stamped INTO the order as `poNumber` so the
                        read-back can find it
resolve_order_id        the RESOLVE order this purchase is for (correlation, from the handoff)
payload_hash            sha256 of the canonical handoff payload (order, product, variant, quantity,
                        currency) — no amount, because the merchant's catalog owns the price
attempted_at            when it sent
external_order_id       the Shopify Order GID, once Shopify's read has named it
status                  NOT_STARTED | PENDING | CONFIRMED | OUTCOME_UNKNOWN | FAILED | RECONCILED
failure_reason          secret-free, bounded to 500 characters
reconciliation_state    what the read-back concluded: matched | no_match | ambiguous
linkage                 NOT_SENT | SENT | DELIVERY_UNKNOWN | REFUSED — the second half of the work,
                        plus event_id/event_body so a redelivery is byte-identical
```

The state machine's one hard rule: **`OUTCOME_UNKNOWN` is never retried blindly.** The transition
out of it is a read, and the read's conclusion is `matched` / `no_match` / `ambiguous` — and
`ambiguous` means a human decides, because picking one of several candidate orders is exactly the
guess RESOLVE refuses to make when filing a return.

This is not a new pattern in this repository, and that is deliberate: 14C.4 built the same shape one
step later in the pipeline, for the one mutation RESOLVE *is* allowed to make — persist the
submission claim before calling, treat a lost answer as `outcome_unknown`, reconcile read-only, never
re-send blind, and read quantity from `FulfillmentLineItem.quantity` (see
`app/tools/shopify/return_provider.py`, `tests/test_shopify_return_provider.py`). Verified live for
that mutation: `returnCreate` has no Shopify idempotency key either. So the rule is uniform:
**a Shopify write whose answer was lost is reconciled, never repeated.**

If RESOLVE ever initiates a purchase rather than receiving one, that channel-shaped outbox is the
seam to reuse — and it would be the moment `write_orders` reappears on RESOLVE's own app, which is
precisely why it is not this project's direction.

## 7. Duplicate safety, by object

| hazard | mechanism that prevents it | where it lives | proven by |
| --- | --- | --- | --- |
| duplicate **Shopify** order | the channel's `correlation_id` + read-back before creating. **No Shopify-side guard exists in the 2026-07 GraphQL schema** (`draftOrderCreate`/`draftOrderComplete` — the pair that can create a selling order, §4 — take no idempotency-key argument; neither does `orderCreate`). Whether the Admin *REST* endpoints honour an `Idempotency-Key` header was **not** verified here and must not be assumed | the channel | schema introspection, read-only, 14C.7H and 14C.7I |
| duplicate **RESOLVE order** | `resolve_orders.mission_id` unique — one order row per mission, and the create path returns the existing row | storage + `create_resolve_order` | existing lifecycle tests |
| duplicate **payment** | pending `create-order` is reused, a `verified` payment refuses a new order, `verify` only accepts the order id this backend created plus a valid HMAC, and the webhook/verify path never trusts a client success flag | `create_shopping_payment_order` / `verify_shopping_payment` | `tests/test_shopping_payment.py` |
| duplicate **linkage event** | `integration_events.event_id` primary key: identical bytes → settled replay with zero Shopify reads; same id, different bytes → 409; unclaimed-but-stale → refused before the claim | `handle_linkage_event` | `tests/test_order_linkage_integration.py`, `tests/test_shopify_pairing_channel_e2e.py` |
| one merchant order backing two purchases | `uq_resolve_orders_provider_order_reference` (UNIQUE, NULLs distinct) plus the writer's own check → 409 "already linked to a different RESOLVE order" | storage invariant, not code habit | 14C.7G negatives |
| an established pairing quietly moved | the writer refuses to re-point a linked row, and never asks Shopify about the new GID | `link_order_to_shopify_order` | 14C.7G negatives |
| a refused claim becoming a second **purchase** | a reclaim is a separate door: it requires a settled refusal, re-reads the store by correlation, re-verifies identity, and never consults `may_create` — so `ONE correlation_id -> AT MOST ONE Shopify order` holds across any number of claims | the channel (`reclaim_linkage`, `begin_linkage_attempt`) | 14C.7L: `tests/test_linkage_reclaim.py` |
| a new claim identity invented by a caller | attempt 1's derivation is byte-frozen and the attempt number exists only on the outbox row, so re-naming a settled claim is not reachable from any call site | the channel (`event_id_for`, `purchase_outbox.attempt`) | 14C.7L: `test_attempt_one_keeps_the_frozen_derivation_and_only_later_attempts_are_named` |

## 8. The signed contract, unchanged

`resolve.order_linkage.v1` still carries exactly `event_id`, `event_type`, `shopify_order_id`,
`issued_at`, and one of `resolve_order_id` / `mission_id`, plus `source`; `extra="forbid"`; HMAC-SHA256
over the raw body in `x-resolve-signature`; freshness 300 s ± 60 s; the linkage route is the single
production caller of the single writer; an unconfigured deployment answers 503.

The currency-relevant property, now pinned by test: **the event carries no amount, no currency and
no price.** Admitting a pairing can therefore never also restate what was paid or in which currency —
those remain facts of RESOLVE's own persisted payment and order row, which the currency gate decides.

## 9. Frontend

No fake Shopify checkout, no conversion UI, no "Shopify order created" state, and no change to the
SerpApi shopping flow. The guard a Shopify-native USD shopper needs already exists and is
server-derived: the 400 detail from the currency gate is surfaced verbatim
(`app/workspace/page.tsx` passes `err?.detail`), and the amount handed to Razorpay is
`data.payment.amount_paise`, computed in the backend.

Known display limitation, recorded rather than fixed: `components/workspace/IssuePanel.tsx` prefixes
a literal `₹` to assessment/refund/replacement amounts while also printing the stored currency code.
Unreachable while the currency gate holds (no non-INR order row can exist), and it belongs to
whichever task takes option A or D — not to a boundary test.

## 10. Prerequisites, in order (as written in 14C.7H; status carried by §11)

1. Decide A vs C vs D for currency (§3). C and D need no conversion; B is out of scope by policy.
2. Stand up the merchant-side purchase channel, holding its own write capability, that receives
   `resolve_order_id` and stamps it into the order it creates.
3. Provision `LINKAGE_INTEGRATION_SECRET` on RESOLVE and give the same value to the channel —
   environment-only, never committed, never printed, never in a fixture, audit row, response or doc.
4. Channel signs and delivers one real event for one real RESOLVE order.
5. Observe the positive path against the real store: existence + corroboration → `linked`.
6. Only then is `RETURN_PROVIDER=shopify` a defensible configuration.

**STATUS (14C.7H): DESIGN COMPLETE — THE EXTERNAL PURCHASE CHANNEL AND THE CURRENCY BOUNDARY REMAINED
PREREQUISITES.** Nothing above was implemented then; the code's boundaries are what the tests pinned.

## 11. 14C.7I — the decision, the channel that follows from it, and the gate that is still shut

### 11.1 Selected architecture: **Option C**, and why the code — not an opinion — selects it

Phase A evaluated only C and D against the repository as it is (A and B analysed, not implemented).
The decision rule was technical state, so the reasons are all verifiable:

* **C is already true of the money path.** `PAYMENT_CURRENCY = "INR"` is a code constant
  (`app/routers/missions.py:553`), `Settings` has no currency field, the gate at
  `missions.py:806-822` refuses any non-INR handoff **before** `amount_paise` is computed, and no FX
  construct exists anywhere in `app/` (AST scan, pinned by `tests/test_currency_boundary.py`). Under
  C the merchant order and RESOLVE's payment are denominated in the same currency, so the single
  currency-blind ceiling (`shopping_refund_max_inr`, `app/config.py:45`, compared as a bare number in
  `app/authority/engine.py:88`) stays meaningful, refunds stay in the original payment currency, and
  nothing needs a rate, a rate source, or an as-of timestamp.
* **D would require a contract RESOLVE does not have.** RESOLVE's order creation is gated on
  `payment_verified` produced by Razorpay's verify path. A purchase the *merchant* collected has no
  such fact, so D needs a distinct order origin and state, a refund authority split (RESOLVE cannot
  refund money it never received), and UI that never claims a payment it did not make. None of that
  exists, and none of it was smuggled in by bypassing the gate. **D is not implemented.**
* **A is a project, not a change** — configurable payment currency, verified Razorpay currency
  support, currency-aware ceilings, refund/replacement currency, and the literal `₹` prefixes in
  `components/workspace/IssuePanel.tsx`. Analysed only.
* **B is rejected**: it requires a rate, and there is no approved FX source in this repository, no
  rate column, and no verified rate. Hard-coding, guessing, calling an external API, converting in
  the frontend, or silently reading USD as INR are all refused — by the gate and now by tests.

### 11.2 What was built: `merchant-channel/`, outside RESOLVE's write boundary

A separate package (`merchant_channel`) standing in for the merchant-owned infrastructure: its own
`MERCHANT_*` environment values (`config.py`, which raises if a RESOLVE-owned name is supplied
instead), no import of `app` anywhere in it, its own documents (`documents.py`), its own correlation
(`correlation.py`), its own outbox (`outbox.py`), its own gateway (`gateway.py`), and its own voice —
exactly the existing `resolve.order_linkage.v1` event, HMAC-SHA256 over the raw bytes it sends
(`linkage.py`). RESOLVE stayed passive: the route, the single writer
(`app/tools/shopify/linkage.link_order_to_shopify_order`) and the contract are untouched, and no
second writer was added.

Payment semantics under C, stated honestly: the channel creates the order and **does not mark it
paid** — no document contains `paymentGatewayId`, `orderMarkAsPaid`, or `orderCreateManualPayment`,
because no money reached the merchant through this path. `draftOrderComplete` is called without a
payment gateway, so the order lands as unpaid/`PENDING` rather than asserting a settlement that
never happened.

Duplicate protection is the outbox's job, since Shopify offers no idempotency key (§7): the
correlation id is a pure function of the purchase, `begin_create` is the only door to a create, and
`OUTCOME_UNKNOWN` is refused there — including a dangling `PENDING`, which is converted to unknown
on the way past. `no_match` from a read-back is the only conclusion that reopens a create; `ambiguous`
keeps it shut. A 5xx from RESOLVE is recorded as delivery-unknown and re-sent as the **same bytes
under the same `event_id`**, never re-stamped.

### 11.3 Proven, and not claimed

`backend/tests/test_merchant_channel_linkage_acceptance.py` drives the real channel code and feeds
the bytes it produces to the real RESOLVE route over HTTP: a positive pairing lands as `linked` on a
RESOLVE order created by the production flow; a byte-identical replay is answered from storage with
zero further Shopify reads; a second RESOLVE order claiming the same merchant order is refused and
the first claim survives; an order RESOLVE cannot confirm with Shopify ends as a recorded rejection,
not a linkage; and a USD handoff never reaches RESOLVE at all (`0` integration events, still
`awaiting_linkage`).

### 11.4 What is still missing — and why no real order exists

The store publishes **USD** (`shop.currencyCode`, measured live) while C requires the merchant order
to be denominated in the currency RESOLVE charges in. So the channel's first gate refuses every
purchase this store can offer, which is the correct outcome and exactly the money lie Option C
exists to prevent — told from the merchant side. Independently, there is no merchant-side credential
in this environment (and asking for one was refused by the task), so the gateway is exercised only
through injected transports.

Therefore: **no real Shopify order was created; no positive pairing against the live store was
achieved; `STATE A` is not claimed.** The dev store's only order (#1001) was read before and after
this task's checks and is byte-identical, is not paired to anything, and was never used as a
positive result.

| prerequisite | status after 14C.7I |
| --- | --- |
| currency/payment architecture decided | **DONE — Option C**, documented above |
| merchant-side purchase channel | **DONE as code** (`merchant-channel/`), never executed against a live store |
| channel's own credential with write capability | **ABSENT** — not held by RESOLVE, not requested, not configured here |
| merchant catalog denominated in the payment currency | **ABSENT** — store currency is USD, payment is INR; changing it is a merchant-side currency setting (`marketCurrencySettingsUpdate` is a mutation RESOLVE cannot make) |
| `LINKAGE_INTEGRATION_SECRET` on both sides | **ABSENT** on this deployment (route answers 503 live) |
| one real event for one real RESOLVE order | **NOT DONE** — blocked by the two rows above |
| positive path observed against the real store | **NOT DONE** |
| a verified way to dispose of a disposable order | **SHAPE VERIFIED (14C.7M), BEHAVIOUR NOT** — `orderCancel`/`orderClose`/`orderDelete`/`draftOrderDelete`/`transactionVoid` read from the live 2026-07 schema into `merchant-channel/schema/cancel_2026-07.json`; no `draftOrderCancel` exists; success conditions, financial-status consequence and required scopes are **UNKNOWN — not exposed by introspection** |
| `RETURN_PROVIDER=shopify` defensible | **NOT DEFENSIBLE** — stays `none` |

Task 14C.7J re-measured this table instead of re-asserting it: `backend/verify_14c7j_preflight.py`
(read-only, three `query` documents, 0 mutations) reported the same six unmet prerequisites and
stopped before creating anything, with before/after snapshots byte-identical. See
`docs/shopify-order-linkage.md` §12.3. Option C is therefore still the selected architecture *and*
still the reason no live pairing exists: it is the architecture that refuses to convert, and this
store is not priced in the currency its payments would have to match.

Task 14C.7K attempted the live pairing again, was stopped by the same prerequisites, and used the
time to settle one protocol question a real attempt would otherwise hit mid-flight: what happens when
the merchant order is confirmed but RESOLVE refuses the linkage, given a deterministic `event_id`.
The answer, measured and pinned by `backend/tests/test_linkage_refused_event_recovery.py`, is in
`docs/shopify-order-linkage.md` §12.4 — a settled refusal is terminal for its event id, and the
protocol's own recovery path is a new event identity (which relaxes no duplicate protection).

Task 14C.7L closed the half 14C.7K left open. The channel *can* now mint that second identity, as an
attempt counter in its own outbox: `reclaim_linkage()` re-reads the merchant store, re-verifies
correlation, store, product, variant and quantity against the original handoff, and only then rotates
the attempt — with no path to a create, and no new identity available over a claim that was never
answered. §12.3's six prerequisites are untouched by that: they are merchant-side facts, not protocol
gaps, and the pairing is still blocked on all of them. See `docs/shopify-order-linkage.md` §12.5.

Task 14C.7M collected the disposal evidence a future disposable order needs, read-only. 24
introspection `query` documents, 0 mutations, saved to
`merchant-channel/schema/cancel_2026-07.json`: `orderCancel(orderId: ID!, reason: OrderCancelReason!,
restock: Boolean!, notifyCustomer, staffNote, refundMethod)` returns `{ job, orderCancelUserErrors }`
— asynchronously, and with **no order object in the payload**, so a result must be re-read rather than
taken from the response; `orderClose`, `orderDelete`, `draftOrderDelete` and `transactionVoid` are
likewise spelled out there. Two absences are findings too: **there is no `draftOrderCancel`** (an
uncompleted draft is deleted, not cancelled) and **`transactionVoid` addresses an
`OrderTransaction`, not an order** — so "void the order" is not a thing this schema offers. What
introspection structurally cannot say is left said as unknown: whether any of them succeeds before or
after `draftOrderComplete`, what `displayFinancialStatus` becomes, and which scope each requires. No
disposal code was implemented, and the shape of these operations is not evidence that calling one is
safe. See `docs/shopify-order-linkage.md` §12.5.

## 12. Tasks 14C.7N and 14C.7O: the two sides of the write line, now enforced in code

This document has spent eleven sections describing a boundary that lived in *decisions*: RESOLVE does
not buy, the channel does. 14C.7N moved one half of it into the transport, and deliberately did not
move the other. 14C.7O finished the first half — the declared return write left the read client for a
capability of its own — and still did not touch the channel's side.

**RESOLVE's read-only client is query-only by construction.** `execute_graphql` classifies the document
it was handed as its first act — before the client-credentials token exchange (which is itself an HTTP
call), before the `X-Shopify-Access-Token`/`Authorization` header is selected, before the JSON body is
built, before `httpx` is touched. A document counts as a read only if it is exactly one root `query`
operation. `subscription`, an empty/non-text/unbalanced/bodyless document, a document with more than
one root operation, and **any write at all — including the one operation `SHOPIFY_DATA_USE` declares,
which since 14C.7O is no longer this client's to carry** — are
refused locally as `ShopifyReadOnlyViolation`, and the refusal says so: *"No token exchange, no header,
no request body and no HTTP call took place."* The refused document's text is never quoted back — only
its kind, and for a write its single root operation name — because a document is caller-supplied
content.

**Concretely, and this is the part that matters for the purchase-channel question: RESOLVE's
read-only client cannot be used to create, cancel, close, delete, void or refund an order — and, since
14C.7O, not even to open a return.** Every disposal operation
14C.7M measured out of the live 2026-07 schema is refused by that gate, and so are
`draftOrderCreate`/`draftOrderComplete` — the channel's own purchase mutations, offered to RESOLVE's
client in `backend/tests/test_shopify_read_only_gate.py` and refused with zero HTTP requests. So is
`returnCreate`'s under-implemented neighbour, `refundCreate`. Nothing about the currency gate, the
INR decision, or Option C changed because of this: a boundary that lives only in a routing decision is
one refactor away from being false.

**The merchant purchase channel is a separate capability, and no global mutation ban was put on it.**
`merchant-channel/` buys through a *merchant-owned* app holding scopes RESOLVE's app does not have, and
§11.2 placed it outside RESOLVE's write boundary on purpose. Enforcing RESOLVE's read-only rule inside
that package would not make RESOLVE safer — it would only disable the component whose job is the
purchase, and the task is explicit that this channel "may eventually perform authorized purchase
mutations". So the gate is absent from every module in `merchant_channel` (asserted by scanning the
package), and `merchant-channel/tests/test_gateway_transport.py` gained the positive half: the gateway
still transmits `draftOrderCreate` → `draftOrderComplete` through an injected transport, with its own
token in its own header. **The channel is not read-only and must not be described as read-only
anywhere in this document.**

**Where the boundary sat after 14C.7N, and where 14C.7O moved it.** Until 14C.7O, RESOLVE routed
exactly one write through `ShopifyReadOnlyClient` — `returnCreate`, from
`app/tools/shopify/return_provider.py` — because 14C.1/14C.4 chose to reuse the one credential system
rather than create a second token path, and the return-provider tests exercised it over the real
client. The gate therefore treated the data-use declaration as an allowlist of one operation, which was
the honest shape of "read-only" then and was not the shape the name implies. 14C.7O made the
announcement in this document's own words true by **moving the write, not by widening the rule**:

| capability tier | object | what it will transmit | whose credential |
| --- | --- | --- | --- |
| RESOLVE read-only path | `ShopifyReadOnlyClient` | **query documents only.** Zero mutation behaviour: no allowlist, no declared-write exception, no method that carries a write | RESOLVE's configured `SHOPIFY_*` values |
| RESOLVE return capability | `ShopifyReturnWriteClient` (`app/tools/shopify/return_write_client.py`), obtained only by `app/tools/returns/__init__.py` | **one declared `returnCreate`** — and no query, because it is mutation-only | the **same** credential, through a `ShopifyAdminTransport` shared with the read client |
| merchant purchase channel | `merchant-channel/`'s `ShopifyAdminGateway` | `draftOrderCreate` → `draftOrderComplete` under merchant-owned scopes | the channel's own access token, unchanged |

Separating the capability did **not** separate the credential: `get_shopify_clients()` builds one
transport for both objects, so there is still one client id, one secret and one short-lived token —
a test measures one token exchange across a read-then-write flow — and 14C.7O requested no scope,
changed no app installation, and enabled no provider (`RETURN_PROVIDER` stays `none`). The
merchant-owned half of the line is exactly as §11.2 placed it: independent, mutation-capable, and
not this repository's boundary to enforce. RESOLVE's capability refuses the channel's documents
because they are not RESOLVE's declared write, and the channel imports none of RESOLVE's gate,
transport, or capability.

| question | answer after 14C.7O |
| --- | --- |
| can a mutation reach RESOLVE's read-only transport? | **No** — none, including `returnCreate`; the read path is query-only |
| where does RESOLVE's declared write go? | `ShopifyReturnWriteClient.submit_return_create`, a separate object, one operation, same credential |
| can a malformed or unknown document reach either capability? | **No** — both fail closed, before the token exchange |
| is the refusal provable rather than asserted? | **Yes** — `backend/tests/test_shopify_return_write_capability.py` and `test_shopify_read_only_gate.py` count HTTP attempts and require exactly 0 |
| is the merchant channel mutation-capable still? | **Yes**, unchanged, with its own credential and gateway; neither RESOLVE capability transmits its purchase documents |
| was cancellation/disposal implemented? | **No** — and a test asserts no `app/` module carries a cancellation document |
| did the six disposal unknowns get answered? | **No** — still `UNKNOWN — NOT EXPOSED BY INTROSPECTION`; only a real disposable order and a real mutation can answer them, and this task was forbidden to make either |

It is not today a live risk: the provider is registered as nothing, no endpoint reaches it, and
`RETURN_PROVIDER` stays `none`.ED BY INTROSPECTION`; only a real disposable order and a real mutation can answer them, and this task was forbidden to make either |


