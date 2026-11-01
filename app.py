import json
import os
import re
import sqlite3
import threading
import time
import uuid

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel

load_dotenv()

# ---------------------------------------------------------------- config
MODEL_API_KEY = os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY")
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "https://api.meta.ai/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "muse-spark-1.3")

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_BASE = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "demo")  # token for the owner panel
MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", "200"))  # above: human approves
MAX_AUTO_REFUND_USD = float(os.getenv("MAX_AUTO_REFUND_USD", "100"))  # above: human approves

STORE_NAME = os.getenv("STORE_NAME", "Demo Store")
PRODUCTS_FILE = os.getenv("PRODUCTS_FILE", "products.json")  # the merchant edits this file
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))  # messages per visitor per minute
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "30"))  # messages kept per visitor
DB_PATH = os.getenv("DB_PATH", "payagent.db")  # SQLite file, created automatically

app = FastAPI(title="PayAgent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(base_url=MODEL_BASE_URL, api_key=MODEL_API_KEY or "missing_key")

# ---------------------------------------------------------------- persistence
# Orders and approvals live in a single SQLite file, so a restart loses nothing.
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db_lock = threading.Lock()
for _t in ("orders", "approvals"):
    _db.execute(f"CREATE TABLE IF NOT EXISTS {_t} (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
_db.commit()


class Row(dict):
    """A dict that writes itself back to the database whenever a field changes."""

    def __init__(self, data, save):
        super().__init__(data)
        self._save = save

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._save(dict(self))


class Table:
    def __init__(self, name: str):
        self.name = name

    def _save(self, key: str, data: dict):
        with _db_lock:
            _db.execute(
                f"INSERT INTO {self.name} (id, data) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (key, json.dumps(data)),
            )
            _db.commit()

    def _row(self, key: str, raw: str) -> Row:
        return Row(json.loads(raw), lambda d, k=key: self._save(k, d))

    def __setitem__(self, key: str, data: dict):
        self._save(key, data)

    def get(self, key: str):
        with _db_lock:
            r = _db.execute(f"SELECT data FROM {self.name} WHERE id = ?", (key,)).fetchone()
        return self._row(key, r[0]) if r else None

    def __getitem__(self, key: str) -> Row:
        row = self.get(key)
        if row is None:
            raise KeyError(key)
        return row

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def items(self):
        with _db_lock:
            rows = _db.execute(f"SELECT id, data FROM {self.name} ORDER BY rowid").fetchall()
        return [(k, self._row(k, d)) for k, d in rows]

    def values(self):
        return [row for _, row in self.items()]


# ---------------------------------------------------------------- catalog
# Prices live on the server: the model only picks a product_id and a quantity.
def load_catalog() -> dict:
    try:
        with open(PRODUCTS_FILE, encoding="utf-8") as f:
            items = json.load(f)
        return {
            str(p["id"]): {"name": p["name"], "price": float(p["price"]), "tags": p.get("tags", "")}
            for p in items
        }
    except Exception as e:
        raise SystemExit(f"Could not load {PRODUCTS_FILE}: {e}")


CATALOG = load_catalog()

ORDERS = Table("orders")          # order_id -> order data
APPROVALS = Table("approvals")  # approval_id -> request waiting for a human


# ---------------------------------------------------------------- PayPal
def paypal_token() -> str:
    r = httpx.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID or "", PAYPAL_CLIENT_SECRET or ""),
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def paypal_create_order(user_id: str, product_id: str, quantity: int) -> dict:
    product = CATALOG[product_id]
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
                        "unit_amount": {"currency_code": "USD", "value": f"{product['price']:.2f}"},
                    }
                ],
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "user_action": "PAY_NOW",
                    "return_url": f"{FRONTEND_ORIGIN}/success",
                    "cancel_url": f"{FRONTEND_ORIGIN}/cancel",
                }
            }
        },
    }
    r = httpx.post(
        f"{PAYPAL_BASE}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {paypal_token()}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(uuid.uuid4()),
        },
        json=body,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    link = next(
        (l["href"] for l in data.get("links", []) if l["rel"] in ("payer-action", "approve")),
        None,
    )
    ORDERS[data["id"]] = {
        "user_id": user_id,
        "item": f"{quantity} x {product['name']}",
        "total": total,
        "status": data["status"],
        "capture_id": None,
        "refunded": False,
    }
    return {"order_id": data["id"], "total_usd": total, "approval_url": link}


def paypal_get_order(order_id: str) -> dict:
    r = httpx.get(
        f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}",
        headers={"Authorization": f"Bearer {paypal_token()}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def paypal_capture_order(order_id: str) -> dict:
    r = httpx.post(
        f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {paypal_token()}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(uuid.uuid4()),
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def paypal_refund(order_id: str) -> dict:
    """Full refund of a captured order."""
    if ORDERS[order_id]["refunded"]:
        raise ValueError("order already refunded")
    capture_id = ORDERS[order_id]["capture_id"]
    r = httpx.post(
        f"{PAYPAL_BASE}/v2/payments/captures/{capture_id}/refund",
        headers={
            "Authorization": f"Bearer {paypal_token()}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(uuid.uuid4()),
        },
        json={},
        timeout=20,
    )
    r.raise_for_status()
    ORDERS[order_id]["refunded"] = True
    ORDERS[order_id]["status"] = "REFUNDED"
    return r.json()


# ---------------------------------------------------------------- approvals
def new_approval(kind: str, user_id: str, summary: str, total: float, payload: dict) -> str:
    aid = uuid.uuid4().hex[:8]
    APPROVALS[aid] = {
        "id": aid, "kind": kind, "user_id": user_id, "summary": summary,
        "total_usd": total, "payload": payload, "status": "pending", "checkout": None,
    }
    return aid


def require_admin(token: str):
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "invalid owner token")


# ---------------------------------------------------------------- tools
def _fn(name, desc, props, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


TOOLS = [
    _fn("search_products", "Search the store catalog. Returns product ids, names and prices.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("create_checkout",
        "Create a PayPal checkout for a catalog product. Only call after the user clearly confirmed.",
        {"product_id": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1, "maximum": 10}},
        ["product_id", "quantity"]),
    _fn("check_order_status", "Check the PayPal status of an order.",
        {"order_id": {"type": "string"}}, ["order_id"]),
    _fn("list_my_orders", "List the orders of the current user, with ids and status.", {}, []),
    _fn("refund_order",
        "Refund a paid order of the current user. Ask the reason first. Use ids from list_my_orders.",
        {"order_id": {"type": "string"}, "reason": {"type": "string"}}, ["order_id", "reason"]),
]

WAIT_NOTE = (
    "A store owner must approve this request. Tell the user it is waiting for approval "
    "and do not retry."
)


def run_tool(name: str, args: dict, user_id: str) -> dict:
    try:
        if name == "search_products":
            q = str(args.get("query", "")).lower().split()
            hits = [
                {"product_id": pid, "name": p["name"], "price_usd": p["price"]}
                for pid, p in CATALOG.items()
                if not q or any(w in (p["name"] + " " + p["tags"]).lower() for w in q)
            ]
            return {"results": hits or "no products found"}

        if name == "create_checkout":
            pid = args.get("product_id")
            qty = int(args.get("quantity", 1))
            if pid not in CATALOG or not 1 <= qty <= 10:
                return {"error": "invalid product_id or quantity"}
            total = round(CATALOG[pid]["price"] * qty, 2)
            if total > MAX_ORDER_USD:
                aid = new_approval("purchase", user_id, f"{qty} x {CATALOG[pid]['name']}", total,
                                   {"product_id": pid, "quantity": qty})
                return {"status": "awaiting_human_approval", "approval_id": aid,
                        "kind": "purchase", "total_usd": total, "note": WAIT_NOTE}
            return paypal_create_order(user_id, pid, qty)

        if name == "check_order_status":
            data = paypal_get_order(args["order_id"])
            return {"order_id": data["id"], "status": data["status"]}

        if name == "list_my_orders":
            mine = [
                {"order_id": oid, "item": o["item"], "total_usd": o["total"], "status": o["status"]}
                for oid, o in ORDERS.items() if o["user_id"] == user_id
            ]
            return {"orders": mine or "no orders yet"}

        if name == "refund_order":
            oid = args.get("order_id")
            order = ORDERS.get(oid)
            if not order or order["user_id"] != user_id:
                return {"error": "order not found for this user"}
            if order["status"] != "COMPLETED" or order["refunded"]:
                return {"error": f"order is {order['status']} and cannot be refunded"}
            if order["total"] > MAX_AUTO_REFUND_USD:
                aid = new_approval("refund", user_id, f"Refund {order['item']}: {args.get('reason', '')}",
                                   order["total"], {"order_id": oid})
                return {"status": "awaiting_human_approval", "approval_id": aid,
                        "kind": "refund", "total_usd": order["total"], "note": WAIT_NOTE}
            paypal_refund(oid)
            return {"status": "refunded", "order_id": oid, "total_usd": order["total"]}

        return {"error": f"unknown tool {name}"}
    except httpx.HTTPStatusError as e:
        print("PAYPAL ERROR:", e.response.status_code, e.response.text)
        return {"error": f"PayPal error {e.response.status_code}"}
    except Exception as e:
        print("TOOL ERROR:", repr(e))
        return {"error": str(e)}


# ---------------------------------------------------------------- agent
SYSTEM_PROMPT = (
    f"You are PayAgent, the shopping assistant of {STORE_NAME}, with PayPal checkout and refunds. "
    "Find products with search_products, confirm product and quantity, then call create_checkout. "
    "Never state a price that did not come from a tool result. After create_checkout succeeds, "
    "tell the user to press the PayPal button. For refunds, ask the reason, look up the order with "
    "list_my_orders, then call refund_order. If a tool returns awaiting_human_approval, explain that "
    "a store owner has to approve it and that the page will update by itself. "
    "Reply in the user's language, short and friendly."
)

chat_sessions: dict[str, list] = {}
_hits: dict[str, list] = {}


def clean_user_id(raw: str) -> str:
    uid = re.sub(r"[^a-zA-Z0-9_-]", "", raw or "")[:64]
    return uid or "guest"


def rate_limited(key: str) -> bool:
    now = time.time()
    recent = [t for t in _hits.get(key, []) if now - t < 60]
    recent.append(now)
    _hits[key] = recent
    return len(recent) > RATE_LIMIT_PER_MIN


def trim_history(history: list) -> None:
    """Keep the system prompt plus the most recent turns, cutting at a user message."""
    if len(history) <= MAX_HISTORY:
        return
    for i in range(1, len(history)):
        if history[i]["role"] == "user" and len(history) - i <= MAX_HISTORY:
            del history[1:i]
            return


class ChatIn(BaseModel):
    message: str
    user_id: str = "hackathon_user"


@app.post("/chat")
def chat(data: ChatIn, request: Request):
    if not MODEL_API_KEY:
        raise HTTPException(500, "MODEL_API_KEY is not set in .env")

    data.user_id = clean_user_id(data.user_id)
    data.message = data.message.strip()[:1000]
    if not data.message:
        raise HTTPException(400, "Empty message")
    ip = request.client.host if request.client else "unknown"
    if rate_limited(f"{ip}:{data.user_id}"):
        raise HTTPException(429, "Too many messages. Please wait a minute and try again.")

    history = chat_sessions.setdefault(data.user_id, [{"role": "system", "content": SYSTEM_PROMPT}])
    trim_history(history)
    history.append({"role": "user", "content": data.message})

    checkout, approval, final_text = None, None, ""

    try:
        for _ in range(5):
            resp = client.chat.completions.create(
                model=MODEL_NAME, messages=history, tools=TOOLS, tool_choice="auto", temperature=0.4
            )
            msg = resp.choices[0].message
            entry = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
            history.append(entry)

            if not msg.tool_calls:
                final_text = msg.content or ""
                break

            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = run_tool(tc.function.name, args, data.user_id)
                if result.get("approval_url"):
                    checkout = result
                if result.get("approval_id"):
                    approval = {k: result[k] for k in ("approval_id", "kind", "total_usd")}
                history.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
    except Exception as e:
        raise HTTPException(502, f"Model API error: {e}")

    return {
        "response": final_text,
        "action": "init_paypal_checkout" if checkout else "none",
        "checkout": checkout,
        "approval": approval,
    }


# ---------------------------------------------------------------- payments
@app.post("/paypal/capture/{order_id}")
def capture(order_id: str):
    """Called by the frontend after the buyer approved the payment on PayPal."""
    if order_id not in ORDERS:
        raise HTTPException(404, "unknown order")
    if ORDERS[order_id]["status"] in ("COMPLETED", "REFUNDED"):
        return {"order_id": order_id, "status": ORDERS[order_id]["status"]}
    try:
        data = paypal_capture_order(order_id)
    except httpx.HTTPStatusError as e:
        if "ORDER_ALREADY_CAPTURED" in e.response.text:
            ORDERS[order_id]["status"] = "COMPLETED"
            return {"order_id": order_id, "status": "COMPLETED"}
        print("PAYPAL CAPTURE ERROR:", e.response.status_code, e.response.text)
        raise HTTPException(502, "PayPal capture failed")
    try:
        ORDERS[order_id]["capture_id"] = data["purchase_units"][0]["payments"]["captures"][0]["id"]
    except (KeyError, IndexError):
        pass
    ORDERS[order_id]["status"] = data.get("status", "UNKNOWN")
    return {"order_id": order_id, "status": ORDERS[order_id]["status"]}


# ---------------------------------------------------------------- human approval
@app.get("/approvals/{approval_id}")
def approval_status(approval_id: str):
    a = APPROVALS.get(approval_id)
    if not a:
        raise HTTPException(404, "unknown approval")
    return {"status": a["status"], "kind": a["kind"], "checkout": a["checkout"]}


@app.get("/owner/approvals")
def owner_list(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return [
        {k: a[k] for k in ("id", "kind", "summary", "total_usd", "user_id")}
        for a in APPROVALS.values() if a["status"] == "pending"
    ]


class Decision(BaseModel):
    approve: bool


@app.post("/owner/approvals/{approval_id}")
def owner_decide(approval_id: str, d: Decision, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    a = APPROVALS.get(approval_id)
    if not a or a["status"] != "pending":
        raise HTTPException(404, "no pending approval with this id")
    if not d.approve:
        a["status"] = "rejected"
        return {"status": "rejected"}
    try:
        if a["kind"] == "purchase":
            p = a["payload"]
            a["checkout"] = paypal_create_order(a["user_id"], p["product_id"], p["quantity"])
        else:
            paypal_refund(a["payload"]["order_id"])
    except httpx.HTTPStatusError as e:
        print("PAYPAL ERROR:", e.response.status_code, e.response.text)
        raise HTTPException(502, "PayPal request failed")
    a["status"] = "approved"
    return {"status": "approved"}


@app.get("/owner/orders")
def owner_orders(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    rows = [
        {"order_id": oid, "item": o["item"], "total_usd": o["total"], "status": o["status"]}
        for oid, o in ORDERS.items()
    ]
    return rows[::-1][:20]


@app.get("/config")
def config():
    return {
        "store_name": STORE_NAME,
        "max_order_usd": MAX_ORDER_USD,
        "max_auto_refund_usd": MAX_AUTO_REFUND_USD,
    }


@app.get("/products")
def products():
    return [
        {"product_id": k, "name": v["name"], "price_usd": v["price"]} for k, v in CATALOG.items()
    ]


@app.get("/health")
def health():
    return {"ok": True}
