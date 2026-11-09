"""
Authenticated inbound integrations (Task 14C.7B).

One contract today: a merchant-side integration telling RESOLVE which Shopify order corresponds
to a RESOLVE order. These are service-to-service entry points — signed, replay-guarded, and
deliberately outside the user-session trust model. Nothing in this package authorizes an action;
each handler validates, then delegates to the ordinary server-side service that owns the change.
"""
