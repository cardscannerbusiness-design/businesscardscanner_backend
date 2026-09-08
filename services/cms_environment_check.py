"""CMS Environment Connection check — verify stored config vs live project runtime."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from services.admin_env_service import get_admin_env_settings
from services.admin_runtime_config import load_admin_env_raw, use_admin_env_payload
from services.email_service import is_email_configured

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wa_creds_complete(wa: dict[str, Any]) -> bool:
    return bool(
        str(wa.get("access_token") or "").strip()
        and str(wa.get("phone_number_id") or "").strip()
    )


def _integ(status: str, message: str = "") -> dict[str, str]:
    return {"status": status, "message": message}


def _probe_whatsapp_graph(wa: dict[str, Any]) -> dict[str, Any]:
    """Live Graph check using CMS (or runtime) WhatsApp credentials. Does not send a message."""
    token = str(wa.get("access_token") or "").strip()
    phone_id = str(wa.get("phone_number_id") or "").strip()
    version = (
        str(wa.get("graph_api_version") or wa.get("api_version") or "").strip() or "v25.0"
    )
    waba = str(wa.get("business_account_id") or "").strip()
    template_name = str(
        wa.get("card_received_template_name")
        or wa.get("business_card_template_name")
        or wa.get("scan_template_name")
        or wa.get("template_name")
        or ""
    ).strip()

    if not token or not phone_id:
        return {
            "ok": False,
            "status": "fail",
            "message": "Missing access_token or phone_number_id.",
            "phone": None,
            "template": None,
        }

    phone_meta: dict[str, Any] | None = None
    template_meta: dict[str, Any] | None = None
    errors: list[str] = []

    try:
        url = f"https://graph.facebook.com/{version}/{phone_id}"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "fields": "display_phone_number,verified_name,status,name_status,quality_rating",
            },
            timeout=20,
        )
        if response.status_code >= 400:
            errors.append(f"Phone lookup HTTP {response.status_code}: {response.text[:240]}")
        else:
            phone_meta = response.json()
    except requests.RequestException as exc:
        errors.append(f"Phone lookup failed: {exc}")

    if waba and template_name:
        try:
            url = f"https://graph.facebook.com/{version}/{waba}/message_templates"
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"name": template_name, "limit": 5},
                timeout=20,
            )
            if response.status_code >= 400:
                errors.append(f"Template lookup HTTP {response.status_code}")
            else:
                rows = response.json().get("data") or []
                approved = [
                    r for r in rows if str(r.get("status") or "").upper() == "APPROVED"
                ]
                chosen = approved[0] if approved else (rows[0] if rows else None)
                if chosen:
                    template_meta = {
                        "name": chosen.get("name"),
                        "language": chosen.get("language"),
                        "status": chosen.get("status"),
                    }
                else:
                    errors.append(f"Template '{template_name}' not found on WABA.")
        except requests.RequestException as exc:
            errors.append(f"Template lookup failed: {exc}")

    if phone_meta and not errors:
        display = phone_meta.get("display_phone_number") or phone_id
        name = phone_meta.get("verified_name") or ""
        msg = f"Connected as {display}"
        if name:
            msg += f" ({name})"
        if template_meta:
            msg += (
                f" · template {template_meta.get('name')} "
                f"[{template_meta.get('status')}/{template_meta.get('language')}]"
            )
        return {
            "ok": True,
            "status": "pass",
            "message": msg,
            "phone": phone_meta,
            "template": template_meta,
        }

    if phone_meta and errors:
        return {
            "ok": True,
            "status": "warn",
            "message": "; ".join(errors),
            "phone": phone_meta,
            "template": template_meta,
        }

    return {
        "ok": False,
        "status": "fail",
        "message": "; ".join(errors) or "WhatsApp Graph credentials rejected.",
        "phone": phone_meta,
        "template": template_meta,
    }


def check_admin_environment(admin_id: str) -> dict[str, Any]:
    """Verify CMS-stored config can load into the project runtime for this Admin."""
    item = get_admin_env_settings(admin_id)
    if not item:
        raise ValueError("Admin not found")

    checked_at = _iso_now()
    stored = bool(item.get("has_settings"))
    locks = item.get("channel_locks") or {}
    wa_locked = bool(locks.get("whatsapp"))
    email_locked = bool(locks.get("email"))
    sheets_locked = bool(locks.get("google_sheets"))

    raw = load_admin_env_raw(admin_id) if stored else None
    wa_raw = dict((raw or {}).get("whatsapp") or {})
    em_raw = dict((raw or {}).get("email") or {})
    tpl_raw = dict((raw or {}).get("templates") or {})
    sheets = item.get("google_sheets") or {}

    wa_enabled_flag = bool(wa_raw.get("enabled"))
    wa_complete = _wa_creds_complete(wa_raw)
    # Same heal as main-app runtime: unlocked + complete creds → treat as active.
    wa_runtime_active = wa_complete and (wa_enabled_flag or not wa_locked)

    configuration_loaded = False
    runtime_uses_cms_whatsapp = False
    if stored and raw is not None:
        with use_admin_env_payload(
            admin_user_id=admin_id,
            whatsapp={**wa_raw, "enabled": True} if wa_runtime_active else wa_raw,
            email=em_raw,
            templates=tpl_raw,
            force_channels=False,
        ) as payload:
            configuration_loaded = True
            runtime_uses_cms_whatsapp = bool(payload.get("whatsapp")) and _wa_creds_complete(
                payload.get("whatsapp") or {}
            )

    integrations: dict[str, dict[str, str]] = {}

    # --- WhatsApp ---
    if wa_locked and not wa_complete:
        integrations["whatsapp"] = _integ(
            "disabled",
            "Locked and no CMS credentials saved. Unlock + Enable, then save token & phone number id.",
        )
    elif wa_locked:
        integrations["whatsapp"] = _integ(
            "disabled",
            "Channel locked for this company — main app will skip WhatsApp sends.",
        )
    elif not wa_complete:
        integrations["whatsapp"] = _integ(
            "fail",
            "Missing access_token or phone_number_id in CMS. Save WhatsApp credentials.",
        )
    else:
        probe = _probe_whatsapp_graph(wa_raw)
        if not wa_enabled_flag and not wa_locked:
            # Credentials usable via auto-heal when unlocked
            note = "Unlocked — CMS credentials will be used by the main app."
            if probe["status"] == "pass":
                integrations["whatsapp"] = _integ("pass", f"{probe['message']} · {note}")
            else:
                integrations["whatsapp"] = _integ(
                    probe["status"],
                    f"{probe['message']} · {note}",
                )
        else:
            integrations["whatsapp"] = _integ(probe["status"], probe["message"])

    # --- Email (server .env) ---
    if email_locked:
        integrations["email"] = _integ("disabled", "Locked for this company.")
    elif is_email_configured():
        integrations["email"] = _integ("pass", "Server SMTP/.env configured.")
    else:
        integrations["email"] = _integ("warn", "Server email not configured in .env.")

    # --- Google Sheets ---
    sheets_configured = bool(
        str(sheets.get("google_sheet_id") or "").strip()
        or sheets.get("google_service_account_json_set")
        or str(sheets.get("google_oauth_client_id") or "").strip()
    )
    if sheets_locked:
        integrations["google_sheets"] = _integ("disabled", "Locked for this company.")
    elif not sheets_configured:
        integrations["google_sheets"] = _integ("disabled", "Not configured in CMS.")
    elif sheets.get("enabled"):
        integrations["google_sheets"] = _integ(
            "pass",
            "Configured & enabled (use Test on Google Sheets tab to verify API access).",
        )
    else:
        integrations["google_sheets"] = _integ(
            "warn",
            "Sheet ID saved but channel Enabled is off.",
        )

    checks = {
        "configurationExists": stored,
        "configurationLoaded": configuration_loaded,
        "backendReachable": True,
        "environmentSynchronized": stored and configuration_loaded,
        "whatsappEnabled": wa_runtime_active,
        "whatsappCredentialsComplete": wa_complete,
        "whatsappChannelUnlocked": not wa_locked,
        "whatsappRuntimeUsesCms": runtime_uses_cms_whatsapp,
        "emailChannelUnlocked": not email_locked,
        "googleSheetsChannelUnlocked": not sheets_locked,
    }

    wa_status = (integrations.get("whatsapp") or {}).get("status")
    blocking_wa = wa_status in {"fail"} or (
        wa_complete and wa_locked and wa_status == "disabled"
    )
    # Success = CMS config stored & loadable; WhatsApp Graph not hard-fail when unlocked+complete
    success = stored and configuration_loaded and not (
        wa_complete and not wa_locked and wa_status == "fail"
    )

    reason = None
    action = None
    if not stored:
        reason = "No CMS environment saved for this Admin."
        action = "Fill WhatsApp / Sheets settings and click Save Environment."
    elif wa_complete and wa_locked:
        reason = "WhatsApp credentials are saved but the channel is locked."
        action = "Turn Enabled ON on the WhatsApp tab (or Unlock on Environment)."
    elif not wa_complete and not wa_locked:
        reason = "WhatsApp is unlocked but credentials are incomplete."
        action = "Save access_token and phone_number_id, then Check again."
    elif wa_status == "fail":
        reason = (integrations.get("whatsapp") or {}).get("message") or "WhatsApp Graph check failed."
        action = "Fix the Meta token / phone number id, Save, then Check again."
    elif not success:
        reason = "Environment checks did not pass."
        action = "Review integration statuses below."

    cms_version = 1 if stored else 0
    return {
        "success": success,
        "status": "connected" if success else "failed",
        "message": (
            "Environment connected — CMS config loads into the project runtime."
            if success
            else "Environment connection failed."
        ),
        "reason": reason,
        "action": action,
        "sync_status": "connected" if success else "failed",
        "checked_at": checked_at,
        "tenant": {
            "admin_id": item.get("admin_id"),
            "tenant_id": item.get("tenant_id") or item.get("company_id"),
            "company_name": item.get("company_name"),
            "email": item.get("email"),
        },
        "versions": {
            "cms": cms_version,
            "project": cms_version if success else None,
            "synchronized": bool(success),
        },
        "checks": checks,
        "integrations": integrations,
        "configuration": {
            "stored": stored,
            "status": "stored" if stored else "missing",
            "updated_at": item.get("settings_updated_at"),
        },
        "whatsapp_runtime": {
            "enabled_flag": wa_enabled_flag,
            "channel_locked": wa_locked,
            "credentials_complete": wa_complete,
            "uses_cms_credentials": runtime_uses_cms_whatsapp,
            "template": str(
                wa_raw.get("card_received_template_name")
                or wa_raw.get("business_card_template_name")
                or wa_raw.get("template_name")
                or ""
            ).strip()
            or None,
        },
    }
