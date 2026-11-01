"""Requests that only the authenticated store owner can approve or decline."""

import json

from . import db


def _decode(row: dict | None) -> dict | None:
    if not row:
        return None
    row = dict(row)
    row["payload"] = json.loads(row["payload"]) if row.get("payload") else {}
    row["checkout"] = json.loads(row["checkout"]) if row.get("checkout") else None
    return row


def create(
    *,
    store_id: str,
    kind: str,
    customer_id: str,
    summary: str,
    total: float,
    payload: dict | None = None,
) -> dict:
    approval_id = db.new_id("ap_")
    db.execute(
        "INSERT INTO approvals (id, store_id, kind, customer_id, summary, total, payload, status, "
        "checkout, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)",
        (approval_id, store_id, kind, customer_id, summary, total, json.dumps(payload or {}), db.now()),
    )
    return get(approval_id)  # type: ignore[return-value]


def get(approval_id: str) -> dict | None:
    return _decode(db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,)))


def get_for_store(approval_id: str, store_id: str) -> dict | None:
    return _decode(
        db.query_one(
            "SELECT * FROM approvals WHERE id = ? AND store_id = ?", (approval_id, store_id)
        )
    )


def list_pending(store_id: str) -> list[dict]:
    rows = db.query(
        "SELECT * FROM approvals WHERE store_id = ? AND status = 'pending' ORDER BY created_at",
        (store_id,),
    )
    return [_decode(r) for r in rows]


def set_decided(
    approval_id: str,
    status: str,
    *,
    decided_by: str | None = None,
    checkout: dict | None = None,
) -> dict | None:
    db.execute(
        "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, "
        "checkout = COALESCE(?, checkout) WHERE id = ?",
        (status, db.now(), decided_by, json.dumps(checkout) if checkout else None, approval_id),
    )
    return get(approval_id)
