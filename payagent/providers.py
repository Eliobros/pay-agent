"""Payment provider credentials: encrypted storage, validation and lookup.

Credentials must be reversible (the backend has to call the provider), so they are
encrypted with AES-256-GCM instead of hashed. The master key lives only in the
environment, and the ciphertext is bound to its store/provider/environment through
the AAD, so a row copied to another store cannot be decrypted.

The registry below is the extension point: adding M-Pesa or e-Mola means one entry
plus a validator. Storage, masking, validation flow and the dashboard stay as-is.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import config, db, security
from .auth import current_owner

log = logging.getLogger("payagent.providers")

PROVIDERS = {
    "paypal": {
        "label": "PayPal",
        "environments": {
            "sandbox": "https://api-m.sandbox.paypal.com",
            "live": "https://api-m.paypal.com",
        },
    },
}

STATUS_OPERATIONAL = "operational"
STATUS_INVALID = "invalid"
STATUS_UNVERIFIED = "unverified"


def _spec(provider: str) -> dict:
    spec = PROVIDERS.get((provider or "").strip().lower())
    if not spec:
        raise HTTPException(404, f"Unknown payment provider: {provider}")
    return spec


def _base_url(provider: str, environment: str) -> str:
    spec = _spec(provider)
    url = spec["environments"].get(environment)
    if not url:
        raise HTTPException(
            422, f"environment must be one of: {', '.join(spec['environments'])}"
        )
    return url


def _aad(store_id: str, provider: str, environment: str) -> str:
    return f"{store_id}:{provider}:{environment}"


# ---------------------------------------------------------------- storage
def get(store_id: str, provider: str, environment: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM payment_providers WHERE store_id = ? AND provider = ? AND environment = ?",
        (store_id, provider, environment),
    )


def list_for_store(store_id: str) -> list[dict]:
    return db.query(
        "SELECT * FROM payment_providers WHERE store_id = ? ORDER BY provider, environment",
        (store_id,),
    )


def _public(row: dict | None) -> dict | None:
    """What the dashboard may see. The secret itself never leaves the backend."""
    if not row:
        return None
    last4 = row.get("client_secret_last4") or ""
    return {
        "provider": row["provider"],
        "environment": row["environment"],
        "client_id": row["client_id"],
        "client_secret_masked": f"••••{last4}" if last4 else "••••",
        "status": row["status"],
        "validated_at": row["validated_at"],
        "updated_at": row["updated_at"],
        "error": row.get("validation_error"),
        "base_url": _base_url(row["provider"], row["environment"]),
    }


def save(
    store_id: str,
    provider: str,
    environment: str,
    client_id: str,
    client_secret: str,
) -> dict:
    provider = (provider or "").strip().lower()
    environment = (environment or "").strip().lower()
    _base_url(provider, environment)

    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(422, "Client ID and Client secret are both required")

    try:
        # Re-encrypt on every save: a new nonce each time, never a reused one.
        encrypted = security.encrypt_secret(
            client_secret, _aad(store_id, provider, environment)
        )
    except security.EncryptionNotConfigured as e:
        raise HTTPException(503, str(e)) from e

    stamp = db.now()
    db.execute(
        "INSERT INTO payment_providers (id, store_id, provider, environment, client_id, "
        "client_secret_enc, client_secret_last4, status, validation_error, validated_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?) "
        "ON CONFLICT(store_id, provider, environment) DO UPDATE SET "
        "client_id = excluded.client_id, client_secret_enc = excluded.client_secret_enc, "
        "client_secret_last4 = excluded.client_secret_last4, status = excluded.status, "
        "validation_error = NULL, validated_at = NULL, updated_at = excluded.updated_at",
        (
            db.new_id("pp_"),
            store_id,
            provider,
            environment,
            client_id,
            encrypted,
            client_secret[-4:] if len(client_secret) > 8 else "",
            STATUS_UNVERIFIED,
            stamp,
            stamp,
        ),
    )
    return get(store_id, provider, environment)


def remove(store_id: str, provider: str, environment: str) -> None:
    db.execute(
        "DELETE FROM payment_providers WHERE store_id = ? AND provider = ? AND environment = ?",
        (store_id, provider, environment),
    )


# ---------------------------------------------------------------- validation
def _check_paypal(row: dict, client_secret: str) -> tuple[bool, str | None]:
    """Ask PayPal for a token: the only thing that proves the credentials work."""
    base = _base_url(row["provider"], row["environment"])
    try:
        response = httpx.post(
            f"{base}/v1/oauth2/token",
            auth=(row["client_id"], client_secret),
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
    except httpx.HTTPError as e:
        return False, f"Could not reach PayPal ({type(e).__name__})"
    if response.status_code == 200 and response.json().get("access_token"):
        return True, None
    # The body can echo details; only the status code is stored and shown.
    return False, f"PayPal rejected these credentials (HTTP {response.status_code})"


_VALIDATORS = {"paypal": _check_paypal}


def _decrypt(row: dict) -> str | None:
    try:
        return security.decrypt_secret(
            row["client_secret_enc"],
            _aad(row["store_id"], row["provider"], row["environment"]),
        )
    except Exception as e:  # wrong/rotated key, tampered or moved ciphertext
        log.warning(
            "stored %s credentials of store %s cannot be decrypted (%s)",
            row["provider"],
            row["store_id"],
            type(e).__name__,
        )
        return None


def validate(store_id: str, provider: str, environment: str) -> dict:
    row = get(store_id, provider, environment)
    if not row:
        raise HTTPException(404, "Save the credentials first")
    secret = _decrypt(row)
    if secret is None:
        _mark(row["id"], STATUS_INVALID, "Stored credentials cannot be decrypted")
        raise HTTPException(
            503,
            "Stored credentials cannot be decrypted. Save the client secret again "
            "(the encryption key may have changed).",
        )

    checker = _VALIDATORS.get(row["provider"])
    ok, error = checker(row, secret) if checker else (False, "No validator for this provider")
    stamp = db.now()
    _mark(row["id"], STATUS_OPERATIONAL if ok else STATUS_INVALID, error, stamp if ok else None)
    if ok:
        set_active_environment(store_id, row["environment"])
    return get(store_id, provider, environment)  # type: ignore[return-value]


def _mark(row_id: str, status: str, error: str | None, validated_at: str | None = None) -> None:
    db.execute(
        "UPDATE payment_providers SET status = ?, validation_error = ?, validated_at = ?, "
        "updated_at = ? WHERE id = ?",
        (status, error, validated_at, db.now(), row_id),
    )


# ---------------------------------------------------------------- activation
def active_environment(store_id: str) -> str:
    row = db.query_one("SELECT payment_environment FROM stores WHERE id = ?", (store_id,))
    return (row or {}).get("payment_environment") or "sandbox"


def set_active_environment(store_id: str, environment: str) -> None:
    _base_url("paypal", environment)  # validates the value
    db.execute(
        "UPDATE stores SET payment_environment = ?, updated_at = ? WHERE id = ?",
        (environment, db.now(), store_id),
    )


def activate(store_id: str, provider: str, environment: str) -> dict:
    row = get(store_id, provider, environment)
    if not row or row["status"] != STATUS_OPERATIONAL:
        raise HTTPException(409, "Only validated credentials can be activated")
    set_active_environment(store_id, environment)
    return row


# ---------------------------------------------------------------- runtime lookup
def paypal_credentials(store_id: str) -> dict:
    """Credentials for a PayPal call: validated dashboard config, else .env."""
    environment = active_environment(store_id)
    row = db.query_one(
        "SELECT * FROM payment_providers WHERE store_id = ? AND provider = 'paypal' "
        "AND environment = ? AND status = ?",
        (store_id, environment, STATUS_OPERATIONAL),
    )
    if row:
        secret = _decrypt(row)
        if secret:
            return {
                "client_id": row["client_id"],
                "client_secret": secret,
                "base_url": _base_url("paypal", environment),
                "environment": environment,
                "source": "dashboard",
            }

    if config.PAYPAL_CLIENT_ID and config.PAYPAL_CLIENT_SECRET:
        base = config.PAYPAL_BASE
        return {
            "client_id": config.PAYPAL_CLIENT_ID,
            "client_secret": config.PAYPAL_CLIENT_SECRET,
            "base_url": base,
            "environment": "live" if "sandbox" not in base else "sandbox",
            "source": "env",
        }

    return {
        "client_id": "",
        "client_secret": "",
        "base_url": config.PAYPAL_BASE,
        "environment": environment,
        "source": "missing",
    }


# ---------------------------------------------------------------- API
router = APIRouter(prefix="/owner/payment-providers", tags=["payment providers"])


class SaveIn(BaseModel):
    environment: str = "sandbox"
    client_id: str
    client_secret: str


class EnvironmentIn(BaseModel):
    environment: str


@router.get("")
def list_providers(owner: dict = Depends(current_owner)):
    return {
        "active_environment": active_environment(owner["store_id"]),
        "supported": [
            {
                "provider": name,
                "label": spec["label"],
                "environments": list(spec["environments"]),
            }
            for name, spec in PROVIDERS.items()
        ],
        "configured": [_public(r) for r in list_for_store(owner["store_id"])],
    }


@router.post("/{provider}")
def save_provider(provider: str, data: SaveIn, owner: dict = Depends(current_owner)):
    """Save encrypted credentials, then validate them before they go live."""
    save(
        owner["store_id"],
        provider,
        data.environment,
        data.client_id,
        data.client_secret,
    )
    row = validate(owner["store_id"], provider.strip().lower(), data.environment.strip().lower())
    return {"provider": _public(row), "active_environment": active_environment(owner["store_id"])}


@router.post("/{provider}/validate")
def validate_provider(provider: str, data: EnvironmentIn, owner: dict = Depends(current_owner)):
    row = validate(owner["store_id"], provider.strip().lower(), data.environment.strip().lower())
    return {"provider": _public(row), "active_environment": active_environment(owner["store_id"])}


@router.post("/{provider}/activate")
def activate_provider(provider: str, data: EnvironmentIn, owner: dict = Depends(current_owner)):
    row = activate(owner["store_id"], provider.strip().lower(), data.environment.strip().lower())
    return {"provider": _public(row), "active_environment": active_environment(owner["store_id"])}


@router.delete("/{provider}/{environment}")
def delete_provider(provider: str, environment: str, owner: dict = Depends(current_owner)):
    remove(owner["store_id"], provider.strip().lower(), environment.strip().lower())
    return {"ok": True, "configured": [_public(r) for r in list_for_store(owner["store_id"])]}
