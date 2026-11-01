"""Idempotency keys for financial operations.

A key is claimed **atomically before** the provider call. Repeating the same
request returns the stored response instead of moving money twice, a concurrent
duplicate is reported as busy (the caller answers 409), and a reuse with a
different payload is refused as a conflict.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from . import db

CLAIMED = "claimed"
REPLAY = "replay"
BUSY = "busy"
CONFLICT = "conflict"

# An operation that never finished (crash, killed worker) must not block the
# order forever: after this window the key can be claimed again.
STALE_SECONDS = 300


def fingerprint(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def get(store_id: str, scope: str, key: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM idempotency_keys WHERE store_id = ? AND scope = ? AND key = ?",
        (store_id, scope, key),
    )


def claim(
    store_id: str, scope: str, key: str, request_hash: str | None = None
) -> tuple[str, dict | None]:
    stamp = db.now()
    cursor = db.execute(
        "INSERT INTO idempotency_keys (store_id, scope, key, request_hash, status, response, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'in_progress', NULL, ?, ?) "
        "ON CONFLICT(store_id, scope, key) DO NOTHING",
        (store_id, scope, key, request_hash, stamp, stamp),
    )
    if cursor.rowcount == 1:
        return CLAIMED, None

    row = get(store_id, scope, key)
    if row is None:  # removed in between: treat as a fresh attempt
        return CLAIMED, None
    if request_hash and row["request_hash"] and row["request_hash"] != request_hash:
        return CONFLICT, None
    if row["status"] == "completed":
        return REPLAY, json.loads(row["response"]) if row["response"] else None

    # Still in progress: reclaim it only if the previous attempt went stale.
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS)
    ).isoformat(timespec="seconds")
    reclaimed = db.execute(
        "UPDATE idempotency_keys SET updated_at = ? WHERE store_id = ? AND scope = ? AND key = ? "
        "AND status = 'in_progress' AND updated_at < ?",
        (stamp, store_id, scope, key, cutoff),
    )
    if reclaimed.rowcount == 1:
        return CLAIMED, None
    return BUSY, None


def complete(store_id: str, scope: str, key: str, response: dict) -> None:
    db.execute(
        "UPDATE idempotency_keys SET status = 'completed', response = ?, updated_at = ? "
        "WHERE store_id = ? AND scope = ? AND key = ?",
        (json.dumps(response, default=str), db.now(), store_id, scope, key),
    )


def fail(store_id: str, scope: str, key: str) -> None:
    """Forget the key so a retry can try again after a failed operation."""
    db.execute(
        "DELETE FROM idempotency_keys WHERE store_id = ? AND scope = ? AND key = ?",
        (store_id, scope, key),
    )
