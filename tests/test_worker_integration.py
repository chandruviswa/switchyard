"""The rest of the suite fakes the broker and the receiver. This runs a real Celery
worker that delivers over real HTTP. Only the broker is swapped, for the in-memory one.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from celery.contrib.testing.worker import start_worker

from switchyard import tasks
from switchyard.db import SessionLocal
from switchyard.models import Delivery

from .conftest import key_headers


class Recorder(BaseHTTPRequestHandler):
    received = []
    status_codes = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Recorder.received.append(
            {"body": json.loads(body), "signature": self.headers["X-Switchyard-Signature"]}
        )
        self.send_response(Recorder.status_codes.pop(0) if Recorder.status_codes else 200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def local_receiver():
    Recorder.received, Recorder.status_codes = [], []
    server = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


@pytest.fixture
def worker():
    original = tasks.celery_app.conf.broker_url
    tasks.celery_app.conf.broker_url = "memory://"
    tasks.celery_app.conf.broker_transport_options = {}
    with start_worker(tasks.celery_app, pool="solo", perform_ping_check=False, loglevel="WARNING"):
        yield
    tasks.celery_app.conf.broker_url = original


def wait_for_status(delivery_id, wanted, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with SessionLocal() as session:
            status = session.get(Delivery, delivery_id).status
        if status == wanted:
            return
        time.sleep(0.05)
    pytest.fail(f"delivery stayed {status}, wanted {wanted}")


def test_event_reaches_a_real_receiver_through_a_real_worker(
    client, account, queued, local_receiver, worker
):
    account.create_webhook(url=local_receiver)
    response = client.post(
        "/v1/events",
        json={"type": "order.paid", "data": {"order": 7}},
        headers=key_headers(account.create_key()),
    )
    assert response.status_code == 201

    # The API's delay() is captured by a fixture, so hand it to the worker the same way.
    tasks.deliver.apply_async((queued[0],))
    wait_for_status(queued[0], Delivery.SUCCEEDED)

    assert len(Recorder.received) == 1
    assert Recorder.received[0]["body"]["data"] == {"order": 7}
    assert Recorder.received[0]["signature"].startswith("t=")
