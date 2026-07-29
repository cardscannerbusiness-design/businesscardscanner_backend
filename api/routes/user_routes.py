"""User management routes — CRUD for users within company scope."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.schemas import (
    AdminResetPasswordRequest,
    CreateUserRequest,
    UpdateUserRequest,
    UserStatusRequest,
)
from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN
from auth.dependencies import get_current_user, require_role
from auth.password_utils import hash_password, validate_password_policy
from db.pool import db_cursor

router = APIRouter(prefix="/api/users", tags=["Users"])
logger = logging.getLogger(__name__)


@router.get(
    "",
    summary="List users",
    description="SuperAdmin sees all users. Admin sees only own company's users.",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def list_users(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    user = get_current_user(request)
    role = user["role"]
    company_id = user.get("company_id")
    offset = (page - 1) * limit

    with db_cursor(commit=False) as cur:
        conditions = ["u.deleted_at IS NULL"]
        params: list = []

        if role == ROLE_SUPER_ADMIN:
            # The User Management screen manages subordinate accounts; the
            # system SuperAdmin account must not appear as a manageable user.
            conditions.append("r.name <> %s")
            params.append(ROLE_SUPER_ADMIN)
        else:
            conditions.append("u.company_id = %s")
            params.append(company_id)
            # Company Admins manage USER accounts only, not their own or other
            # ADMIN identities in the same company.
            conditions.append("r.name = 'USER'")

        where = " AND ".join(conditions)

        cur.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE {where}
            """,
            params,
        )
        total = cur.fetchone()["total"]

        cur.execute(
            f"""
            SELECT u.id, u.email, u.first_name, u.last_name, u.username, u.phone,
                   u.is_active, u.is_verified, u.company_id, u.admin_id,
                   u.last_login, u.created_at, u.updated_at, r.name AS role,
                   COALESCE(comp.company_name, '') AS company_name,
                   COALESCE(
                       NULLIF(TRIM(admin_u.first_name || ' ' || admin_u.last_name), ''),
                       NULLIF(TRIM(admin_u.email), ''),
                       ''
                   ) AS admin_name,
                   COALESCE(admin_u.email, '') AS admin_email,
                   CASE
                     WHEN r.name = 'ADMIN' THEN (
                       SELECT COUNT(*)::int
                       FROM users sub
                       JOIN roles sr ON sr.id = sub.role_id
                       WHERE sub.deleted_at IS NULL
                         AND sr.name = 'USER'
                         AND (
                           sub.admin_id = u.id
                           OR (sub.company_id IS NOT NULL AND sub.company_id = u.company_id)
                         )
                     )
                     ELSE 0
                   END AS user_count
            FROM users u
            JOIN roles r ON r.id = u.role_id
            LEFT JOIN companies comp ON comp.id = u.company_id
            LEFT JOIN users admin_u ON admin_u.id = u.admin_id AND admin_u.deleted_at IS NULL
            WHERE {where}
            ORDER BY
              CASE r.name WHEN 'ADMIN' THEN 0 WHEN 'USER' THEN 1 ELSE 2 END,
              comp.company_name ASC NULLS LAST,
              u.created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()

    items = []
    for row in rows:
        row = dict(row)
        for k in ("id", "company_id", "admin_id"):
            if row.get(k) is not None:
                row[k] = str(row[k])
        for k in ("last_login", "created_at", "updated_at"):
            if row.get(k) and hasattr(row[k], "isoformat"):
                row[k] = row[k].isoformat()
        items.append(row)

    return {"items": items, "total": total, "page": page, "limit": limit}


@router.post(
    "",
    summary="Create user (disabled — use invitations)",
    description="Direct password-based user creation is disabled. Use POST /api/invitations.",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def create_user(body: CreateUserRequest, request: Request):
    raise HTTPException(
        status_code=410,
        detail={
            "code": "USE_INVITATION",
            "message": "Direct account creation is disabled. Send an invitation via POST /api/invitations.",
        },
    )


@router.get(
    "/{user_id}",
    summary="Get user details",
    description="Self / own-company Admin / SuperAdmin can view.",
)
def get_user(user_id: str, request: Request):
    user = get_current_user(request)
    # Self-access
    if str(user["id"]) == user_id:
        pass
    elif user["role"] == ROLE_SUPER_ADMIN:
        pass
    elif user["role"] == ROLE_ADMIN:
        # Must be same company
        pass  # checked below
    else:
        raise HTTPException(status_code=403, detail="Forbidden.")

    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.id, u.email, u.first_name, u.last_name, u.username, u.phone,
                   u.is_active, u.is_verified, u.company_id, u.admin_id,
                   u.profile_image, u.last_login, u.last_password_change,
                   u.created_at, u.updated_at, u.deleted_at, r.name AS role
            FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s AND u.deleted_at IS NULL
            """,
            (user_id,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    row = dict(row)

    # Company isolation
    if user["role"] == ROLE_ADMIN and str(user.get("company_id")) != str(row.get("company_id")):
        raise HTTPException(status_code=403, detail="Forbidden.")

    for k in ("id", "company_id", "admin_id"):
        if row.get(k) is not None:
            row[k] = str(row[k])
    for k in ("last_login", "last_password_change", "created_at", "updated_at", "deleted_at"):
        if row.get(k) and hasattr(row[k], "isoformat"):
            row[k] = row[k].isoformat()
    return row


@router.put(
    "/{user_id}",
    summary="Update user",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def update_user(user_id: str, body: UpdateUserRequest, request: Request):
    user = get_current_user(request)

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT company_id FROM users WHERE id = %s AND deleted_at IS NULL", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
        target = dict(target)

        if user["role"] == ROLE_ADMIN and str(user.get("company_id")) != str(target.get("company_id")):
            raise HTTPException(status_code=403, detail="Forbidden.")

        updates = body.model_dump(exclude_none=True)
        if not updates:
            return {"success": True, "message": "No fields to update."}

        # Role name → role_id
        if "role" in updates:
            new_role = updates.pop("role")
            cur.execute("SELECT id FROM roles WHERE name = %s", (new_role,))
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=400, detail=f"Invalid role: {new_role}")
            updates["role_id"] = r["id"]

        updates["updated_at"] = datetime.now(timezone.utc)
        updates["updated_by"] = user["id"]

        set_parts = []
        params: list = []
        for col, val in updates.items():
            set_parts.append(f'"{col}" = %s')
            params.append(val)
        params.append(user_id)

        cur.execute(f"UPDATE users SET {', '.join(set_parts)} WHERE id = %s", params)

    return {"success": True, "message": "User updated."}


@router.delete(
    "/{user_id}",
    summary="Soft delete user",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def delete_user(user_id: str, request: Request):
    user = get_current_user(request)

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT company_id FROM users WHERE id = %s AND deleted_at IS NULL", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
        target = dict(target)

        if user["role"] == ROLE_ADMIN and str(user.get("company_id")) != str(target.get("company_id")):
            raise HTTPException(status_code=403, detail="Forbidden.")

        cur.execute("UPDATE users SET deleted_at = %s, is_active = FALSE WHERE id = %s",
                    (datetime.now(timezone.utc), user_id))

    return {"success": True, "message": "User deleted (soft)."}


@router.patch(
    "/{user_id}/status",
    summary="Activate or deactivate user",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def update_user_status(user_id: str, body: UserStatusRequest, request: Request):
    from auth.invitation_service import InvitationError, set_user_active

    user = get_current_user(request)
    meta = {
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
    }
    try:
        return set_user_active(
            target_user_id=user_id,
            is_active=body.is_active,
            actor=user,
            ip=meta["ip"],
            user_agent=meta["user_agent"],
        )
    except InvitationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.post(
    "/{user_id}/reset-password",
    summary="Admin reset user password",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def admin_reset_password(user_id: str, body: AdminResetPasswordRequest, request: Request):
    user = get_current_user(request)
    valid, errors = validate_password_policy(body.new_password)
    if not valid:
        raise HTTPException(status_code=422, detail=errors)

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT company_id FROM users WHERE id = %s AND deleted_at IS NULL", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
        target = dict(target)

        if user["role"] == ROLE_ADMIN and str(user.get("company_id")) != str(target.get("company_id")):
            raise HTTPException(status_code=403, detail="Forbidden.")

        cur.execute(
            "UPDATE users SET password_hash = %s, last_password_change = %s, updated_at = %s WHERE id = %s",
            (hash_password(body.new_password), datetime.now(timezone.utc), datetime.now(timezone.utc), user_id),
        )

    from auth.session_service import end_all_sessions
    from auth.token_service import revoke_all_for_user

    revoked = revoke_all_for_user(user_id)
    ended = end_all_sessions(user_id)

    return {
        "success": True,
        "message": "Password reset by admin. All sessions revoked.",
        "tokens_revoked": revoked,
        "sessions_ended": ended,
    }
