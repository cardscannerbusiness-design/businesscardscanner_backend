"""Admin self-registration phone OTP — backend-verified, hashed, rate-limited.

Does not reuse the in-memory profile OTP store (authenticated-only, lost on restart).
Reuses the same 6-digit / 10-minute / SHA-256 pattern as password-reset OTP.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from auth.constants import PASSWORD_RESET_OTP_EXPIRE_MINUTES
from db.pool import db_cursor

logger = logging.getLogger(__name__)

PURPOSE_ADMIN_SIGNUP = "admin_signup"
OTP_EXPIRE_MINUTES = PASSWORD_RESET_OTP_EXPIRE_MINUTES
MAX_SENDS_PER_HOUR = int(os.getenv("PHONE_OTP_SENDS_PER_HOUR", "5"))
MAX_ATTEMPTS = int(os.getenv("PHONE_OTP_MAX_ATTEMPTS", "5"))
VERIFY_TOKEN_TTL_MINUTES = 30
_PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")


class PhoneOtpError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        raise PhoneOtpError("INVALID_PHONE", "Enter a valid mobile number.", 422)
    if len(digits) == 10 and digits[0] in "6789":
        digits = f"91{digits}"
    if len(digits) < 7 or len(digits) > 15:
        raise PhoneOtpError("INVALID_PHONE", "Enter a valid mobile number.", 422)
    return digits


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def assert_phone_available(phone: str) -> None:
    """Reject duplicate live users and pending registration phones."""
    normalized = normalize_phone(phone)
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT 1 FROM users
            WHERE deleted_at IS NULL
              AND phone <> ''
              AND regexp_replace(phone, '[^0-9]', '', 'g') IN (%s, %s)
            LIMIT 1
            """,
            (normalized, normalized[2:] if normalized.startswith("91") and len(normalized) == 12 else normalized),
        )
        if cur.fetchone():
            raise PhoneOtpError("DUPLICATE_PHONE", "This mobile number is already registered.", 409)
        cur.execute(
            """
            SELECT 1 FROM admin_registration_requests
            WHERE status = 'pending'
              AND phone_normalized = %s
            LIMIT 1
            """,
            (normalized,),
        )
        if cur.fetchone():
            raise PhoneOtpError(
                "PENDING_PHONE",
                "A registration request for this mobile number is already pending SuperAdmin approval.",
                409,
            )


def send_signup_otp(*, phone: str, email: str = "", ip: str = "") -> dict[str, Any]:
    normalized = normalize_phone(phone)
    email = (email or "").strip().lower()
    assert_phone_available(normalized)

    now = _now()
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM phone_verification_otps
            WHERE phone = %s AND purpose = %s AND created_at > %s
            """,
            (normalized, PURPOSE_ADMIN_SIGNUP, now - timedelta(hours=1)),
        )
        sent_hour = int(cur.fetchone()["n"] or 0)
        if sent_hour >= MAX_SENDS_PER_HOUR:
            raise PhoneOtpError(
                "RATE_LIMITED",
                f"Too many verification codes. Try again later (limit {MAX_SENDS_PER_HOUR} per hour).",
                429,
            )

        otp = _generate_otp()
        otp_id = str(uuid.uuid4())
        expires = now + timedelta(minutes=OTP_EXPIRE_MINUTES)
        cur.execute(
            """
            UPDATE phone_verification_otps
            SET expires_at = %s
            WHERE phone = %s AND purpose = %s AND verified_at IS NULL AND expires_at > %s
            """,
            (now, normalized, PURPOSE_ADMIN_SIGNUP, now),
        )
        cur.execute(
            """
            INSERT INTO phone_verification_otps (
                id, phone, email, purpose, otp_hash, expires_at, ip, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (otp_id, normalized, email, PURPOSE_ADMIN_SIGNUP, _hash(otp), expires, ip or "", now),
        )

    delivered_via = _deliver_otp(normalized, otp)
    logger.info("Signup phone OTP sent phone=%s via=%s", normalized[-4:].rjust(4, "*"), delivered_via)
    return {
        "success": True,
        "message": "Verification code sent by SMS to your mobile number.",
        "expires_in_minutes": OTP_EXPIRE_MINUTES,
        "delivered_via": delivered_via,
    }


def _deliver_otp(phone: str, otp: str) -> str:
    """SMS only. Does not use WhatsApp or Email."""
    from services.sms_otp_service import SmsOtpError, send_signup_otp_sms

    try:
        return send_signup_otp_sms(phone, otp)
    except SmsOtpError as exc:
        raise PhoneOtpError(exc.code, exc.message, exc.status_code) from exc


def confirm_signup_otp(*, phone: str, otp: str) -> dict[str, Any]:
    normalized = normalize_phone(phone)
    code = (otp or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise PhoneOtpError("INVALID_OTP", "Enter the 6-digit verification code.", 422)

    now = _now()
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            SELECT id, otp_hash, attempt_count, expires_at, verified_at
            FROM phone_verification_otps
            WHERE phone = %s AND purpose = %s
            ORDER BY created_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            (normalized, PURPOSE_ADMIN_SIGNUP),
        )
        row = cur.fetchone()
        if not row:
            raise PhoneOtpError("OTP_NOT_FOUND", "Request a new verification code first.", 400)
        row = dict(row)
        if row.get("verified_at"):
            token = secrets.token_urlsafe(32)
            cur.execute(
                """
                UPDATE phone_verification_otps
                SET verify_token_hash = %s, expires_at = %s
                WHERE id = %s
                """,
                (_hash(token), now + timedelta(minutes=VERIFY_TOKEN_TTL_MINUTES), row["id"]),
            )
            return {
                "success": True,
                "message": "Mobile number already verified.",
                "phone_verification_token": token,
                "phone": normalized,
            }
        if row["expires_at"] and row["expires_at"] < now:
            raise PhoneOtpError("OTP_EXPIRED", "This verification code has expired. Request a new one.", 400)
        attempts = int(row.get("attempt_count") or 0)
        if attempts >= MAX_ATTEMPTS:
            raise PhoneOtpError("TOO_MANY_ATTEMPTS", "Too many incorrect codes. Request a new one.", 429)

        if _hash(code) != str(row.get("otp_hash") or ""):
            cur.execute(
                "UPDATE phone_verification_otps SET attempt_count = attempt_count + 1 WHERE id = %s",
                (row["id"],),
            )
            raise PhoneOtpError("INVALID_OTP", "Incorrect verification code.", 400)

        token = secrets.token_urlsafe(32)
        cur.execute(
            """
            UPDATE phone_verification_otps
            SET verified_at = %s,
                verify_token_hash = %s,
                attempt_count = attempt_count + 1,
                expires_at = %s
            WHERE id = %s
            """,
            (now, _hash(token), now + timedelta(minutes=VERIFY_TOKEN_TTL_MINUTES), row["id"]),
        )

    return {
        "success": True,
        "message": "Mobile number verified.",
        "phone_verification_token": token,
        "phone": normalized,
    }


def require_verified_phone(*, phone: str, token: str) -> str:
    """Return normalized phone when the backend has verified this OTP. Never trust the client flag."""
    normalized = normalize_phone(phone)
    raw = (token or "").strip()
    if not raw:
        raise PhoneOtpError(
            "PHONE_NOT_VERIFIED",
            "Verify your mobile number before submitting registration.",
            400,
        )
    now = _now()
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id FROM phone_verification_otps
            WHERE phone = %s
              AND purpose = %s
              AND verify_token_hash = %s
              AND verified_at IS NOT NULL
              AND expires_at > %s
            ORDER BY verified_at DESC
            LIMIT 1
            """,
            (normalized, PURPOSE_ADMIN_SIGNUP, _hash(raw), now),
        )
        if not cur.fetchone():
            raise PhoneOtpError(
                "PHONE_NOT_VERIFIED",
                "Mobile verification expired or is invalid. Verify your number again.",
                400,
            )
    return normalized
