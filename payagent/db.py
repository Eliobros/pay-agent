"""SQLite access layer: schema, migrations and small query helpers.

Every table that holds business data carries a ``store_id`` so that a single
installation can host several independent stores. Nothing here knows about
authentication or PayPal: it only stores rows.
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_lock = threading.RLock()
_conn = None

SCHEMA_VERSION = 1

# Columns added after the first release. Each one is applied only when missing, so
# an existing payagent.db keeps its data.
COLUMN_MIGRATIONS = [
    ("stores", "payment_environment", "TEXT NOT NULL DEFAULT 'sandbox'"),
    ("payment_providers", "client_secret_last4", "TEXT"),
    ("payment_providers", "validation_error", "TEXT"),
    ("refund_evidence", "kind", "TEXT NOT NULL DEFAULT 'image'"),
    ("refund_evidence", "duration_seconds", "REAL"),
    ("messages", "order_id", "TEXT"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    catalog_file TEXT,
    max_order_usd REAL NOT NULL DEFAULT 200,
    max_auto_refund_usd REAL NOT NULL DEFAULT 100,
    payment_environment TEXT NOT NULL DEFAULT 'sandbox',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owners (
    id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS sessions_owner_idx ON sessions(owner_id);

CREATE TABLE IF NOT EXISTS payment_providers (
    id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    environment TEXT NOT NULL,
    client_id TEXT NOT NULL DEFAULT '',
    client_secret_enc TEXT NOT NULL DEFAULT '',
    client_secret_last4 TEXT,
    status TEXT NOT NULL DEFAULT 'unverified',
    validation_error TEXT,
    validated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (store_id, provider, environment)
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    customer_id TEXT NOT NULL,
    product_id TEXT,
    item TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price REAL,
    total REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL,
    paypal_order_id TEXT,
    capture_id TEXT,
    refunded_total REAL NOT NULL DEFAULT 0,
    approval_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_store_idx ON orders(store_id, created_at);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders(store_id, customer_id);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    type TEXT NOT NULL,
    data TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS order_events_order_idx ON order_events(order_id);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    total REAL NOT NULL DEFAULT 0,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    checkout TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
);
CREATE INDEX IF NOT EXISTS approvals_pending_idx ON approvals(store_id, status, created_at);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    customer_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (store_id, customer_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    store_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,
    tool_call_id TEXT,
    order_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS refund_requests (
    id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    order_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    owner_note TEXT,
    paypal_refund_id TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
);
CREATE INDEX IF NOT EXISTS refund_requests_idx ON refund_requests(store_id, status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS refund_requests_one_active
    ON refund_requests(order_id) WHERE status NOT IN ('REJECTED', 'FAILED', 'REFUNDED');

CREATE TABLE IF NOT EXISTS refund_evidence (
    id TEXT PRIMARY KEY,
    refund_request_id TEXT NOT NULL REFERENCES refund_requests(id) ON DELETE CASCADE,
    store_id TEXT NOT NULL,
    original_name TEXT,
    kind TEXT NOT NULL DEFAULT 'image',
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    duration_seconds REAL,
    sha256 TEXT,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refund_evidence_request_idx ON refund_evidence(refund_request_id);

CREATE TABLE IF NOT EXISTS refund_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    refund_request_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refund_events_request_idx ON refund_events(refund_request_id);

CREATE TABLE IF NOT EXISTS webhook_events (
    id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'paypal',
    event_type TEXT NOT NULL DEFAULT '',
    payload TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'received',
    created_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    store_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT,
    status TEXT NOT NULL DEFAULT 'in_progress',
    response TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (store_id, scope, key)
);
"""


def now() -> str:
    """Timestamp string used for every ``*_at`` column (UTC, sortable)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connection() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect(config.DB_PATH)
        return _conn


def use_database(path: str) -> None:
    """Point the layer at another SQLite file (used by the tests)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = _connect(path)
    init_db()


# ---------------------------------------------------------------- migrations
def _rename_json_tables(conn: sqlite3.Connection) -> None:
    """The first version of PayAgent stored orders/approvals as id+data JSON.

    Those two tables are renamed before the new schema is created so the rows can
    be imported instead of being lost or shadowed.
    """
    for table in ("orders", "approvals"):
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not found:
            continue
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if columns == ["id", "data"]:
            conn.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")


def _seed_demo_store(conn: sqlite3.Connection) -> None:
    stamp = now()
    conn.execute(
        "INSERT OR IGNORE INTO stores "
        "(id, name, currency, catalog_file, max_order_usd, max_auto_refund_usd, created_at, updated_at) "
        "VALUES (?, ?, 'USD', ?, ?, ?, ?, ?)",
        (
            config.DEFAULT_STORE_ID,
            config.STORE_NAME,
            config.PRODUCTS_FILE,
            config.MAX_ORDER_USD,
            config.MAX_AUTO_REFUND_USD,
            stamp,
            stamp,
        ),
    )


def _import_legacy_rows(conn: sqlite3.Connection) -> None:
    stamp = now()
    store_id = config.DEFAULT_STORE_ID

    try:
        legacy_orders = conn.execute("SELECT id, data FROM legacy_orders").fetchall()
    except sqlite3.OperationalError:
        legacy_orders = []
    for row in legacy_orders:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO orders (id, store_id, customer_id, item, quantity, total, currency, "
            "status, capture_id, refunded_total, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, 'USD', ?, ?, ?, ?, ?)",
            (
                row["id"],
                store_id,
                data.get("user_id") or "guest",
                data.get("item") or "",
                float(data.get("total") or 0),
                "REFUNDED" if data.get("refunded") else (data.get("status") or "CREATED"),
                data.get("capture_id"),
                float(data.get("total") or 0) if data.get("refunded") else 0.0,
                stamp,
                stamp,
            ),
        )

    try:
        legacy_approvals = conn.execute("SELECT id, data FROM legacy_approvals").fetchall()
    except sqlite3.OperationalError:
        legacy_approvals = []
    for row in legacy_approvals:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO approvals (id, store_id, kind, customer_id, summary, total, payload, "
            "status, checkout, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                store_id,
                data.get("kind") or "purchase",
                data.get("user_id") or "guest",
                data.get("summary") or "",
                float(data.get("total_usd") or 0),
                json.dumps(data.get("payload") or {}),
                data.get("status") or "pending",
                json.dumps(data["checkout"]) if data.get("checkout") else None,
                stamp,
            ),
        )


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, ddl in COLUMN_MIGRATIONS:
        existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    """Create the schema and migrate data left by older versions. Idempotent."""
    conn = connection()
    with _lock:
        _rename_json_tables(conn)
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        _seed_demo_store(conn)
        _import_legacy_rows(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now()),
        )
        conn.commit()


# ---------------------------------------------------------------- helpers
def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        rows = connection().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cursor = connection().execute(sql, params)
        connection().commit()
        return cursor


@contextmanager
def tx():
    """Run several statements atomically."""
    conn = connection()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
