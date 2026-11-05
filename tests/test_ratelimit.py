import redis

from switchyard import ratelimit, usage
from switchyard.models import ApiKey, UsageDaily

from .conftest import key_headers

T0 = 1_800_000_000 - (1_800_000_000 % 60)  # the start of a minute


def post(client, key):
    return client.post("/v1/events", json={"type": "a", "data": {}}, headers=key_headers(key))


def test_allows_up_to_the_limit_then_blocks(redis_client):
    decisions = [ratelimit.check(redis_client, "k", limit=3, now=T0 + 1) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert [d.remaining for d in decisions[:3]] == [2, 1, 0]
    assert decisions[3].retry_after > 0


def test_burst_across_a_window_boundary_is_limited(redis_client):
    for _ in range(10):
        ratelimit.check(redis_client, "k", limit=10, now=T0 + 59)
    # A plain per-minute counter would allow 10 more here. One second into the new
    # window, 59/60ths of the old one still counts, so only one more gets through.
    decisions = [ratelimit.check(redis_client, "k", limit=10, now=T0 + 61) for _ in range(10)]
    assert sum(d.allowed for d in decisions) == 1


def test_quota_recovers_as_the_old_window_slides_away(redis_client):
    for _ in range(10):
        ratelimit.check(redis_client, "k", limit=10, now=T0 + 59)
    assert ratelimit.check(redis_client, "k", limit=10, now=T0 + 90).allowed


def test_keys_do_not_share_a_quota(redis_client):
    ratelimit.check(redis_client, "a", limit=1, now=T0)
    assert not ratelimit.check(redis_client, "a", limit=1, now=T0).allowed
    assert ratelimit.check(redis_client, "b", limit=1, now=T0).allowed


def test_redis_outage_fails_open():
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.05)
    assert ratelimit.check(dead, "k", limit=1).allowed


def test_api_returns_429_with_headers(client, account):
    key = account.create_key(rate_limit_per_minute=2)
    first = post(client, key)
    assert first.status_code == 201
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"

    post(client, key)
    blocked = post(client, key)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert blocked.headers["X-RateLimit-Remaining"] == "0"


def test_usage_is_counted_and_flushed(client, account, redis_client, session):
    key = account.create_key()
    for _ in range(3):
        post(client, key)

    assert usage.flush(redis_client, session) == 3
    # Flushing again adds nothing, and more traffic adds to the same row.
    assert usage.flush(redis_client, session) == 0
    post(client, key)
    assert usage.flush(redis_client, session) == 1

    rows = client.get("/admin/usage", headers=account.headers).json()
    assert len(rows) == 1
    assert rows[0]["requests"] == 4
    assert rows[0]["api_key_id"] == key["id"]


def test_blocked_requests_are_not_billed(client, account, redis_client, session):
    key = account.create_key(rate_limit_per_minute=1)
    post(client, key)
    post(client, key)
    usage.flush(redis_client, session)
    assert session.query(UsageDaily).one().requests == 1


def test_flush_survives_a_counter_for_a_deleted_key(redis_client, session):
    redis_client.set("usage:2026-01-01:99999", 5)
    assert usage.flush(redis_client, session) == 0
    assert session.query(ApiKey).count() == 0


def test_usage_is_private_to_the_org(client, account, other_account, redis_client, session):
    post(client, other_account.create_key())
    usage.flush(redis_client, session)
    assert client.get("/admin/usage", headers=account.headers).json() == []
