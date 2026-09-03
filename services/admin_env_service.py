"""Per-Admin WhatsApp / Email env settings for Super Admin CMS.

Field names mirror BusinessCardScanner_Backend/.env send keys.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import Json

from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN, ROLE_USER
from db.pool import db_cursor
from services.email_display_name import normalize_email_display_name
from services.entitlement_service import (
    DEFAULT_FREEMIUM_CARD_LIMIT,
    is_default_user_card_limit,
)

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

# Match .env SMTP_* keys used by email_service (Amazon SES SMTP relay per Admin)
EMAIL_KEYS = (
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "sender_notification_email",  # CMS "Receive email" — scanned-details copy inbox
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

GOOGLE_SHEETS_KEYS = (
    "google_sheet_id",
    "google_sheet_name",
    "google_service_account_json",
    "google_drive_folder_id",
    "google_oauth_client_id",
    "google_oauth_client_secret",
    "google_oauth_redirect_uri",
    "enabled",
)

SECRET_KEYS = frozenset(
    {
        "access_token",
        "app_secret",
        "smtp_password",
        "google_service_account_json",
        "google_oauth_client_secret",
    }
)

MASK = "••••••••"

# CMS kill-switches: locked=true → channel OFF for that Admin's company in the main app.
# WhatsApp defaults locked to match the current CMS product stage; Email/Sheets start unlocked.
DEFAULT_CHANNEL_LOCKS: dict[str, bool] = {
    "whatsapp": True,
    "email": False,
    "google_sheets": False,
}
CHANNEL_LOCK_KEYS = tuple(DEFAULT_CHANNEL_LOCKS.keys())

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


def normalize_channel_locks(raw: Any) -> dict[str, bool]:
    """Return {whatsapp, email, google_sheets} locked flags (True = OFF in app)."""
    data = _as_dict(raw)
    out: dict[str, bool] = {}
    for key, default in DEFAULT_CHANNEL_LOCKS.items():
        if key in data:
            out[key] = bool(data[key])
        else:
            out[key] = bool(default)
    return out


def get_channel_locks_for_company(company_id: str | None) -> dict[str, bool]:
    """Resolve CMS channel locks for a company (via its Admin env settings)."""
    if not company_id:
        return dict(DEFAULT_CHANNEL_LOCKS)
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT s.channel_locks
            FROM admin_env_settings s
            JOIN users u ON u.id = s.admin_user_id
            JOIN roles r ON r.id = u.role_id
            WHERE u.company_id = %s
              AND u.deleted_at IS NULL
              AND r.name = %s
            ORDER BY s.updated_at DESC NULLS LAST
            LIMIT 1
            """,
            (company_id, ROLE_ADMIN),
        )
        row = cur.fetchone()
    if not row:
        return dict(DEFAULT_CHANNEL_LOCKS)
    return normalize_channel_locks(row.get("channel_locks") if isinstance(row, dict) else row[0])


def channel_is_locked(company_id: str | None, channel: str) -> bool:
    key = str(channel or "").strip().lower()
    if key in ("sheets", "google", "gsheets"):
        key = "google_sheets"
    locks = get_channel_locks_for_company(company_id)
    return bool(locks.get(key, DEFAULT_CHANNEL_LOCKS.get(key, False)))


def apply_channel_locks_to_entitlement(
    info: dict[str, Any],
    company_id: str | None = None,
) -> dict[str, Any]:
    """AND CMS locks onto entitlement whatsapp/email/sheets allowed flags."""
    cid = company_id if company_id is not None else info.get("company_id")
    locks = get_channel_locks_for_company(str(cid) if cid else None)
    out = dict(info)
    out["cms_channel_locks"] = locks
    out["cms_whatsapp_locked"] = locks["whatsapp"]
    out["cms_email_locked"] = locks["email"]
    out["cms_google_sheets_locked"] = locks["google_sheets"]
    if locks["whatsapp"]:
        out["whatsapp_allowed"] = False
    if locks["email"]:
        out["email_allowed"] = False
    out["google_sheets_allowed"] = not locks["google_sheets"]
    return out


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


def _empty_google_sheets() -> dict[str, Any]:
    return {k: (False if k == "enabled" else "") for k in GOOGLE_SHEETS_KEYS}


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


def _normalize_email_receive_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Map receive_email ↔ sender_notification_email (CMS Receive email field)."""
    out = dict(data)
    receive = str(
        out.get("sender_notification_email")
        or out.get("receive_email")
        or ""
    ).strip()
    out["sender_notification_email"] = receive
    out["receive_email"] = receive
    return out


def get_cms_receive_email(admin_user_id: str | None) -> str | None:
    """Per-Admin CMS Receive email (scanned-details inbox), ignoring SMTP enabled flag."""
    if not admin_user_id:
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT s.email
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
    em = _normalize_email_receive_fields(_as_dict(row.get("email") if isinstance(row, dict) else None))
    return str(em.get("receive_email") or "").strip() or None


def get_company_email_display_name(company_id: str | None) -> str | None:
    """Saved From display name for this company; None means use the default."""
    if not company_id:
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT email_display_name
            FROM companies
            WHERE id = %s
              AND COALESCE(status, 'active') <> 'deleted'
            """,
            (company_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    name = str(row.get("email_display_name") if isinstance(row, dict) else "").strip()
    return name or None


def get_company_id_for_admin(admin_user_id: str | None) -> str | None:
    if not admin_user_id:
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.company_id
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s
              AND u.deleted_at IS NULL
              AND r.name = %s
            """,
            (admin_user_id, ROLE_ADMIN),
        )
        row = cur.fetchone()
    if not row or not row.get("company_id"):
        return None
    return str(row["company_id"])


def set_admin_company_email_display_name(
    admin_user_id: str,
    display_name: str | None,
) -> dict[str, Any]:
    """Store From display name on companies.email_display_name for this Admin's company."""
    existing = get_admin_env_settings(admin_user_id)
    if not existing:
        raise ValueError("Admin not found")
    company_id = existing.get("company_id")
    if not company_id:
        raise ValueError("Admin has no company")
    cleaned = normalize_email_display_name(display_name)
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE companies
            SET email_display_name = %s, updated_at = NOW()
            WHERE id = %s
              AND COALESCE(status, 'active') <> 'deleted'
            """,
            (cleaned, company_id),
        )
        if cur.rowcount == 0:
            raise ValueError("Company not found")
    result = get_admin_env_settings(admin_user_id)
    if not result:
        raise RuntimeError("Failed to reload Admin after saving email display name")
    return result


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
    email_raw = _normalize_email_receive_fields(
        _apply_legacy(_as_dict(row.get("email")), _EMAIL_LEGACY)
    )
    templates_raw = _as_dict(row.get("templates"))
    sheets_raw = _as_dict(row.get("google_sheets"))
    channel_locks = normalize_channel_locks(row.get("channel_locks"))
    return {
        "admin_id": str(row["id"]),
        "email": row.get("email_addr") or row.get("user_email") or "",
        "first_name": row.get("first_name") or "",
        "last_name": row.get("last_name") or "",
        "phone": row.get("phone") or "",
        "is_active": bool(row.get("is_active")),
        "company_id": str(row["company_id"]) if row.get("company_id") else None,
        "tenant_id": str(row["company_id"]) if row.get("company_id") else str(row["id"]),
        "company_name": row.get("company_name") or "",
        "email_display_name": str(row.get("email_display_name") or "").strip(),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "has_settings": bool(row.get("settings_id")),
        "whatsapp": _mask_section(
            {**_empty_whatsapp(), **{k: whatsapp_raw.get(k, "") for k in WHATSAPP_KEYS if k != "enabled"}, "enabled": bool(whatsapp_raw.get("enabled"))},
            WHATSAPP_KEYS,
        ),
        "email_settings": _mask_section(
            {
                **_empty_email(),
                **{k: email_raw.get(k, "") for k in EMAIL_KEYS if k != "enabled"},
                "enabled": bool(email_raw.get("enabled")),
            },
            EMAIL_KEYS,
        ),
        "receive_email": str(email_raw.get("receive_email") or ""),
        "templates": _public_templates(templates_raw),
        "google_sheets": _mask_section(
            {
                **_empty_google_sheets(),
                **{k: sheets_raw.get(k, "") for k in GOOGLE_SHEETS_KEYS if k != "enabled"},
                "enabled": bool(sheets_raw.get("enabled")),
            },
            GOOGLE_SHEETS_KEYS,
        ),
        "channel_locks": channel_locks,
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
                c.email_display_name AS email_display_name,
                u.created_at,
                u.updated_at,
                s.id AS settings_id,
                s.whatsapp,
                s.email,
                s.templates,
                s.google_sheets,
                s.channel_locks,
                s.updated_at AS settings_updated_at
            FROM users u
            JOIN roles r ON r.id = u.role_id
            INNER JOIN companies c ON c.id = u.company_id AND COALESCE(c.status, 'active') <> 'deleted'
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
                c.email_display_name AS email_display_name,
                u.created_at,
                u.updated_at,
                s.id AS settings_id,
                s.whatsapp,
                s.email,
                s.templates,
                s.google_sheets,
                s.channel_locks,
                s.updated_at AS settings_updated_at
            FROM users u
            JOIN roles r ON r.id = u.role_id
            LEFT JOIN companies c ON c.id = u.company_id
            LEFT JOIN admin_env_settings s ON s.admin_user_id = u.id
            WHERE u.id = %s
              AND u.deleted_at IS NULL
              AND r.name = %s
              AND (c.id IS NULL OR COALESCE(c.status, 'active') <> 'deleted')
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
    google_sheets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = get_admin_env_settings(admin_user_id)
    if not existing:
        raise ValueError("Admin not found")

    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT whatsapp, email, templates, google_sheets, channel_locks FROM admin_env_settings WHERE admin_user_id = %s",
            (admin_user_id,),
        )
        prev = cur.fetchone() or {}

    prev_wa = _apply_legacy(_as_dict(prev.get("whatsapp")), _WA_LEGACY)
    prev_em = _normalize_email_receive_fields(
        _apply_legacy(_as_dict(prev.get("email")), _EMAIL_LEGACY)
    )
    prev_tpl = _as_dict(prev.get("templates"))
    prev_gs = _as_dict(prev.get("google_sheets"))
    prev_locks = normalize_channel_locks(prev.get("channel_locks"))

    merged_wa = _merge_section(prev_wa, whatsapp, WHATSAPP_KEYS)
    email_incoming = _as_dict(email) if email is not None else None
    if email_incoming is not None and not str(
        email_incoming.get("sender_notification_email") or ""
    ).strip():
        alias = str(email_incoming.get("receive_email") or "").strip()
        if alias:
            email_incoming["sender_notification_email"] = alias
    merged_em = _normalize_email_receive_fields(
        _merge_section(prev_em, email_incoming, EMAIL_KEYS)
    )
    merged_em.pop("receive_email", None)
    merged_tpl = _merge_templates(prev_tpl, templates)
    merged_gs = _merge_section(prev_gs, google_sheets, GOOGLE_SHEETS_KEYS)

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_env_settings (admin_user_id, whatsapp, email, templates, google_sheets, channel_locks, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (admin_user_id) DO UPDATE SET
                whatsapp = EXCLUDED.whatsapp,
                email = EXCLUDED.email,
                templates = EXCLUDED.templates,
                google_sheets = EXCLUDED.google_sheets,
                channel_locks = COALESCE(admin_env_settings.channel_locks, EXCLUDED.channel_locks),
                updated_at = NOW()
            """,
            (
                admin_user_id,
                Json(merged_wa),
                Json(merged_em),
                Json(merged_tpl),
                Json(merged_gs),
                Json(prev_locks),
            ),
        )

        sheet_id = str(merged_gs.get("google_sheet_id") or "").strip()
        company_id = existing.get("company_id")
        if sheet_id and company_id:
            cur.execute(
                """
                UPDATE companies
                SET google_sheet_id = %s, updated_at = NOW()
                WHERE id = %s AND COALESCE(status, 'active') <> 'deleted'
                """,
                (sheet_id, company_id),
            )

    result = get_admin_env_settings(admin_user_id)
    if not result:
        raise RuntimeError("Failed to load settings after save")
    return result


def set_admin_channel_locks(
    admin_user_id: str,
    locks: dict[str, Any] | None,
) -> dict[str, Any]:
    """Create/update CMS channel locks for an Admin (company kill-switches)."""
    existing = get_admin_env_settings(admin_user_id)
    if not existing:
        raise ValueError("Admin not found")

    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT whatsapp, email, templates, google_sheets, channel_locks FROM admin_env_settings WHERE admin_user_id = %s",
            (admin_user_id,),
        )
        prev = cur.fetchone() or {}

    prev_wa = _apply_legacy(_as_dict(prev.get("whatsapp")), _WA_LEGACY) or _empty_whatsapp()
    prev_em = _apply_legacy(_as_dict(prev.get("email")), _EMAIL_LEGACY) or _empty_email()
    prev_tpl = _as_dict(prev.get("templates")) or _empty_templates()
    prev_gs = _as_dict(prev.get("google_sheets")) or _empty_google_sheets()
    merged_locks = normalize_channel_locks(
        {**normalize_channel_locks(prev.get("channel_locks")), **_as_dict(locks)}
    )

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_env_settings (admin_user_id, whatsapp, email, templates, google_sheets, channel_locks, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (admin_user_id) DO UPDATE SET
                channel_locks = EXCLUDED.channel_locks,
                updated_at = NOW()
            """,
            (
                admin_user_id,
                Json(prev_wa),
                Json(prev_em),
                Json(prev_tpl),
                Json(prev_gs),
                Json(merged_locks),
            ),
        )

    result = get_admin_env_settings(admin_user_id)
    if not result:
        raise RuntimeError("Failed to reload Admin env after channel lock update")
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


def list_cms_tenant_users(admin_user_id: str) -> dict[str, Any]:
    """Admin + Users for this CMS client, with active and currently-connected counts."""
    existing = get_admin_env_settings(admin_user_id)
    if not existing:
        raise ValueError("Admin not found")

    company_id = existing.get("company_id")
    with db_cursor(commit=False) as cur:
        if company_id:
            cur.execute(
                """
                SELECT
                    u.id,
                    u.email,
                    u.first_name,
                    u.last_name,
                    u.is_active,
                    u.last_login,
                    u.created_at,
                    COALESCE(u.scans_unlimited, FALSE) AS scans_unlimited,
                    u.user_card_limit,
                    COALESCE(u.user_cards_used, 0) AS user_cards_used,
                    r.name AS role,
                    EXISTS (
                        SELECT 1
                        FROM sessions s
                        WHERE s.user_id = u.id
                          AND LOWER(COALESCE(s.status, '')) = 'active'
                          AND s.expires_at > NOW()
                    ) AS connected
                FROM users u
                JOIN roles r ON r.id = u.role_id
                WHERE u.deleted_at IS NULL
                  AND u.company_id = %s
                  AND r.name IN (%s, %s)
                ORDER BY
                    CASE r.name WHEN 'ADMIN' THEN 0 ELSE 1 END,
                    u.created_at ASC
                """,
                (company_id, ROLE_ADMIN, ROLE_USER),
            )
        else:
            cur.execute(
                """
                SELECT
                    u.id,
                    u.email,
                    u.first_name,
                    u.last_name,
                    u.is_active,
                    u.last_login,
                    u.created_at,
                    COALESCE(u.scans_unlimited, FALSE) AS scans_unlimited,
                    u.user_card_limit,
                    COALESCE(u.user_cards_used, 0) AS user_cards_used,
                    r.name AS role,
                    EXISTS (
                        SELECT 1
                        FROM sessions s
                        WHERE s.user_id = u.id
                          AND LOWER(COALESCE(s.status, '')) = 'active'
                          AND s.expires_at > NOW()
                    ) AS connected
                FROM users u
                JOIN roles r ON r.id = u.role_id
                WHERE u.deleted_at IS NULL
                  AND u.id = %s
                """,
                (admin_user_id,),
            )
        rows = cur.fetchall() or []

    users: list[dict[str, Any]] = []
    for row in rows:
        first = str(row.get("first_name") or "").strip()
        last = str(row.get("last_name") or "").strip()
        name = f"{first} {last}".strip() or str(row.get("email") or "")
        is_active = bool(row.get("is_active"))
        connected = bool(row.get("connected")) and is_active
        last_login = row.get("last_login")
        created_at = row.get("created_at")
        scans_unlimited = bool(row.get("scans_unlimited"))
        raw_limit = row.get("user_card_limit")
        user_card_limit = int(raw_limit) if raw_limit is not None else DEFAULT_FREEMIUM_CARD_LIMIT
        user_cards_used = max(0, int(row.get("user_cards_used") or 0))
        mode = _scan_entitlement_mode(scans_unlimited, user_card_limit)
        users.append(
            {
                "id": str(row["id"]),
                "name": name,
                "email": str(row.get("email") or ""),
                "role": str(row.get("role") or ""),
                "is_active": is_active,
                "connected": connected,
                "scans_unlimited": scans_unlimited,
                "user_card_limit": user_card_limit,
                "user_cards_used": user_cards_used,
                "scan_entitlement_mode": mode,
                "effective_card_limit": None if mode == "unlimited" else user_card_limit,
                "status": "Active" if is_active else "Inactive",
                "check_status": "pass" if connected else ("pending" if is_active else "fail"),
                "last_login": last_login.isoformat() if last_login and hasattr(last_login, "isoformat") else None,
                "last_test": last_login.isoformat() if last_login and hasattr(last_login, "isoformat") else None,
                "created_at": created_at.isoformat() if created_at and hasattr(created_at, "isoformat") else None,
            }
        )

    total = len(users)
    active = sum(1 for u in users if u["is_active"])
    connected = sum(1 for u in users if u["connected"])
    return {
        "admin_id": str(existing["admin_id"]),
        "tenant_id": existing.get("tenant_id") or existing.get("company_id") or existing["admin_id"],
        "company_name": existing.get("company_name") or "",
        "total": total,
        "active": active,
        "connected": connected,
        "configured": total,
        "created": total,
        "available": active,
        "remaining": 0,
        "users": users,
        "note": (
            f"Only Google Sheets has access. {active} active · {connected} connected "
            f"of {total} tenant user{'s' if total != 1 else ''}."
        ),
    }


def _scan_entitlement_mode(scans_unlimited: bool, user_card_limit: int | None) -> str:
    if scans_unlimited:
        return "unlimited"
    if is_default_user_card_limit(user_card_limit):
        return "default"
    return "custom"


def set_cms_tenant_user_scans_unlimited(
    admin_user_id: str,
    target_user_id: str,
    *,
    scans_unlimited: bool,
) -> dict[str, Any]:
    """Backward-compatible unlimited toggle."""
    mode = "unlimited" if scans_unlimited else "default"
    return set_cms_tenant_user_scan_entitlement(
        admin_user_id,
        target_user_id,
        mode=mode,
        limit=None,
    )


def set_cms_tenant_user_scan_entitlement(
    admin_user_id: str,
    target_user_id: str,
    *,
    mode: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """Set per-user scan entitlement: default (10 cards), unlimited, or custom limit."""
    mode_norm = str(mode or "default").strip().lower()
    if mode_norm not in {"default", "unlimited", "custom"}:
        raise ValueError("mode must be default, unlimited, or custom")

    if mode_norm == "custom":
        if limit is None or int(limit) < 1:
            raise ValueError("limit is required for custom mode and must be at least 1")
        if int(limit) > 100_000:
            raise ValueError("limit must be 100000 or less")
        next_unlimited = False
        next_limit = int(limit)
    elif mode_norm == "unlimited":
        next_unlimited = True
        next_limit = None
    else:
        next_unlimited = False
        next_limit = DEFAULT_FREEMIUM_CARD_LIMIT

    existing = get_admin_env_settings(admin_user_id)
    if not existing:
        raise ValueError("Admin not found")

    company_id = existing.get("company_id")
    with db_cursor(commit=True) as cur:
        if company_id:
            cur.execute(
                """
                SELECT u.id, r.name AS role
                FROM users u
                JOIN roles r ON r.id = u.role_id
                WHERE u.id = %s
                  AND u.deleted_at IS NULL
                  AND u.company_id = %s
                  AND r.name IN (%s, %s)
                """,
                (target_user_id, company_id, ROLE_ADMIN, ROLE_USER),
            )
        else:
            if str(target_user_id) != str(admin_user_id):
                raise ValueError("User not found in this tenant")
            cur.execute(
                """
                SELECT u.id, r.name AS role
                FROM users u
                JOIN roles r ON r.id = u.role_id
                WHERE u.id = %s
                  AND u.deleted_at IS NULL
                  AND r.name = %s
                """,
                (target_user_id, ROLE_ADMIN),
            )
        row = cur.fetchone()
        if not row:
            raise ValueError("User not found in this tenant")
        if str(row.get("role") or "").upper() == ROLE_SUPER_ADMIN:
            raise ValueError("Cannot change entitlement for Super Admin")

        cur.execute(
            """
            UPDATE users
            SET scans_unlimited = %s,
                user_card_limit = %s,
                updated_at = NOW()
            WHERE id = %s
            RETURNING id
            """,
            (bool(next_unlimited), next_limit, target_user_id),
        )
        if not cur.fetchone():
            raise ValueError("User not found in this tenant")

    summary = list_cms_tenant_users(admin_user_id)
    updated = next((u for u in summary["users"] if u["id"] == str(target_user_id)), None)
    return {
        "success": True,
        "scans_unlimited": bool(next_unlimited),
        "user_card_limit": next_limit,
        "scan_entitlement_mode": _scan_entitlement_mode(bool(next_unlimited), next_limit),
        "user": updated,
        "users": summary,
    }
