"""
Unit and integration tests for RESOLVE User Authentication.
"""
import pytest
from app.auth.security import (
    create_session_token,
    hash_password,
    verify_password,
    verify_session_token,
)
from app.models.user import UserLoginRequest, UserRegisterRequest, UserResponse


def test_password_hashing():
    """Verify bcrypt password hashing and verification."""
    password = "SuperSecretPassword123"
    hashed = hash_password(password)

    assert hashed != password
    assert hashed.startswith("$2b$") or hashed.startswith("$2a$")
    assert verify_password(password, hashed) is True
    assert verify_password("WrongPassword!", hashed) is False
    assert verify_password("", hashed) is False


def test_session_token_lifecycle():
    """Verify session token creation, verification, and tampering detection."""
    secret = "test-secret-key-12345"
    user_id = "user_abc123"
    email = "test@example.com"

    # Valid token
    token = create_session_token(user_id=user_id, email=email, secret=secret, expires_in_seconds=3600)
    assert token is not None
    assert "." in token

    payload = verify_session_token(token, secret)
    assert payload is not None
    assert payload["sub"] == user_id
    assert payload["email"] == email
    assert payload["exp"] > 0

    # Tampered signature
    parts = token.split(".")
    tampered_sig = parts[0] + ".invalidSignature123"
    assert verify_session_token(tampered_sig, secret) is None

    # Tampered payload
    tampered_payload = "eyJzdWIiOiJoYWNrZXIifQ." + parts[1]
    assert verify_session_token(tampered_payload, secret) is None

    # Wrong secret
    assert verify_session_token(token, "different-secret-key") is None

    # Expired token
    expired_token = create_session_token(user_id=user_id, email=email, secret=secret, expires_in_seconds=-10)
    assert verify_session_token(expired_token, secret) is None


def test_user_schemas_validation():
    """Verify input validation for registration and login schemas."""
    # Valid registration
    reg = UserRegisterRequest(
        name="  Alice Smith  ",
        email="  Alice@Example.COM  ",
        password="SecurePassword123",
    )
    assert reg.name == "Alice Smith"
    assert reg.email == "alice@example.com"

    # Name too short
    with pytest.raises(ValueError):
        UserRegisterRequest(name="A", email="alice@example.com", password="SecurePassword123")

    # Invalid email
    with pytest.raises(ValueError):
        UserRegisterRequest(name="Alice", email="not-an-email", password="SecurePassword123")

    # Password too short (< 8 chars)
    with pytest.raises(ValueError):
        UserRegisterRequest(name="Alice", email="alice@example.com", password="short")

    # Login schema normalizes email
    login = UserLoginRequest(email=" User@Domain.Com ", password="mypassword")
    assert login.email == "user@domain.com"

