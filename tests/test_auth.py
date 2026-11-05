from datetime import timedelta

import jwt

from switchyard.config import get_settings
from switchyard.db import utcnow
from switchyard.models import ApiKey

from .conftest import key_headers


def login(client, email="owner@alpha.example.com", password="long-enough-password"):
    return client.post("/auth/login", json={"email": email, "password": password})


def event(client, key):
    return client.post(
        "/v1/events", json={"type": "order.paid", "data": {"id": 1}}, headers=key_headers(key)
    )


def test_login_works_with_any_email_case(client, account):
    assert login(client, "OWNER@alpha.example.com").status_code == 200


def test_wrong_password_and_unknown_email_look_the_same(client, account):
    wrong = login(client, password="not-the-password")
    unknown = login(client, email="nobody@alpha.example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_duplicate_email_is_rejected(client, account):
    response = client.post(
        "/auth/register",
        json={"organization": "x", "email": "owner@alpha.example.com", "password": "long-enough-password"},
    )
    assert response.status_code == 409


def test_short_password_is_rejected(client):
    response = client.post(
        "/auth/register", json={"organization": "x", "email": "a@b.example.com", "password": "short"}
    )
    assert response.status_code == 422


def test_login_is_throttled(client, account):
    for _ in range(get_settings().login_attempts_per_minute):
        assert login(client, password="wrong-password").status_code == 401
    blocked = login(client, password="wrong-password")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_admin_requires_a_token(client):
    assert client.get("/admin/api-keys").status_code == 401


def test_expired_token_is_rejected(client, account):
    token = jwt.encode(
        {"sub": "1", "exp": utcnow() - timedelta(minutes=1)},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    response = client.get("/admin/api-keys", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_token_signed_with_another_secret_is_rejected(client, account):
    token = jwt.encode({"sub": "1"}, "someone-elses-secret-someone-elses", algorithm="HS256")
    response = client.get("/admin/api-keys", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_api_key_cannot_be_used_on_admin_routes(client, account):
    key = account.create_key()
    assert client.get("/admin/api-keys", headers=key_headers(key)).status_code == 401


def test_raw_key_is_not_stored_and_only_shown_once(client, account, session):
    key = account.create_key()
    stored = session.get(ApiKey, key["id"])
    assert key["key"] not in (stored.key_hash, stored.prefix)
    listed = client.get("/admin/api-keys", headers=account.headers).json()
    assert "key" not in listed[0]
    assert "key_hash" not in listed[0]


def test_key_works_then_stops_when_revoked(client, account):
    key = account.create_key()
    assert event(client, key).status_code == 201
    assert client.delete(f"/admin/api-keys/{key['id']}", headers=account.headers).status_code == 204
    assert event(client, key).status_code == 401


def test_expired_key_is_rejected(client, account):
    key = account.create_key(expires_at=(utcnow() - timedelta(minutes=1)).isoformat())
    assert event(client, key).status_code == 401


def test_scope_is_enforced(client, account):
    read_only = account.create_key(scopes=["events:read"])
    assert event(client, read_only).status_code == 403


def test_garbage_key_is_rejected(client):
    response = client.post(
        "/v1/events", json={"type": "a", "data": {}}, headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401


def test_cannot_revoke_another_orgs_key(client, account, other_account):
    key = other_account.create_key()
    assert client.delete(f"/admin/api-keys/{key['id']}", headers=account.headers).status_code == 404
    assert event(client, key).status_code == 201


def test_cannot_read_another_orgs_event(client, account, other_account):
    theirs = event(client, other_account.create_key()).json()
    response = client.get(f"/v1/events/{theirs['id']}", headers=key_headers(account.create_key()))
    assert response.status_code == 404


def test_cannot_disable_another_orgs_webhook(client, account, other_account):
    hook = other_account.create_webhook()
    assert client.delete(f"/admin/webhooks/{hook['id']}", headers=account.headers).status_code == 404


def test_audit_log_records_actions_for_own_org_only(client, account, other_account):
    key = account.create_key()
    client.delete(f"/admin/api-keys/{key['id']}", headers=account.headers)
    other_account.create_key()
    actions = [row["action"] for row in client.get("/admin/audit-log", headers=account.headers).json()]
    assert actions == ["api_key.revoked", "api_key.created"]
