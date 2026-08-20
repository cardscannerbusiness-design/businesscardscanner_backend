"""Admin self-registration requests — SuperAdmin approve / reject.

Kept separate from invitation_service so the existing invite flow is untouched.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from auth import audit_service

from auth.constants import (
    AUDIT_ADMIN_REG_APPROVED,
    AUDIT_ADMIN_REG_DELETED,
    AUDIT_ADMIN_REG_REJECTED,
    AUDIT_ADMIN_REG_SUBMITTED,
    ERR_ACCOUNT_PENDING_APPROVAL,
    ERR_ACCOUNT_REGISTRATION_REJECTED,
    ERR_COMPANY_INACTIVE,
    ERR_DUPLICATE_EMAIL,
    ERR_WEAK_PASSWORD,
    REGISTRATION_RATE_LIMIT_PER_HOUR,
    ROLE_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_USER,
)
from auth.email_service import (
    send_registration_approved_email,
    send_registration_received_email,
    send_registration_rejected_email,
)
from auth.password_utils import hash_password, validate_password_policy, verify_password
from db.pool import db_cursor
from services.storage_service import DEFAULT_PLAN_NAME, DEFAULT_STORAGE_LIMIT_BYTES

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

MSG_PENDING_APPROVAL = (
    "Your account is pending SuperAdmin approval. You will be able to sign in once it is approved."
)
MSG_REGISTRATION_REJECTED = (
    "Your NameCardScan registration request was rejected. You cannot sign in with this account."
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_rate_by_ip: dict[str, list[float]] = {}


class RegistrationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_rate_limit(ip: str) -> None:
    key = (ip or "unknown").strip() or "unknown"
    now = time.time()
    stamps = [t for t in _rate_by_ip.get(key, []) if now - t < 3600.0]
    if len(stamps) >= REGISTRATION_RATE_LIMIT_PER_HOUR:
        raise RegistrationError(
            "RATE_LIMITED",
            f"Too many registration attempts. Limit is {REGISTRATION_RATE_LIMIT_PER_HOUR} per hour.",
            429,
        )
    stamps.append(now)
    _rate_by_ip[key] = stamps


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(maxsplit=1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def _serialize(row: dict) -> dict[str, Any]:
    out = dict(row)
    out.pop("password_hash", None)
    for key in ("id", "reviewed_by", "created_user_id", "created_company_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at", "reviewed_at"):
        if out.get(key) and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    return out


def _unique_username(cur, email: str, requested: str = "") -> str:
    if requested and requested.strip():
        username = re.sub(r"[^a-zA-Z0-9_]", "", requested.strip())[:40].lower()
        if len(username) < 3:
            raise RegistrationError("INVALID_USERNAME", "Username must be at least 3 characters.", 422)
        cur.execute(
            "SELECT 1 FROM users WHERE LOWER(username) = %s AND deleted_at IS NULL",
            (username,),
        )
        if cur.fetchone():
            raise RegistrationError("DUPLICATE_USERNAME", "Username is already taken.", 409)
        return username

    base = re.sub(r"[^a-z0-9_]", "", email.split("@")[0].lower())[:40] or "admin"
    username = base
    suffix = 0
    while True:
        cur.execute(
            "SELECT 1 FROM users WHERE LOWER(username) = %s AND deleted_at IS NULL",
            (username,),
        )
        if not cur.fetchone():
            return username
        suffix += 1
        username = f"{base}{suffix}"


def _unique_company_code(cur, email: str, requested: str = "") -> str:
    code = (requested or "").strip()
    if code:
        cur.execute("SELECT 1 FROM companies WHERE LOWER(company_code) = %s", (code.lower(),))
        if cur.fetchone():
            raise RegistrationError("DUPLICATE_COMPANY_CODE", "Company code already exists.", 409)
        return code
    base = re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower())[:12] or "co"
    while True:
        candidate = f"{base}-{secrets.token_hex(3)}"
        cur.execute("SELECT 1 FROM companies WHERE LOWER(company_code) = %s", (candidate.lower(),))
        if not cur.fetchone():
            return candidate


def _role_label(role: str) -> str:
    if role == ROLE_ADMIN:
        return "Admin"
    if role == ROLE_USER:
        return "User"
    return role or "Admin"


def _superadmin_emails(cur) -> list[str]:
    cur.execute(
        """
        SELECT u.email
        FROM users u
        JOIN roles r ON r.id = u.role_id
        WHERE r.name = %s
          AND u.deleted_at IS NULL
          AND u.is_active = TRUE
        """,
        (ROLE_SUPER_ADMIN,),
    )
    emails: list[str] = []
    for row in cur.fetchall() or []:
        email = str(row.get("email") or "").strip().lower()
        if email and email not in emails:
            emails.append(email)
    env_email = (os.getenv("SUPERADMIN_EMAIL") or "").strip().lower()
    if env_email and env_email not in emails:
        emails.append(env_email)
    return emails


def _resolve_existing_company(cur, *, company_code: str, company_name: str) -> dict:
    """Users must join an existing active company that already has an Admin."""
    row = None
    if company_code:
        cur.execute(
            """
            SELECT id, company_name, company_code, admin_id, status
            FROM companies
            WHERE LOWER(company_code) = %s
            """,
            (company_code.lower(),),
        )
        row = cur.fetchone()
        if not row:
            raise RegistrationError(
                "NOT_FOUND",
                "Company code not found. Ask your Admin for the correct company code.",
                404,
            )
    elif company_name:
        cur.execute(
            """
            SELECT id, company_name, company_code, admin_id, status
            FROM companies
            WHERE LOWER(company_name) = %s
            """,
            (company_name.lower(),),
        )
        matches = cur.fetchall() or []
        if not matches:
            raise RegistrationError(
                "NOT_FOUND",
                "Company not found. Use the company code from your Admin.",
                404,
            )
        if len(matches) > 1:
            raise RegistrationError(
                "AMBIGUOUS_COMPANY",
                "Multiple companies match that name. Use the company code.",
                400,
            )
        row = matches[0]
    else:
        raise RegistrationError(
            "INVALID_COMPANY",
            "Company code is required to join a workspace.",
            422,
        )

    row = dict(row)
    if str(row.get("status") or "active").lower() != "active":
        raise RegistrationError(ERR_COMPANY_INACTIVE, "This company account is inactive.", 403)
    if not row.get("admin_id"):
        raise RegistrationError(
            "NO_ADMIN",
            "This company has no Admin yet. An Admin must register and be approved first.",
            400,
        )
    return row


def _notify_superadmins(
    emails: list[str],
    *,
    applicant_name: str,
    applicant_email: str,
    company_name: str,
    role: str,
    phone: str = "",
    designation: str = "",
    company_code: str = "",
) -> None:
    if not emails:
        logger.warning("No SuperAdmin email found — registration request %s was not emailed.", applicant_email)
        return
    sent_any = False
    for sa_email in emails:
        try:
            result = send_registration_received_email(
                to_email=sa_email,
                applicant_name=applicant_name,
                applicant_email=applicant_email,
                company_name=company_name,
                role=role,
                phone=phone,
                designation=designation,
                company_code=company_code,
            )
            if result.get("sent"):
                sent_any = True
            else:
                logger.warning(
                    "SuperAdmin notify to %s did not send: %s",
                    sa_email,
                    result.get("error") or result.get("reason") or "unknown",
                )
        except Exception as exc:
            logger.warning("Could not notify SuperAdmin %s of registration: %s", sa_email, exc)
    if not sent_any:
        logger.error(
            "Registration %s saved but SuperAdmin was not emailed. Check SMTP settings.",
            applicant_email,
        )


def find_blocking_registration(identifier: str, password: str) -> dict[str, str] | None:
    """If identifier+password match a pending/rejected request, return status info."""
    ident = (identifier or "").strip().lower()
    if not ident or not password or not _EMAIL_RE.match(ident):
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT status, password_hash, rejection_reason
            FROM admin_registration_requests
            WHERE LOWER(email) = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ident,),
        )
        row = cur.fetchone()
    if not row:
        return None
    if not verify_password(password, str(row.get("password_hash") or "")):
        return None
    status = str(row.get("status") or "").lower()
    if status == STATUS_PENDING:
        return {"status": STATUS_PENDING, "message": MSG_PENDING_APPROVAL}
    if status == STATUS_REJECTED:
        reason = str(row.get("rejection_reason") or "").strip()
        message = MSG_REGISTRATION_REJECTED
        if reason:
            message = f"{MSG_REGISTRATION_REJECTED} Reason: {reason}"
        return {"status": STATUS_REJECTED, "message": message}
    return None


def create_admin_registration(
    *,
    full_name: str,
    email: str,
    password: str,
    phone: str = "",
    designation: str = "",
    department: str = "",
    company_name: str = "",
    company_code: str = "",
    company_address: str = "",
    company_phone: str = "",
    company_email: str = "",
    company_website: str = "",
    username: str = "",
    role: str = ROLE_ADMIN,
    phone_verification_token: str = "",
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    del phone_verification_token  # Admin signup no longer requires phone OTP.
    _check_rate_limit(ip)

    role = (role or ROLE_ADMIN).strip().upper()
    if role != ROLE_ADMIN:
        raise RegistrationError(
            "SIGNUP_CLOSED",
            "Self-registration is only available for Admin. Users join through an Admin invitation.",
            403,
        )

    email = (email or "").strip().lower()
    full_name = (full_name or "").strip()
    company_name = (company_name or "").strip()
    company_code = (company_code or "").strip()[:64]
    first_name, last_name = _split_name(full_name)

    if not _EMAIL_RE.match(email):
        raise RegistrationError("INVALID_EMAIL", "A valid email address is required.", 422)
    if not first_name:
        raise RegistrationError("INVALID_NAME", "Full name is required.", 422)
    if role == ROLE_ADMIN and not company_name:
        raise RegistrationError("INVALID_COMPANY", "Company name is required.", 422)
    if role == ROLE_USER and not company_code and not company_name:
        raise RegistrationError("INVALID_COMPANY", "Company code is required to join a workspace.", 422)

    valid, errors = validate_password_policy(password)
    if not valid:
        raise RegistrationError(ERR_WEAK_PASSWORD, "; ".join(errors), 422)

    phone = (phone or "").strip()[:64]
    if not phone:
        raise RegistrationError("INVALID_PHONE", "Mobile number is required.", 422)

    from auth.phone_otp_service import PhoneOtpError, assert_phone_available, normalize_phone

    try:
        phone_normalized = normalize_phone(phone)
        assert_phone_available(phone_normalized)
    except PhoneOtpError as exc:
        raise RegistrationError(exc.code, exc.message, exc.status_code) from exc

    designation = (designation or "").strip()[:255]
    department = (department or "").strip()[:255]
    company_address = (company_address or "").strip()
    company_phone = (company_phone or "").strip()[:64]
    company_email = (company_email or "").strip()[:255] or email
    company_website = (company_website or "").strip()[:255]
    username = (username or "").strip()
    requested_company_id: str | None = None

    with db_cursor(commit=True) as cur:
        if role == ROLE_USER:
            company = _resolve_existing_company(cur, company_code=company_code, company_name=company_name)
            requested_company_id = str(company["id"])
            company_name = str(company.get("company_name") or company_name)
            company_code = str(company.get("company_code") or company_code)

        cur.execute(
            "SELECT id FROM users WHERE LOWER(email) = %s AND deleted_at IS NULL",
            (email,),
        )
        if cur.fetchone():
            raise RegistrationError(ERR_DUPLICATE_EMAIL, "This email is already registered.", 409)

        cur.execute(
            """
            SELECT id FROM invitations
            WHERE LOWER(email) = %s AND status = 'pending' AND expires_at > %s
            """,
            (email, _now()),
        )
        if cur.fetchone():
            raise RegistrationError(
                "PENDING_INVITATION",
                "A pending invitation already exists for this email. Open the invitation link from your email instead.",
                409,
            )

        cur.execute(
            """
            SELECT id, status FROM admin_registration_requests
            WHERE LOWER(email) = %s
            ORDER BY created_at DESC
            """,
            (email,),
        )
        existing_rows = [dict(r) for r in (cur.fetchall() or [])]
        pending = next((r for r in existing_rows if r.get("status") == STATUS_PENDING), None)
        if pending:
            raise RegistrationError(
                "PENDING_EXISTS",
                "A registration request for this email is already pending SuperAdmin approval.",
                409,
            )
        approved = next((r for r in existing_rows if r.get("status") == STATUS_APPROVED), None)
        if approved:
            raise RegistrationError(ERR_DUPLICATE_EMAIL, "This email is already registered.", 409)

        password_hash = hash_password(password)
        now = _now()
        rejected = next((r for r in existing_rows if r.get("status") == STATUS_REJECTED), None)

        if rejected:
            request_id = str(rejected["id"])
            cur.execute(
                """
                UPDATE admin_registration_requests SET
                    first_name = %s, last_name = %s, email = %s, phone = %s,
                    designation = %s, department = %s, username = %s, password_hash = %s,
                    role = %s, company_name = %s, company_code = %s,
                    company_address = %s, company_phone = %s, company_email = %s,
                    company_website = %s, status = %s, rejection_reason = '',
                    reviewed_by = NULL, reviewed_at = NULL,
                    created_user_id = NULL, created_company_id = %s,
                    phone_normalized = %s, phone_verified_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    first_name, last_name, email, phone, designation, department,
                    username, password_hash, role, company_name, company_code,
                    company_address, company_phone, company_email, company_website,
                    STATUS_PENDING, requested_company_id, phone_normalized, None, now, request_id,
                ),
            )
        else:
            request_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO admin_registration_requests (
                    id, first_name, last_name, email, phone, designation, department,
                    username, password_hash, role, company_name, company_code,
                    company_address, company_phone, company_email, company_website,
                    status, created_company_id, phone_normalized, phone_verified_at,
                    created_at, updated_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                """,
                (
                    request_id, first_name, last_name, email, phone, designation, department,
                    username, password_hash, role, company_name, company_code,
                    company_address, company_phone, company_email, company_website,
                    STATUS_PENDING, requested_company_id, phone_normalized, None, now, now,
                ),
            )

        cur.execute("SELECT * FROM admin_registration_requests WHERE id = %s", (request_id,))
        row = dict(cur.fetchone())
        notify_emails = _superadmin_emails(cur)

    applicant_name = f"{first_name} {last_name}".strip()
    _notify_superadmins(
        notify_emails,
        applicant_name=applicant_name,
        applicant_email=email,
        company_name=company_name,
        role=role,
        phone=phone,
        designation=designation,
        company_code=company_code,
    )

    audit_service.log_action(
        None,
        AUDIT_ADMIN_REG_SUBMITTED,
        ip=ip,
        user_agent=user_agent,
        new_value={"request_id": request_id, "email": email, "company_name": company_name, "role": role},
    )

    result = _serialize(row)
    result["detail"] = (
        "Registration submitted successfully. Your account is pending SuperAdmin approval."
    )
    return result


def create_user_registration(**kwargs: Any) -> dict[str, Any]:
    kwargs["role"] = ROLE_USER
    return create_admin_registration(**kwargs)


def list_admin_registrations(
    *,
    status: str | None = None,
    page: int = 1,
    limit: int = 10,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    limit = min(200, max(1, int(limit or 10)))
    offset = (page - 1) * limit
    conditions = ["1=1"]
    params: list[Any] = []
    if status:
        conditions.append("status = %s")
        params.append(status.strip().lower())
    where = " AND ".join(conditions)

    with db_cursor(commit=False) as cur:
        cur.execute(
            f"SELECT COUNT(*) AS count FROM admin_registration_requests WHERE {where}",
            params,
        )
        total = int(cur.fetchone()["count"])
        cur.execute(
            f"""
            SELECT r.*,
                   COALESCE(u.first_name || ' ' || u.last_name, u.email, '') AS reviewer_name
            FROM admin_registration_requests r
            LEFT JOIN users u ON u.id = r.reviewed_by
            WHERE {where}
            ORDER BY
              CASE r.status WHEN 'pending' THEN 0 WHEN 'rejected' THEN 1 ELSE 2 END,
              r.created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall() or []

    return {
        "items": [_serialize(dict(r)) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


def approve_admin_registration(
    request_id: str,
    actor: dict[str, Any],
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    if actor.get("role") != ROLE_SUPER_ADMIN:
        raise RegistrationError("FORBIDDEN", "Only SuperAdmin can approve registration requests.", 403)

    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT * FROM admin_registration_requests WHERE id = %s FOR UPDATE",
            (request_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RegistrationError("NOT_FOUND", "Registration request not found.", 404)
        row = dict(row)
        status = str(row.get("status") or "")
        if status == STATUS_APPROVED:
            raise RegistrationError("INVALID_STATUS", "This registration request is already approved.", 400)
        if status != STATUS_PENDING:
            raise RegistrationError("INVALID_STATUS", "Only pending registration requests can be approved.", 400)

        email = str(row["email"]).strip().lower()
        cur.execute("SELECT id FROM users WHERE LOWER(email) = %s AND deleted_at IS NULL", (email,))
        if cur.fetchone():
            raise RegistrationError(ERR_DUPLICATE_EMAIL, "This email is already registered.", 409)

        first_name = str(row.get("first_name") or "")
        last_name = str(row.get("last_name") or "")
        phone = str(row.get("phone") or "")
        designation = str(row.get("designation") or "")
        department = str(row.get("department") or "")
        role = str(row.get("role") or ROLE_ADMIN).strip().upper()
        if role not in (ROLE_ADMIN, ROLE_USER):
            role = ROLE_ADMIN
        username = _unique_username(cur, email, str(row.get("username") or ""))

        cur.execute("SELECT id FROM roles WHERE name = %s", (role,))
        role_row = cur.fetchone()
        if not role_row:
            raise RegistrationError("ROLE_MISSING", f"Role {role} is not configured.", 500)

        now = _now()
        admin_id = None
        company_id: str | None = None
        company_code = str(row.get("company_code") or "")
        company_name = str(row.get("company_name") or "").strip()

        if role == ROLE_USER:
            existing = _resolve_existing_company(
                cur,
                company_code=company_code,
                company_name=company_name,
            )
            company_id = str(existing["id"])
            company_code = str(existing.get("company_code") or company_code)
            company_name = str(existing.get("company_name") or company_name)
            admin_id = str(existing["admin_id"])
        else:
            company_name = company_name or f"{first_name}'s Company"
            company_code = _unique_company_code(cur, email, company_code)
            company_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO companies (
                    id, company_name, company_code, address, phone, email, website,
                    status, plan_name, storage_limit_bytes, used_storage_bytes,
                    created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,0,%s,%s)
                """,
                (
                    company_id,
                    company_name,
                    company_code,
                    str(row.get("company_address") or ""),
                    str(row.get("company_phone") or ""),
                    str(row.get("company_email") or email),
                    str(row.get("company_website") or ""),
                    DEFAULT_PLAN_NAME,
                    DEFAULT_STORAGE_LIMIT_BYTES,
                    now,
                    now,
                ),
            )

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
                row["password_hash"],
                phone,
                designation,
                department,
                role_row["id"],
                company_id,
                admin_id,
                str(actor["id"]),
                now,
                now,
                now,
            ),
        )
        if role == ROLE_ADMIN and company_id:
            cur.execute(
                "UPDATE companies SET admin_id = %s, updated_at = %s WHERE id = %s",
                (user_id, now, company_id),
            )
        cur.execute(
            """
            UPDATE admin_registration_requests
            SET status = %s, reviewed_by = %s, reviewed_at = %s, updated_at = %s,
                created_user_id = %s, created_company_id = %s, company_code = %s
            WHERE id = %s
            """,
            (
                STATUS_APPROVED,
                str(actor["id"]),
                now,
                now,
                user_id,
                company_id,
                company_code,
                request_id,
            ),
        )

    audit_service.log_action(
        str(actor["id"]),
        AUDIT_ADMIN_REG_APPROVED,
        ip=ip,
        user_agent=user_agent,
        new_value={"request_id": request_id, "email": email, "user_id": user_id, "role": role},
    )

    try:
        send_registration_approved_email(
            to_email=email,
            full_name=f"{first_name} {last_name}".strip(),
            role=role,
        )
    except Exception as exc:
        logger.warning("Could not send approval email to %s: %s", email, exc)

    try:
        from services.google_sheets_service import fire_ensure_company_sheet

        fire_ensure_company_sheet(company_id)
    except Exception as exc:
        logger.warning("Could not schedule company Google Sheet for %s: %s", company_id, exc)

    return {
        "success": True,
        "detail": f"Registration approved. The {_role_label(role)} can now sign in.",
        "user_id": user_id,
        "company_id": company_id,
    }


def reject_admin_registration(
    request_id: str,
    actor: dict[str, Any],
    *,
    reason: str = "",
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    if actor.get("role") != ROLE_SUPER_ADMIN:
        raise RegistrationError("FORBIDDEN", "Only SuperAdmin can reject registration requests.", 403)

    reason = (reason or "").strip()[:1000]
    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT * FROM admin_registration_requests WHERE id = %s FOR UPDATE",
            (request_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RegistrationError("NOT_FOUND", "Registration request not found.", 404)
        row = dict(row)
        status = str(row.get("status") or "")
        if status == STATUS_APPROVED:
            raise RegistrationError("INVALID_STATUS", "An approved registration cannot be rejected.", 400)
        if status != STATUS_PENDING:
            raise RegistrationError("INVALID_STATUS", "Only pending registration requests can be rejected.", 400)

        now = _now()
        cur.execute(
            """
            UPDATE admin_registration_requests
            SET status = %s, rejection_reason = %s, reviewed_by = %s,
                reviewed_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (STATUS_REJECTED, reason, str(actor["id"]), now, now, request_id),
        )

    email = str(row["email"])
    audit_service.log_action(
        str(actor["id"]),
        AUDIT_ADMIN_REG_REJECTED,
        ip=ip,
        user_agent=user_agent,
        new_value={"request_id": request_id, "email": email, "reason": reason},
    )
    try:
        send_registration_rejected_email(
            to_email=email,
            full_name=f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip(),
            reason=reason,
            role=str(row.get("role") or ROLE_ADMIN),
        )
    except Exception as exc:
        logger.warning("Could not send rejection email to %s: %s", email, exc)

    return {"success": True, "detail": "Registration request rejected."}


def delete_admin_registration(
    request_id: str,
    actor: dict[str, Any],
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    """Permanently remove a registration request row. Does not delete created users."""
    if actor.get("role") != ROLE_SUPER_ADMIN:
        raise RegistrationError("FORBIDDEN", "Only SuperAdmin can delete registration requests.", 403)

    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT id, email, status FROM admin_registration_requests WHERE id = %s",
            (request_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RegistrationError("NOT_FOUND", "Registration request not found.", 404)
        row = dict(row)
        cur.execute("DELETE FROM admin_registration_requests WHERE id = %s", (request_id,))

    audit_service.log_action(
        str(actor["id"]),
        AUDIT_ADMIN_REG_DELETED,
        ip=ip,
        user_agent=user_agent,
        new_value={"request_id": request_id, "email": row.get("email"), "status": row.get("status")},
    )
    return {"success": True, "detail": "Registration request removed."}


def wipe_registration_requests() -> int:
    """Delete every self-registration request (used by SuperAdmin organisation wipe)."""
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM admin_registration_requests")
        return int(cur.rowcount or 0)
