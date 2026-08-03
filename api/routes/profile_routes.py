"""Profile routes — self-service for the authenticated user."""

from __future__ import annotations

import hashlib
import logging
import random
import string
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.schemas import (
    ChangeEmailRequest,
    ChangePasswordRequest,
    DeleteAccountRequest,
    UpdateProfileRequest,
)
from auth import audit_service
from auth.constants import AUDIT_ACCOUNT_DELETED
from auth.dependencies import get_current_user
from auth.email_service import (
    send_data_deletion_confirmation,
    send_mobile_verification_otp,
)
from auth.service import AuthError, change_password, request_email_change
from db.pool import db_cursor

router = APIRouter(prefix="/api/profile", tags=["Profile"])
logger = logging.getLogger(__name__)

# In-memory OTP store for mobile verification (user_id -> payload). No schema change.
_MOBILE_OTP: dict[str, dict] = {}
_MOBILE_OTP_TTL_SEC = 10 * 60


class MobileVerifySendBody(BaseModel):
    phone: str = Field(min_length=7, max_length=32)


class MobileVerifyConfirmBody(BaseModel):
    phone: str = Field(min_length=7, max_length=32)
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class DataDeletionNoticeBody(BaseModel):
    kind: str = Field(description="local_queue | organisation")


def _generate_otp(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


def _purge_mobile_otps() -> None:
    now = time.time()
    expired = [uid for uid, row in _MOBILE_OTP.items() if float(row.get("expires_at", 0)) <= now]
    for uid in expired:
        _MOBILE_OTP.pop(uid, None)


@router.get(
    "",
    summary="Get own profile",
    description="Returns the authenticated user's profile information.",
)
def get_profile(request: Request):
    user = get_current_user(request)
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.id, u.email, u.first_name, u.last_name, u.username, u.phone,
                   u.profile_image, u.is_active, u.is_verified, u.company_id,
                   u.last_login, u.last_password_change, u.created_at, u.updated_at,
                   r.name AS role
            FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s AND u.deleted_at IS NULL
            """,
            (user["id"],),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found.")

    row = dict(row)
    for k in ("id", "company_id"):
        if row.get(k) is not None:
            row[k] = str(row[k])
    for k in ("last_login", "last_password_change", "created_at", "updated_at"):
        if row.get(k) and hasattr(row[k], "isoformat"):
            row[k] = row[k].isoformat()
    return row


@router.put(
    "",
    summary="Update own profile",
    description="Update first name, last name, and phone number.",
)
def update_profile(body: UpdateProfileRequest, request: Request):
    user = get_current_user(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"success": True, "message": "No fields to update."}

    updates["updated_at"] = datetime.now(timezone.utc)
    set_parts = []
    params: list = []
    for col, val in updates.items():
        set_parts.append(f'"{col}" = %s')
        params.append(val)
    params.append(user["id"])

    with db_cursor() as cur:
        cur.execute(f"UPDATE users SET {', '.join(set_parts)} WHERE id = %s AND deleted_at IS NULL", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Profile not found.")

    return {"success": True, "message": "Profile updated."}


@router.post(
    "/change-password",
    summary="Change own password",
    description="Requires current password. Validates new password against enterprise policy.",
)
def change_password_route(body: ChangePasswordRequest, request: Request):
    user = get_current_user(request)
    meta = {"ip": request.client.host if request.client else "", "user_agent": request.headers.get("user-agent", "")}
    try:
        return change_password(
            user["id"], body.current_password, body.new_password,
            ip=meta["ip"], user_agent=meta["user_agent"],
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post(
    "/change-email",
    summary="Request email change",
    description="Sends a verification link to the new email address. Email is updated after verification.",
)
def change_email_route(body: ChangeEmailRequest, request: Request):
    user = get_current_user(request)
    meta = {"ip": request.client.host if request.client else "", "user_agent": request.headers.get("user-agent", "")}
    try:
        return request_email_change(user["id"], body.new_email, ip=meta["ip"], user_agent=meta["user_agent"])
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post(
    "/mobile-verify/send-otp",
    summary="Send mobile verification OTP",
    description="Emails a 6-digit OTP (same UX as password-reset OTP) to verify the user's mobile after Freemium expiry.",
)
def send_mobile_verify_otp(body: MobileVerifySendBody, request: Request):
    user = get_current_user(request)
    phone = "".join(c for c in body.phone.strip() if c.isdigit() or c == "+")
    if len("".join(c for c in phone if c.isdigit())) < 7:
        raise HTTPException(status_code=400, detail="Enter a valid mobile number.")

    email = str(user.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="No registered email on your account.")

    _purge_mobile_otps()
    otp = _generate_otp()
    user_id = str(user["id"])
    _MOBILE_OTP[user_id] = {
        "phone": phone,
        "otp_hash": hashlib.sha256(otp.encode()).hexdigest(),
        "expires_at": time.time() + _MOBILE_OTP_TTL_SEC,
    }

    result = send_mobile_verification_otp(email, otp, phone)
    if not result.get("sent"):
        logger.warning("Mobile OTP email failed for %s: %s", email, result)
    return {"success": True, "message": "Verification code sent to your registered email."}


@router.post(
    "/mobile-verify/confirm",
    summary="Confirm mobile verification OTP",
)
def confirm_mobile_verify_otp(body: MobileVerifyConfirmBody, request: Request):
    user = get_current_user(request)
    user_id = str(user["id"])
    phone = "".join(c for c in body.phone.strip() if c.isdigit() or c == "+")
    _purge_mobile_otps()
    entry = _MOBILE_OTP.get(user_id)
    if not entry or entry.get("phone") != phone:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")
    expected = entry.get("otp_hash")
    actual = hashlib.sha256(body.otp.strip().encode()).hexdigest()
    if actual != expected:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")
    _MOBILE_OTP.pop(user_id, None)

    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE users
            SET phone = %s, updated_at = %s
            WHERE id = %s AND deleted_at IS NULL
            """,
            (phone, datetime.now(timezone.utc), user_id),
        )

    return {"success": True, "message": "Mobile number verified.", "phone": phone}


@router.post(
    "/data-deletion-notice",
    summary="Email confirmation after local or organisation data deletion",
)
def data_deletion_notice(body: DataDeletionNoticeBody, request: Request):
    user = get_current_user(request)
    kind = (body.kind or "").strip().lower()
    if kind not in ("local_queue", "organisation"):
        raise HTTPException(status_code=400, detail="kind must be local_queue or organisation.")
    email = str(user.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="No registered email on your account.")
    result = send_data_deletion_confirmation(
        email,
        "organisation" if kind == "organisation" else "local_queue",
    )
    return {
        "success": True,
        "sent": bool(result.get("sent")),
        "message": (
            "Confirmation email requested."
            if result.get("sent")
            else "Deletion recorded; email could not be sent."
        ),
    }


@router.delete(
    "/account",
    summary="Delete own account",
    description=(
        "Soft-deletes the authenticated user's account, revokes all sessions/refresh tokens, "
        "and prevents further login."
    ),
)
def delete_own_account(body: DeleteAccountRequest, request: Request):
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Set confirm=true to delete your account.")

    user = get_current_user(request)

    user_id = user["id"]
    now = datetime.now(timezone.utc)
    meta = {
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
    }

    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE users
            SET deleted_at = %s, is_active = FALSE, updated_at = %s
            WHERE id = %s AND deleted_at IS NULL
            RETURNING id, email
            """,
            (now, now, user_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found.")

        # Revoke active sessions and refresh tokens so the account cannot linger.
        cur.execute(
            "UPDATE refresh_tokens SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL",
            (now, user_id),
        )
        cur.execute(
            "UPDATE sessions SET status = 'ended' WHERE user_id = %s AND status = 'active'",
            (user_id,),
        )

    audit_service.log_action(
        user_id,
        AUDIT_ACCOUNT_DELETED,
        ip=meta["ip"],
        user_agent=meta["user_agent"],
        new_value={"email": row["email"]},
    )
    logger.info("User self-deleted account %s", user_id)
    return {"success": True, "message": "Account deleted."}
