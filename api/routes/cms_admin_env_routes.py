"""Super Admin CMS — per-Admin WhatsApp / Email env settings."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth.audit_service import log_action
from auth.constants import (
    AUDIT_SCANS_UNLIMITED_UPDATED,
    AUDIT_USER_SCAN_ENTITLEMENT_UPDATED,
    ROLE_SUPER_ADMIN,
)
from auth.dependencies import get_current_user, require_role
from services.admin_env_service import (
    EMAIL_KEYS,
    TEMPLATE_KEYS,
    WHATSAPP_KEYS,
    delete_admin_env_settings as delete_admin_env,
    get_admin_env_settings as get_admin_env,
    list_admin_env_settings as list_admins_with_env,
    list_cms_tenant_users,
    merge_admin_env_for_test,
    set_admin_channel_locks,
    set_admin_company_email_display_name,
    set_cms_tenant_user_scans_unlimited,
    set_cms_tenant_user_scan_entitlement,
    upsert_admin_env_settings as upsert_admin_env,
)
from services.cms_app_access import update_channel_locks
from services.company_lifecycle import CompanyNotFoundError, remove_cms_client
from services.admin_runtime_config import use_admin_env_payload
from services.email_service import is_email_configured, send_business_thank_you_email
from services.email_template_service import get_thank_you_shell
from services.whatsapp_service import (
    CARD_RECEIVED_TEMPLATE_NAME,
    _active_whatsapp_template_language,
    _active_whatsapp_template_name,
    build_card_received_template_components,
    fetch_waba_message_template,
    is_whatsapp_configured,
    resolve_template_language,
    send_whatsapp_template,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cms", tags=["CMS"])


class AdminEnvUpdateRequest(BaseModel):
    whatsapp: dict[str, Any] | None = Field(default=None)
    email: dict[str, Any] | None = Field(default=None)
    templates: dict[str, Any] | None = Field(default=None)
    google_sheets: dict[str, Any] | None = Field(default=None)


class ChannelLocksUpdateRequest(BaseModel):
    """CMS kill-switches: locked=true turns the channel off for that company in the app."""

    whatsapp: bool | None = None
    email: bool | None = None
    google_sheets: bool | None = None


CmsChannelLocksRequest = ChannelLocksUpdateRequest


class EmailDisplayNameUpdateRequest(BaseModel):
    """From header display name only. Empty string restores the default."""

    email_display_name: str = ""


class CmsWhatsAppTestRequest(BaseModel):
    contact_phone: str = Field(..., min_length=6, description="Recipient phone for the test send")
    full_name: str = Field(default="Test Contact")
    event_name: str = Field(default="CMS Test")
    whatsapp: dict[str, Any] | None = None
    templates: dict[str, Any] | None = None


class CmsWhatsAppInspectRequest(BaseModel):
    whatsapp: dict[str, Any] | None = None


class CmsEmailTestRequest(BaseModel):
    contact_email: str = Field(..., min_length=3, description="Recipient email for the test send")
    email: dict[str, Any] | None = None
    templates: dict[str, Any] | None = None


@router.get(
    "/email-shell",
    summary="Fixed thank-you.html shell for CMS preview",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def get_email_shell():
    return {
        "shell": get_thank_you_shell(),
        "note": "Fixed chrome for every Admin. Only BODY_HTML content is editable in CMS.",
    }


@router.get(
    "/admin-env",
    summary="List Admins with per-Admin env settings",
    description="Super Admin only. Does not include Super Admin accounts.",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def list_admin_env():
    items = list_admins_with_env()
    return {
        "items": items,
        "total": len(items),
        "whatsapp_keys": list(WHATSAPP_KEYS),
        "email_keys": list(EMAIL_KEYS),
        "template_keys": list(TEMPLATE_KEYS),
    }


@router.get(
    "/admin-env/{admin_id}",
    summary="Get one Admin env settings",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def get_one_admin_env(admin_id: str):
    item = get_admin_env(admin_id)
    if not item:
        raise HTTPException(status_code=404, detail="Admin not found.")
    return item


@router.put(
    "/admin-env/{admin_id}",
    summary="Create or update per-Admin WhatsApp/Email env settings",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def put_admin_env(admin_id: str, body: AdminEnvUpdateRequest, request: Request):
    actor = get_current_user(request)
    try:
        item = upsert_admin_env(
            admin_id,
            whatsapp=body.whatsapp,
            email=body.email,
            templates=body.templates,
            google_sheets=body.google_sheets,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_action(
        str(actor["id"]),
        "cms_admin_env_updated",
        ip=request.client.host if request.client else "",
        new_value={
            "admin_id": admin_id,
            "whatsapp_keys": list((body.whatsapp or {}).keys()),
            "email_keys": list((body.email or {}).keys()),
            "template_keys": list((body.templates or {}).keys()),
        },
    )
    return {"success": True, "item": item}


@router.put(
    "/admin-env/{admin_id}/channel-locks",
    summary="Lock or unlock WhatsApp / Email / Google Sheets for this Admin's company",
    description=(
        "When locked=true, that channel is turned off for all users in the Admin's company "
        "in the main app (send/sync blocked)."
    ),
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
@router.patch(
    "/admin-env/{admin_id}/channel-locks",
    summary="Lock or unlock main-app channels for this Admin without changing CMS internals",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def put_admin_channel_locks(admin_id: str, body: ChannelLocksUpdateRequest, request: Request):
    actor = get_current_user(request)
    payload = {
        key: value
        for key, value in {
            "whatsapp": body.whatsapp,
            "email": body.email,
            "google_sheets": body.google_sheets,
        }.items()
        if value is not None
    }
    if not payload:
        raise HTTPException(status_code=400, detail="Provide at least one channel lock flag.")
    try:
        update_channel_locks(admin_id, payload)
        item = set_admin_channel_locks(admin_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_action(
        str(actor["id"]),
        "cms_channel_locks_updated",
        ip=request.client.host if request.client else "",
        new_value={"admin_id": admin_id, "channel_locks": item.get("channel_locks")},
    )
    return {"success": True, "item": item, "channel_locks": item.get("channel_locks")}


@router.put(
    "/admin-env/{admin_id}/email-display-name",
    summary="Set the From display name for this Admin's company",
    description=(
        "Stores companies.email_display_name only. Does not change From address, "
        "Reply-To, Receive email, SMTP, or SES credentials."
    ),
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def put_admin_email_display_name(
    admin_id: str,
    body: EmailDisplayNameUpdateRequest,
    request: Request,
):
    actor = get_current_user(request)
    try:
        item = set_admin_company_email_display_name(admin_id, body.email_display_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_action(
        str(actor["id"]),
        "cms_email_display_name_updated",
        ip=request.client.host if request.client else "",
        new_value={
            "admin_id": admin_id,
            "company_id": item.get("company_id"),
            "email_display_name": item.get("email_display_name"),
        },
    )
    return {
        "success": True,
        "item": item,
        "company_id": item.get("company_id"),
        "email_display_name": item.get("email_display_name") or "",
    }


@router.delete(
    "/admin-env/{admin_id}",
    summary="Remove per-Admin CMS WhatsApp/Email/template settings",
    description="Deletes the CMS env row so this Admin falls back to global .env.",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def delete_one_admin_env_after_locks(admin_id: str, request: Request):
    actor = get_current_user(request)
    try:
        item = delete_admin_env(admin_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_action(
        str(actor["id"]),
        "cms_admin_env_removed",
        ip=request.client.host if request.client else "",
        new_value={"admin_id": admin_id},
    )
    return {"success": True, "item": item}


@router.delete(
    "/clients/{admin_id}",
    summary="Remove a CMS client",
    description="Deletes the company, its Admin/Users, CMS env, and registration requests so the client leaves both the app and CMS.",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def delete_cms_client(admin_id: str, request: Request):
    actor = get_current_user(request)
    try:
        purged = remove_cms_client(admin_id)
    except CompanyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_action(
        str(actor["id"]),
        "cms_client_removed",
        ip=request.client.host if request.client else "",
        new_value={"admin_id": admin_id, **purged},
    )
    return {"success": True, **purged}


@router.post(
    "/admin-env/{admin_id}/test-whatsapp",
    summary="Send a test WhatsApp using this Admin's CMS credentials",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
async def test_admin_whatsapp(admin_id: str, body: CmsWhatsAppTestRequest):
    try:
        merged = merge_admin_env_for_test(
            admin_id,
            whatsapp=body.whatsapp,
            templates=body.templates,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    wa = dict(merged["whatsapp"] or {})
    wa["enabled"] = True

    with use_admin_env_payload(
        admin_user_id=admin_id,
        whatsapp=wa,
        email=merged.get("email"),
        templates=merged.get("templates"),
        force_channels=True,
    ):
        if not is_whatsapp_configured():
            raise HTTPException(
                status_code=400,
                detail="WhatsApp is not configured. Fill ACCESS_TOKEN and PHONE_NUMBER_ID, then try again.",
            )
        contact = {
            "fullName": body.full_name,
            "name": body.full_name,
            "eventName": body.event_name,
        }
        # Prefer CMS form/saved template names — never silently substitute card_final_ula
        # when the Admin has set journey_stack1 (or any other Meta-approved name).
        wa_name = str(
            wa.get("card_received_template_name")
            or wa.get("business_card_template_name")
            or wa.get("scan_template_name")
            or wa.get("template_name")
            or ""
        ).strip()
        template_name = wa_name or _active_whatsapp_template_name(CARD_RECEIVED_TEMPLATE_NAME)
        if (template_name or "").strip().lower() in {
            "",
            "cardscan_intro",
            "hello_world",
        }:
            template_name = CARD_RECEIVED_TEMPLATE_NAME or "card_final_ula"

        meta_template = fetch_waba_message_template(template_name)
        if meta_template is None and wa_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Template '{template_name}' was not found (or not APPROVED) on this "
                    "WhatsApp Business Account. Check the name in Meta Business Manager, "
                    "then Save Environment and test again."
                ),
            )

        language_code = (
            str(wa.get("template_language_code") or "").strip()
            or (str(meta_template.get("language") or "").strip() if meta_template else "")
            or resolve_template_language(template_name)
            or _active_whatsapp_template_language()
            or "en"
        )
        if language_code.lower() in {"en_us", "english"} and (
            template_name or ""
        ).strip().lower() in {
            (CARD_RECEIVED_TEMPLATE_NAME or "").lower(),
            "card_final_ula",
        }:
            language_code = "en"

        components = build_card_received_template_components(
            contact,
            template_name=template_name,
            meta_template=meta_template,
        )
        try:
            result = await asyncio.to_thread(
                send_whatsapp_template,
                body.contact_phone,
                template_name=template_name,
                language_code=language_code,
                components=components,
            )
        except Exception as exc:
            logger.error(
                "CMS WhatsApp test failed for admin=%s template=%s: %s",
                admin_id,
                template_name,
                exc,
                exc_info=True,
            )
            detail = str(exc)
            if "132000" in detail or "132001" in detail:
                detail = (
                    f"{detail} — Template '{template_name}' parameter count/header does not "
                    "match Meta. Fix the CMS Templates section (header format + body {{N}} "
                    "tokens) to match the approved Meta template, Save, then Test again."
                )
            raise HTTPException(status_code=502, detail=detail) from exc

    message_id = (result.get("messages") or [{}])[0].get("id")
    return {
        "success": True,
        "message_id": message_id,
        "template": template_name,
        "language": language_code,
        "to": body.contact_phone,
    }


@router.post(
    "/admin-env/{admin_id}/test-email",
    summary="Send a test thank-you email using this Admin's CMS SMTP credentials",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
async def test_admin_email(admin_id: str, body: CmsEmailTestRequest):
    try:
        merged = merge_admin_env_for_test(
            admin_id,
            email=body.email,
            templates=body.templates,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    em = dict(merged["email"] or {})
    em["enabled"] = True

    with use_admin_env_payload(
        admin_user_id=admin_id,
        whatsapp=merged.get("whatsapp"),
        email=em,
        templates=merged.get("templates"),
        force_channels=True,
    ):
        if not is_email_configured():
            raise HTTPException(
                status_code=400,
                detail="Email is not configured. Set Amazon SES SMTP_EXTERNAL_* credentials in CMS or server .env, then try again."
            )
        try:
            from services.admin_env_service import get_cms_receive_email

            # Data-receive copy → CMS Receive email (Admin/User scan path).
            # Super Admin scans do not use CMS; they keep own email / SUPERADMIN_EMAIL.
            receive_cc = get_cms_receive_email(admin_id)
            admin_item = get_admin_env(admin_id) or {}
            result = await asyncio.to_thread(
                send_business_thank_you_email,
                body.contact_email,
                recipient_name="Test Contact",
                contact={
                    "fullName": "Test Contact",
                    "email": body.contact_email,
                    "eventName": "CMS Test",
                    "owner_company_id": admin_item.get("company_id"),
                    "company_id": admin_item.get("company_id"),
                },
                sender_role="ADMIN",
                cc_addresses=[receive_cc] if receive_cc else None,
            )
        except Exception as exc:
            logger.error("CMS Email test failed for admin=%s: %s", admin_id, exc, exc_info=True)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Email send failed.")

    cc_emails = result.get("cc_emails") or []
    return {
        "success": True,
        "to": result.get("recipient_email") or body.contact_email,
        "subject": result.get("subject"),
        "cc_emails": cc_emails,
        "receive_email": cc_emails[0] if cc_emails else receive_cc,
        "cc_delivery": result.get("cc_delivery") or [],
    }


class CmsGoogleSheetsTestRequest(BaseModel):
    google_sheets: dict[str, Any] | None = None


@router.get(
    "/admin-env/{admin_id}/users",
    summary="List tenant users with active and connected counts",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
@router.get(
    "/admin-env/{admin_id}/test-users",
    summary="Tenant users for CMS Environment (active / connected)",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def get_admin_tenant_users(admin_id: str):
    try:
        return list_cms_tenant_users(admin_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class CmsUserScansUnlimitedRequest(BaseModel):
    scans_unlimited: bool = Field(
        ...,
        description="When true, this user gets unlimited card scans (Freemium bypass for that user only).",
    )


class CmsUserScanEntitlementRequest(BaseModel):
    mode: str = Field(
        ...,
        description="default = company Freemium pool, unlimited = no cap, custom = per-user numeric limit",
    )
    limit: int | None = Field(
        None,
        ge=1,
        le=100_000,
        description="Required when mode=custom (e.g. 500)",
    )


@router.patch(
    "/admin-env/{admin_id}/users/{user_id}/scans-unlimited",
    summary="Grant or revoke unlimited card scans for one tenant user",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def patch_tenant_user_scans_unlimited(
    admin_id: str,
    user_id: str,
    body: CmsUserScansUnlimitedRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    try:
        result = set_cms_tenant_user_scans_unlimited(
            admin_id,
            user_id,
            scans_unlimited=body.scans_unlimited,
        )
    except ValueError as exc:
        msg = str(exc)
        status = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=status, detail=msg) from exc

    target = result.get("user") or {}
    log_action(
        str(user["id"]),
        AUDIT_SCANS_UNLIMITED_UPDATED,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        new_value={
            "cms_admin_id": admin_id,
            "user_id": user_id,
            "email": target.get("email"),
            "scans_unlimited": body.scans_unlimited,
        },
    )
    return result


@router.patch(
    "/admin-env/{admin_id}/users/{user_id}/scan-entitlement",
    summary="Set per-user scan entitlement (default, unlimited, or custom limit)",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def patch_tenant_user_scan_entitlement(
    admin_id: str,
    user_id: str,
    body: CmsUserScanEntitlementRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    try:
        result = set_cms_tenant_user_scan_entitlement(
            admin_id,
            user_id,
            mode=body.mode,
            limit=body.limit,
        )
    except ValueError as exc:
        msg = str(exc)
        status = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=status, detail=msg) from exc

    target = result.get("user") or {}
    log_action(
        str(user["id"]),
        AUDIT_USER_SCAN_ENTITLEMENT_UPDATED,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        new_value={
            "cms_admin_id": admin_id,
            "user_id": user_id,
            "email": target.get("email"),
            "mode": body.mode,
            "limit": body.limit,
            "scans_unlimited": result.get("scans_unlimited"),
            "user_card_limit": result.get("user_card_limit"),
        },
    )
    return result


@router.post(
    "/admin-env/{admin_id}/check-environment",
    summary="Check CMS env is stored and loadable into the project runtime",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def check_admin_environment_route(admin_id: str):
    from services.cms_environment_check import check_admin_environment

    try:
        return check_admin_environment(admin_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/admin-env/{admin_id}/whatsapp-inspect",
    summary="Inspect WhatsApp CMS credentials, template, and Graph connectivity",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def inspect_admin_whatsapp(admin_id: str, body: CmsWhatsAppInspectRequest | None = None):
    """Non-send health checklist for the WhatsApp tab / setup checker."""
    from services.cms_environment_check import _probe_whatsapp_graph, _wa_creds_complete

    payload = body or CmsWhatsAppInspectRequest()
    try:
        merged = merge_admin_env_for_test(admin_id, whatsapp=payload.whatsapp)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    item = get_admin_env(admin_id) or {}
    locks = item.get("channel_locks") or {}
    wa = dict(merged.get("whatsapp") or {})
    checklist: list[dict[str, Any]] = []

    def add(check_id: str, label: str, status: str, detail: str, fix: str | None = None) -> None:
        checklist.append(
            {
                "id": check_id,
                "label": label,
                "status": status,
                "detail": detail,
                **({"fix": fix} if fix else {}),
            }
        )

    complete = _wa_creds_complete(wa)
    add(
        "credentials",
        "Access token + Phone number ID",
        "ok" if complete else "error",
        "CMS credentials present." if complete else "Missing access_token or phone_number_id.",
        None if complete else "Save both fields on the WhatsApp tab.",
    )
    add(
        "enabled",
        "WhatsApp enabled flag",
        "ok" if bool(wa.get("enabled")) else "warn",
        "enabled=true" if bool(wa.get("enabled")) else "enabled=false (unlocked + complete still auto-activates).",
    )
    locked = bool(locks.get("whatsapp"))
    add(
        "channel_lock",
        "Channel unlocked for main app",
        "error" if locked else "ok",
        "Locked — main app skips WhatsApp." if locked else "Unlocked — main app may send.",
        "Turn Enabled ON (or Unlock on Environment)." if locked else None,
    )

    probe = _probe_whatsapp_graph(wa) if complete else {
        "ok": False,
        "status": "fail",
        "message": "Skipped — credentials incomplete.",
        "phone": None,
        "template": None,
    }
    add(
        "graph",
        "Meta Graph API",
        "ok" if probe.get("ok") and probe.get("status") == "pass" else (
            "warn" if probe.get("status") == "warn" else "error"
        ),
        str(probe.get("message") or ""),
        None if probe.get("ok") else "Fix token / phone_number_id / WABA and retry.",
    )

    tpl = str(
        wa.get("card_received_template_name")
        or wa.get("business_card_template_name")
        or wa.get("template_name")
        or ""
    ).strip()
    tpl_meta = probe.get("template") if isinstance(probe.get("template"), dict) else None
    if tpl:
        approved = str((tpl_meta or {}).get("status") or "").upper() == "APPROVED"
        add(
            "template",
            f"Template '{tpl}'",
            "ok" if approved else ("warn" if tpl_meta else "error"),
            (
                f"APPROVED ({(tpl_meta or {}).get('language')})"
                if approved
                else (
                    f"Found status={(tpl_meta or {}).get('status')}"
                    if tpl_meta
                    else "Not found on WABA (set business_account_id + template name)."
                )
            ),
        )
    else:
        add(
            "template",
            "Outbound template name",
            "warn",
            "No card_received / business_card template name set in CMS.",
            "Set the template name to match Meta (e.g. journey_stack1).",
        )

    errors = sum(1 for c in checklist if c["status"] == "error")
    warns = sum(1 for c in checklist if c["status"] == "warn")
    oks = sum(1 for c in checklist if c["status"] == "ok")
    overall = "error" if errors else ("warn" if warns else "ok")

    return {
        "success": overall != "error",
        "overall": overall,
        "summary": {
            "checks_total": len(checklist),
            "checks_ok": oks,
            "checks_error": errors,
        },
        "checklist": checklist,
        "phone": probe.get("phone"),
        "waba": {"id": wa.get("business_account_id") or None},
        "waba_phones": [],
        "webhook": {"subscribed": None, "reason": "Use subscribe endpoint to verify."},
        "templates": [tpl_meta] if tpl_meta else [],
        "configured_templates": [
            {
                "label": "Primary outbound",
                "configured_name": tpl or "",
                "configured_language": str(wa.get("template_language_code") or ""),
                "found": bool(tpl_meta),
                "meta_status": (tpl_meta or {}).get("status"),
                "meta_language": (tpl_meta or {}).get("language"),
                "approved": str((tpl_meta or {}).get("status") or "").upper() == "APPROVED",
            }
        ]
        if tpl
        else [],
        "meta_console_links": {
            "whatsapp_manager": "https://business.facebook.com/wa/manage/",
            "developer_app": None,
            "api_setup": None,
        },
        "still_requires_meta_console": [
            "Approve templates in Meta WhatsApp Manager",
            "Confirm delivery statuses in Meta message logs when wamid is accepted but not received",
        ],
    }


@router.post(
    "/admin-env/{admin_id}/whatsapp-subscribe-webhook",
    summary="Subscribe the Meta app to this WABA's webhooks",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def subscribe_admin_whatsapp_webhook(admin_id: str, body: dict[str, Any] | None = None):
    from services.whatsapp_webhook_setup import ensure_waba_webhook_subscription
    from services.admin_runtime_config import use_admin_env_payload

    body = body or {}
    try:
        merged = merge_admin_env_for_test(admin_id, whatsapp=body.get("whatsapp"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    wa = dict(merged.get("whatsapp") or {})
    wa["enabled"] = True
    with use_admin_env_payload(
        admin_user_id=admin_id,
        whatsapp=wa,
        templates=merged.get("templates"),
        force_channels=True,
    ):
        result = ensure_waba_webhook_subscription()
    return {"success": bool(result.get("subscribed")), **result}


@router.post(
    "/admin-env/{admin_id}/test-google-sheets",
    summary="Probe Google Sheets access for this Admin using saved Sheet ID",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def test_admin_google_sheets(admin_id: str, body: CmsGoogleSheetsTestRequest):
    from services.google_sheets_service import probe_spreadsheet

    item = get_admin_env(admin_id)
    if not item:
        raise HTTPException(status_code=404, detail="Admin not found.")
    incoming = body.google_sheets or {}
    saved = item.get("google_sheets") or {}
    sheet_id = str(incoming.get("google_sheet_id") or saved.get("google_sheet_id") or "").strip()
    sheet_name = str(incoming.get("google_sheet_name") or saved.get("google_sheet_name") or "").strip()
    return probe_spreadsheet(spreadsheet_id=sheet_id, worksheet=sheet_name)
