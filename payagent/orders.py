"""Orders and their audit trail, always scoped by store_id."""

import json

from . import db

ORDER_FIELDS = {
    "customer_id",
    "product_id",
    "item",
    "quantity",
    "unit_price",
    "total",
    "currency",
    "status",
    "paypal_order_id",
    "capture_id",
    "refunded_total",
    "approval_id",
}


def create(
    *,
    id: str | None = None,
    store_id: str,
    customer_id: str,
    item: str,
    total: float,
    status: str,
    product_id: str | None = None,
    quantity: int = 1,
    unit_price: float | None = None,
    currency: str = "USD",
    paypal_order_id: str | None = None,
    approval_id: str | None = None,
) -> dict:
    order_id = id or db.new_id("ord_")
    stamp = db.now()
    db.execute(
        "INSERT INTO orders (id, store_id, customer_id, product_id, item, quantity, unit_price, total, "
        "currency, status, paypal_order_id, capture_id, refunded_total, approval_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?)",
        (
            order_id,
            store_id,
            customer_id,
            product_id,
            item,
            quantity,
            unit_price,
            total,
            currency,
            status,
            paypal_order_id,
            approval_id,
            stamp,
            stamp,
        ),
    )
    log_event(order_id, store_id, "created", {"status": status, "total": total})
    return get(order_id)  # type: ignore[return-value]


def get(order_id: str) -> dict | None:
    return db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))


def get_for_store(order_id: str, store_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM orders WHERE id = ? AND store_id = ?", (order_id, store_id)
    )


def update(order_id: str, **fields) -> dict | None:
    updates = {k: v for k, v in fields.items() if k in ORDER_FIELDS}
    if not updates:
        return get(order_id)
    assignments = ", ".join(f"{key} = ?" for key in updates)
    db.execute(
        f"UPDATE orders SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), db.now(), order_id),
    )
    return get(order_id)


def list_for_store(store_id: str, limit: int = 20, status: str | None = None) -> list[dict]:
    if status:
        return db.query(
            "SELECT * FROM orders WHERE store_id = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
            (store_id, status, limit),
        )
    return db.query(
        "SELECT * FROM orders WHERE store_id = ? ORDER BY created_at DESC LIMIT ?",
        (store_id, limit),
    )


def list_for_customer(store_id: str, customer_id: str) -> list[dict]:
    return db.query(
        "SELECT * FROM orders WHERE store_id = ? AND customer_id = ? ORDER BY created_at DESC",
        (store_id, customer_id),
    )


def log_event(order_id: str, store_id: str, type_: str, data: dict | None = None) -> None:
    db.execute(
        "INSERT INTO order_events (order_id, store_id, type, data, created_at) VALUES (?, ?, ?, ?, ?)",
        (order_id, store_id, type_, json.dumps(data or {}), db.now()),
    )


def events(store_id: str, order_id: str) -> list[dict]:
    return db.query(
        "SELECT * FROM order_events WHERE store_id = ? AND order_id = ? ORDER BY id",
        (store_id, order_id),
    )
