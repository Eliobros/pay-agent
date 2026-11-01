"""Owner dashboard: overview metrics, store settings and account security.

Every query is filtered by the store of the signed-in owner, so one store never
sees another store's numbers.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db, security, stores
from .auth import current_owner

router = APIRouter(prefix="/owner", tags=["dashboard"])

CLOSED_REFUND_STATUSES = ("REFUNDED", "REJECTED", "FAILED")
FAILED_PAYMENT_STATUSES = ("FAILED", "DECLINED", "VOIDED")


def _num(value) -> float:
    return round(float(value or 0), 2)


def _int(value) -> int:
    return int(value or 0)


@router.get("/overview")
def overview(owner: dict = Depends(current_owner)):
    store_id = owner["store_id"]
    orders = db.query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status IN ('COMPLETED', 'REFUNDED') THEN 1 ELSE 0 END) AS paid, "
        "SUM(CASE WHEN status IN ('COMPLETED', 'REFUNDED') THEN total ELSE 0 END) AS sales, "
        "SUM(refunded_total) AS refunded, "
        "SUM(CASE WHEN status IN (?, ?, ?) THEN 1 ELSE 0 END) AS failed "
        "FROM orders WHERE store_id = ?",
        (*FAILED_PAYMENT_STATUSES, store_id),
    ) or {}

    approvals = db.query_one(
        "SELECT COUNT(*) AS pending FROM approvals WHERE store_id = ? AND status = 'pending'",
        (store_id,),
    ) or {}
    refunds = db.query_one(
        "SELECT "
        "SUM(CASE WHEN status NOT IN (?, ?, ?) THEN 1 ELSE 0 END) AS pending, "
        "SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed "
        "FROM refund_requests WHERE store_id = ?",
        (*CLOSED_REFUND_STATUSES, store_id),
    ) or {}
    conversations = db.query_one(
        "SELECT COUNT(*) AS active FROM conversations WHERE store_id = ? AND expires_at > ?",
        (store_id, db.now()),
    ) or {}

    sales = _num(orders.get("sales"))
    refunded = _num(orders.get("refunded"))
    failed_payments = _int(orders.get("failed"))
    failed_refunds = _int(refunds.get("failed"))

    return {
        "orders_total": _int(orders.get("total")),
        "orders_paid": _int(orders.get("paid")),
        "total_sales_usd": sales,
        "refunded_usd": refunded,
        "net_sales_usd": round(sales - refunded, 2),
        "pending_approvals": _int(approvals.get("pending")),
        "refund_requests_pending": _int(refunds.get("pending")),
        "failed_payments": failed_payments,
        "failed_refunds": failed_refunds,
        "failed_transactions": failed_payments + failed_refunds,
        "active_conversations": _int(conversations.get("active")),
    }


@router.get("/usage")
def usage(owner: dict = Depends(current_owner)):
    store_id = owner["store_id"]
    conversations = db.query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN expires_at > ? THEN 1 ELSE 0 END) AS active "
        "FROM conversations WHERE store_id = ?",
        (db.now(), store_id),
    ) or {}
    messages = db.query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END) AS assistant, "
        "SUM(CASE WHEN role = 'tool' THEN 1 ELSE 0 END) AS tool "
        "FROM messages WHERE store_id = ?",
        (store_id,),
    ) or {}

    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    recent = db.query_one(
        "SELECT COUNT(*) AS n FROM messages WHERE store_id = ? AND created_at >= ?",
        (store_id, week_ago),
    ) or {}

    tools: dict[str, int] = {}
    for row in db.query(
        "SELECT tool_calls FROM messages WHERE store_id = ? AND tool_calls IS NOT NULL",
        (store_id,),
    ):
        try:
            calls = json.loads(row["tool_calls"])
        except (TypeError, ValueError):
            continue
        for call in calls or []:
            name = ((call or {}).get("function") or {}).get("name")
            if name:
                tools[name] = tools.get(name, 0) + 1

    return {
        "conversations_total": _int(conversations.get("total")),
        "conversations_active": _int(conversations.get("active")),
        "messages_total": _int(messages.get("total")),
        "assistant_messages": _int(messages.get("assistant")),
        "tool_messages": _int(messages.get("tool")),
        "messages_last_7_days": _int(recent.get("n")),
        "tool_calls_total": sum(tools.values()),
        "tools": [
            {"name": name, "calls": calls}
            for name, calls in sorted(tools.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


# ---------------------------------------------------------------- store settings
class SettingsIn(BaseModel):
    name: str | None = None
    max_order_usd: float | None = None
    max_auto_refund_usd: float | None = None


@router.get("/settings")
def get_settings(owner: dict = Depends(current_owner)):
    return stores.public_store(stores.get_store(owner["store_id"]))


@router.patch("/settings")
def update_settings(data: SettingsIn, owner: dict = Depends(current_owner)):
    fields: dict = {}
    if data.name is not None:
        name = data.name.strip()
        if not 1 <= len(name) <= 80:
            raise HTTPException(422, "Store name must be between 1 and 80 characters")
        fields["name"] = name
    if data.max_order_usd is not None:
        if not 1 <= data.max_order_usd <= 100_000:
            raise HTTPException(422, "The purchase approval limit must be between 1 and 100000")
        fields["max_order_usd"] = round(float(data.max_order_usd), 2)
    if data.max_auto_refund_usd is not None:
        if not 0 <= data.max_auto_refund_usd <= 100_000:
            raise HTTPException(
                422, "The automatic refund limit must be between 0 and 100000"
            )
        fields["max_auto_refund_usd"] = round(float(data.max_auto_refund_usd), 2)

    store = stores.update_store(owner["store_id"], **fields)
    return stores.public_store(store)


# ---------------------------------------------------------------- account security
class PasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/account/password")
def change_password(data: PasswordIn, owner: dict = Depends(current_owner)):
    if len(data.new_password or "") < 10:
        raise HTTPException(422, "The new password must be at least 10 characters")

    row = db.query_one("SELECT password_hash FROM owners WHERE id = ?", (owner["id"],))
    if not row or not security.verify_password(row["password_hash"], data.current_password or ""):
        raise HTTPException(401, "The current password is not correct")

    db.execute(
        "UPDATE owners SET password_hash = ? WHERE id = ?",
        (security.hash_password(data.new_password), owner["id"]),
    )
    revoked = db.execute(
        "UPDATE sessions SET revoked_at = ? WHERE owner_id = ? AND id != ? AND revoked_at IS NULL",
        (db.now(), owner["id"], owner["session_id"]),
    ).rowcount
    return {"ok": True, "other_sessions_revoked": revoked}


@router.get("/sessions")
def sessions(owner: dict = Depends(current_owner)):
    rows = db.query(
        "SELECT id, created_at, last_seen_at, ip, user_agent FROM sessions "
        "WHERE owner_id = ? AND revoked_at IS NULL AND expires_at > ? "
        "ORDER BY last_seen_at DESC",
        (owner["id"], db.now()),
    )
    # Only hashes of session tokens exist in the database, and they are not returned.
    return [
        {
            "id": row["id"][:8],
            "current": row["id"] == owner["session_id"],
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
            "ip": row["ip"],
            "user_agent": row["user_agent"],
        }
        for row in rows
    ]


@router.post("/sessions/revoke-others")
def revoke_other_sessions(owner: dict = Depends(current_owner)):
    revoked = db.execute(
        "UPDATE sessions SET revoked_at = ? WHERE owner_id = ? AND id != ? AND revoked_at IS NULL",
        (db.now(), owner["id"], owner["session_id"]),
    ).rowcount
    return {"revoked": revoked}
