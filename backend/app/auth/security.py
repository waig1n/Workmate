# -*- coding: utf-8 -*-
"""Password hashing and JWT tokens (stdlib + pyjwt only)."""

import hashlib
import hmac
import os
import time

import jwt

from ..config import get_settings

_PBKDF2_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_access_token(user_id: str, token_version: int = 0) -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + settings.jwt_expire_hours * 3600,
        # Integer claim compared against users.token_version; bumping the
        # column revokes every previously issued token (plan §3.2).
        "ver": token_version,
    }
    return jwt.encode(
        payload, settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict | None:
    """Return the token payload, or None when invalid/expired."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError:
        return None
    return payload if isinstance(payload, dict) else None


def normalize_email(email: str) -> str:
    """Canonical form for uniqueness checks and login lookup (plan §3.2).

    The display value in ``users.email`` keeps its original casing.
    """
    return email.strip().casefold()
