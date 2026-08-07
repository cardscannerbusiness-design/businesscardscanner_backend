"""Per-Admin WhatsApp / Email env settings for Super Admin CMS.

Field names mirror BusinessCardScanner_Backend/.env send keys.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import Json

from auth.constants import ROLE_ADMIN
from db.pool import db_cursor

logger = logging.getLogger(__name__)

# Meta WhatsApp Cloud API / Business Manager fields
WHATSAPP_KEYS = (
    "app_id",
    "access_token",
    "phone_number_id",
    "business_account_id",
    "business_phone",
    "graph_api_version",
    "app_secret",
    "verify_token",
    "template_name",
    "template_language_code",
    "enabled",
)

# Match .env SMTP_* keys used by email_service
EMAIL_KEYS = (
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "enabled",
)

TEMPLATE_KEYS = (
    "email_subject",
    "email_body",
    "whatsapp_header_format",
    "whatsapp_header",
    "whatsapp_header_media_url",
    "whatsapp_header_media_filename",
    "whatsapp_body",
    "whatsapp_footer",
    "whatsapp_button_text",
    "whatsapp_button_url",
    "preview_name",
    "preview_company",
    "preview_phone",
    "preview_email",
    "preview_website",
    "preview_signoff",
)

SECRET_KEYS = frozenset(
    {
        "access_token",
        "app_secret",
        "smtp_password",
    }
)

MASK = "••••••••"

# Old CMS keys → current keys (keep existing saved rows working)
_WA_LEGACY = {
    "business_phone_number": "business_phone",
    "waba_id": "business_account_id",
    "api_version": "graph_api_version",
    "language": "template_language_code",
    "card_received_template_name": "template_name",
    "business_card_template_name": "template_name",
    "scan_template_name": "template_name",
    "permanent_token": "access_token",
}
_EMAIL_LEGACY = {
    "smtp_username": "smtp_user",
    "sender_email": "smtp_from",
}


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _apply_legacy(raw: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    out = dict(raw)
    for old, new in mapping.items():
        if new not in out or not str(out.get(new) or "").strip():
            if old in out and str(out.get(old) or "").strip():
                out[new] = out[old]
    return out


def _empty_whatsapp() -> dict[str, Any]:
    return {k: (False if k == "enabled" else "") for k in WHATSAPP_KEYS}


def _empty_email() -> dict[str, Any]:
    return {k: (False if k == "enabled" else "") for k in EMAIL_KEYS}


def _empty_templates() -> dict[str, Any]:
    from services.email_template_service import get_thank_you_body_cms_default
    from services.template_token_service import DEFAULT_TOKEN_MAP

    return {
        "email_subject": "Thank you for connecting, {{1}}",
        "email_body": get_thank_you_body_cms_default(),
        "whatsapp_header_format": "NONE",
        "whatsapp_header": "CardScan Message",
        "whatsapp_header_media_url": "",
        "whatsapp_header_media_filename": "brochure.pdf",
        "whatsapp_body": (
            "Hello {{1}},\n"
            "Thank you for sharing your business card details.\n"
            "Your contact information has been received successfully.\n"
            "We will get back to you regarding the details provided — {{5}}.\n"
            "Thank you"
        ),
        "whatsapp_footer": "Thank you",
        "whatsapp_button_text": "",
        "whatsapp_button_url": "",
        "preview_name": "Alex",
        "preview_company": "Acme Corp",
        "preview_phone": "+91 98765 43210",
        "preview_email": "partner@example.com",
        "preview_website": "https://example.com",
        "preview_signoff": "B2B Team",
        "token_map": dict(DEFAULT_TOKEN_MAP),
    }


def _merge_templates(
    existing: dict[str, Any],
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    from services.template_token_service import normalize_token_map

    base = _empty_templates()
    for key in TEMPLATE_KEYS:
        if key in existing and existing[key] is not None and str(existing[key]) != "":
            base[key] = str(existing[key])
    if "token_map" in existing:
        base["token_map"] = normalize_token_map(existing.get("token_map"))

    if not incoming:
        return base

    for key in TEMPLATE_KEYS:
        if key not in incoming or incoming[key] is None:
            continue
        base[key] = str(incoming[key])

    if "token_map" in incoming:
        base["token_map"] = normalize_token_map(incoming.get("token_map"))
    return base


def _public_templates(data: dict[str, Any]) -> dict[str, Any]:
    from services.template_token_service import normalize_token_map

    defaults = _empty_templates()
    out: dict[str, Any] = {}
    for key in TEMPLATE_KEYS:
        raw = data.get(key)
        out[key] = str(raw) if raw is not None and str(raw) != "" else defaults[key]
    out["token_map"] = normalize_token_map(data.get("token_map") or defaults["token_map"])
    return out


def _mask_section(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        raw = data.get(key, False if key == "enabled" else "")
        if key == "enabled":
            out[key] = bool(raw)
            continue
        text = "" if raw is None else str(raw)
        if key in SECRET_KEYS:
            out[key] = MASK if text.strip() else ""
            out[f"{key}_set"] = bool(text.strip())
        else:
            out[key] = text
    return out


def _merge_section(
    existing: dict[str, Any],
    incoming: dict[str, Any] | None,
    keys: tuple[str, ...],
) -> dict[str, Any]:
    base = {k: (False if k == "enabled" else "") for k in keys}
    base.update({k: existing.get(k, base[k]) for k in keys if k in existing})
    if not incoming:
        return base
    for key in keys:
        if key not in incoming:
            continue
        value = incoming[key]
        if key == "enabled":
            base[key] = bool(value)
            continue
        text = "" if value is None else str(value).strip()
        if key in SECRET_KEYS:
            if not text or text == MASK or set(text) <= {"•", "*"}:
                continue
            base[key] = text
        else:
            base[key] = text
    return base


def _row_to_admin(row: dict[str, Any]) -> dict[str, Any]:
    whatsapp_raw = _apply_legacy(_as_dict(row.get("whatsapp")), _WA_LEGACY)
    email_raw = _apply_legacy(_as_dict(row.get("email")), _EMAIL_LEGACY)
    templates_raw = _as_dict(row.get("templates"))
    return {
        "admin_id": str(row["id"]),
        "email": row.get("email_addr") or row.get("user_email") or "",
        "first_name": row.get("first_name") or "",
        "last_name": row.get("last_name") or "",
        "phone": row.get("phone") or "",
        "is_active": bool(row.get("is_active")),
        "company_id": str(row["company_id"]) if row.get("company_id") else None,
        "company_name": row.get("company_name") or "",
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "has_settings": bool(row.get("settings_id")),
        "whatsapp": _mask_section(
            {**_empty_whatsapp(), **{k: whatsapp_raw.get(k, "") for k in WHATSAPP_KEYS if k != "enabled"}, "enabled": bool(whatsapp_raw.get("enabled"))},
            WHATSAPP_KEYS,
        ),
        "email_settings": _mask_section(
            {**_empty_email(), **{k: email_raw.get(k, "") for k in EMAIL_KEYS if k != "enabled"}, "enabled": bool(email_raw.get("enabled"))},
            EMAIL_KEYS,
        ),
        "templates": _public_templates(templates_raw),
        "settings_updated_at": (
            row["settings_updated_at"].isoformat() if row.get("settings_updated_at") else None
        ),
    }


def list_admin_env_settings() -> list[dict[str, Any]]:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT
                u.id,
                u.email AS email_addr,
                u.first_name,
                u.last_name,
                u.phone,
                u.is_active,
                u.company_id,
                c.company_name AS company_name,
                u.created_at,
                u.updated_at,
                s.id AS settings_id,
                s.whatsapp,
                s.email,
                s.templates,
                s.updated_at AS settings_updated_at
            FROM users u
            JOIN roles r ON r.id = u.role_id
            LEFT JOIN companies c ON c.id = u.company_id
            LEFT JOIN admin_env_settings s ON s.admin_user_id = u.id
            WHERE u.deleted_at IS NULL
              AND r.name = %s
            ORDER BY LOWER(u.first_name), LOWER(u.last_name), LOWER(u.email)
            """,
            (ROLE_ADMIN,),
        )
        rows = cur.fetchall() or []
    return [_row_to_admin(row) for row in rows]


def get_admin_env_settings(admin_user_id: str) -> dict[str, Any] | None:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT
                u.id,
                u.email AS email_addr,
                u.first_name,
                u.last_name,
                u.phone,
                u.is_active,
                u.company_id,
                c.company_name AS company_name,
                u.created_at,
                u.updated_at,
                s.id AS settings_id,
                s.whatsapp,
                s.email,
                s.templates,
                s.updated_at AS settings_updated_at
            FROM users u
            JOIN roles r ON r.id = u.role_id
            LEFT JOIN companies c ON c.id = u.company_id
            LEFT JOIN admin_env_settings s ON s.admin_user_id = u.id
            WHERE u.id = %s
              AND u.deleted_at IS NULL
              AND r.name = %s
            """,
            (admin_user_id, ROLE_ADMIN),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _row_to_admin(row)


def delete_admin_env_settings(admin_user_id: str) -> dict[str, Any]:
    """Remove CMS env row for an Admin. Scanner falls back to global .env."""
    existing = get_admin_env_settings(admin_user_id)
    if not existing:
        raise ValueError("Admin not found")

    with db_cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM admin_env_settings WHERE admin_user_id = %s",
            (admin_user_id,),
        )

    result = get_admin_env_settings(admin_user_id)
    if not result:
        raise RuntimeError("Failed to reload Admin after removing CMS env")
    return result


def upsert_admin_env_settings(
    admin_user_id: str,
    *,
    whatsapp: dict[str, Any] | None = None,
    email: dict[str, Any] | None = None,
    templates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = get_admin_env_settings(admin_user_id)
    if not existing:
        raise ValueError("Admin not found")

    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT whatsapp, email, templates FROM admin_env_settings WHERE admin_user_id = %s",
            (admin_user_id,),
        )
        prev = cur.fetchone() or {}

    prev_wa = _apply_legacy(_as_dict(prev.get("whatsapp")), _WA_LEGACY)
    prev_em = _apply_legacy(_as_dict(prev.get("email")), _EMAIL_LEGACY)
    prev_tpl = _as_dict(prev.get("templates"))

    merged_wa = _merge_section(prev_wa, whatsapp, WHATSAPP_KEYS)
    merged_em = _merge_section(prev_em, email, EMAIL_KEYS)
    merged_tpl = _merge_templates(prev_tpl, templates)

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_env_settings (admin_user_id, whatsapp, email, templates, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (admin_user_id) DO UPDATE SET
                whatsapp = EXCLUDED.whatsapp,
                email = EXCLUDED.email,
                templates = EXCLUDED.templates,
                updated_at = NOW()
            """,
            (
                admin_user_id,
                Json(merged_wa),
                Json(merged_em),
                Json(merged_tpl),
            ),
        )

    result = get_admin_env_settings(admin_user_id)
    if not result:
        raise RuntimeError("Failed to load settings after save")
    return result


def merge_admin_env_for_test(
    admin_user_id: str,
    *,
    whatsapp: dict[str, Any] | None = None,
    email: dict[str, Any] | None = None,
    templates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge form overrides with saved secrets (blank secrets keep DB values). Does not persist."""
    existing = get_admin_env_settings(admin_user_id)
    if not existing:
        raise ValueError("Admin not found")

    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT whatsapp, email, templates FROM admin_env_settings WHERE admin_user_id = %s",
            (admin_user_id,),
        )
        prev = cur.fetchone() or {}

    prev_wa = _apply_legacy(_as_dict(prev.get("whatsapp")), _WA_LEGACY)
    prev_em = _apply_legacy(_as_dict(prev.get("email")), _EMAIL_LEGACY)
    prev_tpl = _as_dict(prev.get("templates"))

    return {
        "admin_user_id": admin_user_id,
        "whatsapp": _merge_section(prev_wa, whatsapp, WHATSAPP_KEYS),
        "email": _merge_section(prev_em, email, EMAIL_KEYS),
        "templates": _merge_templates(prev_tpl, templates),
    }
