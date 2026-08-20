"""Soft-delete a company and drop it from CMS / auth surfaces."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db.pool import db_cursor


class CompanyNotFoundError(LookupError):
    pass


def soft_delete_company_cascade(company_id: str) -> dict[str, Any]:
    """Mark the company deleted, deactivate its users, and wipe CMS env + pending requests.

    SuperAdmin company delete and CMS client Remove both use this so the two UIs stay in sync.
    """
    now = datetime.now(timezone.utc)
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            SELECT id, company_name, company_code
            FROM companies
            WHERE id = %s AND status <> 'deleted'
            """,
            (company_id,),
        )
        company = cur.fetchone()
        if not company:
            raise CompanyNotFoundError("Company not found or already deleted.")
        company = dict(company)

        cur.execute(
            "SELECT id FROM users WHERE company_id = %s AND deleted_at IS NULL",
            (company_id,),
        )
        user_ids = [str(row["id"]) for row in (cur.fetchall() or [])]

        cur.execute(
            """
            UPDATE companies
            SET status = 'deleted', updated_at = %s
            WHERE id = %s AND status <> 'deleted'
            """,
            (now, company_id),
        )

        cur.execute(
            """
            UPDATE users
            SET deleted_at = %s, is_active = FALSE, updated_at = %s
            WHERE company_id = %s AND deleted_at IS NULL
            """,
            (now, now, company_id),
        )

        if user_ids:
            cur.execute(
                "DELETE FROM admin_env_settings WHERE admin_user_id = ANY(%s::uuid[])",
                (user_ids,),
            )

        cur.execute(
            """
            DELETE FROM admin_registration_requests
            WHERE created_company_id = %s
               OR (%s <> '' AND company_code = %s)
            """,
            (company_id, company.get("company_code") or "", company.get("company_code") or ""),
        )

        cur.execute(
            """
            UPDATE invitations
            SET status = 'revoked', revoked_at = %s, updated_at = %s
            WHERE company_id = %s AND status = 'pending'
            """,
            (now, now, company_id),
        )

    return {
        "company_id": str(company["id"]),
        "company_name": company.get("company_name") or "",
        "users_removed": len(user_ids),
    }


def remove_cms_client(admin_user_id: str) -> dict[str, Any]:
    """Remove a CMS client row by deleting its company (or the Admin user if unlinked)."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.id, u.company_id, r.name AS role
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s AND u.deleted_at IS NULL
            """,
            (admin_user_id,),
        )
        row = cur.fetchone()
    if not row or str(row.get("role") or "") != "ADMIN":
        raise CompanyNotFoundError("Client not found.")

    company_id = row.get("company_id")
    if company_id:
        return soft_delete_company_cascade(str(company_id))

    now = datetime.now(timezone.utc)
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE users
            SET deleted_at = %s, is_active = FALSE, updated_at = %s
            WHERE id = %s AND deleted_at IS NULL
            """,
            (now, now, admin_user_id),
        )
        cur.execute(
            "DELETE FROM admin_env_settings WHERE admin_user_id = %s",
            (admin_user_id,),
        )
    return {"company_id": None, "company_name": "", "users_removed": 1}
