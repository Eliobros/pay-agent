"""Persistent conversation history, keyed by store and visitor.

The in-memory chat list is gone: every turn is written to SQLite, so a restart
(or a second worker) does not lose context. Rows are always filtered by
``store_id``, so the same visitor id in two stores is two conversations.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from . import config, db
from .auth import current_owner

router = APIRouter(prefix="/owner/conversations", tags=["conversations"])

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def turn_lock(conversation_id: str) -> threading.Lock:
    """Serialise the turns of one conversation without blocking other visitors."""
    with _locks_guard:
        lock = _locks.get(conversation_id)
        if lock is None:
            lock = _locks[conversation_id] = threading.Lock()
        return lock


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=config.CONVERSATION_TTL_HOURS)).isoformat(
        timespec="seconds"
    )


def get(store_id: str, customer_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM conversations WHERE store_id = ? AND customer_id = ?",
        (store_id, customer_id),
    )


def open_conversation(store_id: str, customer_id: str) -> dict:
    """The visitor's conversation, created, refreshed or restarted if expired."""
    existing = get(store_id, customer_id)
    stamp = db.now()
    expires = _expiry()

    if existing:
        if existing["expires_at"] <= stamp:
            # Expired: the assistant starts a fresh conversation instead of
            # resuming stale context.
            db.execute("DELETE FROM messages WHERE conversation_id = ?", (existing["id"],))
            db.execute(
                "UPDATE conversations SET created_at = ?, updated_at = ?, expires_at = ? WHERE id = ?",
                (stamp, stamp, expires, existing["id"]),
            )
        else:
            db.execute(
                "UPDATE conversations SET updated_at = ?, expires_at = ? WHERE id = ?",
                (stamp, expires, existing["id"]),
            )
        return get(store_id, customer_id)  # type: ignore[return-value]

    conversation_id = db.new_id("cv_")
    db.execute(
        "INSERT INTO conversations (id, store_id, customer_id, created_at, updated_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (conversation_id, store_id, customer_id, stamp, stamp, expires),
    )
    return get(store_id, customer_id)  # type: ignore[return-value]


def append(
    conversation_id: str,
    store_id: str,
    role: str,
    content: str = "",
    *,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
    order_id: str | None = None,
) -> dict:
    cursor = db.execute(
        "INSERT INTO messages (conversation_id, store_id, role, content, tool_calls, tool_call_id, "
        "order_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conversation_id,
            store_id,
            role,
            content or "",
            json.dumps(tool_calls) if tool_calls else None,
            tool_call_id,
            order_id,
            db.now(),
        ),
    )
    return db.query_one("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,))  # type: ignore[return-value]


def messages_of(conversation_id: str, store_id: str) -> list[dict]:
    """Raw rows, oldest first, for audit and tests."""
    return db.query(
        "SELECT * FROM messages WHERE conversation_id = ? AND store_id = ? ORDER BY id",
        (conversation_id, store_id),
    )


def load(conversation_id: str, store_id: str, limit: int | None = None) -> list[dict]:
    """The last messages in the format the model API expects."""
    limit = max(limit or config.MAX_HISTORY, 2)
    rows = db.query(
        "SELECT * FROM messages WHERE conversation_id = ? AND store_id = ? ORDER BY id DESC LIMIT ?",
        (conversation_id, store_id, limit),
    )
    rows.reverse()

    # Never start with a tool result: it would reference an assistant message
    # that was trimmed away and the model API rejects that.
    while rows and rows[0]["role"] != "user":
        rows.pop(0)

    history = []
    for row in rows:
        entry = {"role": row["role"], "content": row["content"] or ""}
        if row["tool_calls"]:
            entry["tool_calls"] = json.loads(row["tool_calls"])
        if row["tool_call_id"]:
            entry["tool_call_id"] = row["tool_call_id"]
        history.append(entry)
    return history


def trim(conversation_id: str, store_id: str, limit: int | None = None) -> int:
    """Keep at most ``MAX_HISTORY`` messages, cutting at a user turn."""
    limit = max(limit or config.MAX_HISTORY, 2)
    rows = db.query(
        "SELECT id, role FROM messages WHERE conversation_id = ? AND store_id = ? ORDER BY id",
        (conversation_id, store_id),
    )
    if len(rows) <= limit:
        return 0

    start = None
    for index in range(max(len(rows) - limit, 0), len(rows)):
        if rows[index]["role"] == "user":
            start = index
            break
    if not start:  # nothing older to drop, or no safe cut point
        return 0

    cursor = db.execute(
        "DELETE FROM messages WHERE conversation_id = ? AND store_id = ? AND id < ?",
        (conversation_id, store_id, rows[start]["id"]),
    )
    return cursor.rowcount


def purge_expired(store_id: str | None = None) -> int:
    """Delete conversations past their expiry, with their messages."""
    sql = "SELECT id FROM conversations WHERE expires_at < ?"
    params: tuple = (db.now(),)
    if store_id:
        sql += " AND store_id = ?"
        params += (store_id,)

    rows = db.query(sql, params)
    for row in rows:
        db.execute("DELETE FROM messages WHERE conversation_id = ?", (row["id"],))
        db.execute("DELETE FROM conversations WHERE id = ?", (row["id"],))
    return len(rows)


@router.post("/purge")
def purge(owner: dict = Depends(current_owner)):
    return {
        "deleted": purge_expired(owner["store_id"]),
        "ttl_hours": config.CONVERSATION_TTL_HOURS,
        "max_history": config.MAX_HISTORY,
    }
