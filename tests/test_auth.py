"""Owner authentication, session handling and store isolation."""

from payagent import approvals, db, orders


def register(client, email="second@example.com", store_name="Second Store"):
    return client.post(
        "/auth/register",
        json={"email": email, "password": "correct-horse-battery", "store_name": store_name},
    )


def login(client, email="owner@example.com", password="correct-horse-battery"):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_first_owner_registration_creates_session(owner_client):
    me = owner_client.get("/auth/me")
    assert me.status_code == 200
    owner = me.json()["owner"]
    assert owner["email"] == "owner@example.com"
    assert owner["store_id"] == "store_demo"

    cookie = me.headers.get("set-cookie")
    assert cookie is None  # /auth/me must not re-issue a session


def test_session_cookie_is_http_only_and_unreadable_by_js(client):
    response = register(client, email="first@example.com")
    assert response.status_code == 201
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "payagent_session=" in cookie


def test_password_is_stored_as_argon2id(owner_client):
    row = db.query_one("SELECT password_hash FROM owners WHERE email = ?", ("owner@example.com",))
    assert row["password_hash"].startswith("$argon2id$")


def test_login_success_and_failure(client, owner_client):
    owner_client.post("/auth/logout")
    assert login(client).status_code == 200

    owner_client.post("/auth/logout")
    bad = login(client, password="wrong-password")
    assert bad.status_code == 401
    assert bad.json()["detail"] == "Invalid email or password"
    assert owner_client.get("/auth/me").status_code == 401


def test_missing_fields_and_short_password_are_rejected(client):
    assert client.post("/auth/register", json={"email": "nope", "password": "short"}).status_code == 422
    assert client.post("/auth/register", json={"email": "x@y.io", "password": "short"}).status_code == 422


def test_logout_revokes_the_session(owner_client):
    assert owner_client.get("/auth/me").status_code == 200
    assert owner_client.post("/auth/logout").status_code == 200
    assert owner_client.get("/auth/me").status_code == 401
    assert owner_client.get("/owner/approvals").status_code == 401


def test_expired_session_is_rejected(owner_client):
    db.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
    response = owner_client.get("/auth/me")
    assert response.status_code == 401
    revoked = db.query_one("SELECT revoked_at FROM sessions")
    assert revoked["revoked_at"] is not None


def test_owner_endpoints_require_authentication(client):
    assert client.get("/owner/approvals").status_code == 401
    assert client.get("/owner/orders").status_code == 401
    assert client.post("/owner/approvals/anything", json={"approve": True}).status_code == 401


def test_login_rate_limit(client, owner_client):
    owner_client.post("/auth/logout")
    statuses = [login(client, password="wrong").status_code for _ in range(6)]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


def test_registration_can_be_closed(client, owner_client, monkeypatch):
    from payagent import config

    monkeypatch.setattr(config, "ALLOW_OWNER_REGISTRATION", False)
    response = register(client, email="closed@example.com")
    assert response.status_code == 403


def test_setup_status_reflects_installation_state(client):
    assert client.get("/auth/setup-status").json() == {"needs_setup": True, "registration_open": True}
    register(client, email="first@example.com")
    status = client.get("/auth/setup-status").json()
    assert status["needs_setup"] is False


def test_owner_cannot_touch_another_stores_data(client, owner_client):
    pending = approvals.create(
        store_id="store_demo",
        kind="purchase",
        customer_id="visitor-1",
        summary="1 x 4K Monitor 27in",
        total=329.0,
        payload={"product_id": "p5", "quantity": 1},
    )
    orders.create(
        store_id="store_demo",
        customer_id="visitor-1",
        item="1 x 4K Monitor 27in",
        total=329.0,
        status="CREATED",
    )

    # A second owner gets a brand-new store with its own data.
    second = register(client, email="intruder@example.com", store_name="Other Store")
    assert second.status_code == 201
    assert second.json()["owner"]["store_id"] != "store_demo"

    assert client.get("/owner/approvals").json() == []
    assert client.get("/owner/orders").json() == []
    assert client.post(f"/owner/approvals/{pending['id']}", json={"approve": True}).status_code == 404

    # The original owner still sees the request.
    login(client, email="owner@example.com")
    visible = client.get("/owner/approvals").json()
    assert [a["id"] for a in visible] == [pending["id"]]
    assert len(client.get("/owner/orders").json()) == 1


def test_duplicate_email_is_rejected(client, owner_client):
    response = register(client, email="owner@example.com")
    assert response.status_code == 409
