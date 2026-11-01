"""PayPal REST client: orders, captures, refunds and webhook verification.

Money-moving functions are idempotent: they claim an idempotency key before the
HTTP call, send a deterministic ``PayPal-Request-Id`` so PayPal can dedupe on its
side, and only update the database after a confirmed response.
"""

import json
import uuid

import httpx

from . import config, idempotency, orders, providers, stores


class NotConfigured(ValueError):
    """No usable PayPal credentials for this store."""


class ConcurrentOperation(RuntimeError):
    """The same financial operation is already in flight."""


# ---------------------------------------------------------------- plumbing
def credentials(store_id: str) -> dict:
    return providers.paypal_credentials(store_id)


def _creds(store_id: str) -> dict:
    creds = credentials(store_id)
    if not creds["client_id"] or not creds["client_secret"]:
        raise NotConfigured("PayPal is not configured for this store yet")
    return creds


def token(store_id: str) -> str:
    creds = _creds(store_id)
    response = httpx.post(
        f"{creds['base_url']}/v1/oauth2/token",
        auth=(creds["client_id"], creds["client_secret"]),
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _headers(store_id: str, request_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token(store_id)}",
        "Content-Type": "application/json",
    }
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    return headers


# ---------------------------------------------------------------- orders
def _create_request(store_id: str, body: dict, request_id: str):
    creds = _creds(store_id)
    return httpx.post(
        f"{creds['base_url']}/v2/checkout/orders",
        headers=_headers(store_id, request_id),
        json=body,
        timeout=20,
    )


def create_order(
    store_id: str,
    customer_id: str,
    product_id: str,
    quantity: int,
    request_id: str | None = None,
) -> dict:
    catalog = stores.catalog_for(store_id)
    if product_id not in catalog:
        raise ValueError("unknown product")
    _creds(store_id)

    product = catalog[product_id]
    total = round(product["price"] * quantity, 2)
    value = f"{total:.2f}"
    body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "amount": {
                    "currency_code": "USD",
                    "value": value,
                    "breakdown": {"item_total": {"currency_code": "USD", "value": value}},
                },
                "items": [
                    {
                        "name": product["name"],
                        "quantity": str(quantity),
                        "unit_amount": {
                            "currency_code": "USD",
                            "value": f"{product['price']:.2f}",
                        },
                    }
                ],
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "user_action": "PAY_NOW",
                    "return_url": f"{config.FRONTEND_ORIGIN}/success",
                    "cancel_url": f"{config.FRONTEND_ORIGIN}/cancel",
                }
            }
        },
    }

    key = request_id or uuid.uuid4().hex
    fingerprint = idempotency.fingerprint(
        {"store": store_id, "customer": customer_id, "product": product_id, "quantity": quantity}
    )
    state, cached = idempotency.claim(store_id, "order_create", key, fingerprint)
    if state == idempotency.REPLAY:
        return cached
    if state == idempotency.BUSY:
        raise ConcurrentOperation("this checkout is already being created")
    if state == idempotency.CONFLICT:
        raise ValueError("this request id was already used for different items")

    try:
        response = _create_request(store_id, body, key)
        response.raise_for_status()
        data = response.json()
        link = next(
            (l["href"] for l in data.get("links", []) if l["rel"] in ("payer-action", "approve")),
            None,
        )
        existing = orders.get(data["id"])
        if existing:
            result = {
                "order_id": existing["id"],
                "total_usd": existing["total"],
                "approval_url": link,
            }
        else:
            orders.create(
                id=data["id"],
                store_id=store_id,
                customer_id=customer_id,
                product_id=product_id,
                item=f"{quantity} x {product['name']}",
                quantity=quantity,
                unit_price=product["price"],
                total=total,
                currency="USD",
                status=data["status"],
                paypal_order_id=data["id"],
            )
            result = {"order_id": data["id"], "total_usd": total, "approval_url": link}
        idempotency.complete(store_id, "order_create", key, result)
        return result
    except Exception:
        idempotency.fail(store_id, "order_create", key)
        raise


def get_order(store_id: str, order_id: str) -> dict:
    creds = _creds(store_id)
    response = httpx.get(
        f"{creds['base_url']}/v2/checkout/orders/{order_id}",
        headers={"Authorization": f"Bearer {token(store_id)}"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------- capture
def _capture_request(store_id: str, order_id: str, request_id: str) -> dict:
    creds = _creds(store_id)
    response = httpx.post(
        f"{creds['base_url']}/v2/checkout/orders/{order_id}/capture",
        headers=_headers(store_id, request_id),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def capture_order(store_id: str, order_id: str) -> dict:
    """Capture an order at most once, no matter how often this is retried."""
    order = orders.get_for_store(order_id, store_id)
    if not order:
        raise LookupError("unknown order")
    if order["status"] in ("COMPLETED", "REFUNDED"):
        return {
            "order_id": order["id"],
            "status": order["status"],
            "capture_id": order["capture_id"],
        }

    state, cached = idempotency.claim(store_id, "capture", order_id)
    if state == idempotency.REPLAY:
        return cached
    if state == idempotency.BUSY:
        raise ConcurrentOperation("this payment is already being confirmed")

    try:
        data = _capture_request(store_id, order_id, f"capture-{order_id}")
        capture_id = order["capture_id"]
        try:
            capture_id = data["purchase_units"][0]["payments"]["captures"][0]["id"]
        except (KeyError, IndexError, TypeError):
            pass
        status = data.get("status", "UNKNOWN")
        orders.update(order_id, status=status, capture_id=capture_id)
        orders.log_event(order_id, store_id, "capture", {"status": status})
        result = {"order_id": order_id, "status": status, "capture_id": capture_id}
        idempotency.complete(store_id, "capture", order_id, result)
        return result
    except httpx.HTTPStatusError as e:
        if "ORDER_ALREADY_CAPTURED" in (e.response.text or ""):
            # Another attempt (or the webhook) captured it first: never capture again.
            orders.update(order_id, status="COMPLETED")
            result = {"order_id": order_id, "status": "COMPLETED", "capture_id": order["capture_id"]}
            idempotency.complete(store_id, "capture", order_id, result)
            return result
        idempotency.fail(store_id, "capture", order_id)
        raise
    except Exception:
        idempotency.fail(store_id, "capture", order_id)
        raise


# ---------------------------------------------------------------- refunds
def _refund_request(
    store_id: str, capture_id: str, amount: float, currency: str, request_id: str
) -> dict:
    creds = _creds(store_id)
    response = httpx.post(
        f"{creds['base_url']}/v2/payments/captures/{capture_id}/refund",
        headers=_headers(store_id, request_id),
        json={"amount": {"currency_code": currency, "value": f"{amount:.2f}"}},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def refund(order: dict, amount: float | None = None) -> dict:
    """Refund up to the captured refundable amount, at most once per amount."""
    store_id = order["store_id"]
    total = round(float(order["total"]), 2)
    refunded = round(float(order.get("refunded_total") or 0), 2)
    remaining = round(total - refunded, 2)

    if not order.get("capture_id"):
        raise ValueError("order has no capture to refund")
    if remaining <= 0:
        raise ValueError("order already refunded")

    amount = remaining if amount is None else round(float(amount), 2)
    if amount <= 0:
        raise ValueError("nothing left to refund")
    if amount > remaining:
        raise ValueError("refund amount exceeds the refundable amount")

    key = f"{order['id']}:{remaining:.2f}"
    state, cached = idempotency.claim(store_id, "refund", key)
    if state == idempotency.REPLAY:
        return cached
    if state == idempotency.BUSY:
        raise ConcurrentOperation("a refund for this order is already running")

    try:
        data = _refund_request(
            store_id,
            order["capture_id"],
            amount,
            order.get("currency") or "USD",
            f"refund-{order['capture_id']}-{key.split(':')[-1]}",
        )
        refunded_total = round(refunded + amount, 2)
        orders.update(
            order["id"],
            refunded_total=refunded_total,
            status="REFUNDED" if refunded_total >= total - 0.005 else order["status"],
        )
        orders.log_event(
            order["id"],
            store_id,
            "refund",
            {"amount_usd": amount, "paypal_refund_id": data.get("id")},
        )
        result = {
            "order_id": order["id"],
            "refund_id": data.get("id"),
            "amount_usd": amount,
            "status": "REFUNDED" if refunded_total >= total - 0.005 else "PARTIALLY_REFUNDED",
        }
        idempotency.complete(store_id, "refund", key, result)
        return result
    except Exception:
        idempotency.fail(store_id, "refund", key)
        raise


# ---------------------------------------------------------------- webhooks
SIGNATURE_HEADERS = (
    "paypal-auth-algo",
    "paypal-cert-url",
    "paypal-transmission-id",
    "paypal-transmission-sig",
    "paypal-transmission-time",
)


def verify_webhook(store_id: str, headers: dict, body: bytes) -> bool:
    """Ask PayPal whether this delivery really came from them.

    Frontend confirmations are not trusted; neither is an unverified webhook.
    """
    webhook_id = config.PAYPAL_WEBHOOK_ID
    if not webhook_id:
        raise NotConfigured("PAYPAL_WEBHOOK_ID is not set: webhooks cannot be verified")
    creds = _creds(store_id)
    if not all(headers.get(name) for name in SIGNATURE_HEADERS):
        return False
    try:
        event = json.loads(body)
    except (TypeError, ValueError):
        return False

    payload = {
        "auth_algo": headers["paypal-auth-algo"],
        "cert_url": headers["paypal-cert-url"],
        "transmission_id": headers["paypal-transmission-id"],
        "transmission_sig": headers["paypal-transmission-sig"],
        "transmission_time": headers["paypal-transmission-time"],
        "webhook_id": webhook_id,
        "webhook_event": event,
    }
    response = httpx.post(
        f"{creds['base_url']}/v1/notifications/verify-webhook-signature",
        headers={
            "Authorization": f"Bearer {token(store_id)}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    return response.json().get("verification_status") == "SUCCESS"
