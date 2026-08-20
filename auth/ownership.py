"""Contact ownership helpers — prevent IDOR across roles/companies."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN, ROLE_USER


def _is_superadmin_owned_contact(contact: dict[str, Any]) -> bool:
    role = str(contact.get("created_by_role") or "").strip().upper()
    if role == ROLE_SUPER_ADMIN:
        return True
    owner_company = str(contact.get("owner_company_id") or contact.get("company_id") or "")
    return not owner_company and role not in (ROLE_ADMIN, ROLE_USER)


def user_can_access_contact(user: dict[str, Any], contact: dict[str, Any]) -> bool:
    """Return True when *user* may read/update the given contact payload."""
    role = user.get("role") or ""
    owner_id = str(contact.get("created_by_user_id") or "")
    user_id = str(user.get("id") or "")

    if role == ROLE_SUPER_ADMIN:
        # SuperAdmin may only see SuperAdmin-owned business data — not Admin/User tenants.
        if owner_id and owner_id == user_id:
            return True
        return _is_superadmin_owned_contact(contact)

    if role == ROLE_USER:
        return bool(owner_id) and owner_id == user_id

    if role == ROLE_ADMIN:
        owner_company = str(contact.get("owner_company_id") or contact.get("company_id") or "")
        user_company = str(user.get("company_id") or "")
        if user_company and owner_company:
            return owner_company == user_company
        # Fallback: allow if the admin created it
        return bool(owner_id) and owner_id == user_id

    return False


def require_contact_access(user: dict[str, Any], contact: dict[str, Any] | None) -> dict[str, Any]:
    """Raise 404 when missing or inaccessible (do not leak existence across tenants)."""
    if not contact or not user_can_access_contact(user, contact):
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact
