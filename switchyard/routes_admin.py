"""The management API, used by people logged in with a JWT."""

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import ratelimit, schemas, security, tasks, webhooks
from .config import get_settings
from .db import get_session, utcnow
from .deps import current_user
from .models import ApiKey, AuditLog, Delivery, Event, Organization, UsageDaily, User, WebhookEndpoint
from .redis_client import get_redis

router = APIRouter()


def audit(session: Session, user: User, action: str, target: str) -> None:
    session.add(AuditLog(org_id=user.org_id, user_id=user.id, action=action, target=target))


def owned(session: Session, model, object_id, user: User):
    """Fetches a row belonging to the user's org. Another org's row looks like a 404."""
    row = session.get(model, object_id)
    if row is None or row.org_id != user.org_id:
        raise HTTPException(404, "Not found.")
    return row


@router.post("/auth/register", response_model=schemas.TokenOut, status_code=201, tags=["auth"])
def register(body: schemas.RegisterIn, session: Session = Depends(get_session)):
    org = Organization(name=body.organization)
    session.add(org)
    session.flush()
    user = User(
        org_id=org.id, email=body.email.lower(), password_hash=security.hash_password(body.password)
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "That email is already registered.") from exc
    return schemas.TokenOut(access_token=security.create_access_token(user.id))


@router.post("/auth/login", response_model=schemas.TokenOut, tags=["auth"])
def login(body: schemas.LoginIn, session: Session = Depends(get_session)):
    email = body.email.lower()
    limit = get_settings().login_attempts_per_minute
    decision = ratelimit.check(get_redis(), f"login:{email}", limit)
    if not decision.allowed:
        raise HTTPException(
            429, "Too many attempts.", headers={"Retry-After": str(decision.retry_after)}
        )

    user = session.scalar(select(User).where(User.email == email))
    if not security.verify_password(body.password, user.password_hash if user else None):
        raise HTTPException(401, "Wrong email or password.")
    return schemas.TokenOut(access_token=security.create_access_token(user.id))


@router.post(
    "/admin/api-keys", response_model=schemas.ApiKeyCreated, status_code=201, tags=["api keys"]
)
def create_api_key(
    body: schemas.ApiKeyIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    raw, key_hash = security.generate_api_key()
    key = ApiKey(
        org_id=user.org_id,
        name=body.name,
        prefix=raw[:12],
        key_hash=key_hash,
        scopes=body.scopes,
        rate_limit_per_minute=body.rate_limit_per_minute or get_settings().default_rate_limit_per_minute,
        expires_at=body.expires_at,
    )
    session.add(key)
    session.flush()
    audit(session, user, "api_key.created", f"api_key:{key.id}")
    session.commit()
    # The only time the raw key ever leaves the server.
    return schemas.ApiKeyCreated(**schemas.ApiKeyOut.model_validate(key).model_dump(), key=raw)


@router.get("/admin/api-keys", response_model=list[schemas.ApiKeyOut], tags=["api keys"])
def list_api_keys(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return session.scalars(select(ApiKey).where(ApiKey.org_id == user.org_id).order_by(ApiKey.id))


@router.delete("/admin/api-keys/{key_id}", status_code=204, tags=["api keys"])
def revoke_api_key(
    key_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    key = owned(session, ApiKey, key_id, user)
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        audit(session, user, "api_key.revoked", f"api_key:{key.id}")
        session.commit()


@router.post(
    "/admin/webhooks", response_model=schemas.WebhookCreated, status_code=201, tags=["webhooks"]
)
def create_webhook(
    body: schemas.WebhookIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    url = str(body.url)
    try:
        webhooks.check_target(url)
    except webhooks.UnsafeTarget as exc:
        raise HTTPException(422, str(exc)) from exc

    endpoint = WebhookEndpoint(
        org_id=user.org_id, url=url, secret=secrets.token_hex(32), event_types=body.event_types
    )
    session.add(endpoint)
    session.flush()
    audit(session, user, "webhook.created", f"webhook:{endpoint.id}")
    session.commit()
    return schemas.WebhookCreated(
        **schemas.WebhookOut.model_validate(endpoint).model_dump(), secret=endpoint.secret
    )


@router.get("/admin/webhooks", response_model=list[schemas.WebhookOut], tags=["webhooks"])
def list_webhooks(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return session.scalars(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.org_id == user.org_id)
        .order_by(WebhookEndpoint.id)
    )


@router.delete("/admin/webhooks/{webhook_id}", status_code=204, tags=["webhooks"])
def disable_webhook(
    webhook_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    # Disabled rather than deleted, so the delivery history still means something.
    endpoint = owned(session, WebhookEndpoint, webhook_id, user)
    endpoint.active = False
    audit(session, user, "webhook.disabled", f"webhook:{endpoint.id}")
    session.commit()


@router.get("/admin/deliveries", response_model=list[schemas.DeliveryOut], tags=["webhooks"])
def list_deliveries(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    query = (
        select(Delivery)
        .join(Event)
        .where(Event.org_id == user.org_id)
        .order_by(Delivery.id.desc())
        .limit(limit)
    )
    if status:
        query = query.where(Delivery.status == status)
    return session.scalars(query)


@router.post("/admin/deliveries/{delivery_id}/retry", response_model=schemas.DeliveryOut, tags=["webhooks"])
def retry_delivery(
    delivery_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    delivery = session.get(Delivery, delivery_id)
    if delivery is None or delivery.event.org_id != user.org_id:
        raise HTTPException(404, "Not found.")
    if delivery.status != Delivery.FAILED:
        raise HTTPException(409, "Only failed deliveries can be retried.")

    delivery.status = Delivery.PENDING
    delivery.attempts = 0
    delivery.next_attempt_at = utcnow()
    audit(session, user, "delivery.retried", f"delivery:{delivery.id}")
    session.commit()
    tasks.deliver.delay(delivery.id)
    return delivery


@router.get("/admin/usage", response_model=list[schemas.UsageOut], tags=["usage"])
def get_usage(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    since = utcnow().date() - timedelta(days=days)
    return session.scalars(
        select(UsageDaily)
        .where(UsageDaily.org_id == user.org_id, UsageDaily.day >= since)
        .order_by(UsageDaily.day.desc(), UsageDaily.api_key_id)
    )


@router.get("/admin/audit-log", response_model=list[schemas.AuditOut], tags=["usage"])
def get_audit_log(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    return session.scalars(
        select(AuditLog)
        .where(AuditLog.org_id == user.org_id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
    )
