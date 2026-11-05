import hashlib
import secrets
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import get_settings
from .db import utcnow

_hasher = PasswordHasher()
# Verified against when the email doesn't exist, so response time doesn't reveal
# which emails have accounts.
_DUMMY_HASH = _hasher.hash("not-a-real-password")

KEY_PREFIX = "sy_"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored: str | None) -> bool:
    try:
        _hasher.verify(stored or _DUMMY_HASH, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return stored is not None


def create_access_token(user_id: int) -> str:
    settings = get_settings()
    now = utcnow()
    claims = {"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=settings.jwt_minutes)}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def read_access_token(token: str) -> int | None:
    try:
        claims = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        return int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def generate_api_key() -> tuple[str, str]:
    """Returns (raw key, hash). Only the hash is stored.

    SHA-256 is fine here even though it would be wrong for passwords. These keys are
    32 random bytes, so there's nothing to brute force, and this runs on every request.
    """
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
