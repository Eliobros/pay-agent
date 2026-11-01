"""Refund requests, private evidence and owner decisions.

The AI can open a request and collect photos/videos; it can never approve one.
Only the authenticated owner of the store may approve or reject, and a refund is
marked REFUNDED only after PayPal confirms it.
"""

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from . import config, db, media, orders, paypal, storage
from .auth import current_owner

router = APIRouter(tags=["refunds"])

REQUESTED = "REQUESTED"
WAITING_EVIDENCE = "WAITING_EVIDENCE"
UNDER_REVIEW = "UNDER_REVIEW"
MORE_EVIDENCE_REQUIRED = "MORE_EVIDENCE_REQUIRED"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
PROCESSING = "PROCESSING"
REFUNDED = "REFUNDED"
FAILED = "FAILED"

ALL_STATUSES = [
    REQUESTED, WAITING_EVIDENCE, UNDER_REVIEW, MORE_EVIDENCE_REQUIRED,
    APPROVED, REJECTED, PROCESSING, REFUNDED, FAILED,
]
CLOSED_STATUSES = {REJECTED, REFUNDED, FAILED}

TRANSITIONS = {
    REQUESTED: {WAITING_EVIDENCE, UNDER_REVIEW, REJECTED},
    WAITING_EVIDENCE: {UNDER_REVIEW, MORE_EVIDENCE_REQUIRED, REJECTED},
    UNDER_REVIEW: {MORE_EVIDENCE_REQUIRED, APPROVED, REJECTED},
    MORE_EVIDENCE_REQUIRED: {UNDER_REVIEW, REJECTED},
    APPROVED: {PROCESSING, FAILED},
    PROCESSING: {REFUNDED, FAILED},
    REJECTED: set(),
    REFUNDED: set(),
    FAILED: set(),
}

SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]")
SAFE_FILENAME = re.compile(r"[^\w.\- ]")


def clean_visitor(raw: str) -> str:
    value = SAFE_NAME.sub("", raw or "")[:64]
    return value or "guest"


# ---------------------------------------------------------------- storage
def get(request_id: str, store_id: str | None = None) -> dict | None:
    if store_id:
        return db.query_one(
            "SELECT * FROM refund_requests WHERE id = ? AND store_id = ?", (request_id, store_id)
        )
    return db.query_one("SELECT * FROM refund_requests WHERE id = ?", (request_id,))


def get_open_for_order(store_id: str, order_id: str, customer_id: str) -> dict | None:
    """The customer's still-open request for this order, if any."""
    closed = ", ".join(f"'{status}'" for status in sorted(CLOSED_STATUSES))
    return db.query_one(
        "SELECT * FROM refund_requests WHERE store_id = ? AND order_id = ? AND customer_id = ? "
        f"AND status NOT IN ({closed})",
        (store_id, order_id, customer_id),
    )


def list_for_store(store_id: str, status: str | None = None) -> list[dict]:
    if status:
        return db.query(
            "SELECT * FROM refund_requests WHERE store_id = ? AND status = ? ORDER BY created_at DESC",
            (store_id, status),
        )
    return db.query(
        "SELECT * FROM refund_requests WHERE store_id = ? ORDER BY created_at DESC", (store_id,)
    )


def evidence_of(request_id: str, store_id: str) -> list[dict]:
    return db.query(
        "SELECT * FROM refund_evidence WHERE refund_request_id = ? AND store_id = ? ORDER BY created_at",
        (request_id, store_id),
    )


def audit_of(request_id: str, store_id: str) -> list[dict]:
    return db.query(
        "SELECT actor_type, actor_id, action, note, created_at FROM refund_events "
        "WHERE refund_request_id = ? AND store_id = ? ORDER BY id",
        (request_id, store_id),
    )


def _log(request_id: str, store_id: str, actor_type: str, actor_id: str, action: str, note: str = "") -> None:
    db.execute(
        "INSERT INTO refund_events (refund_request_id, store_id, actor_type, actor_id, action, note, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (request_id, store_id, actor_type, actor_id, action, note[:500], db.now()),
    )


def _transition(request: dict, new_status: str, actor_type: str, actor_id: str, note: str = "") -> dict:
    allowed = TRANSITIONS.get(request["status"], set())
    if new_status not in allowed:
        raise ValueError(f"cannot move a request from {request['status']} to {new_status}")
    db.execute(
        "UPDATE refund_requests SET status = ?, updated_at = ? WHERE id = ? AND store_id = ?",
        (new_status, db.now(), request["id"], request["store_id"]),
    )
    _log(request["id"], request["store_id"], actor_type, actor_id, f"status:{new_status}", note)
    return get(request["id"], request["store_id"])  # type: ignore[return-value]


# ---------------------------------------------------------------- creation
def create(
    store_id: str,
    customer_id: str,
    order_id: str,
    reason: str = "",
    amount: float | None = None,
) -> dict:
    order = orders.get_for_store(order_id, store_id)
    if not order or order["customer_id"] != customer_id:
        raise LookupError("order not found for this customer")
    if order["status"] != "COMPLETED":
        raise ValueError("only a completed payment can be refunded")

    remaining = round(float(order["total"]) - float(order["refunded_total"] or 0), 2)
    if remaining <= 0:
        raise ValueError("this order has already been refunded")

    requested = remaining if amount is None else round(float(amount), 2)
    if requested <= 0:
        raise ValueError("the refund amount must be positive")
    if requested > remaining + 0.005:
        raise ValueError("the refund amount is higher than the refundable amount")

    request_id = db.new_id("rf_")
    stamp = db.now()
    try:
        db.execute(
            "INSERT INTO refund_requests (id, store_id, order_id, customer_id, amount, currency, reason, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                store_id,
                order["id"],
                customer_id,
                requested,
                order.get("currency") or "USD",
                (reason or "")[:1000],
                REQUESTED,
                stamp,
                stamp,
            ),
        )
    except sqlite3.IntegrityError:
        raise ValueError("this order already has an open refund request") from None
    _log(request_id, store_id, "customer", customer_id, "created", (reason or "")[:300])
    return get(request_id)  # type: ignore[return-value]


# ---------------------------------------------------------------- evidence
def add_evidence(request: dict, data: bytes, filename: str = "") -> dict:
    if request["status"] in CLOSED_STATUSES or request["status"] in (APPROVED, PROCESSING):
        raise ValueError("this refund request is closed for new evidence")

    mime, duration = media.validate(data, filename)
    path = storage.get_storage().put(request["store_id"], data, media.SUFFIX[mime])

    evidence_id = db.new_id("ev_")
    db.execute(
        "INSERT INTO refund_evidence (id, refund_request_id, store_id, original_name, kind, mime, size, "
        "duration_seconds, sha256, storage_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            request["id"],
            request["store_id"],
            SAFE_FILENAME.sub("_", (filename or ""))[:120],
            media.kind_of(mime),
            mime,
            len(data),
            duration,
            hashlib.sha256(data).hexdigest(),
            path,
            db.now(),
        ),
    )
    _log(
        request["id"], request["store_id"], "customer", request["customer_id"],
        "evidence_added", f"{mime} ({len(data)} bytes)",
    )

    current = get(request["id"], request["store_id"])
    if current["status"] in (REQUESTED, WAITING_EVIDENCE, MORE_EVIDENCE_REQUIRED):
        current = _transition(current, UNDER_REVIEW, "customer", request["customer_id"])
    return db.query_one("SELECT * FROM refund_evidence WHERE id = ?", (evidence_id,))


def read_evidence(store_id: str, evidence_id: str) -> tuple[bytes, dict]:
    row = db.query_one(
        "SELECT * FROM refund_evidence WHERE id = ? AND store_id = ?", (evidence_id, store_id)
    )
    if not row:
        raise LookupError("evidence not found")
    return storage.get_storage().read(row["storage_path"]), row


# ---------------------------------------------------------------- owner decisions
def decide(request: dict, approve: bool, owner_id: str, note: str = "") -> dict:
    if approve:
        if request["status"] != UNDER_REVIEW:
            raise ValueError(f"a request in status {request['status']} cannot be approved")
        files = evidence_of(request["id"], request["store_id"])
        if not files:
            # The owner must inspect real photos or videos before approving.
            raise ValueError("inspect at least one evidence file before approving")
        request = _transition(request, APPROVED, "owner", owner_id, note)
        return _refund(request, owner_id)

    request = _transition(request, REJECTED, "owner", owner_id, note)
    return request


def request_more_evidence(request: dict, owner_id: str, note: str = "") -> dict:
    if request["status"] in CLOSED_STATUSES:
        raise ValueError("this request is already closed")
    target = MORE_EVIDENCE_REQUIRED if evidence_of(request["id"], request["store_id"]) else WAITING_EVIDENCE
    if target == request["status"]:
        return request
    return _transition(request, target, "owner", owner_id, note)


def _fail(request: dict, owner_id: str, reason: str) -> dict:
    db.execute(
        "UPDATE refund_requests SET status = ?, failure_reason = ?, updated_at = ? WHERE id = ? AND store_id = ?",
        (FAILED, reason[:500], db.now(), request["id"], request["store_id"]),
    )
    _log(request["id"], request["store_id"], "owner", owner_id, "status:FAILED", reason)
    return get(request["id"], request["store_id"])  # type: ignore[return-value]


def _refund(request: dict, owner_id: str) -> dict:
    """Execute the PayPal refund and only then mark the request REFUNDED."""
    order = orders.get_for_store(request["order_id"], request["store_id"])
    if not order:
        return _fail(request, owner_id, "the order of this request no longer exists")

    remaining = round(float(order["total"]) - float(order["refunded_total"] or 0), 2)
    if remaining <= 0:
        return _fail(request, owner_id, "the order has no refundable amount left")
    if round(float(request["amount"]), 2) > remaining + 0.005:
        return _fail(request, owner_id, "the requested amount is no longer refundable")

    request = _transition(request, PROCESSING, "owner", owner_id)
    try:
        result = paypal.refund(order, amount=float(request["amount"]))
    except paypal.NotConfigured as e:
        return _fail(request, owner_id, str(e))
    except paypal.ConcurrentOperation:
        return _fail(request, owner_id, "another refund for this order is running")
    except ValueError as e:
        return _fail(request, owner_id, str(e))
    except httpx.HTTPStatusError as e:
        return _fail(request, owner_id, f"PayPal refused the refund (HTTP {e.response.status_code})")
    except httpx.HTTPError:
        return _fail(request, owner_id, "PayPal could not be reached")

    db.execute(
        "UPDATE refund_requests SET status = ?, paypal_refund_id = ?, failure_reason = NULL, "
        "updated_at = ?, decided_at = ?, decided_by = ? WHERE id = ? AND store_id = ?",
        (
            REFUNDED,
            result.get("refund_id"),
            db.now(),
            db.now(),
            owner_id,
            request["id"],
            request["store_id"],
        ),
    )
    _log(
        request["id"], request["store_id"], "owner", owner_id, "status:REFUNDED",
        f"PayPal refund {result.get('refund_id')}",
    )
    return get(request["id"], request["store_id"])  # type: ignore[return-value]


# ---------------------------------------------------------------- retention
def purge_expired_evidence(store_id: str | None = None, days: int | None = None) -> int:
    """Delete evidence of closed requests older than the retention window."""
    days = config.EVIDENCE_RETENTION_DAYS if days is None else days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    closed = tuple(CLOSED_STATUSES)
    placeholders = ", ".join("?" for _ in closed)
    sql = (
        "SELECT e.* FROM refund_evidence e JOIN refund_requests r ON r.id = e.refund_request_id "
        f"WHERE r.status IN ({placeholders}) AND r.updated_at < ?"
    )
    params: tuple = (*closed, cutoff)
    if store_id:
        sql += " AND e.store_id = ?"
        params += (store_id,)

    rows = db.query(sql, params)
    store = storage.get_storage()
    for row in rows:
        store.delete(row["storage_path"])
        db.execute(
            "DELETE FROM refund_evidence WHERE id = ? AND store_id = ?",
            (row["id"], row["store_id"]),
        )
    return len(rows)


# ---------------------------------------------------------------- serialisation
def _public(request: dict, *, with_audit: bool = False) -> dict:
    order = orders.get_for_store(request["order_id"], request["store_id"]) or {}
    files = evidence_of(request["id"], request["store_id"])
    payload = {
        "id": request["id"],
        "order_id": request["order_id"],
        "customer_id": request["customer_id"],
        "amount_usd": round(float(request["amount"]), 2),
        "currency": request["currency"],
        "reason": request["reason"],
        "status": request["status"],
        "owner_note": request["owner_note"],
        "paypal_refund_id": request["paypal_refund_id"],
        "failure_reason": request["failure_reason"],
        "created_at": request["created_at"],
        "updated_at": request["updated_at"],
        "decided_at": request["decided_at"],
        "decided_by": request["decided_by"],
        "product_name": order.get("item"),
        "amount_paid_usd": round(float(order["total"]), 2) if order else None,
        "purchase_date": order.get("created_at"),
        "evidence": [
            {
                "id": e["id"],
                "kind": e["kind"],
                "mime": e["mime"],
                "size": e["size"],
                "duration_seconds": e["duration_seconds"],
                "original_name": e["original_name"],
                "created_at": e["created_at"],
            }
            for e in files
        ],
    }
    if with_audit:
        payload["audit"] = audit_of(request["id"], request["store_id"])
    return payload


# ---------------------------------------------------------------- API
class RefundIn(BaseModel):
    store_id: str = config.DEFAULT_STORE_ID
    user_id: str
    order_id: str
    reason: str = ""


@router.post("/refunds", status_code=201)
def create_refund(data: RefundIn):
    store_id = SAFE_NAME.sub("", data.store_id or "")[:64] or config.DEFAULT_STORE_ID
    try:
        request = create(store_id, clean_visitor(data.user_id), data.order_id, data.reason)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _public(request)


@router.post("/refunds/{request_id}/evidence", status_code=201)
async def upload_evidence(request_id: str, user_id: str, file: UploadFile = File(...)):
    request = get(request_id)
    if not request or request["customer_id"] != clean_visitor(user_id):
        raise HTTPException(404, "refund request not found")

    data = await file.read(media.max_bytes() + 1)
    try:
        evidence = add_evidence(request, data, file.filename or "")
    except media.EvidenceError as e:
        raise HTTPException(422, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {
        "evidence": {
            "id": evidence["id"],
            "kind": evidence["kind"],
            "mime": evidence["mime"],
            "size": evidence["size"],
            "duration_seconds": evidence["duration_seconds"],
        },
        "status": get(request_id)["status"],
    }


@router.get("/refunds/{request_id}")
def refund_status(request_id: str, user_id: str):
    request = get(request_id)
    if not request or request["customer_id"] != clean_visitor(user_id):
        raise HTTPException(404, "refund request not found")
    return {
        "id": request["id"],
        "status": request["status"],
        "amount_usd": round(float(request["amount"]), 2),
        "order_id": request["order_id"],
        "evidence_count": len(evidence_of(request["id"], request["store_id"])),
    }


class DecisionIn(BaseModel):
    approve: bool
    note: str = ""


class NoteIn(BaseModel):
    note: str = ""


@router.get("/owner/refund-requests")
def owner_refund_requests(status: str | None = None, owner: dict = Depends(current_owner)):
    return [_public(r) for r in list_for_store(owner["store_id"], status)]


@router.post("/owner/refund-requests/purge")
def owner_purge(owner: dict = Depends(current_owner)):
    """Delete evidence of closed requests past the retention window."""
    deleted = purge_expired_evidence(store_id=owner["store_id"])
    return {"deleted": deleted, "retention_days": config.EVIDENCE_RETENTION_DAYS}


@router.get("/owner/refund-requests/{request_id}")
def owner_refund_request(request_id: str, owner: dict = Depends(current_owner)):
    request = get(request_id, owner["store_id"])
    if not request:
        raise HTTPException(404, "refund request not found")
    return _public(request, with_audit=True)


@router.get("/owner/refund-requests/{request_id}/evidence/{evidence_id}")
def owner_evidence(request_id: str, evidence_id: str, owner: dict = Depends(current_owner)):
    request = get(request_id, owner["store_id"])
    if not request:
        raise HTTPException(404, "refund request not found")
    row = db.query_one(
        "SELECT * FROM refund_evidence WHERE id = ? AND refund_request_id = ? AND store_id = ?",
        (evidence_id, request_id, owner["store_id"]),
    )
    if not row:
        raise HTTPException(404, "evidence not found")
    try:
        data, _ = read_evidence(owner["store_id"], evidence_id)
    except (LookupError, storage.StorageError):
        raise HTTPException(410, "this evidence file is no longer available")
    return Response(
        content=data,
        media_type=row["mime"],
        headers={
            "Content-Disposition": f'attachment; filename="{row["id"]}{media.SUFFIX.get(row["mime"], "")}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/owner/refund-requests/{request_id}/decision")
def owner_decision(request_id: str, data: DecisionIn, owner: dict = Depends(current_owner)):
    request = get(request_id, owner["store_id"])
    if not request:
        raise HTTPException(404, "refund request not found")
    try:
        updated = decide(request, data.approve, owner["id"], data.note)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if data.note:
        db.execute(
            "UPDATE refund_requests SET owner_note = ? WHERE id = ? AND store_id = ?",
            (data.note[:1000], request_id, owner["store_id"]),
        )
    return _public(get(request_id, owner["store_id"]) if updated["status"] != FAILED else updated)


@router.post("/owner/refund-requests/{request_id}/request-evidence")
def owner_request_evidence(request_id: str, data: NoteIn, owner: dict = Depends(current_owner)):
    request = get(request_id, owner["store_id"])
    if not request:
        raise HTTPException(404, "refund request not found")
    try:
        updated = request_more_evidence(request, owner["id"], data.note)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if data.note:
        db.execute(
            "UPDATE refund_requests SET owner_note = ? WHERE id = ? AND store_id = ?",
            (data.note[:1000], request_id, owner["store_id"]),
        )
    return _public(updated)
