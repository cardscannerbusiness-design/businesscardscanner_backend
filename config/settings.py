"""Application settings loaded from environment variables."""
import os
import re

APP_TITLE = "NameCardScan API"
APP_VERSION = "1.0.0"
APP_DESCRIPTION = (
    "Business card scanner with PostgreSQL contact management, "
    "WhatsApp/Email outreach, JWT RBAC, AWS Textract, and PaddleOCR."
)


DEFAULT_HOST = os.getenv("HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("PORT", "5000"))

# ── JWT / Auth Settings (secret must come from .env — no insecure fallback) ─
def _require_jwt_secret() -> str:
    value = (os.getenv("JWT_SECRET_KEY") or "").strip()
    if not value or value == "change-me-in-production":
        raise RuntimeError(
            "JWT_SECRET_KEY is missing or insecure. "
            "Set a strong secret in BusinessCardScanner_Backend/.env and restart."
        )
    return value


JWT_SECRET_KEY = _require_jwt_secret()
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
JWT_REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7"))

# ── SuperAdmin Bootstrap ────────────────────────────────────────────────
SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "superadmin@ulavi.com")
SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "SuperAdmin@123")

# ── Session & Lockout ───────────────────────────────────────────────────
SESSION_TIMEOUT_MINUTES = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
SESSION_ABSOLUTE_TIMEOUT_HOURS = int(os.getenv("SESSION_ABSOLUTE_TIMEOUT_HOURS", "24"))
MAX_FAILED_LOGIN_ATTEMPTS = int(os.getenv("MAX_FAILED_LOGIN_ATTEMPTS", "5"))
ACCOUNT_LOCK_MINUTES = int(os.getenv("ACCOUNT_LOCK_MINUTES", "30"))

# Optional regex for extra CORS origins (e.g. local Vite). Set in .env when needed.
# Example: CORS_ORIGIN_REGEX=https?://(localhost|127\.0\.0\.1)(:\d+)?
CORS_ORIGIN_REGEX = (os.getenv("CORS_ORIGIN_REGEX") or "").strip()


def normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def _cms_cors_origins() -> list[str]:
    """Super Admin CMS hosts (AWS Amplify). Override via CMS_ALLOWED_ORIGINS."""
    raw = (os.getenv("CMS_ALLOWED_ORIGINS") or "").strip()
    if raw:
        return [normalize_origin(part) for part in raw.split(",") if part.strip()]
    return [normalize_origin("https://main.d3ty5vjjsuy3g7.amplifyapp.com")]


def get_allowed_origins() -> list[str]:
    """
    CORS allow-list from environment plus built-in CMS Amplify host.

    Sources:
      - FRONTEND_BASE_URL (preferred) or FRONTEND_URL
      - ALLOWED_ORIGINS (comma-separated)
      - CMS_ALLOWED_ORIGINS (comma-separated) or default Amplify CMS URL
    """
    origins: list[str] = []

    frontend = (
        os.getenv("FRONTEND_BASE_URL")
        or os.getenv("FRONTEND_URL")
        or ""
    ).strip()
    if frontend:
        origins.append(normalize_origin(frontend))

    extra = os.getenv("ALLOWED_ORIGINS") or ""
    for part in extra.split(","):
        part = part.strip()
        if part:
            origins.append(normalize_origin(part))

    origins.extend(_cms_cors_origins())

    # Drop any leftover Netlify hosts
    cleaned = [
        o for o in origins
        if o and "netlify.app" not in o.lower() and "netlify.com" not in o.lower()
    ]
    return list(dict.fromkeys(cleaned))


def is_origin_allowed(origin: str) -> bool:
    """True when the browser Origin header may receive CORS responses."""
    normalized = normalize_origin(origin)
    if not normalized:
        return False
    if "netlify.app" in normalized.lower():
        return False
    if normalized in {normalize_origin(o) for o in get_allowed_origins()}:
        return True
    if CORS_ORIGIN_REGEX and re.fullmatch(CORS_ORIGIN_REGEX, normalized):
        return True
    return False


def cors_headers_for_origin(origin: str | None) -> dict[str, str]:
    if not origin or not is_origin_allowed(origin):
        return {}
    return {
        "Access-Control-Allow-Origin": normalize_origin(origin),
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }
