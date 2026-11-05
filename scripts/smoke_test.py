"""End to end check against a running `docker compose up`.

The unit tests fake the broker and the HTTP receiver. This one doesn't: it sends a real
event and waits for a real worker to deliver it. CI runs it after starting the stack.
"""

import sys
import time
import uuid

import httpx

BASE = "http://localhost:8000"
RECEIVER = "http://receiver:8080/hook"
TIMEOUT_SECONDS = 60


def main():
    client = httpx.Client(base_url=BASE, timeout=10)

    deadline = time.time() + TIMEOUT_SECONDS
    while True:
        try:
            if client.get("/ready").status_code == 200:
                break
        except httpx.TransportError:
            pass
        if time.time() > deadline:
            sys.exit("API never became ready")
        time.sleep(1)

    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    token = client.post(
        "/auth/register",
        json={"organization": "Smoke", "email": email, "password": "smoke-test-password"},
    ).raise_for_status().json()["access_token"]
    admin = {"Authorization": f"Bearer {token}"}

    client.post("/admin/webhooks", json={"url": RECEIVER}, headers=admin).raise_for_status()
    key = client.post(
        "/admin/api-keys",
        json={"name": "smoke", "scopes": ["events:read", "events:write"]},
        headers=admin,
    ).raise_for_status().json()["key"]
    api = {"Authorization": f"Bearer {key}"}

    event = client.post(
        "/v1/events", json={"type": "smoke.test", "data": {"ok": True}}, headers=api
    ).raise_for_status().json()
    print("sent event", event["id"])

    while time.time() < deadline:
        current = client.get(f"/v1/events/{event['id']}", headers=api).raise_for_status().json()
        status = current["deliveries"][0]["status"]
        if status == "succeeded":
            print("delivered through the real worker")
            return
        if status == "failed":
            sys.exit(f"delivery failed: {current['deliveries'][0]['last_error']}")
        time.sleep(1)

    sys.exit("timed out waiting for delivery")


if __name__ == "__main__":
    main()
