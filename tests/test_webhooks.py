"""PayPal webhooks: signature verification, deduplication and status updates."""

import json

import pytest

from payagent import db, orders, paypal

HEADERS = {
    "paypal-auth-algo": "SHA256WithRSA",
    "paypal-cert-url": "https://api.paypal.com/cert.pem",
    "paypal-transmission-id": "tid-1",
    "paypal-transmission-sig": "sig",
    "paypal-transmission-time": "2026-11-01T10:00:00Z",
}


def make_order(**overrides):
    capture_id = overrides.pop("capture_id", None)
    data = {
        "store_id": "store_demo",
        "customer_id": "visitor-1",
        "item": "1 x 4K Monitor 27in",
        "total": 329.0,
        "status": "CREATED",
    }
    data.update(overrides)
    order = orders.create(**data)
    if capture_id:
        order = orders.update(order["id"], capture_id=capture_id)
    return order


def capture_event(order_id, capture_id="CAP-1", event_id="WH-1"):
    return {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "329.00"},
            "supplementary_data": {"related_ids": {"order_id": order_id}},
        },
    }


def refund_event(capture_id="CAP-1", event_id="WH-2"):
    return {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.REFUNDED",
        "resource": {
            "id": "REF-1",
            "amount": {"currency_code": "USD", "value": "329.00"},
            "links": [
                {
                    "rel": "up",
                    "href": f"https://api.paypal.com/v2/payments/captures/{capture_id}",
                }
            ],
        },
    }


def post(client, event, headers=HEADERS):
    return client.post("/webhooks/paypal", content=json.dumps(event), headers=headers)


@pytest.fixture()
def verified(monkeypatch):
    state = {"ok": True}

    def fake_verify(store_id, headers, body):
        return state["ok"]

    monkeypatch.setattr(paypal, "verify_webhook", fake_verify)
    return state


def test_verified_capture_event_completes_the_order(client, verified):
    order = make_order()
    response = post(client, capture_event(order["id"]))
    assert response.status_code == 200
    assert response.json()["status"] == "processed"

    updated = orders.get(order["id"])
    assert updated["status"] == "COMPLETED"
    assert updated["capture_id"] == "CAP-1"

    stored = db.query_one("SELECT * FROM webhook_events WHERE id = ?", ("WH-1",))
    assert stored["verified"] == 1
    assert stored["state"] == "processed"
    assert stored["store_id"] == "store_demo"

    events = orders.events("store_demo", order["id"])
    assert [e["type"] for e in events if e["type"].startswith("webhook.")] == [
        "webhook.capture.completed"
    ]


def test_invalid_signature_is_rejected_without_side_effects(client, verified):
    verified["ok"] = False
    order = make_order()
    response = post(client, capture_event(order["id"]))
    assert response.status_code == 400
    assert "signature" in response.json()["detail"].lower()

    assert orders.get(order["id"])["status"] == "CREATED"
    assert db.query("SELECT * FROM webhook_events") == []
    assert orders.events(order["id"], order["id"]) == []


def test_repeated_delivery_is_deduplicated(client, verified):
    order = make_order()
    first = post(client, capture_event(order["id"]))
    second = post(client, capture_event(order["id"]))

    assert first.json()["status"] == "processed"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert db.query("SELECT * FROM webhook_events") != []
    assert len(db.query("SELECT * FROM webhook_events")) == 1
    events = [
        e for e in orders.events("store_demo", order["id"]) if e["type"].startswith("webhook.")
    ]
    assert len(events) == 1


def test_refund_event_marks_the_order_refunded(client, verified):
    order = make_order(status="COMPLETED", capture_id="CAP-1")
    response = post(client, refund_event())
    assert response.json()["status"] == "processed"

    updated = orders.get(order["id"])
    assert updated["status"] == "REFUNDED"
    assert updated["refunded_total"] == 329.0

    # A second, distinct refund event cannot push the total above what was paid.
    post(client, refund_event(event_id="WH-3"))
    assert orders.get(order["id"])["refunded_total"] == 329.0


def test_unknown_event_type_is_recorded_and_ignored(client, verified):
    response = post(
        client,
        {"id": "WH-9", "event_type": "PAYMENT.SALE.DENIED", "resource": {"id": "x"}},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert db.query_one("SELECT state FROM webhook_events WHERE id = ?", ("WH-9",))["state"] == "ignored"


def test_unverifiable_configuration_blocks_every_webhook(client, monkeypatch):
    def unconfigured(store_id, headers, body):
        raise paypal.NotConfigured("PAYPAL_WEBHOOK_ID is not set")

    monkeypatch.setattr(paypal, "verify_webhook", unconfigured)
    order = make_order()
    assert post(client, capture_event(order["id"])).status_code == 503
    assert orders.get(order["id"])["status"] == "CREATED"


def test_malformed_payloads_are_refused(client, verified):
    assert client.post("/webhooks/paypal", content=b"not json", headers=HEADERS).status_code == 400
    missing_id = client.post(
        "/webhooks/paypal",
        content=json.dumps({"event_type": "PAYMENT.CAPTURE.COMPLETED"}),
        headers=HEADERS,
    )
    assert missing_id.status_code == 400
    assert db.query("SELECT * FROM webhook_events") == []


def test_verification_rejects_a_delivery_without_signature_headers(monkeypatch):
    monkeypatch.setattr(
        paypal,
        "_creds",
        lambda store_id: {
            "client_id": "c",
            "client_secret": "s",
            "base_url": "https://api-m.sandbox.paypal.com",
        },
    )
    assert paypal.verify_webhook("store_demo", {}, b"{}") is False
