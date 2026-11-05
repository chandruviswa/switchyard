# Switchyard

A webhook delivery platform. Your application sends an event to one API call, and Switchyard
gets it to every endpoint subscribed to it: signed, retried when the receiver is down, and logged so
you can see what happened.

It's the piece every SaaS ends up building for itself. I chose it because the interesting
problems are all about failure, not about CRUD: what happens when the receiver is down, the broker
is down, a worker dies halfway through, or a client retries the same request.

Built with FastAPI, PostgreSQL, Redis, Celery and Docker.

```
                       +-------------+
 customer's server --> |  FastAPI    | --> PostgreSQL (events, deliveries, usage, audit)
   API key             |             | --> Redis (rate limits, usage counters, task queue)
                       +-------------+
                                             |
                       +-------------+       v
 their webhook URL <-- | Celery      | <-- queue
   signed POST         | worker      |
                       +-------------+
                       | Celery beat |  requeues stuck deliveries, flushes usage
                       +-------------+
```

## Trying it

```
docker compose up --build
```

Then, using the docs at http://localhost:8000/docs or curl:

```bash
# Sign up
TOKEN=$(curl -s localhost:8000/auth/register -H 'Content-Type: application/json' \
  -d '{"organization":"Acme","email":"me@example.com","password":"a-long-password"}' | jq -r .access_token)

# Point a webhook at the echo container
curl -s localhost:8000/admin/webhooks -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"http://receiver:8080/hook"}'

# Create an API key. This is the only time you'll see it.
KEY=$(curl -s localhost:8000/admin/api-keys -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"backend","scopes":["events:write","events:read"]}' | jq -r .key)

# Send an event
curl -s localhost:8000/v1/events -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-1001' -d '{"type":"order.paid","data":{"order":1001}}'
```

`docker compose logs receiver` shows the webhook arriving.

## How failures are handled

**The receiver is down.** Delivery is retried with exponential backoff (30s, 1m, 2m, 4m, 8m) and marked
`failed` after six attempts. Failed deliveries can be retried by hand once the receiver is back.

**The broker is down.** The event and its deliveries are committed to PostgreSQL before anything is
queued. If queueing fails, the API still returns success, and a job that runs every 30 seconds finds
pending rows and queues them. PostgreSQL is the source of truth, and Redis only holds the to-do list.

**A worker dies mid-send.** A delivery is claimed with an atomic
`UPDATE ... WHERE status = 'pending'` before sending. Claims older than five minutes are released.
The receiver might get the event twice in that case, because delivery is at-least-once,
and exactly-once isn't possible across a network. That's why each webhook carries the event id, so
receivers can ignore repeats.

**A client retries a request.** `Idempotency-Key` returns the original event instead of creating a
second one. A unique constraint settles the race when two arrive together.

**Redis is down.** Rate limiting fails open and logs a warning. Rejecting all traffic because a
limiter is unavailable would turn a small outage into a total one. That's a judgment call. An API
that's protecting something expensive might choose differently.

## Security

- **API keys** are 32 random bytes, stored only as a SHA-256 hash. SHA-256 would be wrong for passwords,
  but these keys have no guessable structure to brute force. Passwords use Argon2.
- **Webhook signatures** are an HMAC over the timestamp and body, so receivers can reject both forgeries
  and old replayed requests.
- **SSRF.** A webhook URL is an instruction for my server to call an address chosen by a stranger, so
  private addresses, including the cloud metadata service, are blocked. DNS is checked at send time too, and
  redirects aren't followed.
- **Tenant isolation.** Another org's row returns a 404, not a 403, so ids can't be probed.

## Rate limiting

It uses a sliding window in a Redis Lua script. A plain per-minute counter lets someone send a full quota
at 11:59:59 and another at 12:00:00. This weights the previous window by how much of it still overlaps.
It has to be Lua because reading and incrementing must be one atomic step across API processes.

## Tests

```
python -m venv .venv
.venv/Scripts/activate        # source .venv/bin/activate on Linux or Mac
pip install -r requirements-dev.txt
pytest
```

Locally this uses SQLite and fakeredis so it runs with nothing installed, and one test starts a real Celery
worker delivering to a real HTTP server. CI runs the same tests against real PostgreSQL and Redis, then starts the
whole stack with Docker Compose and sends an event through it (`scripts/smoke_test.py`).

## API

| | Path | Auth | |
|---|---|---|---|
| POST | `/auth/register`, `/auth/login` | | Get a JWT |
| POST, GET, DELETE | `/admin/api-keys` | JWT | Manage keys |
| POST, GET, DELETE | `/admin/webhooks` | JWT | Manage endpoints |
| GET | `/admin/deliveries` | JWT | Delivery history |
| POST | `/admin/deliveries/{id}/retry` | JWT | Retry a failed delivery |
| GET | `/admin/usage`, `/admin/audit-log` | JWT | Usage per key, and who did what |
| POST | `/v1/events` | API key | Send an event |
| GET | `/v1/events/{id}` | API key | An event and its delivery status |
| GET | `/health`, `/ready` | | Liveness and readiness |

## What's missing

This hasn't run in production. Before it did, I'd want:

- **DNS rebinding protection.** The address is checked and then resolved again by the HTTP client. An attacker
  controlling DNS could change it in between. The fix is to connect to the IP that was checked.
- **Encrypted webhook secrets.** They have to be stored readable to sign with, so they should be
  encrypted at rest.
- **A circuit breaker per endpoint.** One dead endpoint with lots of events currently burns worker time on
  doomed attempts.
- **Roles.** Everyone in an org can do everything.
- **Metrics.** There are structured logs with request ids, but no Prometheus endpoint for
  queue depth and delivery latency.
- **Load testing.** I haven't measured throughput, so I'm not quoting any numbers.
