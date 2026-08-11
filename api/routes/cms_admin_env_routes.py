"""Super Admin CMS — per-Admin WhatsApp / Email env settings."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth.audit_service import log_action
from auth.constants import ROLE_SUPER_ADMIN
from auth.dependencies import get_current_user, require_role
from services.admin_env_service import (
    EMAIL_KEYS,
    TEMPLATE_KEYS,
    WHATSAPP_KEYS,
    delete_admin_env_settings as delete_admin_env,
    get_admin_env_settings as get_admin_env,
    list_admin_env_settings as list_admins_with_env,
    merge_admin_env_for_test,
    upsert_admin_env_settings as upsert_admin_env,
)
from services.admin_runtime_config import use_admin_env_payload
from services.email_service import is_email_configured, send_business_thank_you_email
from services.email_template_service import get_thank_you_shell
from services.whatsapp_service import (
    CARD_RECEIVED_TEMPLATE_NAME,
    _active_whatsapp_template_language,
    _active_whatsapp_template_name,
    build_card_received_template_components,
    is_whatsapp_configured,
    send_whatsapp_template,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cms", tags=["CMS"])


class AdminEnvUpdateRequest(BaseModel):
    whatsapp: dict[str, Any] | None = Field(default=None)
    email: dict[str, Any] | None = Field(default=None)
    templates: dict[str, Any] | None = Field(default=None)


class CmsWhatsAppTestRequest(BaseModel):
    contact_phone: str = Field(..., min_length=6, description="Recipient phone for the test send")
    full_name: str = Field(default="Test Contact")
    event_name: str = Field(default="CMS Test")
    whatsapp: dict[str, Any] | None = None
    templates: dict[str, Any] | None = None


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


@router.delete(
    "/admin-env/{admin_id}",
    summary="Remove per-Admin CMS WhatsApp/Email/template settings",
    description="Deletes the CMS env row so this Admin falls back to global .env.",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def delete_one_admin_env(admin_id: str, request: Request):
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
        template_name = _active_whatsapp_template_name(CARD_RECEIVED_TEMPLATE_NAME)
        # cardscan_intro is not the production approved template on this WABA.
        if (template_name or "").strip().lower() in {
            "",
            "cardscan_intro",
            "hello_world",
        }:
            template_name = CARD_RECEIVED_TEMPLATE_NAME or "card_final_ula"
        language_code = _active_whatsapp_template_language() or "en"
        if language_code.lower() in {"en_us", "english"}:
            # Meta approved card_final_ula as "en"
            language_code = "en"

        components = build_card_received_template_components(
            contact, template_name=template_name
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
            # Last resort: known working production template
            fallback = CARD_RECEIVED_TEMPLATE_NAME or "card_final_ula"
            if template_name != fallback:
                logger.warning(
                    "CMS WhatsApp test %s failed (%s); retrying %s/en",
                    template_name,
                    exc,
                    fallback,
                )
                try:
                    result = await asyncio.to_thread(
                        send_whatsapp_template,
                        body.contact_phone,
                        template_name=fallback,
                        language_code="en",
                        components=build_card_received_template_components(
                            contact, template_name=fallback
                        ),
                    )
                    template_name = fallback
                except Exception as exc2:
                    logger.error(
                        "CMS WhatsApp test failed for admin=%s: %s",
                        admin_id,
                        exc2,
                        exc_info=True,
                    )
                    detail = str(exc2)
                    if "132000" in detail or "132001" in detail:
                        detail = (
                            f"{detail} — Set Message template name to '{fallback}' "
                            "and Template language to 'en', Save, then Test again. "
                            "card_final_ula needs VIDEO header + exactly 2 body vars."
                        )
                    raise HTTPException(status_code=502, detail=detail) from exc2
            else:
                logger.error(
                    "CMS WhatsApp test failed for admin=%s: %s", admin_id, exc, exc_info=True
                )
                detail = str(exc)
                if "132000" in detail:
                    detail = (
                        f"{detail} — Template '{template_name}' expects a different number of "
                        "body/header params. For card_final_ula use language 'en', VIDEO header, "
                        "and body variables {{1}} name + {{2}} event only."
                    )
                raise HTTPException(status_code=502, detail=detail) from exc

    message_id = (result.get("messages") or [{}])[0].get("id")
    return {
        "success": True,
        "message_id": message_id,
        "template": template_name,
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
                detail="Email is not configured. Fill SMTP_USER, SMTP_PASSWORD, and SMTP_FROM, then try again.",
            )
        try:
            result = await asyncio.to_thread(
                send_business_thank_you_email,
                body.contact_email,
                recipient_name="Test Contact",
                contact={
                    "fullName": "Test Contact",
                    "email": body.contact_email,
                    "eventName": "CMS Test",
                },
            )
        except Exception as exc:
            logger.error("CMS Email test failed for admin=%s: %s", admin_id, exc, exc_info=True)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Email send failed.")

    return {
        "success": True,
        "to": result.get("recipient_email") or body.contact_email,
        "subject": result.get("subject"),
    }
