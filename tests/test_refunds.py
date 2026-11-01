"""Refund requests: evidence, human approval and refund execution."""

import httpx
import pytest

from payagent import db, orders, paypal, refunds, storage

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


@pytest.fixture()
def fake_paypal(monkeypatch):
    calls = []

    def fake_refund(store_id, capture_id, amount, currency, request_id):
        calls.append({"capture_id": capture_id, "amount": amount})
        return {"id": "REF-1", "status": "COMPLETED"}

    monkeypatch.setattr(
        paypal,
        "_creds",
        lambda store_id: {
            "client_id": "c",
            "client_secret": "s",
            "base_url": "https://api-m.sandbox.paypal.com",
        },
    )
    monkeypatch.setattr(paypal, "_refund_request", fake_refund)
    return calls


def paid_order(total=329.0, capture_id="CAP-1", customer="visitor-1"):
    order = orders.create(
        store_id="store_demo",
        customer_id=customer,
        item="1 x 4K Monitor 27in",
        total=total,
        status="COMPLETED",
    )
    return orders.update(order["id"], capture_id=capture_id)


def open_request(client, order, reason="the screen arrived cracked"):
    return client.post(
        "/refunds",
        json={
            "store_id": "store_demo",
            "user_id": order["customer_id"],
            "order_id": order["id"],
            "reason": reason,
        },
    )


def upload(client, request_id, data=JPEG, filename="photo.jpg", user="visitor-1"):
    return client.post(
        f"/refunds/{request_id}/evidence?user_id={user}",
        files={"file": (filename, data, "application/octet-stream")},
    )


# ---------------------------------------------------------------- creation
def test_customer_opens_a_refund_request(client):
    order = paid_order()
    response = open_request(client, order)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == refunds.REQUESTED
    assert body["amount_usd"] == 329.0
    assert body["order_id"] == order["id"]
    assert body["product_name"] == "1 x 4K Monitor 27in"
    assert body["amount_paid_usd"] == 329.0
    assert body["evidence"] == []


def test_only_paid_orders_can_be_refunded(client):
    order = orders.create(
        store_id="store_demo", customer_id="visitor-1", item="x", total=10.0, status="CREATED"
    )
    assert open_request(client, order).status_code == 409


def test_another_customer_cannot_open_a_request_for_the_order(client):
    order = paid_order()
    response = client.post(
        "/refunds",
        json={"store_id": "store_demo", "user_id": "someone-else", "order_id": order["id"]},
    )
    assert response.status_code == 404


def test_duplicate_open_request_is_refused(client):
    order = paid_order()
    assert open_request(client, order).status_code == 201
    assert open_request(client, order).status_code == 409


# ------------------------------------------------------- agent tool path
def test_refund_tool_repeats_the_open_request_instead_of_an_error(database):
    """The chat upload box only renders when the tool answers with a request id.

    When the customer asks about the same order twice, the second tool call must
    hand back the already-open request: answering with an error leaves the
    customer with nowhere to attach the evidence the AI just asked for.
    """
    import app as app_module

    from payagent import stores

    stores.ensure_demo_store()
    order = paid_order(total=329.0)
    args = {"order_id": order["id"], "reason": "screen arrived cracked", "damage": True}

    first = app_module.run_tool("refund_order", args, "visitor-1", "store_demo", "req-1")
    assert first["status"] == "awaiting_evidence"
    assert first["refund_request_id"]
    assert first["request_status"] == refunds.REQUESTED

    second = app_module.run_tool("refund_order", args, "visitor-1", "store_demo", "req-2")
    assert second["status"] == "awaiting_evidence", second
    assert second["refund_request_id"] == first["refund_request_id"]
    assert second["request_status"] == refunds.REQUESTED


# ---------------------------------------------------------------- evidence
def test_uploading_a_photo_moves_the_request_under_review(client):
    order = paid_order()
    request_id = open_request(client, order).json()["id"]

    response = upload(client, request_id)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == refunds.UNDER_REVIEW
    assert response.json()["evidence"]["kind"] == "image"

    stored = db.query_one("SELECT * FROM refund_evidence WHERE refund_request_id = ?", (request_id,))
    assert stored["sha256"]
    assert stored["storage_path"].startswith("store_demo/")
    assert storage.get_storage().read(stored["storage_path"]) == JPEG


def test_evidence_that_is_not_media_is_refused(client):
    order = paid_order()
    request_id = open_request(client, order).json()["id"]
    response = upload(client, request_id, data=b"<html>not a photo</html>", filename="photo.jpg")
    assert response.status_code == 422
    assert db.query("SELECT * FROM refund_evidence") == []


def test_video_longer_than_60_seconds_is_refused(client):
    from tests.test_media import mp4

    order = paid_order()
    request_id = open_request(client, order).json()["id"]
    response = upload(client, request_id, data=mp4(120), filename="clip.mp4")
    assert response.status_code == 422
    assert "60" in response.json()["detail"]


def test_evidence_upload_requires_the_right_customer(client):
    order = paid_order()
    request_id = open_request(client, order).json()["id"]
    assert upload(client, request_id, user="intruder").status_code == 404


# ---------------------------------------------------------------- owner decisions
def test_owner_actions_require_authentication(client):
    order = paid_order()
    request_id = open_request(client, order).json()["id"]
    assert client.post(f"/owner/refund-requests/{request_id}/decision", json={"approve": True}).status_code == 401
    assert client.get("/owner/refund-requests").status_code == 401


def test_approval_requires_evidence(owner_client):
    order = paid_order()
    request_id = open_request(owner_client, order).json()["id"]

    # Still waiting for uploads: nothing to inspect yet.
    waiting = owner_client.post(
        f"/owner/refund-requests/{request_id}/decision", json={"approve": True}
    )
    assert waiting.status_code == 409

    upload(owner_client, request_id)
    # Even if the files disappeared (purged, lost volume), approval stays blocked.
    db.execute("DELETE FROM refund_evidence WHERE refund_request_id = ?", (request_id,))
    response = owner_client.post(
        f"/owner/refund-requests/{request_id}/decision", json={"approve": True}
    )
    assert response.status_code == 409
    assert "evidence" in response.json()["detail"].lower()


def test_owner_cannot_decide_another_stores_request(owner_client):
    order = paid_order()
    request_id = open_request(owner_client, order).json()["id"]
    upload(owner_client, request_id)

    second = owner_client.post(
        "/auth/register",
        json={"email": "other@example.com", "password": "correct-horse-battery"},
    )
    assert second.status_code == 201
    assert owner_client.get("/owner/refund-requests").json() == []
    assert (
        owner_client.post(
            f"/owner/refund-requests/{request_id}/decision", json={"approve": True}
        ).status_code
        == 404
    )


def test_owner_approval_executes_the_refund(owner_client, fake_paypal):
    order = paid_order()
    request_id = open_request(owner_client, order).json()["id"]
    upload(owner_client, request_id)

    response = owner_client.post(
        f"/owner/refund-requests/{request_id}/decision",
        json={"approve": True, "note": "photos show the crack"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == refunds.REFUNDED
    assert body["paypal_refund_id"] == "REF-1"
    assert fake_paypal == [{"capture_id": "CAP-1", "amount": 329.0}]

    order = orders.get(order["id"])
    assert order["status"] == "REFUNDED"
    assert order["refunded_total"] == 329.0

    actions = [entry["action"] for entry in refunds.audit_of(request_id, "store_demo")]
    assert "status:APPROVED" in actions
    assert "status:PROCESSING" in actions
    assert "status:REFUNDED" in actions

    # A second approval cannot refund twice.
    again = owner_client.post(f"/owner/refund-requests/{request_id}/decision", json={"approve": True})
    assert again.status_code == 409
    assert len(fake_paypal) == 1


def test_failed_refund_is_recorded_and_not_marked_refunded(owner_client, fake_paypal, monkeypatch):
    def boom(store_id, capture_id, amount, currency, request_id):
        request = httpx.Request("POST", "https://api-m.sandbox.paypal.com/x")
        raise httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(500, request=request, text="error")
        )

    monkeypatch.setattr(paypal, "_refund_request", boom)
    order = paid_order()
    request_id = open_request(owner_client, order).json()["id"]
    upload(owner_client, request_id)

    response = owner_client.post(f"/owner/refund-requests/{request_id}/decision", json={"approve": True})
    assert response.status_code == 200
    assert response.json()["status"] == refunds.FAILED
    assert "HTTP 500" in response.json()["failure_reason"]
    assert orders.get(order["id"])["status"] == "COMPLETED"


def test_owner_can_reject_and_ask_for_more_evidence(owner_client):
    order = paid_order()
    request_id = open_request(owner_client, order).json()["id"]
    upload(owner_client, request_id)

    more = owner_client.post(
        f"/owner/refund-requests/{request_id}/request-evidence",
        json={"note": "please send a photo of the box"},
    )
    assert more.json()["status"] == refunds.MORE_EVIDENCE_REQUIRED

    upload(owner_client, request_id)
    rejected = owner_client.post(
        f"/owner/refund-requests/{request_id}/decision", json={"approve": False, "note": "no damage"}
    )
    assert rejected.json()["status"] == refunds.REJECTED
    assert rejected.json()["owner_note"] == "no damage"

    # A rejected request is closed for new evidence.
    assert upload(owner_client, request_id).status_code == 409


def test_request_evidence_without_files_waits_for_the_customer(owner_client):
    order = paid_order()
    request_id = open_request(owner_client, order).json()["id"]
    response = owner_client.post(f"/owner/refund-requests/{request_id}/request-evidence", json={})
    assert response.json()["status"] == refunds.WAITING_EVIDENCE


# ---------------------------------------------------------------- evidence access
def test_evidence_is_only_served_to_the_owner(owner_client):
    order = paid_order()
    request_id = open_request(owner_client, order).json()["id"]
    evidence_id = upload(owner_client, request_id).json()["evidence"]["id"]

    fetched = owner_client.get(f"/owner/refund-requests/{request_id}/evidence/{evidence_id}")
    assert fetched.status_code == 200
    assert fetched.content == JPEG
    assert fetched.headers["cache-control"] == "no-store"


def test_evidence_of_another_store_is_not_accessible(client, owner_client):
    order = paid_order()
    request_id = open_request(client, order).json()["id"]
    evidence_id = upload(client, request_id).json()["evidence"]["id"]

    second = client.post(
        "/auth/register",
        json={"email": "other@example.com", "password": "correct-horse-battery"},
    )
    assert second.status_code == 201
    assert second.json()["owner"]["store_id"] != "store_demo"

    foreign = client.get(f"/owner/refund-requests/{request_id}/evidence/{evidence_id}")
    assert foreign.status_code == 404


# ---------------------------------------------------------------- retention
def test_purge_removes_evidence_of_closed_requests(owner_client, fake_paypal):
    order = paid_order()
    request_id = open_request(owner_client, order).json()["id"]
    upload(owner_client, request_id)
    owner_client.post(f"/owner/refund-requests/{request_id}/decision", json={"approve": True})

    stored = db.query_one("SELECT * FROM refund_evidence WHERE refund_request_id = ?", (request_id,))
    path = stored["storage_path"]
    assert storage.get_storage().read(path) == JPEG

    db.execute(
        "UPDATE refund_requests SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (request_id,),
    )
    purged = owner_client.post("/owner/refund-requests/purge")
    assert purged.json()["deleted"] == 1
    assert purged.json()["retention_days"] == 30
    assert db.query("SELECT * FROM refund_evidence") == []

    with pytest.raises(storage.StorageError):
        storage.get_storage().read(path)
