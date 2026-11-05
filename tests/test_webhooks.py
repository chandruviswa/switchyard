import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest

from switchyard import tasks, webhooks
from switchyard.config import get_settings
from switchyard.db import utcnow
from switchyard.models import Delivery, Event

from .conftest import key_headers


def send(client, key, type_="order.paid", headers=None):
    return client.post(
        "/v1/events",
        json={"type": type_, "data": {"order": 42}},
        headers={**key_headers(key), **(headers or {})},
    )


@pytest.fixture
def setup(client, account, session, queued, receiver):
    """An org with one webhook and one event already sent. Returns the delivery id."""
    hook = account.create_webhook()
    response = send(client, account.create_key())
    assert response.status_code == 201
    return {"hook": hook, "delivery_id": queued[0], "event": response.json()}


def get_delivery(session, delivery_id):
    session.expire_all()
    return session.get(Delivery, delivery_id)


def test_successful_delivery(setup, receiver, session):
    assert webhooks.attempt(session, setup["delivery_id"]) == ("succeeded", None)

    delivery = get_delivery(session, setup["delivery_id"])
    assert delivery.status == "succeeded"
    assert delivery.attempts == 1
    assert delivery.last_status_code == 200
    assert delivery.delivered_at is not None

    body = json.loads(receiver.requests[0].content)
    assert body["id"] == setup["event"]["id"]
    assert body["type"] == "order.paid"
    assert body["data"] == {"order": 42}


def test_signature_can_be_verified_by_the_receiver(setup, receiver, session):
    webhooks.attempt(session, setup["delivery_id"])
    request = receiver.requests[0]

    parts = dict(p.split("=") for p in request.headers["X-Switchyard-Signature"].split(","))
    signed = f"{parts['t']}.".encode() + request.content
    expected = hmac.new(setup["hook"]["secret"].encode(), signed, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(parts["v1"], expected)


def test_changing_the_body_breaks_the_signature():
    original = webhooks.sign("secret", 1000, b'{"amount":1}')
    assert webhooks.sign("secret", 1000, b'{"amount":9}') != original
    assert webhooks.sign("secret", 1001, b'{"amount":1}') != original
    assert webhooks.sign("other", 1000, b'{"amount":1}') != original


@pytest.mark.parametrize(
    "respond",
    [
        lambda request: httpx.Response(500),
        lambda request: httpx.Response(404),
        lambda request: httpx.Response(302, headers={"Location": "http://169.254.169.254/"}),
    ],
    ids=["server_error", "not_found", "redirect"],
)
def test_bad_responses_are_retried_with_backoff(setup, receiver, session, respond):
    receiver.respond = respond
    status, delay = webhooks.attempt(session, setup["delivery_id"])
    assert status == "pending"
    assert delay == timedelta(seconds=30)

    delivery = get_delivery(session, setup["delivery_id"])
    assert delivery.last_error
    # Redirects must not be followed.
    assert len(receiver.requests) == 1


def test_network_failure_is_retried(setup, receiver, session):
    def boom(request):
        raise httpx.ConnectError("connection refused")

    receiver.respond = boom
    status, _ = webhooks.attempt(session, setup["delivery_id"])
    assert status == "pending"
    assert "connection refused" in get_delivery(session, setup["delivery_id"]).last_error


def test_backoff_doubles():
    assert [webhooks.backoff(n).total_seconds() for n in (1, 2, 3, 4)] == [30, 60, 120, 240]


def test_gives_up_after_max_attempts_and_can_be_retried_by_hand(
    setup, receiver, session, client, account, queued
):
    receiver.respond = lambda request: httpx.Response(500)
    delivery_id = setup["delivery_id"]

    for _ in range(get_settings().webhook_max_attempts - 1):
        assert webhooks.attempt(session, delivery_id)[0] == "pending"
    assert webhooks.attempt(session, delivery_id) == ("failed", None)
    assert len(receiver.requests) == get_settings().webhook_max_attempts

    receiver.respond = lambda request: httpx.Response(200)
    response = client.post(f"/admin/deliveries/{delivery_id}/retry", headers=account.headers)
    assert response.status_code == 200
    assert webhooks.attempt(session, delivery_id)[0] == "succeeded"


def test_only_failed_deliveries_can_be_retried(setup, client, account):
    url = f"/admin/deliveries/{setup['delivery_id']}/retry"
    assert client.post(url, headers=account.headers).status_code == 409


def test_cannot_retry_another_orgs_delivery(setup, client, other_account):
    url = f"/admin/deliveries/{setup['delivery_id']}/retry"
    assert client.post(url, headers=other_account.headers).status_code == 404


def test_same_delivery_cannot_be_sent_twice(setup, receiver, session):
    assert webhooks.attempt(session, setup["delivery_id"])[0] == "succeeded"
    assert webhooks.attempt(session, setup["delivery_id"]) == ("skipped", None)
    assert len(receiver.requests) == 1


def test_claim_is_exclusive(setup, session):
    assert webhooks.claim(session, setup["delivery_id"]) is True
    assert webhooks.claim(session, setup["delivery_id"]) is False


def test_disabled_endpoint_stops_delivery(setup, receiver, session, client, account):
    client.delete(f"/admin/webhooks/{setup['hook']['id']}", headers=account.headers)
    assert webhooks.attempt(session, setup["delivery_id"])[0] == "failed"
    assert receiver.requests == []


def test_requeue_finds_only_what_is_due(setup, session):
    delivery_id = setup["delivery_id"]
    assert webhooks.find_due(session) == [delivery_id]

    delivery = get_delivery(session, delivery_id)
    delivery.next_attempt_at = utcnow() + timedelta(minutes=5)
    session.commit()
    assert webhooks.find_due(session) == []


def test_worker_that_died_mid_send_gets_recovered(setup, session):
    delivery_id = setup["delivery_id"]
    webhooks.claim(session, delivery_id)
    assert webhooks.find_due(session) == []

    delivery = get_delivery(session, delivery_id)
    delivery.claimed_at = utcnow() - timedelta(minutes=10)
    session.commit()
    assert webhooks.find_due(session) == [delivery_id]


def test_requeue_task_queues_due_deliveries(setup, queued):
    queued.clear()
    assert tasks.requeue_due() == 1
    assert queued == [setup["delivery_id"]]


def test_task_schedules_the_retry(setup, receiver, monkeypatch):
    receiver.respond = lambda request: httpx.Response(503)
    scheduled = []
    monkeypatch.setattr(
        tasks.deliver, "apply_async", lambda args, countdown: scheduled.append((args, countdown))
    )
    assert tasks.deliver(setup["delivery_id"]) == "pending"
    assert scheduled == [((setup["delivery_id"],), 30.0)]


def test_event_type_filter(client, account, queued):
    account.create_webhook(url="http://a.test/", event_types=["order.paid"])
    account.create_webhook(url="http://b.test/", event_types=["user.created"])
    account.create_webhook(url="http://c.test/")
    send(client, account.create_key(), "order.paid")
    assert len(queued) == 2


def test_other_orgs_webhooks_do_not_get_my_events(client, account, other_account, queued):
    other_account.create_webhook()
    send(client, account.create_key())
    assert queued == []


def test_idempotency_key_returns_the_original_event(client, account, queued, session):
    account.create_webhook()
    key = account.create_key()
    headers = {"Idempotency-Key": "abc-123"}

    first = send(client, key, headers=headers)
    second = send(client, key, headers=headers)

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert session.query(Event).count() == 1
    assert len(queued) == 1


def test_idempotency_keys_are_per_org(client, account, other_account, session):
    headers = {"Idempotency-Key": "same"}
    assert send(client, account.create_key(), headers=headers).status_code == 201
    assert send(client, other_account.create_key(), headers=headers).status_code == 201
    assert session.query(Event).count() == 2


def test_event_is_kept_when_the_broker_is_down(client, account, monkeypatch, session):
    from kombu.exceptions import OperationalError

    def broker_down(delivery_id):
        raise OperationalError("no broker")

    account.create_webhook()
    monkeypatch.setattr(tasks.deliver, "delay", broker_down)
    assert send(client, account.create_key()).status_code == 201
    assert len(webhooks.find_due(session)) == 1


def test_event_shows_its_deliveries(setup, client, account, session):
    webhooks.attempt(session, setup["delivery_id"])
    key = account.create_key()
    body = client.get(f"/v1/events/{setup['event']['id']}", headers=key_headers(key)).json()
    assert body["deliveries"][0]["status"] == "succeeded"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",        # not https
        "https://127.0.0.1/hook",
        "https://localhost/hook",
        "https://10.0.0.5/hook",
        "https://192.168.1.1/hook",
        "https://169.254.169.254/latest/meta-data",  # cloud metadata service
        "https://[::1]/hook",
        "ftp://example.com/hook",
    ],
)
def test_unsafe_targets_are_rejected(monkeypatch, url):
    settings = get_settings().model_copy(update={"webhook_allow_private_targets": False})
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    with pytest.raises(webhooks.UnsafeTarget):
        webhooks.check_target(url)


def test_public_https_target_is_allowed(monkeypatch):
    settings = get_settings().model_copy(update={"webhook_allow_private_targets": False})
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    monkeypatch.setattr(
        webhooks.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    webhooks.check_target("https://example.com/hook")


def test_dns_pointing_at_a_private_address_is_rejected(monkeypatch):
    settings = get_settings().model_copy(update={"webhook_allow_private_targets": False})
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    monkeypatch.setattr(
        webhooks.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))]
    )
    with pytest.raises(webhooks.UnsafeTarget):
        webhooks.check_target("https://innocent-looking.example.com/hook")


def test_creating_an_unsafe_webhook_is_a_422(client, account, monkeypatch):
    settings = get_settings().model_copy(update={"webhook_allow_private_targets": False})
    monkeypatch.setattr(webhooks, "get_settings", lambda: settings)
    response = client.post(
        "/admin/webhooks", json={"url": "https://127.0.0.1/x"}, headers=account.headers
    )
    assert response.status_code == 422
