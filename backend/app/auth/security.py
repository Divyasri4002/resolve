"""
Cryptographic and security utilities for user authentication.

Implements:
1. Password hashing with bcrypt (single consistent algorithm).
2. Session token signing and verification with HMAC-SHA256.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import bcrypt


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt. Passwords are never stored in plaintext."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _base64url_decode(data_str: str) -> bytes:
    padding = 4 - (len(data_str) % 4)
    if padding != 4:
        data_str += "=" * padding
    return base64.urlsafe_b64decode(data_str)


def create_session_token(
    user_id: str, email: str, secret: str, expires_in_seconds: int = 604800
) -> str:
    """
    Create a signed session token containing user_id and expiration timestamp.
    Uses HMAC-SHA256 signature with constant-time verification.
    """
    payload = {
        "sub": user_id,
        "email": email,
        "exp": int(time.time()) + expires_in_seconds,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _base64url_encode(payload_json)

    signature = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
    ).digest()
    sig_b64 = _base64url_encode(signature)

    return f"{payload_b64}.{sig_b64}"


def verify_session_token(token: str, secret: str) -> dict[str, Any] | None:
    """
    Verify the signature and expiration of a session token.
    Returns payload dictionary if valid, None if invalid or expired.
    """
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None

        payload_b64, sig_b64 = parts
        expected_sig = hmac.new(
            secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
        ).digest()
        provided_sig = _base64url_decode(sig_b64)

        if not hmac.compare_digest(expected_sig, provided_sig):
            return None

        payload_bytes = _base64url_decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))

        if payload.get("exp", 0) < time.time():
            return None

        return payload
    except Exception:
        return None

