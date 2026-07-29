"""Invitation-based user onboarding — SuperAdmin invites Admins/Users, Admins invite Users."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from auth import audit_service, session_service, token_service
from auth.constants import (
    AUDIT_ACCOUNT_DISABLED,
    AUDIT_ACCOUNT_ENABLED,
    AUDIT_INVITE_ACCEPTED,
    AUDIT_INVITE_RESENT,
    AUDIT_INVITE_REVOKED,
    AUDIT_INVITE_SENT,
    INVITATION_EXPIRE_HOURS,
    INVITATION_RATE_LIMIT_PER_HOUR,
    ROLE_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_USER,
)
from auth.email_service import send_invitation_email
from auth.password_utils import hash_password, validate_password_policy
from db.pool import db_cursor

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"

# Simple in-memory rate limit: inviter_id → list of timestamps
_invite_rate: dict[str, list[float]] = {}


class InvitationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _check_rate_limit(inviter_id: str) -> None:
    now = time.time()
    window = 3600.0
    stamps = [t for t in _invite_rate.get(inviter_id, []) if now - t < window]
    if len(stamps) >= INVITATION_RATE_LIMIT_PER_HOUR:
        raise InvitationError(
            "RATE_LIMITED",
            f"Too many invitations. Limit is {INVITATION_RATE_LIMIT_PER_HOUR} per hour.",
            429,
        )
    stamps.append(now)
    _invite_rate[inviter_id] = stamps


def _serialize_invite(row: dict) -> dict[str, Any]:
    out = dict(row)
    for key in ("id", "company_id", "invited_by", "created_user_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("expires_at", "used_at", "revoked_at", "created_at", "updated_at"):
        if out.get(key) and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    out.pop("token_hash", None)
    return out


def _expire_stale(cur) -> None:
    cur.execute(
        """
        UPDATE invitations
        SET status = %s, updated_at = %s
        WHERE status = %s AND expires_at < %s
        """,
        (STATUS_EXPIRED, _now(), STATUS_PENDING, _now()),
    )


def create_invitation(
    *,
    email: str,
    role: str,
    invited_by: dict[str, Any],
    company_id: str | None = None,
    company_name: str = "",
    company_code: str = "",
    company_address: str = "",
    company_phone: str = "",
    company_email: str = "",
    company_website: str = "",
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    """Create a pending invitation and email the registration link."""
    email = email.strip().lower()
    role = role.strip().upper()
    inviter_id = str(invited_by["id"])
    inviter_role = invited_by.get("role", "")

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise InvitationError("INVALID_EMAIL", "A valid email address is required.", 422)

    if inviter_role == ROLE_SUPER_ADMIN:
        if role not in (ROLE_ADMIN, ROLE_USER):
            raise InvitationError(
                "FORBIDDEN",
                "SuperAdmin can invite Admins or Users.",
                403,
            )
        if role == ROLE_USER and not company_id:
            raise InvitationError(
                "NO_COMPANY",
                "Select a company when inviting a User.",
                400,
            )
    elif inviter_role == ROLE_ADMIN:
        if role != ROLE_USER:
            raise InvitationError("FORBIDDEN", "Admins can only invite Users.", 403)
        company_id = invited_by.get("company_id")
        if not company_id:
            raise InvitationError("NO_COMPANY", "Admin has no company assigned.", 400)
    else:
        raise InvitationError("FORBIDDEN", "You cannot send invitations.", 403)

    _check_rate_limit(inviter_id)

    with db_cursor(commit=True) as cur:
        _expire_stale(cur)

        # Already registered?
        cur.execute(
            "SELECT id FROM users WHERE LOWER(email) = %s AND deleted_at IS NULL",
            (email,),
        )
        if cur.fetchone():
            raise InvitationError("DUPLICATE_EMAIL", "This email is already registered.", 409)

        # Pending invite already exists?
        cur.execute(
            """
            SELECT id FROM invitations
            WHERE LOWER(email) = %s AND status = %s AND expires_at > %s
            """,
            (email, STATUS_PENDING, _now()),
        )
        if cur.fetchone():
            raise InvitationError(
                "PENDING_EXISTS",
                "A pending invitation already exists for this email. Resend or revoke it first.",
                409,
            )

        resolved_company_name = (company_name or "").strip()
        resolved_company_code = (company_code or "").strip()
        resolved_company_address = (company_address or "").strip()
        resolved_company_phone = (company_phone or "").strip()
        resolved_company_email = (company_email or "").strip()
        resolved_company_website = (company_website or "").strip()

        if role in (ROLE_ADMIN, ROLE_USER) and company_id:
            cur.execute(
                """
                SELECT id, admin_id, company_name, company_code, address, phone, email, website
                FROM companies WHERE id = %s
                """,
                (company_id,),
            )
            company_row = cur.fetchone()
            if not company_row:
                raise InvitationError("NOT_FOUND", "Company not found.", 404)
            if role == ROLE_USER and not company_row.get("admin_id"):
                raise InvitationError(
                    "NO_ADMIN",
                    "This company has no Admin yet. Invite an Admin first.",
                    400,
                )
            resolved_company_name = resolved_company_name or str(company_row.get("company_name") or "")
            resolved_company_code = resolved_company_code or str(company_row.get("company_code") or "")
            resolved_company_address = resolved_company_address or str(company_row.get("address") or "")
            resolved_company_phone = resolved_company_phone or str(company_row.get("phone") or "")
            resolved_company_email = resolved_company_email or str(company_row.get("email") or "")
            resolved_company_website = resolved_company_website or str(company_row.get("website") or "")

        # Prefer company contact email from invite payload; fall back later to invitee email
        raw_token = _generate_token()
        token_hash = _hash_token(raw_token)
        invite_id = str(uuid.uuid4())
        expires_at = _now() + timedelta(hours=INVITATION_EXPIRE_HOURS)

        cur.execute(
            """
            INSERT INTO invitations (
                id, email, role, company_id, company_name, company_code,
                company_address, company_phone, company_email, company_website,
                invited_by, token_hash, status, expires_at, created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                invite_id,
                email,
                role,
                company_id,
                resolved_company_name,
                resolved_company_code,
                resolved_company_address,
                resolved_company_phone,
                resolved_company_email,
                resolved_company_website,
                inviter_id,
                token_hash,
                STATUS_PENDING,
                expires_at,
                _now(),
                _now(),
            ),
        )

        cur.execute(
            """
            SELECT i.*,
                   COALESCE(u.first_name || ' ' || u.last_name, u.email) AS inviter_name
            FROM invitations i
            JOIN users u ON u.id = i.invited_by
            WHERE i.id = %s
            """,
            (invite_id,),
        )
        row = dict(cur.fetchone())

    inviter_name = str(row.get("inviter_name") or "NameCardScan").strip()
    org_name = (company_name or "").strip() or "NameCardScan"
    email_result = send_invitation_email(
        to_email=email,
        inviter_name=inviter_name,
        role=role,
        organization_name=org_name,
        raw_token=raw_token,
        expires_hours=INVITATION_EXPIRE_HOURS,
    )
    if not email_result.get("sent"):
        err = (
            email_result.get("error")
            or email_result.get("reason")
            or "Failed to send invitation email."
        )
        raise InvitationError(
            "EMAIL_SEND_FAILED",
            f"Invitation was created but the email could not be sent: {err}",
            502,
        )

    audit_service.log_action(
        inviter_id,
        AUDIT_INVITE_SENT,
        ip=ip,
        user_agent=user_agent,
        new_value={"invitation_id": invite_id, "email": email, "role": role},
    )

    result = _serialize_invite(row)
    result["detail"] = "Invitation sent."
    # Local/dev only: allow completing POST /api/invitations/accept without reading email.
    # Production never returns the raw token (email link only).
    if (os.getenv("APP_ENV") or "development").strip().lower() == "development":
        result["invite_token"] = raw_token
        try:
            from config.urls import get_frontend_base_url

            result["invite_url"] = f"{get_frontend_base_url()}/register?token={raw_token}"
        except RuntimeError:
            result["invite_url"] = f"/register?token={raw_token}"
    return result


def list_invitations(
    actor: dict[str, Any],
    *,
    status: str | None = None,
    page: int = 1,
    limit: int = 10,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    limit = min(200, max(1, int(limit or 10)))
    offset = (page - 1) * limit

    with db_cursor(commit=True) as cur:
        _expire_stale(cur)
        conditions = ["1=1"]
        params: list[Any] = []

        if actor.get("role") == ROLE_ADMIN:
            conditions.append("i.company_id = %s")
            params.append(actor.get("company_id"))
            conditions.append("i.role = %s")
            params.append(ROLE_USER)
        elif actor.get("role") != ROLE_SUPER_ADMIN:
            raise InvitationError("FORBIDDEN", "Forbidden.", 403)

        if status:
            conditions.append("i.status = %s")
            params.append(status.lower())

        where = " AND ".join(conditions)
        cur.execute(
            f"SELECT COUNT(*) AS count FROM invitations i WHERE {where}",
            params,
        )
        total = int(cur.fetchone()["count"])

        cur.execute(
            f"""
            SELECT i.*,
                   COALESCE(u.first_name || ' ' || u.last_name, u.email) AS inviter_name
            FROM invitations i
            JOIN users u ON u.id = i.invited_by
            WHERE {where}
            ORDER BY i.created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()

    return {
        "items": [_serialize_invite(dict(r)) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


def resend_invitation(
    invitation_id: str,
    actor: dict[str, Any],
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    _check_rate_limit(str(actor["id"]))
    with db_cursor(commit=True) as cur:
        _expire_stale(cur)
        cur.execute("SELECT * FROM invitations WHERE id = %s", (invitation_id,))
        row = cur.fetchone()
        if not row:
            raise InvitationError("NOT_FOUND", "Invitation not found.", 404)
        row = dict(row)

        if actor.get("role") == ROLE_ADMIN:
            if str(row.get("company_id") or "") != str(actor.get("company_id") or ""):
                raise InvitationError("FORBIDDEN", "Forbidden.", 403)
            if row.get("role") != ROLE_USER:
                raise InvitationError("FORBIDDEN", "Forbidden.", 403)
        elif actor.get("role") != ROLE_SUPER_ADMIN:
            raise InvitationError("FORBIDDEN", "Forbidden.", 403)

        if row["status"] != STATUS_PENDING:
            raise InvitationError("INVALID_STATUS", "Only pending invitations can be resent.", 400)

        raw_token = _generate_token()
        token_hash = _hash_token(raw_token)
        expires_at = _now() + timedelta(hours=INVITATION_EXPIRE_HOURS)
        cur.execute(
            """
            UPDATE invitations
            SET token_hash = %s, expires_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (token_hash, expires_at, _now(), invitation_id),
        )
        cur.execute(
            """
            SELECT COALESCE(first_name || ' ' || last_name, email) AS inviter_name
            FROM users WHERE id = %s
            """,
            (actor["id"],),
        )
        inviter = cur.fetchone()

    email_result = send_invitation_email(
        to_email=row["email"],
        inviter_name=str((inviter or {}).get("inviter_name") or "NameCardScan"),
        role=row["role"],
        organization_name=str(row.get("company_name") or "NameCardScan"),
        raw_token=raw_token,
        expires_hours=INVITATION_EXPIRE_HOURS,
    )
    if not email_result.get("sent"):
        err = (
            email_result.get("error")
            or email_result.get("reason")
            or "Failed to send invitation email."
        )
        raise InvitationError(
            "EMAIL_SEND_FAILED",
            f"Invitation email could not be sent: {err}",
            502,
        )
    audit_service.log_action(
        actor["id"],
        AUDIT_INVITE_RESENT,
        ip=ip,
        user_agent=user_agent,
        new_value={"invitation_id": invitation_id, "email": row["email"]},
    )
    out: dict[str, Any] = {"success": True, "detail": "Invitation resent."}
    if (os.getenv("APP_ENV") or "development").strip().lower() == "development":
        out["invite_token"] = raw_token
    return out


def revoke_invitation(
    invitation_id: str,
    actor: dict[str, Any],
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM invitations WHERE id = %s", (invitation_id,))
        row = cur.fetchone()
        if not row:
            raise InvitationError("NOT_FOUND", "Invitation not found.", 404)
        row = dict(row)

        if actor.get("role") == ROLE_ADMIN:
            if str(row.get("company_id") or "") != str(actor.get("company_id") or ""):
                raise InvitationError("FORBIDDEN", "Forbidden.", 403)
            if row.get("role") != ROLE_USER:
                raise InvitationError("FORBIDDEN", "Forbidden.", 403)
        elif actor.get("role") != ROLE_SUPER_ADMIN:
            raise InvitationError("FORBIDDEN", "Forbidden.", 403)
        if row["status"] != STATUS_PENDING:
            raise InvitationError("INVALID_STATUS", "Only pending invitations can be revoked.", 400)

        cur.execute(
            """
            UPDATE invitations
            SET status = %s, revoked_at = %s, updated_at = %s, token_hash = %s
            WHERE id = %s
            """,
            (STATUS_REVOKED, _now(), _now(), _hash_token(secrets.token_urlsafe(16)), invitation_id),
        )

    audit_service.log_action(
        actor["id"],
        AUDIT_INVITE_REVOKED,
        ip=ip,
        user_agent=user_agent,
        new_value={"invitation_id": invitation_id, "email": row["email"]},
    )
    return {"success": True, "detail": "Invitation revoked."}


def validate_invitation_token(raw_token: str) -> dict[str, Any]:
    token_hash = _hash_token(raw_token.strip())
    with db_cursor(commit=True) as cur:
        _expire_stale(cur)
        cur.execute("SELECT * FROM invitations WHERE token_hash = %s", (token_hash,))
        row = cur.fetchone()
        if not row:
            raise InvitationError("TOKEN_INVALID", "Invalid or unknown invitation link.", 400)
        row = dict(row)
        if row["status"] == STATUS_USED:
            raise InvitationError("TOKEN_USED", "This invitation has already been used.", 400)
        if row["status"] == STATUS_REVOKED:
            raise InvitationError("TOKEN_REVOKED", "This invitation has been revoked.", 400)
        if row["status"] == STATUS_EXPIRED or row["expires_at"] < _now():
            cur.execute(
                "UPDATE invitations SET status = %s, updated_at = %s WHERE id = %s",
                (STATUS_EXPIRED, _now(), row["id"]),
            )
            raise InvitationError("TOKEN_EXPIRED", "This invitation has expired.", 400)
        if row["status"] != STATUS_PENDING:
            raise InvitationError("TOKEN_INVALID", "Invitation is not available.", 400)

    needs_company = row["role"] == ROLE_ADMIN and not row.get("company_id")
    return {
        "valid": True,
        "email": row["email"],
        "role": row["role"],
        "company_id": str(row["company_id"]) if row.get("company_id") else None,
        "company_name": row.get("company_name") or "",
        "company_code": row.get("company_code") or "",
        "company_address": row.get("company_address") or "",
        "company_phone": row.get("company_phone") or "",
        "company_email": row.get("company_email") or "",
        "company_website": row.get("company_website") or "",
        "needs_company": needs_company,
        "expires_at": row["expires_at"].isoformat() if hasattr(row["expires_at"], "isoformat") else str(row["expires_at"]),
    }


def accept_invitation(
    *,
    raw_token: str,
    password: str,
    full_name: str = "",
    first_name: str = "",
    last_name: str = "",
    phone: str = "",
    username: str | None = None,
    designation: str = "",
    department: str = "",
    company_name: str = "",
    company_code: str = "",
    company_address: str = "",
    company_phone: str = "",
    company_email: str = "",
    company_website: str = "",
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    valid, errors = validate_password_policy(password)
    if not valid:
        raise InvitationError("WEAK_PASSWORD", "; ".join(errors), 422)

    full_name = full_name.strip()
    first_name = first_name.strip()
    last_name = last_name.strip()
    if full_name:
        name_parts = full_name.split(maxsplit=1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""
    phone = (phone or "").strip()
    designation = (designation or "").strip()[:255]
    department = (department or "").strip()[:255]
    if not first_name:
        raise InvitationError("INVALID_NAME", "Full name is required.", 422)

    token_hash = _hash_token(raw_token.strip())

    with db_cursor(commit=True) as cur:
        _expire_stale(cur)
        cur.execute("SELECT * FROM invitations WHERE token_hash = %s FOR UPDATE", (token_hash,))
        row = cur.fetchone()
        if not row:
            raise InvitationError("TOKEN_INVALID", "Invalid or unknown invitation link.", 400)
        row = dict(row)

        if row["status"] != STATUS_PENDING or row["expires_at"] < _now():
            raise InvitationError("TOKEN_INVALID", "Invitation is expired or unavailable.", 400)

        email = row["email"]
        role = row["role"]

        cur.execute("SELECT id FROM users WHERE LOWER(email) = %s AND deleted_at IS NULL", (email,))
        if cur.fetchone():
            raise InvitationError("DUPLICATE_EMAIL", "This email is already registered.", 409)

        # Username: prefer provided, else email local-part
        if username and username.strip():
            username = re.sub(r"[^a-zA-Z0-9_]", "", username.strip())[:40].lower()
            if len(username) < 3:
                raise InvitationError("INVALID_USERNAME", "Username must be at least 3 characters.", 422)
            cur.execute(
                "SELECT 1 FROM users WHERE LOWER(username) = %s AND deleted_at IS NULL",
                (username,),
            )
            if cur.fetchone():
                raise InvitationError("DUPLICATE_USERNAME", "Username is already taken.", 409)
        else:
            base_username = re.sub(r"[^a-z0-9_]", "", email.split("@")[0].lower())[:40] or "user"
            username = base_username
            suffix = 0
            while True:
                cur.execute(
                    "SELECT 1 FROM users WHERE LOWER(username) = %s AND deleted_at IS NULL",
                    (username,),
                )
                if not cur.fetchone():
                    break
                suffix += 1
                username = f"{base_username}{suffix}"

        company_id = str(row["company_id"]) if row.get("company_id") else None
        admin_id = None

        # Merge company fields: registration form overrides invitation defaults
        final_company_name = (company_name or row.get("company_name") or "").strip()
        final_company_code = (company_code or row.get("company_code") or "").strip()
        final_company_address = (company_address or row.get("company_address") or "").strip()
        final_company_phone = (company_phone or row.get("company_phone") or "").strip()
        final_company_email = (company_email or row.get("company_email") or "").strip() or email
        final_company_website = (company_website or row.get("company_website") or "").strip()

        if role == ROLE_ADMIN and not company_id:
            if not final_company_name:
                final_company_name = f"{first_name}'s Company"
            if not final_company_code:
                base = re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower())[:12] or "co"
                final_company_code = f"{base}-{secrets.token_hex(3)}"
            else:
                cur.execute(
                    "SELECT 1 FROM companies WHERE LOWER(company_code) = %s",
                    (final_company_code.lower(),),
                )
                if cur.fetchone():
                    raise InvitationError("DUPLICATE_COMPANY_CODE", "Company code already exists.", 409)

            company_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO companies (
                    id, company_name, company_code, address, phone, email, website,
                    status, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)
                """,
                (
                    company_id,
                    final_company_name,
                    final_company_code,
                    final_company_address,
                    final_company_phone,
                    final_company_email,
                    final_company_website,
                    _now(),
                    _now(),
                ),
            )
        elif role == ROLE_USER:
            company_id = str(row["company_id"]) if row.get("company_id") else None
            # Prefer the company's Admin; fall back to inviter when an Admin invited.
            admin_id = None
            if company_id:
                cur.execute(
                    "SELECT admin_id FROM companies WHERE id = %s",
                    (company_id,),
                )
                company_row = cur.fetchone()
                if company_row and company_row.get("admin_id"):
                    admin_id = str(company_row["admin_id"])
            if not admin_id:
                admin_id = str(row["invited_by"])

        cur.execute("SELECT id FROM roles WHERE name = %s", (role,))
        role_row = cur.fetchone()
        if not role_row:
            raise InvitationError("ROLE_MISSING", f"Role {role} is not configured.", 500)

        user_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO users (
                id, first_name, last_name, email, username, password_hash, phone,
                designation, department,
                role_id, company_id, admin_id, is_active, is_verified,
                created_by, created_at, updated_at, last_password_change
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,TRUE,%s,%s,%s,%s)
            """,
            (
                user_id,
                first_name,
                last_name,
                email,
                username,
                hash_password(password),
                phone,
                designation,
                department,
                role_row["id"],
                company_id,
                admin_id,
                str(row["invited_by"]),
                _now(),
                _now(),
                _now(),
            ),
        )

        if role == ROLE_ADMIN and company_id:
            cur.execute(
                "UPDATE companies SET admin_id = %s, updated_at = %s WHERE id = %s",
                (user_id, _now(), company_id),
            )

        # Invalidate invitation
        cur.execute(
            """
            UPDATE invitations
            SET status = %s, used_at = %s, updated_at = %s, created_user_id = %s,
                token_hash = %s
            WHERE id = %s
            """,
            (
                STATUS_USED,
                _now(),
                _now(),
                user_id,
                _hash_token(secrets.token_urlsafe(16)),
                str(row["id"]),
            ),
        )

    audit_service.log_action(
        user_id,
        AUDIT_INVITE_ACCEPTED,
        ip=ip,
        user_agent=user_agent,
        new_value={"invitation_id": str(row["id"]), "email": email, "role": role},
    )

    return {
        "success": True,
        "detail": "Registration complete. Please sign in.",
        "user": {
            "id": user_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "username": username,
            "designation": designation,
            "department": department,
            "role": role,
            "company_id": company_id,
            "company_name": final_company_name if role == ROLE_ADMIN else (row.get("company_name") or ""),
        },
    }


def set_user_active(
    *,
    target_user_id: str,
    is_active: bool,
    actor: dict[str, Any],
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    """Enable/disable a user. Disabling ends sessions and revokes refresh tokens."""
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            SELECT u.id, u.company_id, u.is_active, r.name AS role
            FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s AND u.deleted_at IS NULL
            """,
            (target_user_id,),
        )
        target = cur.fetchone()
        if not target:
            raise InvitationError("NOT_FOUND", "User not found.", 404)
        target = dict(target)

        if actor.get("role") == ROLE_ADMIN:
            if str(actor.get("company_id")) != str(target.get("company_id")):
                raise InvitationError("FORBIDDEN", "Forbidden.", 403)
            if target["role"] != ROLE_USER:
                raise InvitationError("FORBIDDEN", "Admins can only manage Users.", 403)
        elif actor.get("role") != ROLE_SUPER_ADMIN:
            raise InvitationError("FORBIDDEN", "Forbidden.", 403)

        if target["role"] == ROLE_SUPER_ADMIN:
            raise InvitationError("FORBIDDEN", "Cannot disable SuperAdmin.", 403)

        cur.execute(
            "UPDATE users SET is_active = %s, updated_at = %s WHERE id = %s",
            (is_active, _now(), target_user_id),
        )

    if not is_active:
        token_service.revoke_all_for_user(target_user_id)
        session_service.end_all_sessions(target_user_id)

    audit_service.log_action(
        actor["id"],
        AUDIT_ACCOUNT_ENABLED if is_active else AUDIT_ACCOUNT_DISABLED,
        ip=ip,
        user_agent=user_agent,
        new_value={"user_id": target_user_id, "is_active": is_active},
    )
    return {"success": True, "is_active": is_active}
