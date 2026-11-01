"""Owner authentication: registration, login, logout and session middleware.

Sessions are HTTP-only cookie tokens. Only the SHA-256 hash of the token is
stored, so a database leak does not hand out live sessions.
"""

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from . import config, db, security, stores

router = APIRouter(prefix="/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 10
login_limiter = security.RateLimiter()


class RegisterIn(BaseModel):
    email: str
    password: str
    name: str = ""
    store_name: str = ""
    signup_code: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _public_owner(owner: dict) -> dict:
    store = stores.get_store(owner["store_id"])
    return {
        "id": owner["id"],
        "email": owner["email"],
        "name": owner["name"],
        "store_id": owner["store_id"],
        "store": stores.public_store(store),
    }


def _expiry(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _start_session(owner_id: str, request: Request, response: Response) -> None:
    token = security.new_token()
    now = db.now()
    db.execute(
        "INSERT INTO sessions (id, owner_id, created_at, expires_at, last_seen_at, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            security.hash_token(token),
            owner_id,
            now,
            _expiry(config.SESSION_TTL_HOURS),
            now,
            _client_ip(request),
            (request.headers.get("user-agent") or "")[:200],
        ),
    )
    response.set_cookie(
        config.SESSION_COOKIE_NAME,
        token,
        max_age=config.SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=config.SESSION_COOKIE_SECURE,
        samesite=config.SESSION_COOKIE_SAMESITE,
        path="/",
    )


def current_owner(request: Request) -> dict:
    """FastAPI dependency: resolves the signed-in owner or raises 401."""
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Sign in to continue")

    session = db.query_one(
        "SELECT s.id AS session_id, s.expires_at, o.id, o.store_id, o.email, o.name, o.disabled "
        "FROM sessions s JOIN owners o ON o.id = s.owner_id "
        "WHERE s.id = ? AND s.revoked_at IS NULL",
        (security.hash_token(token),),
    )
    if not session:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    if session["expires_at"] <= db.now():
        db.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ?", (db.now(), session["session_id"])
        )
        raise HTTPException(401, "Your session has expired. Sign in again.")
    if session["disabled"]:
        raise HTTPException(403, "This account is disabled")

    db.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE id = ?", (db.now(), session["session_id"])
    )
    return {
        "id": session["id"],
        "store_id": session["store_id"],
        "email": session["email"],
        "name": session["name"],
        "session_id": session["session_id"],
    }


def _owner_count() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM owners")
    return int(row["n"]) if row else 0


@router.get("/setup-status")
def setup_status():
    """Public: tells the dashboard whether to show 'create the first account' or 'sign in'."""
    count = _owner_count()
    return {
        "needs_setup": count == 0,
        "registration_open": count == 0 or config.ALLOW_OWNER_REGISTRATION,
    }


@router.post("/register", status_code=201)
def register(data: RegisterIn, request: Request, response: Response):
    if not login_limiter.hit(f"register:{_client_ip(request)}", config.LOGIN_RATE_LIMIT_PER_MIN):
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.")

    email = (data.email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address")
    if len(data.password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(422, f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    owner_count = _owner_count()
    # The first account creates the installation's owner. Later accounts are only
    # allowed when the operator opens registration (optionally with a signup code).
    if owner_count and not config.ALLOW_OWNER_REGISTRATION:
        raise HTTPException(403, "Registration is closed. Sign in with the existing owner account.")
    if owner_count and config.OWNER_SIGNUP_CODE:
        if not security.constant_time_equals(data.signup_code or "", config.OWNER_SIGNUP_CODE):
            raise HTTPException(403, "Invalid signup code")
    if db.query_one("SELECT 1 FROM owners WHERE email = ?", (email,)):
        raise HTTPException(409, "An account with this email already exists")

    if owner_count == 0:
        store = stores.ensure_demo_store()
        if data.store_name.strip():
            store = stores.update_store(store["id"], name=data.store_name.strip()[:80])
    else:
        store = stores.create_store(data.store_name.strip() or f"{email}'s store")

    owner_id = db.new_id("own_")
    db.execute(
        "INSERT INTO owners (id, store_id, email, name, password_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            owner_id,
            store["id"],
            email,
            (data.name or "").strip()[:80],
            security.hash_password(data.password),
            db.now(),
        ),
    )
    _start_session(owner_id, request, response)
    owner = db.query_one("SELECT * FROM owners WHERE id = ?", (owner_id,))
    return {"owner": _public_owner(owner)}


@router.post("/login")
def login(data: LoginIn, request: Request, response: Response):
    ip = _client_ip(request)
    email = (data.email or "").strip().lower()
    # Two windows: one per address, one per address+account, so brute forcing a
    # single account is slow even from a shared IP.
    allowed = login_limiter.hit(
        f"login-ip:{ip}", config.LOGIN_RATE_LIMIT_PER_MIN * 2
    ) and login_limiter.hit(
        f"login-account:{ip}:{email}", config.LOGIN_RATE_LIMIT_PER_MIN
    )
    if not allowed:
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.")

    owner = db.query_one("SELECT * FROM owners WHERE email = ?", (email,))
    if not owner:
        security.dummy_verify(data.password or "")
        raise HTTPException(401, "Invalid email or password")
    if not security.verify_password(owner["password_hash"], data.password or ""):
        raise HTTPException(401, "Invalid email or password")
    if owner["disabled"]:
        raise HTTPException(403, "This account is disabled")

    if security.needs_rehash(owner["password_hash"]):
        db.execute(
            "UPDATE owners SET password_hash = ? WHERE id = ?",
            (security.hash_password(data.password), owner["id"]),
        )
    db.execute("UPDATE owners SET last_login_at = ? WHERE id = ?", (db.now(), owner["id"]))

    _start_session(owner["id"], request, response)
    return {"owner": _public_owner(owner)}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if token:
        db.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (db.now(), security.hash_token(token)),
        )
    response.delete_cookie(config.SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(owner: dict = Depends(current_owner)):
    return {"owner": _public_owner(owner)}
