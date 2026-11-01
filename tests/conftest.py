"""Test setup: isolated SQLite file per test, fake model key, no real PayPal."""

import base64
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Configuration is read at import time, so the environment is prepared first.
_TMP = tempfile.mkdtemp(prefix="payagent-tests-")
os.environ["DB_PATH"] = os.path.join(_TMP, "bootstrap.db")
os.environ["EVIDENCE_DIR"] = os.path.join(_TMP, "evidence")
os.environ["MODEL_API_KEY"] = "test-model-key"
os.environ["CREDENTIAL_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"k" * 32).decode()
os.environ["ALLOW_OWNER_REGISTRATION"] = "true"
os.environ["LOGIN_RATE_LIMIT_PER_MIN"] = "5"
os.environ["RATE_LIMIT_PER_MIN"] = "20"
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["MAX_ORDER_USD"] = "200"
os.environ["MAX_AUTO_REFUND_USD"] = "100"
# Never inherit a developer's .env PayPal keys: tests assert the fallback chain.
os.environ["PAYPAL_CLIENT_ID"] = ""
os.environ["PAYPAL_CLIENT_SECRET"] = ""
os.environ["PAYPAL_WEBHOOK_ID"] = "test-webhook-id"

from fastapi.testclient import TestClient  # noqa: E402

from payagent import auth as auth_module  # noqa: E402
from payagent import db, storage  # noqa: E402
import app as app_module  # noqa: E402


@pytest.fixture()
def database(tmp_path):
    db.use_database(str(tmp_path / "payagent.db"))
    storage.reset_storage(tmp_path / "evidence")
    auth_module.login_limiter.reset()
    app_module.chat_limiter.reset()
    return db


@pytest.fixture()
def client(database):
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def owner_client(client):
    """A client already signed in as the owner of the demo store."""
    response = client.post(
        "/auth/register",
        json={"email": "owner@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 201, response.text
    return client
