"""Stores: the unit of isolation. Every query and tool call is scoped by store_id."""

import json
import os

from . import config, db

STORE_FIELDS = {"name", "currency", "catalog_file", "max_order_usd", "max_auto_refund_usd"}

# catalog cache: path -> (mtime, catalog)
_catalog_cache: dict[str, tuple[float, dict]] = {}


def ensure_demo_store() -> dict:
    return get_store(config.DEFAULT_STORE_ID) or create_store(config.STORE_NAME)


def get_store(store_id: str) -> dict | None:
    if not store_id:
        return None
    return db.query_one("SELECT * FROM stores WHERE id = ?", (store_id,))


def create_store(name: str, catalog_file: str | None = None) -> dict:
    store_id = db.new_id("store_")
    stamp = db.now()
    db.execute(
        "INSERT INTO stores (id, name, currency, catalog_file, max_order_usd, max_auto_refund_usd, "
        "created_at, updated_at) VALUES (?, ?, 'USD', ?, ?, ?, ?, ?)",
        (
            store_id,
            (name or "New Store").strip()[:80],
            catalog_file or config.PRODUCTS_FILE,
            config.MAX_ORDER_USD,
            config.MAX_AUTO_REFUND_USD,
            stamp,
            stamp,
        ),
    )
    return get_store(store_id)


def update_store(store_id: str, **fields) -> dict | None:
    """Update only known columns; the owner can change their own store settings."""
    updates = {k: v for k, v in fields.items() if k in STORE_FIELDS}
    if not updates:
        return get_store(store_id)
    assignments = ", ".join(f"{key} = ?" for key in updates)
    db.execute(
        f"UPDATE stores SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), db.now(), store_id),
    )
    return get_store(store_id)


def public_store(store: dict | None) -> dict | None:
    if not store:
        return None
    return {
        "id": store["id"],
        "name": store["name"],
        "currency": store["currency"],
        "max_order_usd": store["max_order_usd"],
        "max_auto_refund_usd": store["max_auto_refund_usd"],
    }


# ---------------------------------------------------------------- catalog
def catalog_for(store_id: str) -> dict:
    """Load the trusted catalog. Prices always come from here, never from the model."""
    store = get_store(store_id)
    path = (store or {}).get("catalog_file") or config.PRODUCTS_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    cached = _catalog_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        catalog = {
            str(p["id"]): {
                "name": p["name"],
                "price": float(p["price"]),
                "tags": p.get("tags", ""),
            }
            for p in items
        }
    except Exception as e:  # a broken catalog must never be silently empty
        raise RuntimeError(f"Could not load catalog {path}: {e}") from e
    _catalog_cache[path] = (mtime, catalog)
    return catalog
