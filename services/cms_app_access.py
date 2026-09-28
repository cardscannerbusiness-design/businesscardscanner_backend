"""CMS Super Admin: per-account feature locks and payment status.

Locks live on companies.cms_channel_locks (JSONB) and are mirrored onto
admin_env_settings.channel_locks. They are account-scoped — blocking Email
Templates for company A never changes company B.

Plan entitlement (card quota) stays in entitlement_service. This module only
records whether the CMS owner has blocked a module/action.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import Json

from db.pool import db_cursor

logger = logging.getLogger(__name__)

# Outreach channels originally stored in cms_channel_locks.
CHANNEL_KEYS = ("whatsapp", "email", "google_sheets")

FEATURE_KEYS = (
    "capture",
    "contacts",
    "events",
    "email_templates",
    "email",
    "whatsapp",
    "google_sheets",
    "media",
    "subscription",
    "offline_queue",
    "settings",
)

ACTION_KEYS = (
    "email_templates.create",
    "email_templates.edit",
    "email_templates.delete",
    "events.create",
    "events.edit",
    "events.delete",
    "contacts.export",
    "media.upload",
    "email.send",
    "whatsapp.send",
)

FEATURE_CATALOG: tuple[dict[str, str], ...] = (
    {"key": "capture", "label": "Capture Card", "group": "Management"},
    {"key": "contacts", "label": "Contacts", "group": "Management"},
    {"key": "events", "label": "Events", "group": "Management"},
    {"key": "email_templates", "label": "Email Templates", "group": "Management"},
    {"key": "media", "label": "Media", "group": "Management"},
    {"key": "email", "label": "Email", "group": "Communication"},
    {"key": "whatsapp", "label": "WhatsApp", "group": "Communication"},
    {"key": "google_sheets", "label": "Google Sheets", "group": "Communication"},
    {"key": "subscription", "label": "Payment", "group": "Payments"},
    {"key": "offline_queue", "label": "Offline Queue", "group": "Management"},
    {"key": "settings", "label": "Settings", "group": "Settings"},
)

ACTION_CATALOG: tuple[dict[str, str], ...] = (
    {"key": "email_templates.create", "module": "email_templates", "action": "CREATE", "label": "Create email template"},
    {"key": "email_templates.edit", "module": "email_templates", "action": "EDIT", "label": "Edit email template"},
    {"key": "email_templates.delete", "module": "email_templates", "action": "DELETE", "label": "Delete email template"},
    {"key": "events.create", "module": "events", "action": "CREATE", "label": "Create event"},
    {"key": "events.edit", "module": "events", "action": "EDIT", "label": "Edit event"},
    {"key": "events.delete", "module": "events", "action": "DELETE", "label": "Delete event"},
    {"key": "contacts.export", "module": "contacts", "action": "EXPORT", "label": "Export contacts"},
    {"key": "media.upload", "module": "media", "action": "UPLOAD", "label": "Upload media"},
    {"key": "email.send", "module": "email", "action": "SEND", "label": "Send email"},
    {"key": "whatsapp.send", "module": "whatsapp", "action": "SEND", "label": "Send WhatsApp"},
)

PAID_INTENT_STATUSES = frozenset(
    {"paid", "captured", "success", "succeeded", "completed", "fulfilled"}
)
PENDING_INTENT_STATUSES = frozenset(
    {"pending", "created", "authorized", "awaiting_gateway", "processing"}
)
FAILED_INTENT_STATUSES = frozenset({"failed", "provider_error", "error"})
REFUNDED_INTENT_STATUSES = frozenset({"refunded", "refund"})
CANCELLED_INTENT_STATUSES = frozenset({"cancelled", "canceled"})
EXPIRED_INTENT_STATUSES = frozenset({"expired"})

_STATUS_MARKS = {
    "paid": "✓",
    "pending": "!",
    "failed": "×",
    "refunded": "↩",
    "cancelled": "–",
    "expired": "⌛",
    "unpaid": "–",
}
_STATUS_LABELS = {
    "paid": "Paid",
    "pending": "Pending",
    "failed": "Failed",
    "refunded": "Refunded",
    "cancelled": "Cancelled",
    "expired": "Expired",
    "unpaid": "Unpaid",
}


def default_channel_locks() -> dict[str, bool]:
    return {key: False for key in FEATURE_KEYS}


def default_action_locks() -> dict[str, bool]:
    return {key: False for key in ACTION_KEYS}


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


def normalize_action_locks(raw: Any) -> dict[str, bool]:
    data = _locks_dict(raw)
    nested = data.get("actions") if isinstance(data.get("actions"), dict) else {}
    out = default_action_locks()
    for key in ACTION_KEYS:
        if key in data:
            out[key] = bool(data.get(key))
        elif key in nested:
            out[key] = bool(nested.get(key))
    if bool(data.get("email")):
        out["email.send"] = True
    if bool(data.get("whatsapp")):
        out["whatsapp.send"] = True
    return out


def persistable_feature_control(raw: Any, incoming: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge stored JSON with a partial CMS update. True = blocked."""
    data = _locks_dict(raw)
    features = default_channel_locks()
    for key in FEATURE_KEYS:
        if key in data:
            features[key] = bool(data.get(key))
    actions = normalize_action_locks(data)
    incoming = incoming or {}
    for key in FEATURE_KEYS:
        if key in incoming and incoming[key] is not None:
            features[key] = bool(incoming[key])
    nested_in = incoming.get("actions") if isinstance(incoming.get("actions"), dict) else {}
    for key in ACTION_KEYS:
        if key in incoming and incoming[key] is not None:
            actions[key] = bool(incoming[key])
        elif key in nested_in and nested_in[key] is not None:
            actions[key] = bool(nested_in[key])
    if features.get("email"):
        actions["email.send"] = True
    if features.get("whatsapp"):
        actions["whatsapp.send"] = True
    out: dict[str, Any] = dict(features)
    for key, value in actions.items():
        out[key] = value
    out["actions"] = actions
    return out


def normalize_channel_locks(raw: Any) -> dict[str, bool]:
    data = _locks_dict(raw)
    out = default_channel_locks()
    for key in FEATURE_KEYS:
        if key in data:
            out[key] = bool(data.get(key))
    return out


def explicit_channel_locks(raw: Any) -> dict[str, bool]:
    data = _locks_dict(raw)
    out: dict[str, bool] = {}
    for key in FEATURE_KEYS:
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
    return {key: bool(explicit.get(key, False)) for key in FEATURE_KEYS}


def classify_payment_status(*, plan_name: str | None, intent_status: str | None) -> str:
    plan = str(plan_name or "FREEMIUM").strip() or "FREEMIUM"
    plan_upper = plan.upper()
    intent = str(intent_status or "").strip().lower()
    paid_plan = plan_upper not in {"FREEMIUM", ""} and "FREEMIUM" not in plan_upper
    if intent in REFUNDED_INTENT_STATUSES:
        return "refunded"
    if intent in CANCELLED_INTENT_STATUSES:
        return "cancelled"
    if intent in EXPIRED_INTENT_STATUSES:
        return "expired"
    if intent in FAILED_INTENT_STATUSES:
        return "failed"
    if intent in PAID_INTENT_STATUSES or paid_plan:
        return "paid"
    if intent in PENDING_INTENT_STATUSES:
        return "pending"
    return "unpaid"


def payment_status_view(code: str) -> dict[str, str]:
    key = code if code in _STATUS_LABELS else "unpaid"
    return {
        "status_code": key,
        "status_label": _STATUS_LABELS[key],
        "status_mark": _STATUS_MARKS[key],
    }


def payment_snapshot(
    *,
    plan_name: str | None,
    intent_status: str | None = None,
    intent_at: Any = None,
    package_id: str | None = None,
) -> dict[str, Any]:
    plan = str(plan_name or "FREEMIUM").strip() or "FREEMIUM"
    code = classify_payment_status(plan_name=plan, intent_status=intent_status)
    done = code == "paid"
    view = payment_status_view(code)
    return {
        "payment_done": done,
        "payment_status": "paid" if done else "not_paid",
        "payment_label": "payment paid" if done else "payment not paid",
        "status_code": view["status_code"],
        "status_label": view["status_label"],
        "status_mark": view["status_mark"],
        "plan_name": plan,
        "intent_status": intent_status or None,
        "intent_at": intent_at.isoformat() if hasattr(intent_at, "isoformat") else intent_at,
        "package_id": package_id or None,
    }


def feature_control_payload(raw: Any) -> dict[str, Any]:
    features = normalize_channel_locks(raw)
    actions = normalize_action_locks(raw)
    return {
        "features": [
            {
                **item,
                "enabled": not bool(features.get(item["key"])),
                "blocked": bool(features.get(item["key"])),
            }
            for item in FEATURE_CATALOG
        ],
        "actions": [
            {
                **item,
                "enabled": not bool(actions.get(item["key"])),
                "blocked": bool(actions.get(item["key"])),
            }
            for item in ACTION_CATALOG
        ],
        "locks": features,
        "action_locks": actions,
    }


def update_channel_locks(admin_user_id: str, incoming: dict[str, Any] | None) -> dict[str, Any]:
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
        merged = persistable_feature_control(raw, incoming)
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
    persisted_raw = written.get("cms_channel_locks") if isinstance(written, dict) else merged
    persisted = persistable_feature_control(persisted_raw)
    logger.info(
        "CMS feature locks updated company_id=%s admin_id=%s locks=%s",
        company_id,
        admin_user_id,
        {k: persisted.get(k) for k in FEATURE_KEYS},
    )
    return persisted
