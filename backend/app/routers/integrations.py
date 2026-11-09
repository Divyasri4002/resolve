"""
The inbound route for the order-linkage integration (Task 14C.7B).

Thin on purpose: read the raw bytes, hand them to `app.integrations.order_linkage`, which owns the
verification order, and turn the outcome into a deterministic status code. There is no session
dependency, no cookie, and no user identity anywhere in this file — the trust root is the HMAC
signature over the exact bytes received, and the handler refuses outright when the deployment has
no signing secret configured.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.db_enforcement import require_protected_ops
from app.integrations.order_linkage import (
    SIGNATURE_HEADER,
    LinkageIntegrationError,
    handle_linkage_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.post("/shopify/order-linkage")
async def submit_order_linkage(
    request: Request,
    x_resolve_signature: str = Header(default="", alias=SIGNATURE_HEADER),
    db: AsyncSession = Depends(get_db),
):
    """
    Establish that one RESOLVE order corresponds to one Shopify order, for an authenticated
    integration and for nobody else.

    The handler's ordering is the design and is not repeated here: signature over the raw body →
    strict schema (unknown or authority-shaped fields are refused) → freshness → `event_id`
    replay guard → RESOLVE order lookup → Shopify's own confirmation of the exact GID → the
    single, immutable, 1:1 writer. This route is the only production caller of that writer, and
    even here it supplies two references and no authority: ownership is taken from the persisted
    order, and the money, product and reason data a later return needs come from RESOLVE's own
    rows and Shopify's own reads.
    """
    # Phase 4.6.1: linkage writes touch resolve_orders (protected) — refuse when startup
    # verification has not proved the separated topology.
    require_protected_ops()
    body = await request.body()
    try:
        status_code, payload = await handle_linkage_event(
            db, body=body, signature=x_resolve_signature
        )
    except LinkageIntegrationError as exc:
        # One shape for every refusal, and none of it echoes the payload or says which step of
        # "not authenticated" this was.
        status_code, payload = exc.status_code, {"detail": exc.message}
    # Logged: the outcome and nothing else. The body is signed material and never a log line.
    logger.info("order-linkage integration responded %s", status_code)
    return JSONResponse(status_code=status_code, content=payload)
