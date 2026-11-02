"""Idempotent capture, refund and checkout creation."""

import httpx
import pytest

from payagent import idempotency, orders, paypal


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


@pytest.fixture()
def fake_creds(monkeypatch):
    monkeypatch.setattr(
        paypal,
        "_creds",
        lambda store_id: {
            "client_id": "client",
            "client_secret": "secret",
            "base_url": "https://api-m.sandbox.paypal.com",
        },
    )


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


# ---------------------------------------------------------------- capture
def test_capture_is_idempotent(client, monkeypatch):
    calls = []

    def fake_capture(store_id, order_id, request_id):
        calls.append((store_id, order_id, request_id))
        # Same shape as a real PayPal capture response.
        return {
            "id": order_id,
            "status": "COMPLETED",
            "purchase_units": [{"payments": {"captures": [{"id": "CAP-1", "status": "COMPLETED"}]}}],
        }

    monkeypatch.setattr(paypal, "_capture_request", fake_capture)
    order = make_order()

    first = client.post(f"/paypal/capture/{order['id']}")
    second = client.post(f"/paypal/capture/{order['id']}")

    assert first.status_code == 200
    assert first.json()["status"] == "COMPLETED"
    assert second.status_code == 200
    assert second.json()["status"] == "COMPLETED"
    assert len(calls) == 1, "a retried capture must not reach PayPal twice"
    assert calls[0][2] == f"capture-{order['id']}"
    assert orders.get(order["id"])["capture_id"] == "CAP-1"


def test_concurrent_capture_is_refused(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        paypal, "_capture_request", lambda *a: calls.append(a) or {"id": "CAP-1", "status": "COMPLETED"}
    )
    order = make_order()
    assert idempotency.claim("store_demo", "capture", order["id"])[0] == idempotency.CLAIMED

    response = client.post(f"/paypal/capture/{order['id']}")
    assert response.status_code == 409
    assert calls == []


def test_capture_of_unknown_order_is_404(client):
    assert client.post("/paypal/capture/does-not-exist").status_code == 404


def test_failed_capture_can_be_retried(client, monkeypatch):
    attempts = {"n": 0}

    def flaky(store_id, order_id, request_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            request = httpx.Request("POST", "https://api-m.sandbox.paypal.com/x")
            raise httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(500, request=request, text="error")
            )
        return {"id": "CAP-1", "status": "COMPLETED"}

    monkeypatch.setattr(paypal, "_capture_request", flaky)
    order = make_order()

    assert client.post(f"/paypal/capture/{order['id']}").status_code == 502
    assert idempotency.get("store_demo", "capture", order["id"]) is None
    retry = client.post(f"/paypal/capture/{order['id']}")
    assert retry.status_code == 200
    assert retry.json()["status"] == "COMPLETED"


def test_capture_does_not_call_paypal_for_an_already_paid_order(client, monkeypatch):
    calls = []
    monkeypatch.setattr(paypal, "_capture_request", lambda *a: calls.append(a) or {})
    order = make_order(status="COMPLETED", capture_id="CAP-1")

    response = client.post(f"/paypal/capture/{order['id']}")
    assert response.json()["status"] == "COMPLETED"
    assert calls == []


# ---------------------------------------------------------------- status
def test_order_status_of_unknown_order_is_404(client):
    assert client.get("/paypal/orders/does-not-exist").status_code == 404


def test_order_status_of_a_settled_order_skips_paypal(client, monkeypatch):
    def boom(*args):
        raise AssertionError("a settled order must not call PayPal")

    monkeypatch.setattr(paypal, "get_order", boom)
    order = make_order(status="COMPLETED", capture_id="CAP-1")

    response = client.get(f"/paypal/orders/{order['id']}")

    assert response.status_code == 200
    assert response.json() == {"order_id": order["id"], "status": "COMPLETED"}


def test_order_status_reports_the_paypal_state(client, monkeypatch):
    seen = []
    monkeypatch.setattr(
        paypal,
        "get_order",
        lambda store_id, order_id: seen.append((store_id, order_id)) or {"status": "APPROVED"},
    )
    order = make_order()

    response = client.get(f"/paypal/orders/{order['id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"
    assert seen == [("store_demo", order["id"])]


# ---------------------------------------------------------------- refunds
def test_refund_sends_the_remaining_amount_once(database, fake_creds, monkeypatch):
    amounts = []

    def fake_refund(store_id, capture_id, amount, currency, request_id):
        amounts.append(amount)
        return {"id": "REF-1", "status": "COMPLETED"}

    monkeypatch.setattr(paypal, "_refund_request", fake_refund)
    order = orders.update(make_order(status="COMPLETED", capture_id="CAP-1")["id"], refunded_total=40.0)

    result = paypal.refund(orders.get(order["id"]))
    assert amounts == [289.0]
    assert result["amount_usd"] == 289.0
    assert orders.get(order["id"])["refunded_total"] == 329.0
    assert orders.get(order["id"])["status"] == "REFUNDED"

    with pytest.raises(ValueError, match="already refunded"):
        paypal.refund(orders.get(order["id"]))
    assert amounts == [289.0]


def test_refund_never_exceeds_the_captured_amount(database, fake_creds, monkeypatch):
    amounts = []
    monkeypatch.setattr(
        paypal,
        "_refund_request",
        lambda store_id, capture_id, amount, currency, request_id: amounts.append(amount)
        or {"id": "REF-1"},
    )
    order = make_order(status="COMPLETED", capture_id="CAP-1")

    with pytest.raises(ValueError, match="exceeds"):
        paypal.refund(order, amount=400.0)
    assert amounts == []

    partial = paypal.refund(order, amount=100.0)
    assert partial["status"] == "PARTIALLY_REFUNDED"
    assert orders.get(order["id"])["status"] == "COMPLETED"


def test_refund_requires_a_capture(database, fake_creds):
    order = make_order(status="COMPLETED")
    with pytest.raises(ValueError, match="no capture"):
        paypal.refund(order)


def test_duplicate_refund_request_replays_the_stored_result(database, fake_creds, monkeypatch):
    calls = []
    monkeypatch.setattr(
        paypal,
        "_refund_request",
        lambda *a: calls.append(a) or {"id": "REF-1"},
    )
    order = make_order(status="COMPLETED", capture_id="CAP-1")

    first = paypal.refund(order)
    # A retry of the same operation (same remaining amount) replays it.
    second = idempotency.claim("store_demo", "refund", f"{order['id']}:329.00")
    assert second[0] == idempotency.REPLAY
    assert second[1]["refund_id"] == first["refund_id"]
    assert len(calls) == 1


# ---------------------------------------------------------------- checkout creation
def test_create_order_replays_the_same_request_id(database, fake_creds, monkeypatch):
    calls = []

    def fake_create(store_id, body, request_id):
        calls.append(request_id)
        return FakeResponse(
            {
                "id": f"PAYPAL-{len(calls)}",
                "status": "CREATED",
                "links": [{"rel": "payer-action", "href": "https://www.paypal.com/checkoutnow?token=x"}],
            }
        )

    monkeypatch.setattr(paypal, "_create_request", fake_create)

    first = paypal.create_order("store_demo", "visitor-1", "p5", 1, request_id="msg-1")
    second = paypal.create_order("store_demo", "visitor-1", "p5", 1, request_id="msg-1")

    assert first["order_id"] == second["order_id"] == "PAYPAL-1"
    assert first["total_usd"] == 329.0  # price from the catalog, not the caller
    assert len(calls) == 1
    assert len(orders.list_for_store("store_demo")) == 1


def test_create_order_with_a_new_request_id_opens_a_new_checkout(database, fake_creds, monkeypatch):
    counter = {"n": 0}

    def fake_create(store_id, body, request_id):
        counter["n"] += 1
        return FakeResponse({"id": f"PAYPAL-{counter['n']}", "status": "CREATED", "links": []})

    monkeypatch.setattr(paypal, "_create_request", fake_create)

    paypal.create_order("store_demo", "visitor-1", "p1", 2, request_id="msg-1")
    paypal.create_order("store_demo", "visitor-1", "p1", 2, request_id="msg-2")

    assert counter["n"] == 2
    assert len(orders.list_for_store("store_demo")) == 2


def test_create_order_refuses_a_reused_request_id_with_other_items(database, fake_creds, monkeypatch):
    monkeypatch.setattr(
        paypal,
        "_create_request",
        lambda *a: FakeResponse({"id": "PAYPAL-1", "status": "CREATED", "links": []}),
    )
    paypal.create_order("store_demo", "visitor-1", "p5", 1, request_id="msg-1")
    with pytest.raises(ValueError, match="different items"):
        paypal.create_order("store_demo", "visitor-1", "p1", 4, request_id="msg-1")
