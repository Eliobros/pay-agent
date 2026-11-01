"""PayPal webhook endpoint: verified, deduplicated and idempotent.

Only a delivery whose signature PayPal itself confirms may change a payment
status. Each PayPal event id is stored once, so repeated deliveries never apply
the same financial change twice.
"""

import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import config, db, orders, paypal

log = logging.getLogger("payagent.webhooks")

MAX_BODY_BYTES = 1_000_000

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------- helpers
def _related_ids(event: dict) -> dict:
    resource = event.get("resource") or {}
    return ((resource.get("supplementary_data") or {}).get("related_ids") or {})


def _up_link_id(resource: dict) -> str | None:
    for link in resource.get("links") or []:
        if link.get("rel") == "up" and "/captures/" in (link.get("href") or ""):
            return link["href"].rstrip("/").split("/")[-1]
    return None


def _amount_of(resource: dict) -> float:
    try:
        return float((resource.get("amount") or {}).get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_by_capture(store_id: str, capture_id: str | None) -> dict | None:
    if not capture_id:
        return None
    return db.query_one(
        "SELECT * FROM orders WHERE store_id = ? AND capture_id = ?", (store_id, capture_id)
    )


def find_order(store_id: str, event: dict) -> dict | None:
    resource = event.get("resource") or {}
    related = _related_ids(event)

    order = orders.get_for_store(related.get("order_id") or "", store_id)
    if order:
        return order

    order = _order_by_capture(store_id, related.get("capture_id") or _up_link_id(resource))
    if order:
        return order

    if str(event.get("event_type", "")).startswith("CHECKOUT.ORDER"):
        return orders.get_for_store(resource.get("id") or "", store_id)
    return None


def resolve_store(event: dict) -> str:
    """Best-effort store lookup from the payload, used only to pick credentials.

    Nothing is trusted until the signature check has passed and nothing is
    written before that.
    """
    resource = event.get("resource") or {}
    related = _related_ids(event)
    order_id = related.get("order_id")
    if order_id:
        row = db.query_one("SELECT store_id FROM orders WHERE id = ?", (order_id,))
        if row:
            return row["store_id"]
    capture_id = related.get("capture_id") or _up_link_id(resource)
    if not capture_id and "CAPTURE" in str(event.get("event_type", "")):
        capture_id = resource.get("id")
    if capture_id:
        row = db.query_one("SELECT store_id FROM orders WHERE capture_id = ?", (capture_id,))
        if row:
            return row["store_id"]
    return config.DEFAULT_STORE_ID


# ---------------------------------------------------------------- handlers
def _mark_refund_requests(order: dict, status: str, note: str) -> None:
    """Keep refund requests in sync when PayPal confirms a refund (phase 4 data)."""
    db.execute(
        "UPDATE refund_requests SET status = ?, updated_at = ? "
        "WHERE order_id = ? AND store_id = ? AND status NOT IN ('REFUNDED', 'FAILED')",
        (status, db.now(), order["id"], order["store_id"]),
    )
    db.execute(
        "INSERT INTO refund_events (refund_request_id, store_id, actor_type, actor_id, action, note, "
        "created_at) SELECT id, store_id, 'paypal', 'webhook', ?, ?, ? FROM refund_requests "
        "WHERE order_id = ? AND store_id = ? AND status = ?",
        (note, note, db.now(), order["id"], order["store_id"], status),
    )


def _capture_completed(store_id: str, event: dict) -> str:
    resource = event.get("resource") or {}
    order = find_order(store_id, event)
    if not order:
        return "ignored"
    if order["status"] in ("COMPLETED", "REFUNDED"):
        return "processed"  # already applied: webhook is idempotent
    capture_id = resource.get("id") or order["capture_id"]
    orders.update(order["id"], status="COMPLETED", capture_id=capture_id)
    orders.log_event(
        order["id"],
        store_id,
        "webhook.capture.completed",
        {"event_id": event.get("id"), "capture_id": capture_id, "amount_usd": _amount_of(resource)},
    )
    return "processed"


def _capture_refunded(store_id: str, event: dict) -> str:
    resource = event.get("resource") or {}
    order = find_order(store_id, event)
    if not order:
        return "ignored"

    amount = _amount_of(resource)
    total = round(float(order["total"]), 2)
    refunded = round(float(order.get("refunded_total") or 0) + amount, 2)
    refunded = min(refunded, total)  # never above the captured amount
    status = "REFUNDED" if refunded >= total - 0.005 else order["status"]
    orders.update(order["id"], refunded_total=refunded, status=status)
    orders.log_event(
        order["id"],
        store_id,
        "webhook.capture.refunded",
        {"event_id": event.get("id"), "amount_usd": amount, "refunded_total": refunded},
    )
    _mark_refund_requests(order, "REFUNDED", "PayPal confirmed the refund")
    return "processed"


def _order_approved(store_id: str, event: dict) -> str:
    order = find_order(store_id, event)
    if not order or order["status"] not in ("CREATED", "PAYER_ACTION_REQUIRED"):
        return "ignored"
    orders.update(order["id"], status="APPROVED")
    orders.log_event(order["id"], store_id, "webhook.order.approved", {"event_id": event.get("id")})
    return "processed"


HANDLERS = {
    "PAYMENT.CAPTURE.COMPLETED": _capture_completed,
    "PAYMENT.CAPTURE.REFUNDED": _capture_refunded,
    "CHECKOUT.ORDER.APPROVED": _order_approved,
}


# ---------------------------------------------------------------- endpoint
def _record_event(store_id: str, event_id: str, event: dict, event_type: str) -> bool:
    """Insert the event id. False means it was already delivered before."""
    cursor = db.execute(
        "INSERT INTO webhook_events (id, store_id, provider, event_type, payload, verified, state, "
        "created_at) VALUES (?, ?, 'paypal', ?, ?, 1, 'received', ?) "
        "ON CONFLICT(id) DO NOTHING",
        (event_id, store_id, event_type, json.dumps(event), db.now()),
    )
    return cursor.rowcount == 1


def _finish_event(store_id: str, event_id: str, state: str) -> None:
    db.execute(
        "UPDATE webhook_events SET state = ?, processed_at = ? WHERE id = ? AND store_id = ?",
        (state, db.now(), event_id, store_id),
    )


def _forget_event(store_id: str, event_id: str) -> None:
    """Drop the record so PayPal's retry of a failed delivery can be processed."""
    db.execute("DELETE FROM webhook_events WHERE id = ? AND store_id = ?", (event_id, store_id))


@router.post("/paypal")
async def paypal_webhook(request: Request):
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "Payload too large")
    try:
        event = json.loads(body)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid JSON payload")
    if not isinstance(event, dict):
        raise HTTPException(400, "Invalid webhook event")

    event_id = str(event.get("id") or "").strip()
    event_type = str(event.get("event_type") or "").strip()
    if not event_id:
        raise HTTPException(400, "Missing event id")

    headers = {name: request.headers.get(name, "") for name in paypal.SIGNATURE_HEADERS}
    store_id = resolve_store(event)

    try:
        verified = paypal.verify_webhook(store_id, headers, body)
    except paypal.NotConfigured as e:
        raise HTTPException(503, str(e)) from e
    except httpx.HTTPError as e:
        log.warning("PayPal signature verification failed to answer (%s)", type(e).__name__)
        raise HTTPException(502, "Could not verify the webhook with PayPal") from e

    if not verified:
        log.warning("rejected PayPal webhook %s: invalid signature", event_id)
        raise HTTPException(400, "Invalid webhook signature")

    if not _record_event(store_id, event_id, event, event_type):
        return {"status": "duplicate", "event_id": event_id}

    handler = HANDLERS.get(event_type)
    if not handler:
        _finish_event(store_id, event_id, "ignored")
        return {"status": "ignored", "event_id": event_id}

    try:
        state = handler(store_id, event) or "processed"
    except Exception:
        _forget_event(store_id, event_id)
        log.exception("could not process PayPal webhook %s (%s)", event_id, event_type)
        raise HTTPException(500, "Could not process the event")

    _finish_event(store_id, event_id, state)
    return {"status": state, "event_id": event_id}
