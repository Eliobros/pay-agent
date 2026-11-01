"""Owner dashboard: overview, usage, store settings and account security."""

from payagent import approvals as approvals_store
from payagent import conversations, db, orders, refunds, stores


def paid(total=100.0, status="COMPLETED", **overrides):
    data = {
        "store_id": "store_demo",
        "customer_id": "visitor-1",
        "item": f"item {total}",
        "total": total,
        "status": status,
    }
    data.update(overrides)
    return orders.create(**data)


def test_dashboard_requires_an_owner_session(client):
    for path in ("/owner/overview", "/owner/usage", "/owner/settings", "/owner/sessions"):
        assert client.get(path).status_code == 401
    assert client.patch("/owner/settings", json={"name": "x"}).status_code == 401
    assert (
        client.post(
            "/owner/account/password",
            json={"current_password": "a", "new_password": "brand-new-password"},
        ).status_code
        == 401
    )


def test_overview_reports_the_store_numbers(owner_client):
    first = paid(100.0)
    paid(50.0)
    paid(20.0, status="FAILED")
    orders.update(first["id"], refunded_total=40.0)
    approvals_store.create(
        store_id="store_demo",
        kind="purchase",
        customer_id="visitor-1",
        summary="1 x 4K Monitor 27in",
        total=329.0,
        payload={},
    )
    refunds.create("store_demo", "visitor-1", first["id"], "the screen is cracked")

    data = owner_client.get("/owner/overview").json()
    assert data["orders_total"] == 3
    assert data["orders_paid"] == 2
    assert data["total_sales_usd"] == 150.0
    assert data["refunded_usd"] == 40.0
    assert data["net_sales_usd"] == 110.0
    assert data["pending_approvals"] == 1
    assert data["refund_requests_pending"] == 1
    assert data["failed_payments"] == 1
    assert data["failed_transactions"] == 1


def test_overview_starts_at_zero_for_a_new_store(owner_client):
    owner_client.post(
        "/auth/register",
        json={"email": "fresh@example.com", "password": "correct-horse-battery"},
    )
    data = owner_client.get("/owner/overview").json()
    assert data["orders_total"] == 0
    assert data["total_sales_usd"] == 0
    assert data["net_sales_usd"] == 0
    assert data["failed_transactions"] == 0


def test_usage_counts_conversations_messages_and_tools(owner_client):
    conversation = conversations.open_conversation("store_demo", "visitor-1")
    conversations.append(conversation["id"], "store_demo", "user", "find me a keyboard")
    conversations.append(
        conversation["id"],
        "store_demo",
        "assistant",
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "search_products", "arguments": "{}"},
            }
        ],
    )
    conversations.append(conversation["id"], "store_demo", "tool", "{}", tool_call_id="c1")
    conversations.append(conversation["id"], "store_demo", "assistant", "here you go")

    data = owner_client.get("/owner/usage").json()
    assert data["conversations_total"] == 1
    assert data["conversations_active"] == 1
    assert data["messages_total"] == 4
    assert data["assistant_messages"] == 2
    assert data["tool_messages"] == 1
    assert data["tool_calls_total"] == 1
    assert data["tools"] == [{"name": "search_products", "calls": 1}]


def test_usage_is_scoped_to_the_owner_store(owner_client):
    other = stores.create_store("Other Store")
    conversation = conversations.open_conversation(other["id"], "visitor-1")
    conversations.append(conversation["id"], other["id"], "user", "their message")

    data = owner_client.get("/owner/usage").json()
    assert data["messages_total"] == 0
    assert data["conversations_total"] == 0


def test_store_settings_can_be_updated(owner_client):
    current = owner_client.get("/owner/settings").json()
    assert current["max_order_usd"] == 200.0
    assert current["currency"] == "USD"

    response = owner_client.patch(
        "/owner/settings",
        json={"name": "Eliobros Tech", "max_order_usd": 500, "max_auto_refund_usd": 25},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Eliobros Tech"
    assert body["max_order_usd"] == 500.0
    assert body["max_auto_refund_usd"] == 25.0
    assert stores.get_store("store_demo")["name"] == "Eliobros Tech"


def test_invalid_settings_are_rejected(owner_client):
    assert owner_client.patch("/owner/settings", json={"name": "   "}).status_code == 422
    assert owner_client.patch("/owner/settings", json={"max_order_usd": 0}).status_code == 422
    assert owner_client.patch("/owner/settings", json={"max_auto_refund_usd": -1}).status_code == 422
    # Nothing was written by the rejected calls.
    assert stores.get_store("store_demo")["max_order_usd"] == 200.0


def test_settings_only_touch_the_owner_store(owner_client):
    other = stores.create_store("Other Store")
    owner_client.patch("/owner/settings", json={"name": "Mine"})
    assert stores.get_store(other["id"])["name"] == "Other Store"


def test_sessions_list_marks_the_current_one(owner_client):
    rows = owner_client.get("/owner/sessions").json()
    assert len(rows) == 1
    assert rows[0]["current"] is True
    assert "payagent_session" not in str(rows)


def test_password_change_requires_the_current_password(owner_client):
    response = owner_client.post(
        "/owner/account/password",
        json={"current_password": "wrong-password", "new_password": "brand-new-password"},
    )
    assert response.status_code == 401


def test_password_change_revokes_other_sessions(owner_client):
    assert (
        owner_client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "correct-horse-battery"},
        ).status_code
        == 200
    )
    assert len(owner_client.get("/owner/sessions").json()) == 2

    response = owner_client.post(
        "/owner/account/password",
        json={"current_password": "correct-horse-battery", "new_password": "brand-new-password"},
    )
    assert response.status_code == 200
    assert response.json()["other_sessions_revoked"] == 1

    # The session used for the change stays valid, the other one is gone.
    assert owner_client.get("/auth/me").status_code == 200
    assert len(owner_client.get("/owner/sessions").json()) == 1

    owner_client.post("/auth/logout")
    assert (
        owner_client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "correct-horse-battery"},
        ).status_code
        == 401
    )
    assert (
        owner_client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "brand-new-password"},
        ).status_code
        == 200
    )


def test_revoke_other_sessions(owner_client):
    owner_client.post(
        "/auth/login",
        json={"email": "owner@example.com", "password": "correct-horse-battery"},
    )
    response = owner_client.post("/owner/sessions/revoke-others")
    assert response.json()["revoked"] == 1
    assert len(owner_client.get("/owner/sessions").json()) == 1


def test_orders_endpoint_filters_by_status(owner_client):
    paid(10.0)
    paid(20.0, status="FAILED")
    assert len(owner_client.get("/owner/orders").json()) == 2
    failed = owner_client.get("/owner/orders?status=failed").json()
    assert [order["status"] for order in failed] == ["FAILED"]
    assert len(db.query("SELECT * FROM orders")) == 2
