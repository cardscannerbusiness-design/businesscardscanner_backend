"""CMS Super Admin: lock channels in the main app, and surface payment status.

Does not change WhatsApp or Email send implementations. Locks only flip the
main-app entitlement flags (whatsapp_allowed / email_allowed / google_sheets_allowed).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import Json

from db.pool import db_cursor

logger = logging.getLogger(__name__)

CHANNEL_KEYS = ("whatsapp", "email", "google_sheets")
PAID_INTENT_STATUSES = frozenset(
    {"paid", "captured", "success", "succeeded", "completed", "fulfilled"}
)


def default_channel_locks() -> dict[str, bool]:
    return {key: False for key in CHANNEL_KEYS}


def _locks_dict(raw: Any) -> dict[str, Any]:
    """Accept JSONB dicts, JSON strings, or empty values from PostgreSQL."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        raw = parsed
    if not isinstance(raw, dict):
        return {}
    return dict(raw)


def normalize_channel_locks(raw: Any) -> dict[str, bool]:
    data = _locks_dict(raw)
    out = default_channel_locks()
    for key in CHANNEL_KEYS:
        if key in data:
            out[key] = bool(data.get(key))
    return out


def explicit_channel_locks(raw: Any) -> dict[str, bool]:
    data = _locks_dict(raw)
    out: dict[str, bool] = {}
    for key in CHANNEL_KEYS:
        if key in data:
            out[key] = bool(data.get(key))
    return out


def effective_channel_locks(raw: Any, payment_done: bool = False) -> dict[str, bool]:
    """CMS manager explicit locks only.

    Unpaid Freemium stays unlocked until card allowance is used up, or a CMS
    manager clicks Lock. ``payment_done`` is unused; kept for call-site compatibility.
    """
    del payment_done
    explicit = explicit_channel_locks(raw)
    return {key: bool(explicit.get(key, False)) for key in CHANNEL_KEYS}


def payment_snapshot(
    *,
    plan_name: str | None,
    intent_status: str | None = None,
    intent_at: Any = None,
    package_id: str | None = None,
) -> dict[str, Any]:
    plan = str(plan_name or "FREEMIUM").strip() or "FREEMIUM"
    plan_upper = plan.upper()
    intent = str(intent_status or "").strip().lower()
    paid_plan = plan_upper not in {"FREEMIUM", ""} and "FREEMIUM" not in plan_upper
    paid_intent = intent in PAID_INTENT_STATUSES
    done = paid_plan or paid_intent
    status = "paid" if done else "not_paid"
    return {
        "payment_done": done,
        "payment_status": status,
        "payment_label": "payment paid" if done else "payment not paid",
        "plan_name": plan,
        "intent_status": intent_status or None,
        "intent_at": intent_at.isoformat() if hasattr(intent_at, "isoformat") else intent_at,
        "package_id": package_id or None,
    }


def update_channel_locks(admin_user_id: str, incoming: dict[str, Any] | None) -> dict[str, bool]:
    from services.admin_env_service import get_admin_env_settings

    existing = get_admin_env_settings(admin_user_id)
    if not existing:
        raise ValueError("Admin not found")
    company_id = existing.get("company_id")
    if not company_id:
        raise ValueError("This Admin is not linked to a company.")

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT cms_channel_locks
            FROM companies
            WHERE id = %s AND COALESCE(status, 'active') <> 'deleted'
            """,
            (company_id,),
        )
        stored = cur.fetchone() or {}
        raw = stored.get("cms_channel_locks") if isinstance(stored, dict) else None
        merged = normalize_channel_locks(raw)
        for key in CHANNEL_KEYS:
            if incoming and key in incoming and incoming[key] is not None:
                merged[key] = bool(incoming[key])
        cur.execute(
            """
            UPDATE companies
            SET cms_channel_locks = %s, updated_at = NOW()
            WHERE id = %s AND COALESCE(status, 'active') <> 'deleted'
            RETURNING cms_channel_locks
            """,
            (Json(merged), company_id),
        )
        if cur.rowcount == 0:
            raise ValueError("Company not found")
        written = cur.fetchone() or {}
    persisted = normalize_channel_locks(
        written.get("cms_channel_locks") if isinstance(written, dict) else merged
    )
    logger.info(
        "CMS channel locks updated company_id=%s admin_id=%s locks=%s",
        company_id,
        admin_user_id,
        persisted,
    )
    return persisted
