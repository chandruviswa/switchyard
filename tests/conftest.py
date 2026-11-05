"""Uses SQLite and fakeredis by default so the tests run anywhere.

CI sets DATABASE_URL and USE_REAL_REDIS=1 to run the same tests on the real thing.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_switchyard.db")
os.environ["WEBHOOK_ALLOW_PRIVATE_TARGETS"] = "1"

import fakeredis  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from switchyard import deps, routes_admin, routes_public, tasks, webhooks  # noqa: E402
from switchyard.db import Base, SessionLocal, engine  # noqa: E402
from switchyard.main import app  # noqa: E402
from switchyard.redis_client import get_redis  # noqa: E402


@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def redis_client(monkeypatch):
    if os.environ.get("USE_REAL_REDIS") == "1":
        client = get_redis()
        client.flushdb()
    else:
        client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(deps, "get_redis", lambda: client)
    monkeypatch.setattr(routes_admin, "get_redis", lambda: client)
    monkeypatch.setattr(tasks, "get_redis", lambda: client)
    return client


@pytest.fixture(autouse=True)
def queued(monkeypatch):
    """Captures queued deliveries instead of needing a broker. Tests run them explicitly."""
    ids = []
    monkeypatch.setattr(tasks.deliver, "delay", lambda delivery_id: ids.append(delivery_id))
    monkeypatch.setattr(routes_public.tasks.deliver, "delay", lambda delivery_id: ids.append(delivery_id))
    return ids


@pytest.fixture
def receiver(monkeypatch):
    """A fake webhook receiver. Set .respond to control what it does."""

    class Receiver:
        def __init__(self):
            self.requests = []
            self.respond = lambda request: httpx.Response(200)

    fake = Receiver()

    def handler(request):
        fake.requests.append(request)
        return fake.respond(request)

    monkeypatch.setattr(
        webhooks, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return fake


@pytest.fixture
def session():
    with SessionLocal() as db_session:
        yield db_session


@pytest.fixture
def client():
    return TestClient(app)


class Account:
    """A registered org, with helpers for calling the API as it."""

    def __init__(self, client, email):
        self.client = client
        response = client.post(
            "/auth/register",
            json={"organization": email, "email": email, "password": "long-enough-password"},
        )
        assert response.status_code == 201, response.text
        self.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    def create_key(self, scopes=("events:read", "events:write"), **extra):
        response = self.client.post(
            "/admin/api-keys",
            json={"name": "test", "scopes": list(scopes), **extra},
            headers=self.headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    def create_webhook(self, url="http://receiver.test/hook", event_types=()):
        response = self.client.post(
            "/admin/webhooks",
            json={"url": url, "event_types": list(event_types)},
            headers=self.headers,
        )
        assert response.status_code == 201, response.text
        return response.json()


@pytest.fixture
def account(client):
    return Account(client, "owner@alpha.example.com")


@pytest.fixture
def other_account(client):
    return Account(client, "owner@beta.example.com")


def key_headers(key):
    return {"Authorization": f"Bearer {key['key']}"}

