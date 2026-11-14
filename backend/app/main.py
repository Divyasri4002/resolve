"""
RESOLVE backend entrypoint.

Wires together the Mission API, the webhook receiver, the WebSocket activity stream, and starts
the mission engine's background loop. Nothing in this file makes an authority decision — it
only assembles the pieces that do.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.body_limit import EvidenceBodyLimitMiddleware
from app.config import get_settings
from app.database import async_session, engine, init_models
from app.db_enforcement import current_report, initialize as initialize_db_enforcement
from app.logging_redaction import install_credential_query_redaction
from app.ledger.publisher import run_forever as run_ledger_publisher_forever
from app.mission_engine.scheduler import run_forever
from app.routers import auth, integrations, missions, webhooks, ws

logger = logging.getLogger(__name__)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
install_credential_query_redaction()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    # Phase 4.6.1: prove the separated database topology before serving. The resulting
    # report arms (or permanently refuses) every protected purchase/linkage operation.
    await initialize_db_enforcement(engine)
    task = asyncio.create_task(run_forever())
    publisher_task = await _start_ledger_publisher()
    yield
    task.cancel()
    if publisher_task is not None:
        publisher_task.cancel()


async def _start_ledger_publisher() -> asyncio.Task | None:
    """
    Phase 4.6.2: the external evidence publisher starts only on a verified topology
    with the ledger installed and the store configured. Every refusal is a loud,
    honest log line — never a silent no-op and never a publisher running blind.
    """
    if not settings.ledger_publish_enabled:
        logger.info("ledger publisher: disabled by configuration")
        return None
    report = current_report()
    if report is None or not report.protected_ops_allowed:
        logger.error(
            "ledger publisher: not started — database role enforcement is not verified"
        )
        return None
    if not settings.ledger_object_store_endpoint or not settings.ledger_object_store_bucket:
        logger.error(
            "ledger publisher: not started — the external evidence store is not "
            "configured (endpoint and bucket are required)"
        )
        return None
    async with engine.connect() as conn:
        installed = (
            await conn.execute(
                text("SELECT to_regclass('public.ledger_publish_state') IS NOT NULL")
            )
        ).scalar_one()
    if not installed:
        logger.error(
            "ledger publisher: not started — the purchase-event ledger is not "
            "installed on this database; run the owner-ledger migration, then restart"
        )
        return None
    logger.info("ledger publisher: starting")
    return asyncio.create_task(run_ledger_publisher_forever(async_session, settings))


app = FastAPI(
    title="RESOLVE",
    description="The settlement layer for agent-to-agent commerce disputes.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow credentials for HTTP-only cookies from frontend origins
cors_origins = [
    settings.frontend_origin,
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
allowed_origins = list(dict.fromkeys([o for o in cors_origins if o]))

# P2.1 request-body resource protection. Registered BEFORE CORSMiddleware so the CORS
# layer sits outside it and wraps the 413 rejection — cross-origin browser clients can
# then read the rejection instead of an opaque network error.
app.add_middleware(EvidenceBodyLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(missions.router)
app.include_router(webhooks.router)
app.include_router(ws.router)
# Authenticated inbound integrations (Task 14C.7B). Disabled by configuration: with no signing
# secret set, the route refuses everything with 503 rather than accepting an unsigned event.
app.include_router(integrations.router)


@app.get("/")
async def root():
    return {
        "service": "RESOLVE Backend",
        "status": "running",
        "health": "/health",
        "documentation": "/docs",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "resolve-backend"}
