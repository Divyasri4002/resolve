"""
Authentication router for RESOLVE.

Provides:
- POST /auth/register: Create user account and start authenticated session
- POST /auth/login: Verify credentials and start authenticated session
- POST /auth/logout: Terminate authenticated session
- GET /auth/me: Retrieve authenticated user profile
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.security import create_session_token, hash_password, verify_password
from app.config import get_settings
from app.database import get_db
from app.models.db_models import UserRecord
from app.models.user import UserLoginRequest, UserRegisterRequest, UserResponse

router = APIRouter(prefix="/auth", tags=["authentication"])
settings = get_settings()


def _set_auth_cookie(response: Response, token: str) -> None:
    is_production = settings.env.lower() == "production"
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        max_age=settings.auth_cookie_max_age_seconds,
        expires=settings.auth_cookie_max_age_seconds,
        samesite="lax",
        secure=is_production,
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    is_production = settings.env.lower() == "production"
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        samesite="lax",
        secure=is_production,
    )


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
)
async def register(
    payload: UserRegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new user, store a bcrypt password hash, establish an authenticated
    HTTP-only session cookie, and return safe user information.
    """
    # Check for existing email
    result = await db.execute(select(UserRecord).where(UserRecord.email == payload.email))
    if result.scalars().first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    # Hash password with bcrypt
    pwd_hash = hash_password(payload.password)

    user = UserRecord(
        name=payload.name,
        email=payload.email,
        password_hash=pwd_hash,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Establish authenticated session
    token = create_session_token(
        user_id=user.id,
        email=user.email,
        secret=settings.auth_secret,
        expires_in_seconds=settings.auth_cookie_max_age_seconds,
    )
    _set_auth_cookie(response, token)

    return user


@router.post(
    "/login",
    response_model=UserResponse,
    summary="Sign in to an existing account",
)
async def login(
    payload: UserLoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Verify user credentials, establish an authenticated HTTP-only session cookie,
    and return safe user information.
    """
    result = await db.execute(select(UserRecord).where(UserRecord.email == payload.email))
    user = result.scalars().first()

    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Establish authenticated session
    token = create_session_token(
        user_id=user.id,
        email=user.email,
        secret=settings.auth_secret,
        expires_in_seconds=settings.auth_cookie_max_age_seconds,
    )
    _set_auth_cookie(response, token)

    return user


@router.post(
    "/logout",
    summary="Sign out and clear authenticated session",
)
async def logout(response: Response):
    """
    Clear the session cookie, terminating the authenticated session.
    """
    _clear_auth_cookie(response)
    return {"message": "Logged out successfully"}


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current authenticated user profile",
)
async def get_me(user: UserRecord = Depends(get_current_user)):
    """
    Return the profile of the currently authenticated user.
    Rejects unauthenticated requests with 401 Unauthorized.
    """
    return user

