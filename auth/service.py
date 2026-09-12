"""Core authentication service — login, register, refresh, logout, forgot/reset password."""

from __future__ import annotations

import hashlib
import logging
import random
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from auth import audit_service, session_service, token_service
from auth.constants import (
    ACCOUNT_LOCK_MINUTES,
    AUDIT_EMAIL_CHANGE_REQUESTED,
    AUDIT_LOGIN,
    AUDIT_LOGIN_FAILED,
    AUDIT_LOGOUT,
    AUDIT_LOGOUT_ALL,
    AUDIT_PASSWORD_CHANGE,
    AUDIT_PASSWORD_RESET,
    AUDIT_TOKEN_REFRESHED,
    AUDIT_USER_CREATED,
    ERR_ACCOUNT_INACTIVE,
    ERR_ACCOUNT_LOCKED,
    ERR_ACCOUNT_PENDING_APPROVAL,
    ERR_ACCOUNT_REGISTRATION_REJECTED,
    ERR_DUPLICATE_EMAIL,
    ERR_DUPLICATE_USERNAME,
    ERR_FORBIDDEN,
    ERR_INVALID_CREDENTIALS,
    ERR_TOKEN_EXPIRED,
    ERR_TOKEN_INVALID,
    ERR_WEAK_PASSWORD,
    MAX_FAILED_LOGIN_ATTEMPTS,
    PASSWORD_RESET_OTP_EXPIRE_MINUTES,
    ROLE_SUPER_ADMIN,
)
from auth.email_service import (
    send_email_change_verification,
    send_email_verification,
    send_forgot_password_otp,
    send_welcome_email,
)
from auth.jwt_utils import (
    create_access_token,
    create_refresh_token,
    token_expiry_days,
    verify_refresh_token,
)
from auth.password_utils import hash_password, validate_password_policy, verify_password
from db.pool import db_cursor

logger = logging.getLogger(__name__)


class AuthError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_role_name_by_id(cur, role_id: int) -> str:
    cur.execute("SELECT name FROM roles WHERE id = %s", (role_id,))
    row = cur.fetchone()
    return row["name"] if row else ""


def _get_role_id_by_name(cur, name: str) -> int | None:
    cur.execute("SELECT id FROM roles WHERE name = %s", (name,))
    row = cur.fetchone()
    return row["id"] if row else None


def _user_row_to_dict(row: dict) -> dict[str, Any]:
    """Convert a raw DB user row to a safe (no password_hash) dict."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k == "password_hash":
            continue
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _generate_otp(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


def _client_info(ip: str = "", user_agent: str = "") -> dict[str, str]:
    """Derive device/browser labels from user-agent string (lightweight)."""
    browser = "unknown"
    ua_lower = user_agent.lower()
    if "chrome" in ua_lower and "edg" not in ua_lower:
        browser = "Chrome"
    elif "firefox" in ua_lower:
        browser = "Firefox"
    elif "safari" in ua_lower and "chrome" not in ua_lower:
        browser = "Safari"
    elif "edg" in ua_lower:
        browser = "Edge"
    device = "desktop"
    if "mobile" in ua_lower or "android" in ua_lower:
        device = "mobile"
    return {"device": device, "browser": browser}


# ─────────────────────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────────────────────

def login(
    identifier: str,
    password: str,
    *,
    ip: str = "",
    user_agent: str = "",
    required_role: str | None = None,
) -> dict[str, Any]:
    """Authenticate by email or username + password. Returns token pair + user info."""
    identifier = identifier.strip().lower()
    info = _client_info(ip, user_agent)

    with db_cursor(commit=True) as cur:
        # Lookup by email OR username
        cur.execute(
            """
            SELECT u.*, r.name AS role_name
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE (LOWER(u.email) = %s OR LOWER(u.username) = %s)
              AND u.deleted_at IS NULL
            """,
            (identifier, identifier),
        )
        user = cur.fetchone()

        if not user:
            from auth.registration_service import (
                MSG_PENDING_APPROVAL,
                MSG_REGISTRATION_REJECTED,
                STATUS_PENDING,
                STATUS_REJECTED,
            )

            cur.execute(
                """
                SELECT status, password_hash, rejection_reason
                FROM admin_registration_requests
                WHERE LOWER(email) = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (identifier,),
            )
            req = cur.fetchone()
            if req and verify_password(password, str(req.get("password_hash") or "")):
                status = str(req.get("status") or "").lower()
                if status == STATUS_PENDING:
                    raise AuthError(ERR_ACCOUNT_PENDING_APPROVAL, MSG_PENDING_APPROVAL, 403)
                if status == STATUS_REJECTED:
                    reason = str(req.get("rejection_reason") or "").strip()
                    message = MSG_REGISTRATION_REJECTED
                    if reason:
                        message = f"{MSG_REGISTRATION_REJECTED} Reason: {reason}"
                    raise AuthError(ERR_ACCOUNT_REGISTRATION_REJECTED, message, 403)
            audit_service.log_action(None, AUDIT_LOGIN_FAILED, ip=ip, user_agent=user_agent,
                                     new_value={"identifier": identifier})
            raise AuthError(ERR_INVALID_CREDENTIALS, "Invalid email/username or password.", 401)

        user = dict(user)
        user_id = str(user["id"])

        # Check locked
        locked_until = user.get("locked_until")
        if locked_until:
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)
            if locked_until > datetime.now(timezone.utc):
                raise AuthError(ERR_ACCOUNT_LOCKED,
                                f"Account is locked. Try again after {locked_until.isoformat()}.", 423)

        # Check active
        if not user.get("is_active"):
            raise AuthError(ERR_ACCOUNT_INACTIVE, "Account is deactivated. Contact your admin.", 403)

        # Verify password
        if not verify_password(password, user["password_hash"]):
            # Increment failed attempts
            attempts = user.get("failed_login_attempts", 0) + 1
            lock_until = None
            if attempts >= MAX_FAILED_LOGIN_ATTEMPTS:
                lock_until = datetime.now(timezone.utc) + timedelta(minutes=ACCOUNT_LOCK_MINUTES)
                audit_service.log_action(user_id, AUDIT_LOGIN_FAILED, ip=ip, user_agent=user_agent,
                                         new_value={"locked_until": lock_until.isoformat()})
            cur.execute(
                """
                UPDATE users SET failed_login_attempts = %s, locked_until = %s
                WHERE id = %s
                """,
                (attempts, lock_until, user_id),
            )
            raise AuthError(ERR_INVALID_CREDENTIALS, "Invalid email/username or password.", 401)

        # ── Check role restriction before resetting failed attempts or minting tokens ──
        role_name = user["role_name"]
        if required_role and role_name != required_role:
            audit_service.log_action(user_id, AUDIT_LOGIN_FAILED, ip=ip, user_agent=user_agent,
                                     new_value={"reason": f"Required role {required_role}, got {role_name}"})
            raise AuthError(
                ERR_FORBIDDEN,
                f"Access denied. Only {required_role} accounts can log into this portal.",
                403,
            )

        # ── Successful login ──────────────────────────────────────────────
        # Reset failed attempts
        cur.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, last_login = %s WHERE id = %s",
            (datetime.now(timezone.utc), user_id),
        )

    # Generate tokens (outside transaction)
    company_id = str(user["company_id"]) if user.get("company_id") else None

    # Mint session_id first so both access + refresh JWTs carry a valid binding.
    session_id = str(uuid.uuid4())
    refresh_token_raw = create_refresh_token(user_id, session_id)
    token_id = token_service.store_refresh_token(
        user_id, refresh_token_raw, device=info["device"], browser=info["browser"], ip=ip,
    )
    session_service.create_session(
        user_id,
        token_id,
        session_id=session_id,
        device=info["device"],
        browser=info["browser"],
        ip=ip,
    )
    access_token = create_access_token(user_id, role_name, company_id, session_id=session_id)

    audit_service.log_action(user_id, AUDIT_LOGIN, ip=ip, user_agent=user_agent,
                             new_value={"session_id": session_id, "device": info["device"]})

    from services.user_profile_identity import effective_display_name, public_profile_image_url

    display_name = str(user.get("display_name") or "").strip()
    return {
        "access_token": access_token,
        "refresh_token": refresh_token_raw,
        "token_type": "bearer",
        "expires_in": 15 * 60,  # seconds
        "user": {
            "id": user_id,
            "email": user["email"],
            "first_name": user["first_name"],
            "last_name": user["last_name"],
            "display_name": display_name,
            "effective_display_name": effective_display_name(
                display_name=display_name,
                first_name=user.get("first_name"),
                last_name=user.get("last_name"),
            ),
            "profile_image": public_profile_image_url(user.get("profile_image")),
            "phone": str(user.get("phone") or ""),
            "role": role_name,
            "company_id": company_id,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Register
# ─────────────────────────────────────────────────────────────────────────────

def register_user(
    *,
    first_name: str,
    last_name: str,
    email: str,
    username: str,
    password: str,
    role_name: str,
    company_id: str | None = None,
    admin_id: str | None = None,
    created_by: str | None = None,
    is_verified: bool = True,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    """Create a new user. Validates uniqueness and password policy."""
    valid, errors = validate_password_policy(password)
    if not valid:
        raise AuthError(ERR_WEAK_PASSWORD, "; ".join(errors), 422)

    with db_cursor(commit=True) as cur:
        # Check email uniqueness
        cur.execute("SELECT 1 FROM users WHERE LOWER(email) = %s AND deleted_at IS NULL", (email.lower(),))
        if cur.fetchone():
            raise AuthError(ERR_DUPLICATE_EMAIL, "A user with this email already exists.", 409)

        # Check username uniqueness
        cur.execute("SELECT 1 FROM users WHERE LOWER(username) = %s AND deleted_at IS NULL", (username.lower(),))
        if cur.fetchone():
            raise AuthError(ERR_DUPLICATE_USERNAME, "A user with this username already exists.", 409)

        role_id = _get_role_id_by_name(cur, role_name)
        if not role_id:
            raise AuthError("INVALID_ROLE", f"Role '{role_name}' does not exist.", 400)

        now = datetime.now(timezone.utc)
        user_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO users (
                id, first_name, last_name, email, username, password_hash,
                role_id, company_id, admin_id, is_active, is_verified,
                created_by, created_at, updated_at, last_password_change
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                user_id, first_name.strip(), last_name.strip(),
                email.strip().lower(), username.strip().lower(),
                hash_password(password),
                role_id, company_id, admin_id,
                True, is_verified,
                created_by, now, now, now,
            ),
        )

    audit_service.log_action(
        created_by or user_id, AUDIT_USER_CREATED, ip=ip, user_agent=user_agent,
        new_value={"user_id": user_id, "email": email, "role": role_name},
    )

    # Send welcome email (non-blocking)
    full_name = f"{first_name} {last_name}".strip()
    try:
        send_welcome_email(email, full_name)
    except Exception:
        pass

    return {
        "id": user_id,
        "email": email,
        "username": username,
        "role": role_name,
        "company_id": company_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Refresh tokens
# ─────────────────────────────────────────────────────────────────────────────

def refresh_tokens(
    refresh_token_raw: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    """Rotate refresh token and issue a new access token."""
    payload = verify_refresh_token(refresh_token_raw)
    if not payload:
        raise AuthError(ERR_TOKEN_INVALID, "Invalid or expired refresh token.", 401)

    session_id = payload.get("session_id")
    if not session_id:
        raise AuthError(ERR_TOKEN_INVALID, "Refresh token missing session_id.", 401)

    # Find stored token
    stored = token_service.find_refresh_token(refresh_token_raw)
    if not stored:
        raise AuthError(ERR_TOKEN_INVALID, "Refresh token not found or revoked.", 401)

    user_id = str(stored["user_id"])
    if str(payload.get("sub")) != user_id:
        raise AuthError(ERR_TOKEN_INVALID, "Refresh token subject mismatch.", 401)

    info = _client_info(ip, user_agent)

    # Validate session before rotation (idle + absolute timeout)
    if not session_service.validate_session(session_id, user_id, touch=False):
        token_service.revoke_refresh_token(str(stored["id"]))
        raise AuthError(ERR_TOKEN_EXPIRED, "Session expired or invalid. Please log in again.", 401)

    # Fetch user for role info
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.id, u.email, u.is_active, u.deleted_at, r.name AS role_name, u.company_id
            FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s
            """,
            (user_id,),
        )
        user = cur.fetchone()

    if not user or user.get("deleted_at"):
        raise AuthError(ERR_TOKEN_INVALID, "User no longer exists.", 401)
    if not user["is_active"]:
        raise AuthError(ERR_ACCOUNT_INACTIVE, "Account is deactivated.", 403)

    user = dict(user)
    role_name = user["role_name"]
    company_id = str(user["company_id"]) if user.get("company_id") else None

    # Revoke old token (rotation)
    token_service.revoke_refresh_token(str(stored["id"]))

    # Issue new tokens (same session_id)
    new_access = create_access_token(user_id, role_name, company_id, session_id=session_id)
    new_refresh_raw = create_refresh_token(user_id, session_id)
    new_token_id = token_service.store_refresh_token(
        user_id, new_refresh_raw, device=info["device"], browser=info["browser"], ip=ip,
    )

    # Update session activity + linked refresh token
    session_service.update_activity(session_id)
    with db_cursor() as cur:
        cur.execute("UPDATE sessions SET refresh_token_id = %s WHERE id = %s", (new_token_id, session_id))

    audit_service.log_action(user_id, AUDIT_TOKEN_REFRESHED, ip=ip, user_agent=user_agent)

    return {
        "access_token": new_access,
        "refresh_token": new_refresh_raw,
        "token_type": "bearer",
        "expires_in": 15 * 60,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Logout
# ─────────────────────────────────────────────────────────────────────────────

def logout(
    refresh_token_raw: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, str]:
    """Logout the current device — revoke refresh token + end session."""
    payload = verify_refresh_token(refresh_token_raw)
    session_id = payload.get("session_id") if payload else None

    token_service.revoke_token_by_raw(refresh_token_raw)
    if session_id:
        session_service.end_session(session_id)

    user_id = payload.get("sub") if payload else None
    audit_service.log_action(user_id, AUDIT_LOGOUT, ip=ip, user_agent=user_agent)
    return {"detail": "Logged out successfully."}


def logout_all(
    user_id: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    """Logout all devices — revoke all refresh tokens + end all sessions."""
    revoked = token_service.revoke_all_for_user(user_id)
    ended = session_service.end_all_sessions(user_id)
    audit_service.log_action(user_id, AUDIT_LOGOUT_ALL, ip=ip, user_agent=user_agent,
                             new_value={"tokens_revoked": revoked, "sessions_ended": ended})
    return {"detail": "All sessions ended.", "tokens_revoked": revoked, "sessions_ended": ended}


# ─────────────────────────────────────────────────────────────────────────────
# Forgot / Reset password
# ─────────────────────────────────────────────────────────────────────────────

def forgot_password(email: str, *, ip: str = "", user_agent: str = "") -> dict:
    """Generate and email a 6-digit OTP for password reset."""
    email = email.strip().lower()

    with db_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM users WHERE LOWER(email) = %s AND deleted_at IS NULL", (email,))
        user = cur.fetchone()

    if not user:
        # Don't reveal whether the email exists
        return {"detail": "If the email is registered, a reset code has been sent."}

    user_id = str(user["id"])
    otp = _generate_otp()
    token_hash = hashlib.sha256(otp.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_OTP_EXPIRE_MINUTES)

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO password_reset_tokens (user_id, token_hash, otp_code, expires_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, token_hash, otp, expires_at),
        )

    try:
        email_result = send_forgot_password_otp(email, otp)
        if not email_result.get("sent"):
            logger.error(
                "Password reset OTP email failed for %s: %s",
                email,
                email_result.get("error") or email_result.get("reason"),
            )
    except Exception:
        logger.exception("Password reset OTP email raised for %s", email)

    return {"detail": "If the email is registered, a reset code has been sent."}


def reset_password(
    email: str,
    otp: str,
    new_password: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, str]:
    """Verify OTP and update password."""
    valid, errors = validate_password_policy(new_password)
    if not valid:
        raise AuthError(ERR_WEAK_PASSWORD, "; ".join(errors), 422)

    email = email.strip().lower()
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()

    with db_cursor(commit=True) as cur:
        # Find valid OTP
        cur.execute(
            """
            SELECT prt.id, prt.user_id, prt.expires_at, prt.used_at
            FROM password_reset_tokens prt
            JOIN users u ON u.id = prt.user_id
            WHERE LOWER(u.email) = %s
              AND prt.token_hash = %s
              AND prt.used_at IS NULL
              AND prt.expires_at > %s
            ORDER BY prt.created_at DESC
            LIMIT 1
            """,
            (email, otp_hash, datetime.now(timezone.utc)),
        )
        token_row = cur.fetchone()
        if not token_row:
            raise AuthError(ERR_TOKEN_INVALID, "Invalid or expired OTP.", 400)

        token_row = dict(token_row)
        user_id = str(token_row["user_id"])

        # Mark OTP used
        cur.execute(
            "UPDATE password_reset_tokens SET used_at = %s WHERE id = %s",
            (datetime.now(timezone.utc), token_row["id"]),
        )

        # Update password
        cur.execute(
            "UPDATE users SET password_hash = %s, last_password_change = %s WHERE id = %s",
            (hash_password(new_password), datetime.now(timezone.utc), user_id),
        )

    # Revoke all existing tokens and end sessions after password reset.
    token_service.revoke_all_for_user(user_id)
    session_service.end_all_sessions(user_id)

    audit_service.log_action(user_id, AUDIT_PASSWORD_RESET, ip=ip, user_agent=user_agent)
    return {"detail": "Password reset successfully. Please log in again."}


# ─────────────────────────────────────────────────────────────────────────────
# Email verification
# ─────────────────────────────────────────────────────────────────────────────

def verify_email(token: str) -> dict[str, str]:
    """Verify a user's email via the verification token."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            SELECT id, user_id, expires_at, used_at
            FROM email_verification_tokens
            WHERE token_hash = %s AND used_at IS NULL AND expires_at > %s
            """,
            (token_hash, datetime.now(timezone.utc)),
        )
        row = cur.fetchone()
        if not row:
            raise AuthError(ERR_TOKEN_INVALID, "Invalid or expired verification token.", 400)

        row = dict(row)
        user_id = str(row["user_id"])

        cur.execute(
            "UPDATE email_verification_tokens SET used_at = %s WHERE id = %s",
            (datetime.now(timezone.utc), row["id"]),
        )
        cur.execute("UPDATE users SET is_verified = TRUE, updated_at = %s WHERE id = %s",
                    (datetime.now(timezone.utc), user_id))

    return {"detail": "Email verified successfully."}


def request_email_change(
    user_id: str,
    new_email: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict:
    """Request an email change — sends a verification link to the new address."""
    new_email = new_email.strip().lower()

    with db_cursor(commit=False) as cur:
        cur.execute("SELECT 1 FROM users WHERE LOWER(email) = %s AND deleted_at IS NULL AND id != %s",
                    (new_email, user_id))
        if cur.fetchone():
            raise AuthError(ERR_DUPLICATE_EMAIL, "This email is already in use.", 409)

    token = str(uuid.uuid4())
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_verification_tokens (user_id, token_hash, new_email, expires_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, token_hash, new_email, expires_at),
        )

    try:
        send_email_change_verification(new_email, token)
    except Exception:
        pass

    audit_service.log_action(user_id, AUDIT_EMAIL_CHANGE_REQUESTED, ip=ip, user_agent=user_agent,
                             new_value={"new_email": new_email})
    return {"detail": f"Verification email sent to {new_email}."}


# ─────────────────────────────────────────────────────────────────────────────
# Change password (authenticated user)
# ─────────────────────────────────────────────────────────────────────────────

def change_password(
    user_id: str,
    current_password: str,
    new_password: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, str]:
    valid, errors = validate_password_policy(new_password)
    if not valid:
        raise AuthError(ERR_WEAK_PASSWORD, "; ".join(errors), 422)

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT password_hash FROM users WHERE id = %s AND deleted_at IS NULL", (user_id,))
        user = cur.fetchone()
        if not user:
            raise AuthError(ERR_INVALID_CREDENTIALS, "User not found.", 404)

        if not verify_password(current_password, dict(user)["password_hash"]):
            raise AuthError(ERR_INVALID_CREDENTIALS, "Current password is incorrect.", 401)

        cur.execute(
            "UPDATE users SET password_hash = %s, last_password_change = %s, updated_at = %s WHERE id = %s",
            (hash_password(new_password), datetime.now(timezone.utc), datetime.now(timezone.utc), user_id),
        )

    # Force re-login on every device after a password change.
    token_service.revoke_all_for_user(user_id)
    session_service.end_all_sessions(user_id)

    audit_service.log_action(user_id, AUDIT_PASSWORD_CHANGE, ip=ip, user_agent=user_agent)
    return {"detail": "Password changed successfully. Please log in again."}
