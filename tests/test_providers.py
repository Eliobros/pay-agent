"""Payment provider credentials: encryption at rest, validation and store scoping."""

import pytest

from payagent import providers, security


def fake_validator(ok=True, error=None):
    calls = []

    def check(row, client_secret):
        calls.append((row["store_id"], row["environment"], client_secret))
        return ok, error

    check.calls = calls
    return check


@pytest.fixture()
def paypal_ok(monkeypatch):
    check = fake_validator(True, None)
    monkeypatch.setitem(providers._VALIDATORS, "paypal", check)
    return check


def save(client, secret="sandbox-secret-wxyz", environment="sandbox", cid="client-abc"):
    return client.post(
        "/owner/payment-providers/paypal",
        json={"environment": environment, "client_id": cid, "client_secret": secret},
    )


def test_credentials_require_owner_session(client):
    assert client.get("/owner/payment-providers").status_code == 401
    assert save(client).status_code == 401


def test_secret_is_encrypted_at_rest_and_never_returned(owner_client, paypal_ok):
    response = save(owner_client, secret="sandbox-secret-wxyz")
    assert response.status_code == 200, response.text

    stored = providers.get("store_demo", "paypal", "sandbox")
    assert "sandbox-secret-wxyz" not in stored["client_secret_enc"]
    assert stored["client_secret_enc"].startswith("v1.")
    assert security.decrypt_secret(
        stored["client_secret_enc"], "store_demo:paypal:sandbox"
    ) == "sandbox-secret-wxyz"

    listing = owner_client.get("/owner/payment-providers")
    assert listing.status_code == 200
    assert "sandbox-secret-wxyz" not in listing.text
    configured = listing.json()["configured"][0]
    assert configured["client_secret_masked"] == "••••wxyz"
    assert configured["client_id"] == "client-abc"


def test_valid_credentials_become_operational_and_active(owner_client, paypal_ok):
    body = save(owner_client).json()
    assert body["provider"]["status"] == "operational"
    assert body["active_environment"] == "sandbox"

    creds = providers.paypal_credentials("store_demo")
    assert creds["source"] == "dashboard"
    assert creds["client_id"] == "client-abc"
    assert creds["client_secret"] == "sandbox-secret-wxyz"
    assert creds["base_url"] == "https://api-m.sandbox.paypal.com"


def test_rejected_credentials_are_not_operational(owner_client, monkeypatch):
    monkeypatch.setitem(
        providers._VALIDATORS,
        "paypal",
        fake_validator(False, "PayPal rejected these credentials (HTTP 401)"),
    )
    body = save(owner_client).json()
    assert body["provider"]["status"] == "invalid"
    assert "401" in body["provider"]["error"]
    # Secrets stay encrypted but unusable: the runtime falls back to .env.
    assert providers.paypal_credentials("store_demo")["source"] == "missing"


def test_validation_is_bound_to_the_saved_secret(owner_client, paypal_ok):
    save(owner_client, secret="first-secret-1111")
    assert paypal_ok.calls[-1][2] == "first-secret-1111"
    save(owner_client, secret="second-secret-2222")
    assert paypal_ok.calls[-1][2] == "second-secret-2222"
    # Only one row per store/provider/environment, with a fresh nonce.
    rows = providers.list_for_store("store_demo")
    assert len(rows) == 1


def test_revalidate_without_entering_the_secret_again(owner_client, paypal_ok):
    save(owner_client)
    response = owner_client.post(
        "/owner/payment-providers/paypal/validate", json={"environment": "sandbox"}
    )
    assert response.status_code == 200
    assert response.json()["provider"]["status"] == "operational"


def test_live_environment_uses_the_live_endpoint(owner_client, paypal_ok):
    body = save(owner_client, environment="live").json()
    assert body["provider"]["base_url"] == "https://api-m.paypal.com"
    assert providers.paypal_credentials("store_demo")["environment"] == "live"


def test_other_store_cannot_read_or_delete_the_credentials(owner_client, paypal_ok):
    save(owner_client)

    intruder = owner_client.post(
        "/auth/register",
        json={"email": "intruder@example.com", "password": "correct-horse-battery"},
    )
    assert intruder.status_code == 201
    assert intruder.json()["owner"]["store_id"] != "store_demo"

    assert owner_client.get("/owner/payment-providers").json()["configured"] == []
    deleted = owner_client.delete("/owner/payment-providers/paypal/sandbox")
    assert deleted.status_code == 200
    assert providers.get("store_demo", "paypal", "sandbox") is not None


def test_unknown_provider_and_environment_are_rejected(owner_client, paypal_ok):
    assert owner_client.get("/owner/payment-providers").status_code == 200
    bad_provider = owner_client.post(
        "/owner/payment-providers/stripe", json={"environment": "sandbox", "client_id": "a", "client_secret": "b"}
    )
    assert bad_provider.status_code == 404
    bad_env = save(owner_client, environment="production")
    assert bad_env.status_code == 422


def test_missing_values_and_key_are_handled(owner_client, paypal_ok, monkeypatch):
    empty = owner_client.post(
        "/owner/payment-providers/paypal", json={"environment": "sandbox", "client_id": "a", "client_secret": ""}
    )
    assert empty.status_code == 422

    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    response = save(owner_client)
    assert response.status_code == 503
    assert providers.get("store_demo", "paypal", "sandbox") is None


def test_ciphertext_moved_to_another_store_cannot_be_used(owner_client, paypal_ok):
    save(owner_client)
    stored = providers.get("store_demo", "paypal", "sandbox")

    second = owner_client.post(
        "/auth/register",
        json={"email": "copy@example.com", "password": "correct-horse-battery"},
    )
    other_store = second.json()["owner"]["store_id"]
    from payagent import db

    db.execute(
        "INSERT INTO payment_providers (id, store_id, provider, environment, client_id, "
        "client_secret_enc, client_secret_last4, status, created_at, updated_at) "
        "VALUES (?, ?, 'paypal', 'sandbox', 'client-abc', ?, 'wxyz', 'operational', ?, ?)",
        ("pp_copy", other_store, stored["client_secret_enc"], db.now(), db.now()),
    )
    # AAD binds the ciphertext to its store: it must not decrypt elsewhere.
    assert providers.paypal_credentials(other_store)["source"] == "missing"
