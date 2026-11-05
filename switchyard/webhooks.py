"""Webhook delivery. At-least-once, so receivers should dedupe on the event id."""

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from .config import get_settings
from .db import aware, utcnow
from .models import Delivery

BASE_BACKOFF_SECONDS = 30
STALE_CLAIM = timedelta(minutes=5)


class UnsafeTarget(Exception):
    pass


def check_target(url: str) -> None:
    """Stops a webhook being used to reach our own internal network (SSRF)."""
    settings = get_settings()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeTarget("Webhook URL must be http or https.")
    if settings.webhook_allow_private_targets:
        return
    if parsed.scheme != "https":
        raise UnsafeTarget("Webhook URL must use https.")

    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeTarget("Webhook host could not be resolved.") from exc

    for *_, sockaddr in addresses:
        address = ipaddress.ip_address(sockaddr[0])
        if not address.is_global:
            raise UnsafeTarget("Webhook URL points at a private address.")


def sign(secret: str, timestamp: int, body: bytes) -> str:
    # The timestamp is part of what's signed, so an old request can't be replayed later.
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def backoff(attempt: int) -> timedelta:
    """30s, 1m, 2m, 4m, 8m, ..."""
    return timedelta(seconds=BASE_BACKOFF_SECONDS * 2 ** (attempt - 1))


def http_client() -> httpx.Client:
    # No redirects: a safe URL could otherwise bounce us to an internal one.
    return httpx.Client(timeout=get_settings().webhook_timeout_seconds, follow_redirects=False)


def claim(session: Session, delivery_id: int) -> bool:
    """Atomically moves a delivery to "sending". False means someone else has it.

    Celery can run a task twice and the requeue job can overlap with a retry, so this
    is what stops a receiver being hit twice at the same moment. The row isn't locked
    during the HTTP call because that would hold a database connection for up to the
    full timeout.
    """
    result = session.execute(
        update(Delivery)
        .where(Delivery.id == delivery_id, Delivery.status == Delivery.PENDING)
        .values(status=Delivery.SENDING, claimed_at=utcnow())
    )
    session.commit()
    return result.rowcount == 1


def attempt(session: Session, delivery_id: int) -> tuple[str, timedelta | None]:
    """Makes one delivery attempt. Returns (status, delay before the next try)."""
    if not claim(session, delivery_id):
        return "skipped", None

    delivery = session.scalar(
        select(Delivery)
        .where(Delivery.id == delivery_id)
        .options(joinedload(Delivery.event), joinedload(Delivery.endpoint))
    )
    event, endpoint = delivery.event, delivery.endpoint
    delivery.attempts += 1

    if not endpoint.active:
        delivery.status = Delivery.FAILED
        delivery.last_error = "Endpoint was disabled."
        session.commit()
        return delivery.status, None

    body = json.dumps(
        {
            "id": event.id,
            "type": event.type,
            "created_at": aware(event.created_at).isoformat(),
            "data": event.payload,
        },
        separators=(",", ":"),
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Switchyard-Event": event.id,
        "X-Switchyard-Signature": sign(endpoint.secret, int(time.time()), body),
    }

    error, status_code = None, None
    try:
        # Checked on every send, not just when the URL was saved, because DNS can change.
        check_target(endpoint.url)
        with http_client() as client:
            response = client.post(endpoint.url, content=body, headers=headers)
        status_code = response.status_code
        if not response.is_success:
            error = f"Receiver answered {status_code}."
    except (httpx.HTTPError, UnsafeTarget) as exc:
        error = str(exc) or type(exc).__name__

    delivery.last_status_code = status_code
    delivery.last_error = error
    delivery.claimed_at = None

    delay = None
    if error is None:
        delivery.status = Delivery.SUCCEEDED
        delivery.delivered_at = utcnow()
    elif delivery.attempts >= get_settings().webhook_max_attempts:
        delivery.status = Delivery.FAILED
    else:
        delay = backoff(delivery.attempts)
        delivery.status = Delivery.PENDING
        delivery.next_attempt_at = utcnow() + delay

    session.commit()
    return delivery.status, delay


def find_due(session: Session) -> list[int]:
    """Deliveries that should be running but aren't.

    The event is committed before its task is queued. If the broker is down or a worker
    dies, the database row still exists, and this is how it gets picked back up.
    """
    now = utcnow()
    session.execute(
        update(Delivery)
        .where(Delivery.status == Delivery.SENDING, Delivery.claimed_at < now - STALE_CLAIM)
        .values(status=Delivery.PENDING, claimed_at=None)
    )
    session.commit()
    return list(
        session.scalars(
            select(Delivery.id)
            .where(Delivery.status == Delivery.PENDING, Delivery.next_attempt_at <= now)
            .limit(500)
        )
    )
