"""Auth constants — role names, token lifetimes, public paths, error codes."""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Role names (must match the seeded roles table)
# ---------------------------------------------------------------------------
ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"

ALL_ROLES = (ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER)

# ---------------------------------------------------------------------------
# JWT settings
# ---------------------------------------------------------------------------
def _require_jwt_secret() -> str:
    """Load JWT_SECRET_KEY from the environment. Never fall back to a default."""
    value = (os.getenv("JWT_SECRET_KEY") or "").strip()
    if not value or value == "change-me-in-production":
        raise RuntimeError(
            "JWT_SECRET_KEY is missing or insecure. "
            "Set a strong secret in BusinessCardScanner_Backend/.env and restart."
        )
    return value


JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_SECRET_KEY = _require_jwt_secret()
JWT_ISSUER = "cardsync"
JWT_AUDIENCE = "cardsync-api"
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
JWT_REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7"))

# ---------------------------------------------------------------------------
# Session settings
# ---------------------------------------------------------------------------
SESSION_TIMEOUT_MINUTES = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
SESSION_ABSOLUTE_TIMEOUT_HOURS = int(os.getenv("SESSION_ABSOLUTE_TIMEOUT_HOURS", "24"))

# ---------------------------------------------------------------------------
# Account lockout
# ---------------------------------------------------------------------------
MAX_FAILED_LOGIN_ATTEMPTS = int(os.getenv("MAX_FAILED_LOGIN_ATTEMPTS", "5"))
ACCOUNT_LOCK_MINUTES = int(os.getenv("ACCOUNT_LOCK_MINUTES", "30"))

# ---------------------------------------------------------------------------
# Password reset OTP
# ---------------------------------------------------------------------------
PASSWORD_RESET_OTP_EXPIRE_MINUTES = 10

# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------
EMAIL_VERIFY_TOKEN_EXPIRE_HOURS = 24
PUBLIC_PATHS: set[str] = {
    "/",
    "/health",
    "/webhook",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/static",
    "/assets",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/verify-email",
    "/api/auth/password-reset/send-otp",
    "/api/auth/password-reset/confirm",
    "/api/invitations/validate",
    "/api/invitations/accept",
}

# Prefixes that are also public (e.g. /static/anything)
PUBLIC_PREFIXES: tuple[str, ...] = (
    "/static",
    "/assets",
    "/docs",
    "/redoc",
)


def is_public_path(path: str) -> bool:
    """Return True when the path should bypass authentication."""
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix + "/") or path == prefix for prefix in PUBLIC_PREFIXES)


# ---------------------------------------------------------------------------
# Audit action labels
# ---------------------------------------------------------------------------
AUDIT_LOGIN = "login"
AUDIT_LOGIN_FAILED = "login_failed"
AUDIT_LOGOUT = "logout"
AUDIT_LOGOUT_ALL = "logout_all"
AUDIT_PASSWORD_CHANGE = "password_change"
AUDIT_PASSWORD_RESET = "password_reset"
AUDIT_USER_CREATED = "user_created"
AUDIT_USER_UPDATED = "user_updated"
AUDIT_USER_DELETED = "user_deleted"
AUDIT_USER_LOCKED = "user_locked"
AUDIT_COMPANY_CREATED = "company_created"
AUDIT_COMPANY_UPDATED = "company_updated"
AUDIT_COMPANY_DELETED = "company_deleted"
AUDIT_ROLE_CHANGED = "role_changed"
AUDIT_TOKEN_REFRESHED = "token_refreshed"
AUDIT_EMAIL_CHANGE_REQUESTED = "email_change_requested"
AUDIT_INVITE_SENT = "invite_sent"
AUDIT_INVITE_RESENT = "invite_resent"
AUDIT_INVITE_REVOKED = "invite_revoked"
AUDIT_INVITE_ACCEPTED = "invite_accepted"
AUDIT_ACCOUNT_DISABLED = "account_disabled"
AUDIT_ACCOUNT_ENABLED = "account_enabled"

# ---------------------------------------------------------------------------
# Invitation settings
# ---------------------------------------------------------------------------
INVITATION_EXPIRE_HOURS = int(os.getenv("INVITATION_EXPIRE_HOURS", "48"))
INVITATION_RATE_LIMIT_PER_HOUR = int(os.getenv("INVITATION_RATE_LIMIT_PER_HOUR", "20"))

# ---------------------------------------------------------------------------
# Error codes (returned in API error responses)
# ---------------------------------------------------------------------------
ERR_INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
ERR_ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
ERR_ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
ERR_EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
ERR_TOKEN_EXPIRED = "TOKEN_EXPIRED"
ERR_TOKEN_INVALID = "TOKEN_INVALID"
ERR_FORBIDDEN = "FORBIDDEN"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_DUPLICATE_EMAIL = "DUPLICATE_EMAIL"
ERR_DUPLICATE_USERNAME = "DUPLICATE_USERNAME"
ERR_WEAK_PASSWORD = "WEAK_PASSWORD"
ERR_COMPANY_INACTIVE = "COMPANY_INACTIVE"
