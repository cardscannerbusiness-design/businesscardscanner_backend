"""Company management routes — CRUD for companies (SuperAdmin only, with /me for any user)."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.schemas import CreateCompanyRequest, UpdateCompanyRequest
from auth.constants import (
    AUDIT_COMPANY_CREATED,
    AUDIT_COMPANY_DELETED,
    AUDIT_COMPANY_UPDATED,
    ROLE_ADMIN,
    ROLE_SUPER_ADMIN,
)
from auth.dependencies import get_current_user, require_role
from auth.invitation_service import InvitationError, create_invitation
from auth import audit_service
from db.pool import db_cursor

router = APIRouter(prefix="/api/companies", tags=["Companies"])
logger = logging.getLogger(__name__)


@router.get(
    "",
    summary="List all companies",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def list_companies(page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200)):
    offset = (page - 1) * limit
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT COUNT(*) AS total FROM companies")
        total = cur.fetchone()["total"]
        cur.execute(
            """
            SELECT c.id, c.company_name, c.company_code, c.admin_id, c.address, c.phone, c.email,
                   c.website, c.status, c.created_at, c.updated_at,
                   COALESCE(
                       NULLIF(TRIM(a.first_name || ' ' || a.last_name), ''),
                       NULLIF(TRIM(a.email), ''),
                       ''
                   ) AS admin_name,
                   COALESCE(a.email, '') AS admin_email,
                   COALESCE(a.username, '') AS admin_username,
                   COALESCE(NULLIF(TRIM(a.designation), ''), '') AS admin_designation,
                   COALESCE(NULLIF(TRIM(a.department), ''), '') AS admin_department,
                   (
                     SELECT COUNT(*)::int
                     FROM users u
                     JOIN roles r ON r.id = u.role_id
                     WHERE u.company_id = c.id
                       AND u.deleted_at IS NULL
                       AND r.name = 'USER'
                   ) AS user_count
            FROM companies c
            LEFT JOIN users a ON a.id = c.admin_id AND a.deleted_at IS NULL
            ORDER BY c.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()

    items = []
    for row in rows:
        row = dict(row)
        for k in ("id", "admin_id"):
            if row.get(k) is not None:
                row[k] = str(row[k])
        for k in ("created_at", "updated_at"):
            if row.get(k) and hasattr(row[k], "isoformat"):
                row[k] = row[k].isoformat()
        items.append(row)

    return {"items": items, "total": total, "page": page, "limit": limit}


@router.post(
    "",
    summary="Invite Admin for a new company",
    description=(
        "Stores company details on the invitation. The Admin registers via the invite link "
        "and sets their own password. Company row is created when the invitation is accepted."
    ),
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def create_company(body: CreateCompanyRequest, request: Request):
    user = get_current_user(request)
    meta = {
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
    }

    with db_cursor(commit=False) as cur:
        cur.execute("SELECT 1 FROM companies WHERE company_code = %s", (body.company_code,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="Company code already exists.")

    try:
        invitation = create_invitation(
            email=str(body.admin_email),
            role="ADMIN",
            invited_by=user,
            company_id=None,
            company_name=body.company_name,
            company_code=body.company_code,
            company_address=body.address,
            company_phone=body.phone,
            company_email=body.email,
            company_website=body.website,
            ip=meta["ip"],
            user_agent=meta["user_agent"],
        )
    except InvitationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    audit_service.log_action(
        user["id"],
        AUDIT_COMPANY_CREATED,
        ip=meta["ip"],
        user_agent=meta["user_agent"],
        new_value={
            "company_name": body.company_name,
            "company_code": body.company_code,
            "invitation_id": invitation.get("id"),
            "admin_email": str(body.admin_email),
        },
    )

    return {
        "success": True,
        "detail": "Admin invitation sent. Company will be created when the Admin registers.",
        "invitation": invitation,
        "company": {
            "company_name": body.company_name,
            "company_code": body.company_code,
            "pending": True,
        },
    }


@router.get(
    "/me",
    summary="Get own company",
    description="Returns the company the authenticated user belongs to.",
)
def get_my_company(request: Request):
    user = get_current_user(request)
    company_id = user.get("company_id")
    if not company_id:
        raise HTTPException(status_code=404, detail="You are not associated with any company.")

    with db_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Company not found.")

    row = dict(row)
    for k in ("id", "admin_id"):
        if row.get(k) is not None:
            row[k] = str(row[k])
    for k in ("created_at", "updated_at"):
        if row.get(k) and hasattr(row[k], "isoformat"):
            row[k] = row[k].isoformat()
    return row


@router.get(
    "/{company_id}",
    summary="Get company details",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def get_company(company_id: str, request: Request):
    user = get_current_user(request)
    if user["role"] == ROLE_ADMIN and str(user.get("company_id")) != company_id:
        raise HTTPException(status_code=403, detail="Forbidden.")

    with db_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Company not found.")

    row = dict(row)
    for k in ("id", "admin_id"):
        if row.get(k) is not None:
            row[k] = str(row[k])
    for k in ("created_at", "updated_at"):
        if row.get(k) and hasattr(row[k], "isoformat"):
            row[k] = row[k].isoformat()
    return row


@router.put(
    "/{company_id}",
    summary="Update company",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def update_company(company_id: str, body: UpdateCompanyRequest, request: Request):
    user = get_current_user(request)
    meta = {"ip": request.client.host if request.client else "", "user_agent": request.headers.get("user-agent", "")}

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"success": True, "message": "No fields to update."}

    updates["updated_at"] = datetime.now(timezone.utc)
    set_parts = []
    params: list = []
    for col, val in updates.items():
        set_parts.append(f"{col} = %s")
        params.append(val)
    params.append(company_id)

    with db_cursor() as cur:
        cur.execute(f"UPDATE companies SET {', '.join(set_parts)} WHERE id = %s", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Company not found.")

    audit_service.log_action(user["id"], AUDIT_COMPANY_UPDATED, ip=meta["ip"], user_agent=meta["user_agent"],
                             new_value={"company_id": company_id, "updates": list(updates.keys())})
    return {"success": True, "message": "Company updated."}


@router.delete(
    "/{company_id}",
    summary="Soft delete company",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def delete_company(company_id: str, request: Request):
    user = get_current_user(request)
    meta = {"ip": request.client.host if request.client else "", "user_agent": request.headers.get("user-agent", "")}

    with db_cursor() as cur:
        cur.execute("UPDATE companies SET status = 'deleted', updated_at = %s WHERE id = %s AND status != 'deleted'",
                    (datetime.now(timezone.utc), company_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Company not found or already deleted.")

    audit_service.log_action(user["id"], AUDIT_COMPANY_DELETED, ip=meta["ip"], user_agent=meta["user_agent"],
                             new_value={"company_id": company_id})
    return {"success": True, "message": "Company deleted (soft)."}
