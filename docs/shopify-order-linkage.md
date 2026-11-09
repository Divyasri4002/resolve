# Authoritative Shopify order pairing

Status: **implemented, read-only, and inert by configuration.** Shopify discovery, selection and
identity are live; `RETURN_PROVIDER=none`, so nothing in this document can file a return.

Scope: Tasks 14C.5 (single writer), 14C.7B (signed inbound contract), 14C.7D–7E (canonical Shopify
identity and variant-aware return grounding) and 14C.7F (corroboration of a pairing, and the
lifecycle that honest reporting requires).

---

## 1. Why product similarity is not enough

A RESOLVE order and a Shopify order are two records in two systems. Everything that makes them look
alike — the product title, the amount, the currency, the merchant, the timestamp, the SKU — is
**shared material**. Two customers can buy the same shampoo at the same price on the same minute
from two different stores, and a Google Shopping result and a Shopify order can describe the same
product without being the same purchase. The dev environment contains exactly that trap:
`order_44ec95eb8e58` (410.00 **INR**, Google Shopping, merchant TabletBox) and Shopify order #1001
(410.00 **USD**, Shopify-native, RESOLVE Commerce Lab). Same number, same product name, no
relationship whatsoever.

Acting on that resemblance would not be a small error. A linkage is what a return is aimed at, so a
similarity match files a return against someone else's purchase — an action a merchant must honour,
against goods a different customer bought. The pairing code in `app/tools/shopify/linkage.py`
therefore never reads a title, an amount, a currency, a merchant name, a timestamp or a SKU. That is
not a policy stated in prose; `tests/test_shopify_order_pairing.py` fails if those fields appear in
the module's code or in the GraphQL document it sends.

## 2. Why the Shopify Order GID must come from an authoritative purchase/order channel

RESOLVE holds a **read-only** Shopify integration. It can ask the store about an order; it cannot
create, edit or annotate one. So the only honest sources for "this RESOLVE order corresponds to
this Shopify order" are:

1. the system that actually placed the Shopify order — the purchase channel, which knows both
   identifiers because it caused one and observed the other; or
2. a field on the Shopify order that already carries a RESOLVE identifier, which RESOLVE can read.

Option 2 was investigated live against 2026-07 (see §3). Every field that could carry a correlation
value is empty on this store today, and populating any of them is a **write** — `write_orders` for
`tags`/`note`/`metafields`, and a metafield *definition* of type `id` before `customId` look-up
works at all. RESOLVE has neither, and requesting them is out of scope.

So today the pairing arrives only through option 1: a signed event from an integration that knows
both ids. RESOLVE never infers it, and the absence of a pairing is a stated condition, not a
failure to be worked around.

## 3. What Shopify actually exposes (measured live, 2026-09-28, API 2026-07)

Read with the scopes held today, against order #1001 (`gid://shopify/Order/7990366601503`):

| Candidate | Readable now? | Value on #1001 | Merchant-visible | Stable | Unique | Survives retry | Crash recovery | Proves the relationship | Needs a new scope | Needs a write | Fit for reconciliation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `name` / `number` | yes | `#1001` / `1001` | yes | yes | per store, not per pairing | yes | yes | **no** — a sequence number says nothing about RESOLVE | no | n/a | as a display id only |
| `sourceName` | yes | `shopify_draft_order` | yes | yes | no | yes | yes | **no** — names the channel, not the order's counterpart | no | n/a | weak context |
| `sourceIdentifier` | yes | `null` | store-defined | if the channel sets it | only if the channel guarantees it | yes | yes | **only if the channel puts the RESOLVE id there** — a convention RESOLVE cannot require or verify | no | **yes**, to populate | possible with a documented convention |
| `tags` | yes | `[]` | yes, admin-visible | no (mutable) | no | yes | yes | **no** | no | **yes** | no |
| `note` | yes | `null` | yes, and human text | no | no | yes | yes | **no** | no | **yes** | no |
| `customAttributes` | yes | `[]` | yes | no | no | yes | yes | **no** | no | **yes** | no |
| `metafields` / `metafield(namespace,key)` | yes | empty / `null` | depends on definition | yes | only under a unique definition | yes | yes | **only** with a metafield *definition of type `id`*, which does not exist here | no | **yes** — to define, and to write | the strongest available, not available today |
| `orderByIdentifier(identifier:)` | callable | accepts `id` and `customId` only | n/a | n/a | yes | yes | yes | `customId` requires that unique metafield definition; without it Shopify answers `Metafield definition of type 'id' is required when using custom ids.` | no | **yes** | blocked on the row above |
| `statusPageUrl`, `registeredSourceUrl` | **refused** | — | customer-facing | — | — | — | — | — | **yes** (protected customer data approval) | — | never: it is customer data |
| `lineItems… product { id } variant { id }` | yes | product/variant GIDs | n/a | yes | per line | yes | yes | **identity corroboration, not pairing** — see §5 | no | n/a | yes, for checking |

`poNumber` (`null`), `confirmationNumber` (`LFFMPMM8D`, Shopify-generated) and `legacyResourceId`
were also read and are equally unable to name a RESOLVE order.

**Recorded conclusion:** no field currently available to RESOLVE can establish a correlation without
a write RESOLVE is not permitted to perform. 14C.7F does not invent a workaround for that; it makes
the signed channel's claim *checkable* (§5) and states the unpaired condition honestly (§6).

## 4. How `resolve.order_linkage.v1` works

`POST /integrations/shopify/order-linkage` — the only inbound door to a pairing. It takes **no
session, no cookie and no `Authorization` value**: a browser cannot reach it, signed or not.

The event may name two references and nothing else
(`app/integrations/order_linkage.py`, `OrderLinkageEvent`, `extra="forbid"`):

```
event_id, event_type, shopify_order_id, issued_at, source,
and exactly one of { resolve_order_id, mission_id }
```

Anything else — an amount, a currency, a title, a merchant, a Razorpay payment id, a fulfillment
line id, a return reason, a `user_id`, or a `provider_order_reference` supplied by the sender —
invalidates the event outright.

The order of operations is the design, and nothing is read, stored or acted on before step 2
finishes:

1. **secret configured?** unset → `503` for everything. Disabled is stated, never implied.
2. **HMAC-SHA256 over the exact raw bytes** in `x-resolve-signature`, constant-time compared.
   Missing, malformed and wrong all answer `401`; the response never says which.
3. strict parse, then **freshness** on `issued_at` (window + 60 s skew, in both directions).
4. **`event_id` claim** — an INSERT against a unique index, not a read that could race.
   Same id, same bytes, already settled → `200` replayed from storage. Same id, different bytes →
   `409`. Same id still in flight → `503`, because "done" would describe work that has not happened.
5. **RESOLVE order lookup** by the reference the event names; ownership is taken from the
   **persisted row**, never from the event body.
6. **`link_order_to_shopify_order`** — the single writer (see §5).
7. **audit**, accepted or refused, plus the inbox row's settled outcome.

`integration_events` is the inbox; `resolve_orders.provider_order_reference` is the fact. The handler
never touches the column itself, and a refused event is remembered rather than discarded.

## 5. What a signature proves, and what only Shopify's data can prove

A signature proves **who said it**. It cannot prove the statement is about the same purchase. So
before anything is stored, `linkage.py` requires two things of Shopify:

1. **existence** — `read_order_reference(gid)`: Shopify must name that exact order at this store.
   An invented, mistyped or foreign id is refused instead of becoming a permanent record.
2. **corroboration** (14C.7F) — `read_order_line_identity(gid)`: when the RESOLVE row carries
   canonical Shopify identity (the Product GID, Variant GID and store domain written at 14C.7D's
   server-side selection), Shopify's own `LineItem.product.id` / `LineItem.variant.id` must sell
   **exactly one line** with that product *and* that variant, at that store domain.

It is a contradiction detector, not a matcher. It refuses when:

* the variant RESOLVE sold is not on the order (even if the product is);
* the product is not on the order at all;
* the variant appears on more than one line — which one this is cannot be determined;
* the matched line's own product disagrees with its variant;
* the identity was read from a different store than the order being linked;
* Shopify reports the line list was not fully returned (`hasNextPage`) — "not among the lines I
  read" is not "not on this order", so neither a match nor a refusal is claimed;
* the RESOLVE row carries only part of a Shopify identity, which is a contradiction inside RESOLVE's
  own record and is caught **before** the store is asked anything.

A merchant order whose lines sell what RESOLVE sold is still not proof of a pairing — the buyer could
have ordered twice. That is why this check only ever **narrows a claim that a signature already
authorised**, and never generates one. Where RESOLVE has no Shopify identity to compare (a purchase
discovered outside a catalog it can name), nothing is corroborated and nothing is denied: existence
is the whole of what RESOLVE can know, and the row's Shopify columns stay NULL, which is what keeps
a return from being aimed at it.

## 6. Why the browser cannot submit the Shopify order reference

Three separate reasons, each enforced structurally:

* **No channel.** The return endpoint takes no body; `POST /{mission_id}/order` and
  `/order/confirm` read every authoritative value from persisted state and ignore anything sent. A
  test that includes a signed-in `Authorization` header with an event body still gets `401` — a
  user's own session buys nothing.
* **No use for it.** The merchant-side order id is never sent to the client. The order payload
  carries `merchant_order_linked` and `merchant_order_linkage`, both derived. A value the browser
  holds is a value the browser can try to hand back.
* **No authority even if it arrives.** `link_order_to_shopify_order` derives the linked order's owner
  from the persisted row (`actor_user_id = order.user_id`), so a body that named another user,
  another mission or another Shopify order would change nothing about which pair is written.

## 7. Why RESOLVE does not create Shopify orders in this task

Creating an order is `orderCreate`/`draftOrderCreate` + `write_orders` — a write capability that
would make RESOLVE the author of the merchant record it later has to judge. The whole value of the
linkage is that the merchant-side fact comes from **outside** RESOLVE's own wishes. If RESOLVE
created the order it could also choose which order to pair, and the corroboration in §5 would be
checking its own homework. Payment, discovery and identity are already wired; the missing piece is a
merchant-side purchase, which belongs to a purchase channel, not to this application.

## 8. How payment verification differs from merchant-order creation

`payment_verified` means Razorpay TEST mode moved test money. `order_created` / `order_confirmed`
mean RESOLVE recorded its own order context and the buyer acknowledged it. None of them means a
merchant order exists — and, symmetrically, an existing pairing does not mean anything shipped.
`_fulfillment_note()` states only the case it is in: no merchant order is claimed while the
reference is NULL; once paired it says the merchant order was confirmed by the store and that
RESOLVE has **not** asserted shipping, delivery, or a return.

## 9. What happens when linkage is delayed

The order sits in `awaiting_linkage` (see §10) and everything merchant-side refuses honestly:
`resolve_return_target` refuses before any network call when `provider_order_reference` is NULL,
and refund/replacement stay governed by the existing Authority Engine rules, unchanged by this task.
Nothing times out, nothing is guessed, nothing retries itself into a linkage. When the signed event
finally arrives — minutes, days, or after any number of restarts — it is processed exactly like any
other event, because the pairing is a fact in storage rather than a state to be reconstructed.

## 10. The lifecycle, and why it is derived rather than stored

```
PAYMENT_VERIFIED → RESOLVE order (order_created → order_confirmed)
                                    │
                    ┌───────────────┴────────────────┐
                    │                                │
             merchant_order_linkage          merchant_order_linkage
              = "awaiting_linkage"              = "linked"
             (canonical Shopify identity       (provider_order_reference
              and no pairing yet)               is set, by the single writer)
                    │
             merchant_order_linkage = "unlinked"
             (no Shopify catalog identity on this purchase: RESOLVE cannot
              say a merchant order is coming, and cannot rule one out)
```

`resolve_orders.provider_order_reference` is the single canonical answer to "is it paired?" — 14C.5
made it a storage invariant with a unique index, and 14C.7B made one function the only writer. A
second stored copy of the same fact could only ever drift from it, and a return aimed by a stale
copy is the failure mode this whole chain exists to prevent. So the state is **computed** from the
columns that already exist (`_merchant_order_linkage`, `app/routers/missions.py`), which also means
14C.7F needed **no migration at all**: no new column, no new index, no new status value, no backfill
of the historical Google Shopping row.

## 11. What must be true before `RETURN_PROVIDER=shopify` can be enabled safely

1. `LINKAGE_INTEGRATION_SECRET` configured, and a real merchant-side integration signing
   `resolve.order_linkage.v1` events for orders **it** created.
2. At least one RESOLVE order carrying canonical Shopify identity (a purchase made through
   `PRODUCT_SEARCH_PROVIDER=shopify` selection) with `provider_order_reference` set — through the
   writer, never through a migration or a guess.
3. A currency path: the dev store prices in USD while payment is INR-only
   (`PAYMENT_CURRENCY`), and RESOLVE does not convert. A Shopify-native cart cannot be paid today.
4. `write_returns` stays the only write scope, `returnCreate` the only mutation, the
   claim-before-mutation and `outcome_unknown` rules unchanged, and quantity still read from
   `FulfillmentLineItem.quantity` — never `remainingQuantity`.
5. A decision about multi-variant and multi-line products on the store, so the corroboration in §5
   is exercised against real data rather than only against fixtures.

## 12. Task 14C.7G: is there a positive purchase channel yet? (measured 2026-09-28)

**No. POSITIVE SHOPIFY PURCHASE CHANNEL NOT AVAILABLE.** The store was re-read live, read-only:

| measured | value |
| --- | --- |
| orders in the dev store | 1 (`#1001`, `gid://shopify/Order/7990366601503`) |
| how it was created | `sourceName = shopify_draft_order` — a merchant-side draft order, before this task existed |
| returns in the store | 0 |
| RESOLVE rows with `provider_order_reference` | 0 (dev DB: 1 order, the Google Shopping row, identity columns NULL) |
| scopes held | the five above; no `write_orders`, no `write_products`, no `write_merchant_managed_fulfillment_orders` |
| `LINKAGE_INTEGRATION_SECRET` on the deployment | unset, so `POST /integrations/shopify/order-linkage` answers 503 |

Creating the missing order would take one of: `orderCreate` / `draftOrderCreate` +
`draftOrderComplete` (all need a write scope RESOLVE does not hold and must not acquire), or an
external merchant-side integration that signs `resolve.order_linkage.v1` for orders it created
(none exists in this environment). So the positive pairing is not demonstrated by pretending a
fixture is a store. What 14C.7G does demonstrate is that the trust boundary at RESOLVE's edge is
complete, end-to-end and over real HTTP — `tests/test_shopify_pairing_channel_e2e.py`, 34 tests:
a real mission → real Shopify-catalog selection → real Razorpay TEST verification → a real order
row whose identity was written by production code → a signed raw-byte event → existence read plus
Product/Variant corroboration → the reference persisted by the single writer → `linked` read back
through the authenticated order endpoint → durable across a restart, idempotent on replay, refused
on every conflict and every unclean corroboration, consumable by the return seam, and still unable
to execute a return.

Two things in that file are load-bearing and deliberately uncosmetic:

* the merchant "store" is a fixture with no mutation surface at all, and an autouse guard makes
  `get_shopify_client` raise, so no test in the file can reach the real store; and
* a "positive" pairing there proves the channel works. It does not prove the pairing is about a
  real purchase, because no real purchase exists. The test module's docstring says so, and §2 of
  this document remains the reason: only an integration that owns the Shopify order lifecycle can
  supply the fact this channel carries.

### 12.1 What 14C.7H added

Task 14C.7H took the two prerequisites apart instead of waving at them: the **currency boundary**
(this store publishes USD, `PAYMENT_CURRENCY` is the code constant `"INR"`, and no FX exists
anywhere) and the **purchase-channel boundary** (what kind of system could legitimately say "I own
this Shopify order and this is its RESOLVE order"). Both are documented in
`docs/purchase-channel-and-currency-boundary.md`, along with the four currency architectures judged
against this code, the five payment/merchant-authority interleavings, the channel-side outbox that
would be needed (14C.7I §12.2 records that shape as built — inside the channel, never inside RESOLVE),
and the read-only schema finding that `orderCreate`, `draftOrderCreate` and
`draftOrderComplete` expose **no** idempotency-key argument in 2026-07 — so duplicate protection is
the channel's job and cannot be delegated to the signed event, which arrives only after the order
exists. No pairing contract, currency behavior, schema or scope changed.

### 12.2 What 14C.7I added: the decision, and the channel as a separate package

Task 14C.7I picked an architecture (**Option C** — the merchant order and RESOLVE's payment are
denominated in the same currency, so nothing converts; the reasons are in
`docs/purchase-channel-and-currency-boundary.md` §11) and then built the thing 14C.7G/7H said was
missing — **outside RESOLVE**, as `merchant-channel/`, so that no write capability, credential or
document joins the RESOLVE Shopify client. RESOLVE's half of the contract did not change: same route,
same seven permitted fields with `extra="forbid"`, same raw-byte HMAC, same single writer
(`app/tools/shopify/linkage.link_order_to_shopify_order`), no second writer, no schema change, no new
status, and `write_orders` still absent.

Two schema findings corrected §12.1/12.1-era assumptions, and both are measured live (read-only, 0
mutations):

* `orderCreate(order, options)` **cannot create a selling order**: `OrderInput.id` is non-null and
  there is no `lineItems` field on it. The create path is `draftOrderCreate(input: DraftOrderInput!)`
  followed by `draftOrderComplete(id)`, and the order must be read from
  `DraftOrderCompletePayload.draftOrder.order` — a DraftOrder GID is not an order GID, and putting
  one in a linkage event would be a false claim about what Shopify was given;
* of the correlation-shaped fields, only **`po_number:` is a filter Shopify honours** on
  `orders(query:)`. `metafield:` requires a provisioned definition RESOLVE cannot create, and
  `custom_attribute:` is ignored (an impossible value answers with every order), so it cannot support
  a deterministic 0/1/many read-back. The channel therefore stamps its correlation as `poNumber`.

What the channel's output does against the real contract is now proven rather than described:
`backend/tests/test_merchant_channel_linkage_acceptance.py` (11 tests) runs the channel's own code and
feeds the bytes it signs to the real route over HTTP. A positive pairing lands as `linked`; a
byte-identical replay is answered from storage with zero further Shopify reads; a second RESOLVE
order claiming the same merchant order is refused while the first claim survives; an order RESOLVE
cannot confirm with Shopify ends as a recorded **rejection**, not a linkage (RESOLVE does not take the
channel's word); and a handoff in the wrong currency never reaches RESOLVE at all.

**And the state of the world is unchanged: no real Shopify order was created, so no positive pairing
against the live store exists and `STATE A` is not claimed.** The connected store publishes USD while
C requires the merchant order in RESOLVE's payment currency, and there is no merchant-side credential
in this environment. The read-only re-check after the whole task (`backend/verify_14c7i_safety.py`,
three `query` documents, zero mutations): granted scopes still exactly
`read_merchant_managed_fulfillment_orders, read_orders, read_products, read_returns, write_returns`;
`write_orders`/`write_products`/`write_merchant_managed_fulfillment_orders`/`write_fulfillments`
absent; shop "RESOLVE Commerce Lab", `currencyCode` USD; **1** order; **0** returns; order #1001
byte-identical to the 14C.7G/7H snapshot (PAID, FULFILLED, 410.0 USD, `returnStatus NO_RETURN`, line
quantity 1, `poNumber` null, `sourceName shopify_draft_order`); RESOLVE's linkage route still answers
503 (secret unset); `RETURN_PROVIDER=none`; dev database unchanged (1 order row, **0** with a
`provider_order_reference`, 0 `integration_events` rows).

### 12.3 Task 14C.7J: the live pairing was attempted, and stopped before any write

14C.7J existed to try exactly one disposable live pairing. It ran a read-only pre-flight
(`backend/verify_14c7j_preflight.py` — three `query` documents, **0 mutations**, re-run before and
after with byte-identical output) and stopped, as its own rules require, because six prerequisites
are unmet:

| prerequisite | measured |
| --- | --- |
| RESOLVE payment currency INR | satisfied (`PAYMENT_CURRENCY = "INR"`) |
| merchant channel expects INR | **not loadable** — `load_config` refuses to run in this environment because RESOLVE-owned env names (`SHOPIFY_*`, `RAZORPAY_*`) are present, which is the credential boundary working, not a bug to route around |
| connected store publishes INR | **no** — `shop.currencyCode = USD`, and all 3 ACTIVE/available variants publish `priceRangeV2.minVariantPrice.currencyCode = USD` |
| merchant-side credential | **absent** — no `MERCHANT_SHOPIFY_ACCESS_TOKEN` exists here, and asking for one in chat is forbidden |
| linkage secret on both sides | **absent** — RESOLVE's `linkage_integration_secret` unset (route answers 503), so the channel has no voice either |
| a RESOLVE order with canonical Shopify identity | **none** — the dev row `order_44ec95eb8e58` is the google_shopping/TabletBox lookalike with all three `shopify_*` columns NULL; it must not be paired, and no legitimate Shopify-native purchase can be created on this deployment while the store publishes USD, because the payment gate refuses that currency before any Razorpay call |
| `write_orders` absent from RESOLVE, `RETURN_PROVIDER=none` | both satisfied and re-read live |

**REAL POSITIVE PAIRING ACHIEVED: NO. STATE A IS NOT CLAIMED.** Store totals unchanged
(1 order, 0 returns, #1001 byte-identical: PAID / FULFILLED / 410.0 USD / `returnStatus NO_RETURN` /
line quantity 1 / `poNumber` null), RESOLVE totals unchanged (1 order row, 0 linked, 0
`integration_events`). Nothing was cancelled or cleaned up because nothing was created — and the
channel has **no** cancel/void document at all, deliberately: `documents.py` will not gain one until
its schema is verified and a real disposable order exists to justify it.

The live attempt is therefore blocked on merchant-side facts outside this repository: an INR-priced
catalog (or a genuinely merchant-owned INR store reachable through `MERCHANT_SHOPIFY_*`), a separate
merchant token with write capability, and the same linkage secret on both sides.

### 12.4 Confirmed-but-refused linkage: what actually happens, and who can recover it (14C.7K §12)

The dangerous corner of a deterministic `event_id`: the merchant order exists and is verified, but
RESOLVE refuses the claim. Measured against the real route, the real event state machine, and a
fixture store — `tests/test_linkage_refused_event_recovery.py` (5 tests):

1. **A refusal settles the event.** `integration_events.outcome` moves `received` → `rejected`, and
   `rejected` is terminal. Re-delivering the **identical bytes** after the underlying cause has gone
   away (the fixture store, which had refused the order, now confirms it) still returns
   `200 {outcome: "rejected", already_processed: true}` and links nothing — and asks Shopify nothing,
   because a settled replay is answered from storage. That is replay protection working, not a bug:
   one `event_id` states one claim, and RESOLVE has already ruled on it.
2. **Design A ("reuse the same event forever") is therefore not supported here.** The only recovery
   the protocol itself names is a new event identity — RESOLVE's public 503 wording says so:
   *"This event id is now spent; a further attempt must arrive as a new event."* That wording is now
   pinned by a test, because it is the caller's only instruction and drifting it would silently
   change the contract.
3. **Design C ("a new identity") works, and relaxes nothing.** Measured: a fresh `event_id` carrying
   the same pairing after the cause is corrected is verified from scratch (existence + corroboration
   run again) and links. `event_id` was never the duplicate guard for the *pairing* — that is
   `uq_resolve_orders_provider_order_reference` plus the writer's ownership check, and a second
   RESOLVE order claiming the same merchant order still gets 409 regardless of how the event is
   named. So a new identity is a recovery path, not a loophole. Design B (an operator-triggered
   re-delivery of the *same* claim) would need exactly this new-identity semantics plus an audit
   trail, and does not exist today.
4. **The gap is on the channel's side.** `event_id_for()` is a pure function of
   (correlation, order GID), which is what makes a resend byte-identical and safe — and is precisely
   why the channel cannot produce the second claim RESOLVE asks for. Today the pair therefore ends as
   `REFUSED_LINKAGE` in the channel's outbox, with the merchant order standing and no retry loop
   (and never a second create: `test_case_17`/`test_case_18`).

Deliberately **not** changed while writing this: `event_id` derivation, replay protection, or the
event contract. Closing the gap means deciding *who* may mint a second identity for a pairing and
under what operator control — a protocol decision with a live consequence, so it belongs in the task
that can actually exercise it, not in a papering-over. §12.3's blockers are why that task still has
no live pairing to protect.

### 12.5 Reclaim: how a refused pairing is stated again (14C.7L)

14C.7K's open half is closed. Design C — a merchant-channel attempt counter — was chosen because the
architecture already had everything the decision needs: the outbox owns the correlation, `event_id`
is already derived rather than invented, and the refusal state is already recorded. Design B (an
operator-controlled reclaim) would have needed the same new-identity semantics *plus* an approval
surface that does not exist and would be invented for one operation. Anything wider than a counter
would have been a second system.

**The invariant, in one line:**

    one correlation_id  ->  at most one Shopify order, however many claims are made about it

**The state machine.** RESOLVE's half is unchanged: `received` → `linked | already_linked | rejected`,
`rejected` terminal, a byte-identical replay answered from storage with **no Shopify read at all**.
The channel's half gains one transition:

```
linkage:  NOT_SENT ──deliver──> SENT                      (nothing to reclaim)
                        DELIVERY_UNKNOWN                  (no verdict — re-send the SAME bytes)
                        REFUSED ──reclaim_linkage──> NOT_SENT (attempt N+1)
```

`reclaim_linkage(correlation, handoff)` is a separate door, not a looser `fulfil`. It refuses unless
the previous claim was **settled as refused**, because minting a new identity over a lost answer would
put two live claims on one purchase with no "no" between them. It then re-reads the merchant store by
correlation and only proceeds on exactly one order: zero leaves the refusal standing and reopens
nothing (notably not `may_create` — "RESOLVE declined this" must never become "so we buy it again"),
and two is ambiguous and stops. It verifies the order against the handoff — correlation stamp, store
identity, Product GID, Variant GID, quantity — and only then rotates the attempt and signs. It has no
path to a create, by structure: it never consults `may_create`.

**`event_id` semantics.** Attempt 1's derivation is byte-frozen (`sha256(correlation|order)`), because
re-naming an already-settled claim is precisely the duplicate the determinism was bought for. Attempts
beyond 1 fold in the attempt number (`sha256(correlation|order|N)`). The number comes from the outbox
row and nowhere else, so no caller, clock or random draw can produce a new claim; and an unrotated row
redelivers the identical bytes under the identical id forever. Re-delivery therefore stays safe, and
a reclaimed event is re-stamped with a fresh `issued_at` — which RESOLVE's own freshness window still
checks, so a captured event cannot be filed at leisure under a new number.

**`correlation_id` semantics: unchanged, and that is the point.** A correlation is a pure function of
the purchase (RESOLVE order, Product GID, Variant GID, quantity, currency). A reclaim re-derives it
from the handoff and refuses on mismatch, so a reclaim cannot re-use a spent correlation as the name
for a different purchase. Everything immutable stays immutable: amount, currency, product, variant,
quantity, owner, RESOLVE order, store, and the merchant order GID are never inputs to the rotation.
Only the claim's identity moves.

**What E2 re-verifies.** RESOLVE's single writer runs its whole chain again on a new identity, because
a new `event_id` is a new claim rather than an appeal: RESOLVE order exists, ownership from the
persisted row, order-shape validation, Shopify existence, store-domain match, and line corroboration
against the canonical Product/Variant GIDs; a second RESOLVE order reaching the same merchant order is
still refused by the unique index plus the writer's ownership check. Measured in
`tests/test_linkage_refused_event_recovery.py`: a reclaim that arrives after the outage clears but
whose merchant order sells a different product, a different variant, or neither, is refused 409 and
links nothing — a second attempt buys a second look at the store, never a softer one.

**One boundary, stated rather than papered over.** RESOLVE does not check *quantity*. Its own sold
quantity is a constant (`CHECKOUT_QUANTITY = 1`), not a column, and the 14C.7F corroboration read
deliberately carries only ids. A reclaim is quantity-checked — by the channel, against the merchant
order's own line, on every attempt — because that is the half that reads the order at creation.
Extending RESOLVE's corroboration to quantity is a change to the frozen 14C.7F read for *every* event,
so it was left as a named, deliberate deferral rather than folded into a recovery task.

**The audit.** `linkage_attempt_history`, in the channel's own SQLite outbox, one row per settled
attempt: attempt number, event id, the event id it supersedes, outcome, merchant order id, timestamp.
No signature, no secret, no token, no raw body, no customer field. A lost answer writes no row — the
absence of a result is not a result. `merchant_channel` still imports nothing from `app`, and RESOLVE's
audit trail keeps its existing refusal records unchanged.

**Cancellation and void: the schema facts are now verified; the behaviour is not.** 14C.7L could only
record the *absence* of a disposal mechanism. 14C.7M read it out of the live 2026-07 Admin schema with
24 introspection `query` documents and **0 mutations**, and saved the output to
`merchant-channel/schema/cancel_2026-07.json`. Of 470 mutation operations, 96 are cancellation-shaped
by name; the five that matter, verbatim from the schema:

| operation | arguments | payload | deprecated |
| --- | --- | --- | --- |
| `orderCancel` | `orderId: ID!`, `reason: OrderCancelReason!`, `restock: Boolean!`, `notifyCustomer: Boolean`, `staffNote: String`, `refundMethod: OrderCancelRefundMethodInput` | `job: Job`, `orderCancelUserErrors: [OrderCancelUserError!]!` | no |
| `orderClose` | `input: OrderCloseInput!` (`{ id: ID! }`) | `order: Order`, `userErrors: [UserError!]!` | no |
| `orderDelete` | `orderId: ID!` | `deletedId: ID`, `userErrors: [OrderDeleteUserError!]!` | no |
| `draftOrderDelete` | `input: DraftOrderDeleteInput!` (`{ id: ID! }`) | `deletedId: ID`, `userErrors: [UserError!]!` | no |
| `transactionVoid` | `parentTransactionId: ID!` | `transaction: OrderTransaction`, `userErrors: [TransactionVoidUserError!]!` | no |

What that establishes, and what it does not:

- **VERIFIED — there is no `draftOrderCancel`.** The candidate list contains `draftOrderDelete` and
  `draftOrderBulkDelete` and no cancel-shaped draft operation. Disposing of an uncompleted draft order
  in 2026-07 is **deletion**, not cancellation.
- **VERIFIED — "void" is not an order operation.** `transactionVoid` is addressed at an
  `OrderTransaction` through `parentTransactionId`; it names no order. A future task that wants to
  "void an order" is asking for something the schema does not offer under that name.
- **VERIFIED — `orderCancel` answers asynchronously and hands back no order.** Its payload is
  `{ job, orderCancelUserErrors }`. There is no `order` field to read a result from, so a caller must
  re-read the order to learn what happened — which for RESOLVE means the existing read paths, never
  the mutation response. Note also that 14C.7L's own script draft had assumed an
  `OrderCancelInput`; introspection shows **that input object does not exist in 2026-07** and the
  arguments are flat on the field. That assumption was wrong and is now corrected by measurement.
- **VERIFIED — `orderCancel` can be told to refund**: `refundMethod` accepts
  `originalPaymentMethodsRefund: Boolean` and `storeCreditRefund: OrderCancelStoreCreditRefundInput`,
  and `restock: Boolean!` is **required**. So the caller must decide restock rather than inherit a
  default — and whether money actually moves is a behavioural question, not a schema one.
- **VERIFIED — the marker fields exist on `Order` (`cancelledAt`, `cancelReason`, `closedAt`,
  `displayFinancialStatus`) and none of them exist on `DraftOrder`,** which instead carries `order`.
  Consistent with the two being different objects with different lifecycles; not proof of any
  transition.
- **VERIFIED — `OrderCancelReason` = CUSTOMER, DECLINED, FRAUD, INVENTORY, OTHER, STAFF**, no
  deprecated values.
- **UNKNOWN — NOT EXPOSED BY INTROSPECTION, and not to be filled in from documentation prose or
  memory:** whether any of these succeeds on a draft order that has not been completed; whether
  cancelling a completed order is permitted at all and under what store conditions; what
  `displayFinancialStatus` becomes (`VOIDED` is a plausible reading of the enum's absence here, not a
  finding); which access scope each operation requires; whether cancellation is reversible; and
  whether a cancelled order stays readable through `read_orders` in the same shape. Shopify's schema
  expresses no preconditions, no state transitions and no scope grants — the names of the argument
  types are as far as structure goes.

So the disposal contract is *shaped* but not *proved safe*. A later task that implements it must
establish the behavioural half against a real disposable order, with real authority, and must not
present the argument list above as evidence that a call will work. Nothing was cancelled, created, or
provisioned to learn any of this.

## Current Shopify scopes, and the deliberate absence of `write_orders`

Granted on the installed app, re-read live from
`currentAppInstallation { accessScopes { handle } }`:

```
read_products, read_orders, read_returns,
read_merchant_managed_fulfillment_orders, write_returns
```

Four reads and one write. `write_orders`, `write_products`,
`write_merchant_managed_fulfillment_orders` and `write_fulfillments` are **not requested and not
held**, declared so in `app/tools/shopify/data_use.py` and pinned by tests. `write_returns` exists
for one operation — `returnCreate` — and is broader than that on Shopify's side, so the scope grant
is not the limit: RESOLVE's own code must keep exposing only the controlled path. 14C.7F requested
no scope and needed none: the corroboration read runs under `read_orders`, verified live.

## The read-only boundary is enforced in the transport, not only declared (14C.7N; absolute since 14C.7O)

Until 14C.7N, "RESOLVE's Shopify client is read-only" was true of the documents the client's
methods happened to pass. `execute_graphql(document, variables)` itself accepted any text: hand it a
`mutation` and it would authenticate, build headers, build a body and send it. Nothing in the code
said that was not allowed. The boundary is now a property of the transport.

**Where the decision happens.** `ShopifyReadOnlyClient.execute_graphql` classifies its document as
its *first* statement, before the token exchange — which is itself an HTTP call — before the
`X-Shopify-Access-Token`/`Authorization` header is chosen, before the request body is built, before
`httpx` is touched. A refused document therefore has no request to leak a credential into, and no
possibility of having been acted on: there is nothing to reconcile, because nothing was sent. The
refusal is `ShopifyReadOnlyViolation` (`app/tools/shopify/errors.py`), and it names the document's
**kind** and, for a write, its single root operation — never the document's text, because a document
is caller-supplied content that could carry an object id, a customer field or a pasted token in it.

**What counts as a read.** `app/tools/shopify/document_gate.py` classifies without a GraphQL
dependency: comments and string literals are skipped, brace/paren/bracket depth is tracked, and a
document is a read only if it is **exactly one root `query` operation** (named, or the anonymous
`{…}` shorthand) with a selection body it can read the root field names of. Everything else fails
closed — `subscription`, an empty or non-text document, unbalanced braces, a fragment definition
with no operation, an operation with no body, or more than one root operation. The classifier does
not need to recognise a dangerous operation to refuse it; it only needs to fail to be sure the
document is a read. Counting parentheses matters: `query Q($mutation: Boolean!)` is a read, and a
field named `query` inside a selection is invisible to the root scan.

**The one write that is not refused, and why it is not a hole.** *(14C.7N's shape; superseded by
14C.7O, below — kept because it is the reason the exception existed.)* RESOLVE already declares a
single permitted write — `SHOPIFY_DATA_USE.intended_write_mutations == ("returnCreate",)` — and
`app/tools/shopify/return_provider.py` sent it through this same client, because that module's
whole design is to reuse the one credential system instead of creating a second token path
(14C.1/14C.4). The gate therefore consulted that declaration: a `mutation` passed only when it was
well-formed, had **exactly one** root field, and that field was named in the tuple. The tuple was
the allowlist, so `write_returns` was no longer the only thing bounding it — `returnClose`,
`returnApproveRequest`, `returnDeclineRequest`, `returnProcess`, `returnReopen` and `returnCancel`
were all refused even though the scope would permit them, and a mutation that bundled a declared
write next to an undeclared one was refused as a whole. `enforces_read_only_transport` is declared on
the same data-use object so a test can tell whether the boundary still holds, rather than
leaving that claim to prose.

**What the read-only client now cannot do, checked rather than claimed.** Every
cancellation/void/delete/close-shaped operation 14C.7M measured out of the live 2026-07 schema is
refused locally with **zero HTTP requests**, including the token exchange: `orderCancel`,
`orderClose`, `orderDelete`, `draftOrderDelete`, `transactionVoid`, and the fulfillment-order
cancel/close operations from the same evidence file. It likewise refuses the merchant channel's own
`draftOrderCreate`/`draftOrderComplete`. Nothing about cancellation was implemented by this task: the
six behavioural unknowns 14C.7M recorded (does disposal work on an uncompleted draft, on a completed
and paid order, what `displayFinancialStatus` becomes, which scope each operation needs, whether
cancelling is reversible, whether a cancelled order stays readable) are still unknown, still marked
`UNKNOWN — NOT EXPOSED BY INTROSPECTION`, and a test now asserts both that they are still six and
that no `app/` module contains a cancellation document.

**The merchant channel is a separate capability, and this is not a global mutation ban.**
`merchant-channel/` buys through a merchant-owned app with merchant-owned write scopes; its
`ShopifyAdminGateway` holds a different credential and speaks for a different installation. Putting
RESOLVE's boundary on that side would not make RESOLVE safer — it would only stop the channel being
able to do the one job 14C.7I decided it has. So no file in `merchant_channel` imports the gate or
the violation (asserted by scanning every module in the package), and the channel's own suite still
transmits `draftOrderCreate` → `draftOrderComplete` through an injected transport. The direction that
*is* enforced is the other one: RESOLVE's read-only client refuses the channel's purchase documents.

**Requirement conflict, recorded rather than smoothed over.** 14C.7N asked for "no mutation reaches
the transport" *and* for docs stating the read-only client cannot create/cancel/refund/return, while
asking that existing tests not be weakened or reduced. In this repository those cannot all be true at
once, because `returnCreate` rides this client and `tests/test_shopify_return_provider.py` +
`tests/test_return_provider_registration.py` deliberately exercise it over the **real**
`ShopifyReadOnlyClient` with a mock transport — that is how 14C.4 proved there is no second auth path.
An unconditional ban would have deleted that coverage, and the alternative (a second credential path
for the return provider) is exactly the architecture the return-provider tests forbid. The reading
implemented was therefore the one that kept every existing assertion standing: **no read path can
send a write, no unclassifiable document can be sent, and the only write sendable at all is the one
operation the data-use declaration names.** Widening that to a true zero-mutation client means
deciding where the declared return write should live, which is a task with its own consequences, not
a wording change here. **14C.7O made that decision, and the conflict is closed**: the write has its
own capability object, the read client's exception is gone, and every assertion 14C.7N kept standing
still stands — now with `returnCreate` refused by the read client instead of transmitted by it.

**Tests (14C.7N).** `backend/tests/test_shopify_read_only_gate.py` — mutation-per-operation over the
evidence file, subscription, the malformed/unknown set, multi-root documents, the
credential-and-document non-leakage check, the unconfigured-client check, the narrow-allowlist check,
the bundled-write check, the "every shipped read still passes its own gate" check, the
channel-not-banned check, and the "production factory carries the gate" check.
`merchant-channel/tests/test_gateway_transport.py` gained the matching proof that the channel's write
capability is intact and borrowed nothing from RESOLVE.

### 14C.7O: the declared write moved out, and the read boundary became absolute

14C.7N left one sentence in the docs that could not be made true without moving something: the
read-only client was also RESOLVE's Admin GraphQL transport, and `returnCreate` rode it. The gate
could refuse everything except the one declared write, which is a rule *about documents*, not the
property RESOLVE's name claims. 14C.7O moved the write, so the property is now structural.

**Three tiers of capability, and only one of them is RESOLVE's read path.**

| Tier | Object | Documents it will carry | Credential |
| --- | --- | --- | --- |
| RESOLVE read-only path | `ShopifyReadOnlyClient` (`app/tools/shopify/client.py`) | **query documents only** — exactly one root `query`. Zero mutation behaviour: no allowlist, no declared-write exception, no method that carries a write. | the configured `SHOPIFY_*` values |
| RESOLVE return capability | `ShopifyReturnWriteClient` (`app/tools/shopify/return_write_client.py`) | **exactly one declared `returnCreate`**, and nothing else — no other mutation, and no query either. | the SAME credential, via a shared `ShopifyAdminTransport` |
| Merchant purchase channel | `merchant-channel/`'s `ShopifyAdminGateway` | its own `draftOrderCreate` → `draftOrderComplete`, under a merchant-owned app and merchant-owned scopes | its own access token; unchanged by this task |

**One credential, not two secrets.** `get_shopify_clients(settings)` builds a single
`ShopifyAdminTransport` — the client-credentials exchange, the in-memory token cache, the POST and
the typed errors, with no document policy of its own — and hands it to both capability objects.
`get_shopify_client(settings)` still returns the read half, so every existing caller is unchanged. A
test measures the consequence on the wire: a provider that reads a return target and then submits it
performs **one** token exchange, not two. Splitting a capability while duplicating a secret would
have created a second grant to audit and a second thing to leak, and no scope was requested, changed,
or re-granted to do this.

**Who may hold the write.** The capability is constructed in the package factory and obtained by
`app/tools/returns/__init__.py` — the return-provider seam — and passed to
`ShopifyReturnProvider(client=…, write_client=…)`. A scan of every production module under `app/`
asserts that exactly those four files (`return_write_client.py`, `shopify/__init__.py`,
`shopify/return_provider.py`, `returns/__init__.py`) name it in their **code**, docstrings stripped:
prose may explain a boundary, code may not cross it. Discovery, linkage, product search and the
missions router still hold only the read client, and `ShopifyAdminTransport` is deliberately absent
from the package's `__all__` so nothing advertises a policy-free sender.

**The provider refuses, definitely, when it has no capability.** `request_return` validates its
payload first (so every existing 400-before-any-network-call expectation is untouched), then requires
the capability. Without one it raises `ReturnProviderUnavailable` — a definite "nothing reached
Shopify", never `ReturnOutcomeUnknown`, and never a fallback to the read client, which has no write
path to fall back to. That ordering is also why a capability refusal maps to a definite unavailability
inside `_run(..., mutating=True)`: a refusal RESOLVE made itself cannot have landed anywhere.

**Cancellation and disposal: still UNKNOWN, still unimplemented.** Giving the declared write its own
home answered none of 14C.7M's six questions and added no cancellation code. `orderCancel`,
`orderClose`, `orderDelete`, `draftOrderDelete` and `transactionVoid` are now refused by **both**
RESOLVE capabilities — by the read client because they are not reads, and by the write capability
because they are not the declared operation — each with zero HTTP attempts, token exchange included.
The six unknowns are still six, still `UNKNOWN — NOT EXPOSED BY INTROSPECTION`, and re-asserted from
`merchant-channel/schema/cancel_2026-07.json` rather than from memory. No order was cancelled, closed,
deleted or voided, and #1001 is unchanged: 1 order, PAID, FULFILLED, `returnStatus: NO_RETURN`, 0
Shopify returns, before and after the live preflight.

**Tests.** `backend/tests/test_shopify_return_write_capability.py` (96 cases: every existing read
still transits; `returnCreate` refused by the read client; the write capability accepts only the
declared document and refuses 17 undeclared writes, all 9 reads, bundled and malformed documents;
zero HTTP on every refusal, including with a token already cached; one shared transport; only the
return-provider seam obtains it; the transport module carries no policy; the disposal unknowns are
still unknown; the channel still owns its mutations). `backend/tests/test_shopify_read_only_gate.py`
re-homed its declared-write case as a refusal case and gained the static no-allowlist scan.
`backend/verify_14c7o_preflight.py` re-measured all of it against the live store with query documents
only: 13 GraphQL reads transmitted, 1 token exchange, **0 mutation bodies offered to the live
transport**, held scopes still exactly the declared five, `write_orders` still absent.
