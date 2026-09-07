"""Resolve authenticated user from FastAPI request state (RBAC middleware)."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import Request

from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN, ROLE_USER

logger = logging.getLogger(__name__)


def get_request_app_user(request: Request) -> dict[str, Any] | None:
    """Return the authenticated user dict attached by RBACMiddleware.

    Returns None if the request is unauthenticated (e.g. auth disabled or public route).
    """
    user = getattr(request.state, "auth_user", None)
    if not isinstance(user, dict):
        return None
    user_id = str(user.get("id") or "").strip()
    email = str(user.get("email") or "").strip().lower()
    if not user_id and not email:
        return None
    return {
        "id": user_id,
        "email": email,
        "role": user.get("role", ""),
        "company_id": user.get("company_id"),
        "admin_id": user.get("admin_id"),
    }


def app_user_filter_kwargs(request: Request) -> dict[str, str | None]:
    """Keyword args for scoped contact queries — email/id of the authenticated user."""
    user = get_request_app_user(request)
    if not user:
        return {"filter_user_email": None, "filter_user_id": None}

    email = str(user.get("email") or "").strip().lower() or None
    user_id = str(user.get("id") or "").strip() or None
    return {"filter_user_email": email, "filter_user_id": user_id}


def get_request_app_user_for_sync(request: Request) -> dict[str, Any] | None:
    """Authenticated user stamped onto contacts on create/sync."""
    return get_request_app_user(request)


def _superadmin_env_email() -> str | None:
    email = (os.getenv("SUPERADMIN_EMAIL") or "").strip().lower()
    return email or None


def _lookup_user_email(user_id: str) -> str | None:
    """Fetch a user's email by id (for Admin parent of a scanning User)."""
    from db.pool import db_cursor

    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT email FROM users
                WHERE id = %s AND deleted_at IS NULL AND is_active = TRUE
                """,
                (user_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.error("Failed to look up receive parent email for %s: %s", user_id, exc)
        return None
    if not row:
        return None
    return str(row.get("email") or "").strip().lower() or None


def get_scanner_email_from_request(request: Request) -> str | None:
    """Logged-in scanner email (legacy helper). Prefer get_receive_email_from_request."""
    user = get_request_app_user(request)
    if not user:
        return None
    email = str(user.get("email") or "").strip().lower()
    return email or None


def get_receive_email_from_request(request: Request) -> str | None:
    """
    Who gets the scanned-details (receive) email for this scan.

    Hierarchy:
      SUPER_ADMIN → own login email, else SUPERADMIN_EMAIL (unchanged; never CMS)
      USER        → CMS Receive email for their Admin (required source when set),
                    else Admin account email, else SUPERADMIN_EMAIL
      ADMIN       → CMS Receive email for this Admin (required source when set),
                    else Admin account email, else SUPERADMIN_EMAIL
    """
    user = get_request_app_user(request)
    if not user:
        return None

    role = str(user.get("role") or "").strip().upper()

    # Super Admin: previous default only — never CMS Receive email.
    if role == ROLE_SUPER_ADMIN:
        own = str(user.get("email") or "").strip().lower()
        return own or _superadmin_env_email()

    from services.admin_env_service import get_cms_receive_email

    if role == ROLE_USER:
        admin_id = str(user.get("admin_id") or "").strip()
        if admin_id:
            # CMS Receive email is the source of truth for this company (not hardcoded).
            cms_receive = get_cms_receive_email(admin_id)
            if cms_receive:
                return cms_receive.lower()
            admin_email = _lookup_user_email(admin_id)
            if admin_email:
                return admin_email
        logger.warning(
            "User %s has no CMS/Admin receive email; falling back to SUPERADMIN_EMAIL",
            user.get("id"),
        )
        return _superadmin_env_email()

    if role == ROLE_ADMIN:
        admin_id = str(user.get("id") or "").strip()
        # CMS Receive email is the source of truth for this Admin's company.
        cms_receive = get_cms_receive_email(admin_id) if admin_id else None
        if cms_receive:
            return cms_receive.lower()
        # No CMS value yet → Admin's own account email (not a hardcoded inbox).
        own = str(user.get("email") or "").strip().lower()
        if own:
            return own
        return _superadmin_env_email()

    return _superadmin_env_email()
