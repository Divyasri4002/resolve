"""
FastAPI dependencies for user authentication, plus the machine-to-machine service identity
the scratch purchase runner presents for its two read-only preflight reads.
"""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import verify_session_token
from app.config import get_settings
from app.database import get_db
from app.models.db_models import UserRecord

settings = get_settings()

# The dedicated machine credential header, deliberately distinct from Authorization: a service
# token is not a session, is verified differently, and can never be mistaken for one.
SERVICE_TOKEN_HEADER = "X-Resolve-Service-Token"


def is_service_principal(request: Request) -> bool:
    """
    True only when the request carries the configured machine credential in the dedicated
    service header.

    An empty configured value DISABLES the service path outright (the linkage receiver's
    empty-secret rule), so an unconfigured deployment cannot be opened by a header nobody
    checks. The comparison is constant-time over non-empty values only: an absent header is
    simply not a service call, and an empty configured token can never match an empty header.
    """
    expected = settings.service_runner_token
    if not expected:
        return False
    supplied = request.headers.get(SERVICE_TOKEN_HEADER)
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserRecord | None:
    """
    Extracts and verifies the session token from cookies or Authorization header.
    Returns the UserRecord if authenticated, None otherwise.
    """
    token: str | None = request.cookies.get(settings.auth_cookie_name)

    # Fallback to Authorization header if cookie not present
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    if not token:
        return None

    payload = verify_session_token(token, settings.auth_secret)
    if not payload:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    user = await db.get(UserRecord, user_id)
    return user


async def get_current_user(
    user: UserRecord | None = Depends(get_current_user_optional),
) -> UserRecord:
    """
    Requires an authenticated user. Raises 401 Unauthorized if not authenticated.
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user

