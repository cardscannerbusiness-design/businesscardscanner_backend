import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from api.auth_context import get_receive_email_from_request
from api.outreach import (
    body_to_outreach_contact,
    email_response,
    is_online_mode,
    schedule_outreach_for_contact,
    whatsapp_response,
)
from api.schemas import (
    EmailMessageRequest,
    EmailTestRequest,
    LocalContactBody,
    WhatsAppCardReceivedRequest,
    WhatsAppChatReplyRegisterRequest,
    WhatsAppMessageRequest,
    WhatsAppTestRequest,
)
from services.email_service import email_queue, is_email_configured, send_business_thank_you_email
from services.whatsapp_inbound import (
    get_phone_verify_status,
    register_pending_chat_reply,
)
from services.whatsapp_service import (
    CARD_RECEIVED_TEMPLATE_NAME,
    build_card_received_template_components,
    get_whatsapp_chat_link_config,
    is_whatsapp_configured,
    send_business_card_template,
    send_whatsapp_message,
    send_whatsapp_template,
    send_whatsapp_text,
    whatsapp_queue,
)

router = APIRouter(tags=["Integrations"])
logger = logging.getLogger(__name__)


@router.get(
    "/integrations/whatsapp/chat-link",
    summary="wa.me link for user-initiated WhatsApp chat (Chats inbox)",
)
async def get_whatsapp_chat_link(prefill: str = "Hi, verify my number"):
    if not is_whatsapp_configured():
        raise HTTPException(
            status_code=503,
            detail="WhatsApp is not configured.",
        )
    config = get_whatsapp_chat_link_config(prefill_text=prefill)
    if not config.get("configured"):
        raise HTTPException(
            status_code=503,
            detail="WhatsApp business phone not available. Set WHATSAPP_BUSINESS_PHONE in .env.",
        )
    return {"success": True, **config}


@router.post(
    "/integrations/whatsapp/register-chat-reply",
    summary="Register contact for auto-reply when they message via wa.me QR",
)
async def register_whatsapp_chat_reply(body: WhatsAppChatReplyRegisterRequest):
    if not is_whatsapp_configured():
        raise HTTPException(status_code=503, detail="WhatsApp is not configured.")
    contact = body_to_outreach_contact_from_chat_register(body)
    try:
        result = register_pending_chat_reply(contact)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    link = get_whatsapp_chat_link_config()
    return {"success": True, **result, **link}


@router.post(
    "/integrations/whatsapp/start-verify",
    summary="Start WhatsApp verify — contact scans QR and messages to open Chats thread",
)
async def start_whatsapp_verify(body: WhatsAppChatReplyRegisterRequest):
    if not is_whatsapp_configured():
        raise HTTPException(status_code=503, detail="WhatsApp is not configured.")
    contact = body_to_outreach_contact_from_chat_register(body)
    try:
        result = register_pending_chat_reply(contact, verify_only=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    link = get_whatsapp_chat_link_config()
    return {"success": True, **result, **link}


@router.get(
    "/integrations/whatsapp/verify-status",
    summary="Poll whether a phone has messaged us (WhatsApp thread verified)",
)
async def whatsapp_verify_status(phone: str):
    if not phone.strip():
        raise HTTPException(status_code=400, detail="phone query parameter is required.")
    status = get_phone_verify_status(phone)
    return {"success": True, **status}


def body_to_outreach_contact_from_chat_register(body: WhatsAppChatReplyRegisterRequest) -> dict:
    emails = [e.strip() for e in (body.email, body.secondaryEmail) if e and str(e).strip()]
    phones = [p.strip() for p in (body.phone, body.secondaryPhone) if p and str(p).strip()]
    return {
        "fullName": body.fullName,
        "name": body.fullName,
        "firstName": body.firstName,
        "lastName": body.lastName,
        "designation": body.designation,
        "company": body.company,
        "companyName": body.company,
        "email": body.email,
        "emailAddress": body.email,
        "emails": emails,
        "phone": body.phone,
        "phones": phones,
        "website": body.website,
        "secondaryWebsite": body.secondaryWebsite,
        "address": body.address,
        "secondaryAddress": body.secondaryAddress,
    }


@router.post("/integrations/whatsapp/queue")
async def queue_whatsapp_message(request: WhatsAppMessageRequest):
    await whatsapp_queue.enqueue_message(request.contact_phone, request.message)
    return {"success": True, "message": "Message enqueued successfully"}


@router.post("/integrations/whatsapp/test")
async def test_whatsapp_message(request: WhatsAppTestRequest):
    if not is_whatsapp_configured():
        raise HTTPException(
            status_code=503,
            detail="WhatsApp is not configured. Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID in .env.",
        )

    try:
        if request.mode == "text":
            result = await asyncio.to_thread(send_whatsapp_text, request.contact_phone, request.message)
        elif request.mode == "template":
            result = await asyncio.to_thread(send_whatsapp_template, request.contact_phone)
        elif request.mode == "business-card":
            result = await asyncio.to_thread(
                send_business_card_template,
                request.contact_phone,
                "Contact",
            )
        else:
            result = await asyncio.to_thread(send_whatsapp_message, request.contact_phone, request.message)
    except Exception as exc:
        logger.error("WhatsApp test send failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    message_id = (result.get("messages") or [{}])[0].get("id")
    return {"success": True, "message_id": message_id, "response": result}


@router.post(
    "/integrations/whatsapp/card-received",
    summary="Send the approved card_final_ula WhatsApp template",
    description=(
        "Sends the card_final_ula NCS template with {{1}}=name and {{2}}=event "
        "to the provided phone number. Useful for Swagger testing."
    ),
)
async def send_card_received_template(request: WhatsAppCardReceivedRequest):
    if not is_whatsapp_configured():
        raise HTTPException(
            status_code=503,
            detail="WhatsApp is not configured. Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID in .env.",
        )

    contact = {"fullName": request.full_name, "eventName": request.event_name}
    try:
        result = await asyncio.to_thread(
            send_whatsapp_template,
            request.contact_phone,
            template_name=CARD_RECEIVED_TEMPLATE_NAME,
            components=build_card_received_template_components(contact),
        )
    except Exception as exc:
        logger.error("WhatsApp card-received send failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    message_id = (result.get("messages") or [{}])[0].get("id")
    return {"success": True, "message_id": message_id, "template": CARD_RECEIVED_TEMPLATE_NAME, "response": result}


@router.post("/integrations/email/queue")
async def queue_email_message(request: EmailMessageRequest):
    await email_queue.enqueue_message(request.contact_email, request.message)
    return {"success": True, "message": "Message enqueued successfully"}


@router.post(
    "/integrations/email/test",
    summary="Send a test thank-you email (Swagger / production check)",
)
async def test_email_message(request: EmailTestRequest):
    if not is_email_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Email is not configured. Set GMAIL_USER + GMAIL_APP_PASSWORD "
                "(or SMTP_USER + SMTP_PASSWORD) in .env."
            ),
        )

    try:
        result = await asyncio.to_thread(
            send_business_thank_you_email,
            request.contact_email,
            test_override=request.test_override or None,
        )
    except Exception as exc:
        logger.error("Email test send failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Email send failed.")

    return {"success": True, "result": result}


@router.post("/api/outreach/thank-you", summary="Send thank-you after review save")
async def send_thank_you_outreach(body: LocalContactBody, request: Request):
    try:
        contact = body_to_outreach_contact(body)
        whatsapp_result, email_result = await schedule_outreach_for_contact(
            contact,
            online_mode=is_online_mode(body.connectionMode),
            contact_id=None,
            skip_whatsapp=body.skipWhatsApp,
            skip_email=body.skipEmail,
            scanner_email=get_receive_email_from_request(request),
        )
        return {
            "success": True,
            **whatsapp_response(whatsapp_result),
            **email_response(email_result),
        }
    except Exception as exc:
        logger.error("Thank-you outreach failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
