"""Runtime configuration, read once from the environment (or .env)."""

import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return raw in ("1", "true", "yes", "on") if raw else default


# ---------------------------------------------------------------- AI model
MODEL_API_KEY = os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY")
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "https://api.meta.ai/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "muse-spark-1.3")

# ---------------------------------------------------------------- PayPal (default provider, used until the owner
# saves sandbox/live credentials in the dashboard)
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_BASE = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
# Webhook id of the PayPal REST app. Without it no notification can be verified,
# so the webhook endpoint refuses everything until it is configured.
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID") or ""

# ---------------------------------------------------------------- app
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
STORE_NAME = os.getenv("STORE_NAME", "Demo Store")
DEFAULT_STORE_ID = os.getenv("DEFAULT_STORE_ID", "store_demo")
PRODUCTS_FILE = os.getenv("PRODUCTS_FILE", "products.json")  # the merchant edits this file
DB_PATH = os.getenv("DB_PATH", "payagent.db")  # SQLite file, created automatically

# ---------------------------------------------------------------- safety rules
MAX_ORDER_USD = _float("MAX_ORDER_USD", 200.0)  # above: the owner approves
MAX_AUTO_REFUND_USD = _float("MAX_AUTO_REFUND_USD", 100.0)  # above: the owner approves
RATE_LIMIT_PER_MIN = _int("RATE_LIMIT_PER_MIN", 20)  # chat messages per visitor per minute
MAX_HISTORY = _int("MAX_HISTORY", 30)  # messages kept per visitor
CONVERSATION_TTL_HOURS = _int("CONVERSATION_TTL_HOURS", 24)
MAX_EVIDENCE_MB = _int("MAX_EVIDENCE_MB", 15)  # per uploaded refund evidence file
MAX_VIDEO_SECONDS = _int("MAX_VIDEO_SECONDS", 60)  # longer videos are refused
EVIDENCE_DIR = os.getenv("EVIDENCE_DIR", "evidence")  # private storage, never served as static files
# Evidence of closed refund requests is deleted after this many days.
EVIDENCE_RETENTION_DAYS = _int("EVIDENCE_RETENTION_DAYS", 30)
# "local" only for now; the storage interface allows adding cloud drivers later.
EVIDENCE_STORAGE = os.getenv("EVIDENCE_STORAGE", "local").strip().lower()

# ---------------------------------------------------------------- owner auth
SESSION_TTL_HOURS = _int("SESSION_TTL_HOURS", 12)
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "payagent_session")
SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", FRONTEND_ORIGIN.startswith("https"))
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "lax")
# The very first owner account is always allowed (first-run setup). After that,
# registration stays closed unless the operator opens it.
ALLOW_OWNER_REGISTRATION = _bool("ALLOW_OWNER_REGISTRATION", False)
OWNER_SIGNUP_CODE = os.getenv("OWNER_SIGNUP_CODE") or ""
LOGIN_RATE_LIMIT_PER_MIN = _int("LOGIN_RATE_LIMIT_PER_MIN", 8)

# ---------------------------------------------------------------- secret encryption
# Master key for AES-256-GCM encryption of provider credentials. Lives outside the
# database (environment only). Generate one with:
#   python -c "from payagent.security import generate_key; print(generate_key())"
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY") or ""
