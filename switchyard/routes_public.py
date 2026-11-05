"""The public API, used by customers' servers with an API key."""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from kombu.exceptions import OperationalError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from . import schemas, tasks
from .db import get_session
from .deps import require_scope
from .models import ApiKey, Delivery, Event, WebhookEndpoint

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["events"])


def _load_event(session: Session, org_id: int, **filters) -> Event | None:
    return session.scalar(
        select(Event)
        .filter_by(org_id=org_id, **filters)
        .options(selectinload(Event.deliveries))
    )


@router.post("/events", response_model=schemas.EventOut, status_code=201)
def create_event(
    body: schemas.EventIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, max_length=100),
    key: ApiKey = Depends(require_scope("events:write")),
    session: Session = Depends(get_session),
):
    if idempotency_key:
        existing = _load_event(session, key.org_id, idempotency_key=idempotency_key)
        if existing:
            response.status_code = 200
            return existing

    event = Event(
        org_id=key.org_id, type=body.type, payload=body.data, idempotency_key=idempotency_key
    )
    session.add(event)

    endpoints = session.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.org_id == key.org_id, WebhookEndpoint.active.is_(True)
        )
    )
    deliveries = [
        Delivery(event=event, endpoint=endpoint)
        for endpoint in endpoints
        if not endpoint.event_types or body.type in endpoint.event_types
    ]
    session.add_all(deliveries)

    try:
        session.commit()
    except IntegrityError as exc:
        # Two requests with the same key raced, and the other one won.
        session.rollback()
        existing = _load_event(session, key.org_id, idempotency_key=idempotency_key)
        if existing is None:
            raise HTTPException(409, "Could not store the event.") from exc
        response.status_code = 200
        return existing

    for delivery in deliveries:
        try:
            tasks.deliver.delay(delivery.id)
        except OperationalError:
            # The event is safely stored. The requeue job will find this one.
            logger.warning("broker unavailable, delivery %s left for requeue", delivery.id)

    return event


@router.get("/events/{event_id}", response_model=schemas.EventOut)
def get_event(
    event_id: str,
    key: ApiKey = Depends(require_scope("events:read")),
    session: Session = Depends(get_session),
):
    event = _load_event(session, key.org_id, id=event_id)
    if event is None:
        raise HTTPException(404, "No such event.")
    return event
