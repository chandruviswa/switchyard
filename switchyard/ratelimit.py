"""Sliding window rate limiter.

A plain "count per minute" lets someone send a full quota at 11:59:59 and another
at 12:00:00. This weights the previous window by how much of it still overlaps,
which smooths that out without storing a timestamp per request.

It's a Lua script because the read and the increment have to be one atomic step.
With several API processes, two separate calls would let requests through in the gap.
"""

import logging
import time
from dataclasses import dataclass

import redis

logger = logging.getLogger(__name__)

SLIDING_WINDOW = """
local current_key = KEYS[1]
local previous_key = KEYS[2]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local elapsed = tonumber(ARGV[3])

local current = tonumber(redis.call('GET', current_key) or '0')
local previous = tonumber(redis.call('GET', previous_key) or '0')
local weighted = previous * ((window - elapsed) / window) + current

if weighted >= limit then
  return {0, math.floor(weighted)}
end

redis.call('INCR', current_key)
redis.call('EXPIRE', current_key, window * 2)
return {1, math.floor(weighted) + 1}
"""


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


def check(client: redis.Redis, name: str, limit: int, window: int = 60, now: float | None = None) -> Decision:
    now = time.time() if now is None else now
    slot = int(now // window)
    elapsed = now - slot * window
    keys = [f"rl:{name}:{slot}", f"rl:{name}:{slot - 1}"]

    try:
        allowed, used = client.eval(SLIDING_WINDOW, 2, *keys, limit, window, elapsed)
    except redis.RedisError:
        # A Redis outage shouldn't take the whole API down with it. Losing rate
        # limiting for a few minutes is the better failure.
        logger.warning("rate limiter unavailable, allowing request", exc_info=True)
        return Decision(True, limit, limit, 0)

    retry_after = 0 if allowed else max(1, int(window - elapsed))
    return Decision(bool(allowed), limit, max(0, limit - used), retry_after)
