"""PayAgent backend: AI sales chat, PayPal checkout and the owner panel.

The model talks, the backend decides. Prices come from the trusted catalog,
amounts and limits are computed here, and every money-moving call is idempotent:
PayPal is the only source of truth for a payment status.
"""

import json
import re

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel

from payagent import approvals as approvals_store
from payagent import config, conversations, db, orders as orders_store, paypal, refunds, stores
from payagent.auth import current_owner
from payagent.auth import router as auth_router
from payagent.conversations import router as conversations_router
from payagent.dashboard import router as dashboard_router
from payagent.providers import router as providers_router
from payagent.refunds import router as refunds_router
from payagent.security import RateLimiter
from payagent.webhooks import router as webhooks_router

client = OpenAI(base_url=config.MODEL_BASE_URL, api_key=config.MODEL_API_KEY or "missing_key")

app = FastAPI(title="PayAgent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_db()
app.include_router(auth_router)
app.include_router(providers_router)
app.include_router(webhooks_router)
app.include_router(refunds_router)
app.include_router(conversations_router)
app.include_router(dashboard_router)
# Conversations past their TTL are removed when the process starts.
conversations.purge_expired()

chat_limiter = RateLimiter()


# ---------------------------------------------------------------- AI tools
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
    _fn("list_my_orders", "List the orders of the current customer, with ids and status.", {}, []),
    _fn("refund_order",
        "Refund a paid order of the current customer. Ask the reason first and use ids from "
        "list_my_orders. Small refunds without damage are handled immediately; anything else "
        "becomes a request that the store owner reviews.",
        {"order_id": {"type": "string"}, "reason": {"type": "string"},
         "damage": {"type": "boolean",
                    "description": "true when the item arrived damaged, broken or not as described"}},
        ["order_id", "reason"]),
    _fn("open_refund_request",
        "Open a refund request that needs evidence (photos or a short video of the problem) and "
        "the store owner's decision. Use it when the item is damaged and the customer must show proof. "
        "Call it before asking for evidence: the chat upload box only appears after this tool returns.",
        {"order_id": {"type": "string"}, "reason": {"type": "string"}},
        ["order_id", "reason"]),
]

WAIT_NOTE = (
    "A store owner must approve this request. Tell the user it is waiting for approval "
    "and do not retry."
)

EVIDENCE_NOTE = (
    "Ask the customer to attach photos or a short video (at most 60 seconds) of the problem "
    "using the upload box in the chat. Then say that the store owner will review it. "
    "Never decide or promise the refund yourself."
)


def run_tool(name: str, args: dict, customer_id: str, store_id: str, request_id: str = "") -> dict:
    try:
        store = stores.get_store(store_id)
        if not store:
            return {"error": "unknown store"}

        if name == "search_products":
            q = str(args.get("query", "")).lower().split()
            hits = [
                {"product_id": pid, "name": p["name"], "price_usd": p["price"]}
                for pid, p in stores.catalog_for(store_id).items()
                if not q or any(w in (p["name"] + " " + p["tags"]).lower() for w in q)
            ]
            return {"results": hits or "no products found"}

        if name == "create_checkout":
            catalog = stores.catalog_for(store_id)
            pid = str(args.get("product_id", ""))
            qty = int(args.get("quantity", 1))
            if pid not in catalog or not 1 <= qty <= 10:
                return {"error": "invalid product_id or quantity"}
            # The price always comes from the catalog, never from the model.
            total = round(catalog[pid]["price"] * qty, 2)
            if total > float(store["max_order_usd"]):
                a = approvals_store.create(
                    store_id=store_id,
                    kind="purchase",
                    customer_id=customer_id,
                    summary=f"{qty} x {catalog[pid]['name']}",
                    total=total,
                    payload={"product_id": pid, "quantity": qty},
                )
                return {"status": "awaiting_human_approval", "approval_id": a["id"],
                        "kind": "purchase", "total_usd": total, "note": WAIT_NOTE}
            idempotency_key = f"{request_id}:{pid}:{qty}" if request_id else None
            return paypal.create_order(
                store_id, customer_id, pid, qty, request_id=idempotency_key
            )

        if name == "check_order_status":
            order = orders_store.get_for_store(str(args.get("order_id")), store_id)
            if not order:
                return {"error": "order not found"}
            if order["customer_id"] != customer_id:
                return {"error": "order not found for this customer"}
            data = paypal.get_order(store_id, order["id"])
            return {"order_id": data["id"], "status": data["status"]}

        if name == "list_my_orders":
            mine = [
                {"order_id": o["id"], "item": o["item"], "total_usd": o["total"], "status": o["status"]}
                for o in orders_store.list_for_customer(store_id, customer_id)
            ]
            return {"orders": mine or "no orders yet"}

        if name in ("refund_order", "open_refund_request"):
            order = orders_store.get_for_store(str(args.get("order_id")), store_id)
            if not order or order["customer_id"] != customer_id:
                return {"error": "order not found for this customer"}
            if order["status"] != "COMPLETED" or float(order["refunded_total"] or 0) > 0:
                return {"error": f"order is {order['status']} and cannot be refunded"}

            remaining = round(float(order["total"]) - float(order["refunded_total"] or 0), 2)
            # Damage claims always need evidence and a human decision, and so does
            # anything above the store's automatic refund limit. The model cannot
            # override either rule with the arguments it sends.
            needs_human = (
                name == "open_refund_request"
                or bool(args.get("damage"))
                or remaining > float(store["max_auto_refund_usd"])
            )
            if needs_human:
                try:
                    request = refunds.create(
                        store_id, customer_id, order["id"], str(args.get("reason", ""))
                    )
                except ValueError as e:
                    # The customer asked about the same order twice (or reloaded the
                    # chat): hand back the already-open request so the upload box
                    # renders instead of leaving the customer with nowhere to attach
                    # the evidence the assistant just asked for.
                    request = refunds.get_open_for_order(store_id, order["id"], customer_id)
                    if request is None:
                        return {"error": str(e)}
                return {
                    "status": "awaiting_evidence",
                    "refund_request_id": request["id"],
                    "request_status": request["status"],
                    "amount_usd": round(float(request["amount"]), 2),
                    "note": EVIDENCE_NOTE,
                }
            result = paypal.refund(order)
            return {"status": "refunded", "order_id": order["id"], "total_usd": result["amount_usd"]}

        return {"error": f"unknown tool {name}"}
    except paypal.ConcurrentOperation as e:
        return {"error": str(e), "status": "in_progress", "note": "Ask the user to wait a moment."}
    except paypal.NotConfigured as e:
        return {"error": str(e)}
    except httpx.HTTPStatusError as e:
        print("PAYPAL ERROR:", e.response.status_code, e.response.text)
        return {"error": f"PayPal error {e.response.status_code}"}
    except Exception as e:
        print("TOOL ERROR:", repr(e))
        return {"error": str(e)}


# ---------------------------------------------------------------- chat
def system_prompt(store: dict) -> str:
    return (
        f"You are PayAgent, the shopping assistant of {store['name']}, with PayPal checkout and refunds. "
        "Find products with search_products, confirm product and quantity, then call create_checkout. "
        "Never state a price that did not come from a tool result. After create_checkout succeeds, "
        "tell the user to press the PayPal button. For refunds, ask the reason, look up the order with "
        "list_my_orders, then call refund_order (open_refund_request for damage). Always call the tool "
        "first: the upload box in the chat only appears after the tool returns, so never ask for photos "
        "or a video before calling it. Damage claims always need evidence and the store owner reviews "
        "it. Never promise or decide a refund yourself. If a tool returns awaiting_human_approval, "
        "explain that a store owner has to approve it and that the page will update by itself. "
        "Reply in the user's language, short and friendly."
    )


def clean_user_id(raw: str) -> str:
    uid = re.sub(r"[^a-zA-Z0-9_-]", "", raw or "")[:64]
    return uid or "guest"


def clean_store_id(raw: str) -> str:
    sid = re.sub(r"[^a-zA-Z0-9_-]", "", raw or "")[:64]
    return sid or config.DEFAULT_STORE_ID


def clean_request_id(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw or "")[:64]


class ChatIn(BaseModel):
    message: str
    user_id: str = "hackathon_user"
    store_id: str = config.DEFAULT_STORE_ID
    # Lets a retried request reuse the idempotency key instead of opening a
    # second PayPal checkout for the same message.
    request_id: str = ""


@app.post("/chat")
def chat(data: ChatIn, request: Request):
    if not config.MODEL_API_KEY:
        raise HTTPException(500, "MODEL_API_KEY is not set in .env")

    store_id = clean_store_id(data.store_id)
    store = stores.get_store(store_id)
    if not store:
        raise HTTPException(404, "unknown store")

    customer_id = clean_user_id(data.user_id)
    request_id = clean_request_id(data.request_id)
    message = data.message.strip()[:1000]
    if not message:
        raise HTTPException(400, "Empty message")
    ip = request.client.host if request.client else "unknown"
    if not chat_limiter.hit(f"{ip}:{customer_id}", config.RATE_LIMIT_PER_MIN):
        raise HTTPException(429, "Too many messages. Please wait a minute and try again.")

    conversation = conversations.open_conversation(store_id, customer_id)
    conversations.append(conversation["id"], store_id, "user", message)
    conversations.trim(conversation["id"], store_id)
    history = [{"role": "system", "content": system_prompt(store)}] + conversations.load(
        conversation["id"], store_id
    )

    checkout, approval, refund, final_text = None, None, None, ""

    try:
        for _ in range(5):
            resp = client.chat.completions.create(
                model=config.MODEL_NAME, messages=history, tools=TOOLS,
                tool_choice="auto", temperature=0.4,
            )
            msg = resp.choices[0].message
            tool_calls = None
            entry = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                tool_calls = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
                entry["tool_calls"] = tool_calls
            history.append(entry)
            conversations.append(
                conversation["id"], store_id, "assistant", msg.content or "", tool_calls=tool_calls
            )

            if not msg.tool_calls:
                final_text = msg.content or ""
                break

            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = run_tool(tc.function.name, args, customer_id, store_id, request_id)
                if result.get("approval_url"):
                    checkout = result
                if result.get("approval_id"):
                    approval = {k: result[k] for k in ("approval_id", "kind", "total_usd")}
                if result.get("refund_request_id"):
                    refund = {
                        "request_id": result["refund_request_id"],
                        "amount_usd": result.get("amount_usd"),
                        "status": result.get("request_status"),
                    }
                content = json.dumps(result)
                history.append({"role": "tool", "tool_call_id": tc.id, "content": content})
                conversations.append(
                    conversation["id"], store_id, "tool", content,
                    tool_call_id=tc.id, order_id=result.get("order_id"),
                )
    except Exception as e:
        raise HTTPException(502, f"Model API error: {e}")

    conversations.trim(conversation["id"], store_id)

    return {
        "response": final_text,
        "action": "init_paypal_checkout" if checkout else "none",
        "checkout": checkout,
        "approval": approval,
        "refund": refund,
    }


# ---------------------------------------------------------------- payments
@app.post("/paypal/capture/{order_id}")
def capture(order_id: str):
    """Confirm a payment returned from PayPal. Safe to call more than once."""
    order = orders_store.get(order_id)
    if not order:
        raise HTTPException(404, "unknown order")
    try:
        return paypal.capture_order(order["store_id"], order_id)
    except paypal.ConcurrentOperation:
        raise HTTPException(409, "This payment is already being confirmed. Try again in a moment.")
    except paypal.NotConfigured as e:
        raise HTTPException(503, str(e))
    except LookupError:
        raise HTTPException(404, "unknown order")
    except httpx.HTTPStatusError as e:
        print("PAYPAL CAPTURE ERROR:", e.response.status_code, e.response.text)
        raise HTTPException(502, "PayPal capture failed")


# ---------------------------------------------------------------- human approval
@app.get("/approvals/{approval_id}")
def approval_status(approval_id: str):
    """Public: the customer page polls this to see the owner's decision."""
    a = approvals_store.get(approval_id)
    if not a:
        raise HTTPException(404, "unknown approval")
    return {"status": a["status"], "kind": a["kind"], "checkout": a["checkout"]}


class Decision(BaseModel):
    approve: bool


@app.get("/owner/approvals")
def owner_approvals(owner: dict = Depends(current_owner)):
    return [
        {
            "id": a["id"],
            "kind": a["kind"],
            "summary": a["summary"],
            "total_usd": a["total"],
            "customer_id": a["customer_id"],
            "created_at": a["created_at"],
        }
        for a in approvals_store.list_pending(owner["store_id"])
    ]


@app.post("/owner/approvals/{approval_id}")
def owner_decide(approval_id: str, d: Decision, owner: dict = Depends(current_owner)):
    a = approvals_store.get_for_store(approval_id, owner["store_id"])
    if not a or a["status"] != "pending":
        raise HTTPException(404, "no pending approval with this id")
    if not d.approve:
        approvals_store.set_decided(approval_id, "rejected", decided_by=owner["id"])
        return {"status": "rejected"}
    try:
        if a["kind"] == "purchase":
            p = a["payload"]
            checkout = paypal.create_order(
                owner["store_id"], a["customer_id"], p["product_id"], p["quantity"],
                request_id=f"approval-{approval_id}",
            )
            approvals_store.set_decided(approval_id, "approved", decided_by=owner["id"], checkout=checkout)
        else:
            order = orders_store.get_for_store(a["payload"].get("order_id", ""), owner["store_id"])
            if not order:
                raise ValueError("order not found in this store")
            paypal.refund(order)
            approvals_store.set_decided(approval_id, "approved", decided_by=owner["id"])
    except paypal.ConcurrentOperation:
        raise HTTPException(409, "That operation is already running. Try again in a moment.")
    except paypal.NotConfigured as e:
        raise HTTPException(503, str(e))
    except httpx.HTTPStatusError as e:
        print("PAYPAL ERROR:", e.response.status_code, e.response.text)
        raise HTTPException(502, "PayPal request failed")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"status": "approved"}


@app.get("/owner/orders")
def owner_orders(
    status: str | None = None,
    limit: int = 50,
    owner: dict = Depends(current_owner),
):
    rows = orders_store.list_for_store(
        owner["store_id"],
        limit=min(max(limit, 1), 200),
        status=status.upper() if status else None,
    )
    return [
        {
            "order_id": o["id"],
            "item": o["item"],
            "total_usd": o["total"],
            "status": o["status"],
            "customer_id": o["customer_id"],
            "created_at": o["created_at"],
        }
        for o in rows
    ]


# ---------------------------------------------------------------- public info
@app.get("/config")
def config_endpoint():
    store = stores.ensure_demo_store()
    return {
        "store_id": store["id"],
        "store_name": store["name"],
        "max_order_usd": store["max_order_usd"],
        "max_auto_refund_usd": store["max_auto_refund_usd"],
    }


@app.get("/products")
def products():
    store = stores.ensure_demo_store()
    return [
        {"product_id": k, "name": v["name"], "price_usd": v["price"]}
        for k, v in stores.catalog_for(store["id"]).items()
    ]


@app.get("/health")
def health():
    return {"ok": True}
