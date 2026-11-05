"""Request counting. Redis takes the per-request write, Postgres gets the totals.

Writing a row per API call would make the database the bottleneck, so requests only
bump a Redis counter and a periodic task moves the totals across.
"""

import logging
from datetime import date

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import ApiKey, UsageDaily

logger = logging.getLogger(__name__)
TTL_SECONDS = 3 * 24 * 3600


def record(client: redis.Redis, key_id: int) -> None:
    name = f"usage:{utcnow().date().isoformat()}:{key_id}"
    try:
        pipe = client.pipeline()
        pipe.incr(name)
        pipe.expire(name, TTL_SECONDS)
        pipe.execute()
    except redis.RedisError:
        logger.warning("could not record usage for key %s", key_id, exc_info=True)


def flush(client: redis.Redis, session: Session) -> int:
    """Moves counters into usage_daily. Returns how many requests were moved."""
    moved = 0
    for name in client.scan_iter(match="usage:*"):
        _, day_text, key_id = name.split(":")
        # GETDEL so a request arriving mid-flush lands in a fresh counter.
        count = client.getdel(name)
        if not count:
            continue

        api_key = session.get(ApiKey, int(key_id))
        if api_key is None:
            continue

        day = date.fromisoformat(day_text)
        row = session.scalar(
            select(UsageDaily)
            .where(UsageDaily.api_key_id == api_key.id, UsageDaily.day == day)
            .with_for_update()
        )
        if row is None:
            row = UsageDaily(org_id=api_key.org_id, api_key_id=api_key.id, day=day, requests=0)
            session.add(row)
        row.requests += int(count)
        moved += int(count)
    session.commit()
    return moved
