"""Resolve per-Admin CMS env at scan/send time (fallback to global .env)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN, ROLE_USER
from db.pool import db_cursor

logger = logging.getLogger(__name__)

_runtime: ContextVar[dict[str, Any] | None] = ContextVar("admin_env_runtime", default=None)


def resolve_owner_admin_id(user: dict[str, Any] | None) -> str | None:
    """Admin who owns outreach config for this scanner."""
    if not user:
        return None
    role = str(user.get("role") or "")
    if role == ROLE_ADMIN:
        return str(user["id"]) if user.get("id") else None
    if role == ROLE_USER:
        admin_id = user.get("admin_id")
        return str(admin_id) if admin_id else None
    # SUPER_ADMIN keeps using global .env
    if role == ROLE_SUPER_ADMIN:
        return None
    return None


def resolve_owner_admin_id_from_user_id(user_id: str | None) -> str | None:
    if not user_id:
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.id, u.admin_id, r.name AS role
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s AND u.deleted_at IS NULL
            """,
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return resolve_owner_admin_id(
        {"id": row["id"], "role": row["role"], "admin_id": row.get("admin_id")}
    )


def resolve_owner_admin_for_outreach(
    *,
    user: dict[str, Any] | None = None,
    admin_user_id: str | None = None,
    contact: dict[str, Any] | None = None,
) -> str | None:
    """Prefer explicit Admin id, then scanner user, then contact creator."""
    if admin_user_id:
        return str(admin_user_id)
    from_user = resolve_owner_admin_id(user)
    if from_user:
        return from_user
    if not contact:
        return None
    creator = (
        contact.get("created_by_user_id")
        or contact.get("createdByUserId")
        or contact.get("created_by")
    )
    return resolve_owner_admin_id_from_user_id(str(creator) if creator else None)


def load_admin_env_raw(admin_user_id: str) -> dict[str, Any] | None:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT s.whatsapp, s.email, s.templates
            FROM admin_env_settings s
            JOIN users u ON u.id = s.admin_user_id
            JOIN roles r ON r.id = u.role_id
            WHERE s.admin_user_id = %s
              AND u.deleted_at IS NULL
              AND r.name = %s
            """,
            (admin_user_id, ROLE_ADMIN),
        )
        row = cur.fetchone()
    if not row:
        return None

    def _as_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        return {}

    return {
        "whatsapp": _as_dict(row.get("whatsapp")),
        "email": _as_dict(row.get("email")),
        "templates": _as_dict(row.get("templates")),
    }


def get_runtime() -> dict[str, Any] | None:
    return _runtime.get()


@contextmanager
def use_admin_env_payload(
    *,
    admin_user_id: str | None = None,
    whatsapp: dict[str, Any] | None = None,
    email: dict[str, Any] | None = None,
    templates: dict[str, Any] | None = None,
    force_channels: bool = False,
) -> Iterator[dict[str, Any]]:
    """Activate an explicit CMS payload (used by CMS Test buttons)."""
    wa = dict(whatsapp or {})
    em = dict(email or {})
    tpl = dict(templates or {})

    if not force_channels:
        if not bool(wa.get("enabled")):
            wa = {}
        if not bool(em.get("enabled")):
            em = {}

    payload = {
        "whatsapp": wa,
        "email": em,
        "templates": tpl,
        "admin_user_id": admin_user_id,
    }
    token = _runtime.set(payload)
    try:
        yield payload
    finally:
        _runtime.reset(token)


@contextmanager
def use_admin_env(admin_user_id: str | None) -> Iterator[dict[str, Any] | None]:
    """Activate CMS env for this Admin (if saved). Nested calls restore previous.

    Disabled WhatsApp/Email sections fall back to global .env credentials.
    Templates still apply when present.
    """
    if not admin_user_id:
        yield None
        return

    raw = load_admin_env_raw(admin_user_id)
    if not raw:
        logger.debug("No CMS admin_env_settings for admin=%s — using global .env", admin_user_id)
        yield None
        return

    with use_admin_env_payload(
        admin_user_id=admin_user_id,
        whatsapp=raw.get("whatsapp"),
        email=raw.get("email"),
        templates=raw.get("templates"),
        force_channels=False,
    ) as payload:
        logger.info(
            "Using CMS env for admin_user_id=%s (wa=%s email=%s templates=%s)",
            admin_user_id,
            bool(payload.get("whatsapp")),
            bool(payload.get("email")),
            bool(payload.get("templates")),
        )
        yield payload


def runtime_whatsapp() -> dict[str, Any]:
    rt = get_runtime()
    return (rt or {}).get("whatsapp") or {}


def runtime_email() -> dict[str, Any]:
    rt = get_runtime()
    return (rt or {}).get("email") or {}


def runtime_templates() -> dict[str, Any]:
    rt = get_runtime()
    return (rt or {}).get("templates") or {}
