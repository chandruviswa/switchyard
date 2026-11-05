from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ratelimit, security, usage
from .db import aware, get_session, utcnow
from .models import ApiKey, User
from .redis_client import get_redis

bearer = HTTPBearer(auto_error=False)


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(401, message, headers={"WWW-Authenticate": "Bearer"})


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: Session = Depends(get_session),
) -> User:
    user_id = security.read_access_token(credentials.credentials) if credentials else None
    user = session.get(User, user_id) if user_id else None
    if user is None:
        raise _unauthorized("Log in to use this endpoint.")
    return user


def require_scope(scope: str):
    """Authenticates an API key, checks its scope, rate limits it and counts the call."""

    def dependency(
        request: Request,
        response: Response,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        session: Session = Depends(get_session),
    ) -> ApiKey:
        if credentials is None:
            raise _unauthorized("Send your API key as a bearer token.")

        key = session.scalar(
            select(ApiKey).where(ApiKey.key_hash == security.hash_api_key(credentials.credentials))
        )
        if key is None or key.revoked_at is not None:
            raise _unauthorized("Invalid or revoked API key.")
        if key.expires_at is not None and aware(key.expires_at) <= utcnow():
            raise _unauthorized("This API key has expired.")
        if scope not in key.scopes:
            raise HTTPException(403, f"This key is missing the {scope} scope.")

        client = get_redis()
        decision = ratelimit.check(client, f"key:{key.id}", key.rate_limit_per_minute)
        limit_headers = {
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
        }
        if not decision.allowed:
            raise HTTPException(
                429,
                "Rate limit exceeded.",
                headers={**limit_headers, "Retry-After": str(decision.retry_after)},
            )
        response.headers.update(limit_headers)

        usage.record(client, key.id)
        return key

    return dependency
