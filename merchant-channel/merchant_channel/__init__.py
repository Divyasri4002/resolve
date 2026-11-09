"""
The merchant-side purchase channel (Task 14C.7I Phase B).

This package is NOT part of RESOLVE. It is a separate module that stands in for the merchant-owned
infrastructure 14C.7H/14C.7G identified as the missing prerequisite: the system that can truthfully
say "I created this Shopify order, and this is the RESOLVE order it belongs to."

The boundary is the whole point, so it is enforced rather than described:

  * it holds its OWN Shopify credential (``MERCHANT_SHOPIFY_*`` environment values) and never reads
    RESOLVE's settings, RESOLVE's token, or anything from the ``app`` package;
  * it is the only thing here that can write to Shopify — and under the selected architecture
    (Option C, `docs/purchase-channel-and-currency-boundary.md` §11) it does not mark the order paid,
    because no money reached the merchant through this path;
  * it owns the correlation identifier, the outbox, the read-before-create-again rule, and the
    reconciliation;
  * it emits exactly the existing ``resolve.order_linkage.v1`` event, HMAC-signed over the exact
    raw bytes, and POSTs it to RESOLVE. RESOLVE stays passive: authenticate, claim, verify, store.

Two things it deliberately cannot do, and does not pretend to:

  * it has never created a real Shopify order. The connected store publishes USD
    (``shop.currencyCode == "USD"``, measured live) while the selected architecture requires the
    merchant order to be denominated in the currency RESOLVE actually charges in, so creating an
    order here would pair a RESOLVE INR payment against a merchant USD order — the same money lie
    Option C exists to prevent, told from the other side;
  * its Shopify gateway is exercised only through an injected transport in tests. There is no
    merchant credential in this environment, and the task forbids asking for one.

There is no facade on purpose: importing ``merchant_channel`` must not pull in the gateway, its
credential handling, or (worst of all) anything that could reach RESOLVE. Import the module you mean
to use — ``merchant_channel.channel``, ``merchant_channel.linkage`` — and the dependency you get is
the one you asked for.
"""
