"""WhatsApp and email outreach helpers shared by contact and lead routes."""
import asyncio
import logging
from typing import Any

from services import contact_storage as storage
from services.email_service import is_test_recipient_mode, schedule_email_for_contact
from services.whatsapp_service import schedule_whatsapp_for_contact

from api.schemas import LocalContactBody

logger = logging.getLogger(__name__)


def _delivery_state(result: dict[str, Any]) -> str:
    if result.get("sent") is True:
        return "success"
    if result.get("attempted") is True:
        return "failure"
    if result.get("error"):
        return "not_sent"
    return "pending"


def is_online_mode(value: str | None) -> bool:
    return str(value or "online").strip().lower() != "offline"


def body_to_outreach_contact(body: LocalContactBody) -> dict[str, Any]:
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
        "countryCode": body.countryCode,
        "countryName": body.countryName,
        "phones": phones,
        "website": body.website,
        "secondaryWebsite": body.secondaryWebsite,
        "address": body.address,
        "secondaryAddress": body.secondaryAddress,
        "eventName": body.eventName,
        "eventDay": body.eventDay,
    }


def whatsapp_response(result: dict[str, Any]) -> dict[str, Any]:
    attempted = result.get("attempted")
    sent = bool(result.get("sent"))
    if attempted is None:
        attempted = sent or bool(result.get("error"))
    return {
        "whatsapp_attempted": bool(attempted),
        "whatsapp_queued": sent,
        "whatsapp_sent": sent,
        "whatsapp_error": result.get("error"),
        "whatsapp_to": result.get("recipient_phone"),
        "whatsapp_recipient_name": result.get("recipient_name"),
        "whatsapp_message": result.get("message"),
        "whatsapp_message_id": result.get("message_id"),
        "whatsapp_send_mode": result.get("send_mode"),
        "whatsapp_recipient_has_whatsapp": bool(result.get("recipient_has_whatsapp", False)),
        "whatsapp_contact_chat_verified": bool(result.get("contact_chat_verified", False)),
        "whatsapp_wa_id": result.get("wa_id"),
    }


def email_response(
    result: dict[str, Any],
    *,
    fallback_extracted: str | None = None,
) -> dict[str, Any]:
    extracted = (result.get("extracted_email") or fallback_extracted or "").strip() or None
    delivered_to = result.get("recipient_email")
    test_override = is_test_recipient_mode()
    sent = bool(result.get("sent"))
    attempted = bool(result.get("attempted"))
    return {
        "email_attempted": attempted,
        "email_queued": sent,
        "email_sent": sent,
        "email_error": result.get("error"),
        "email_to": delivered_to,
        "email_cc": result.get("cc_emails") or [],
        "email_cc_invalid": result.get("cc_invalid") or [],
        "email_extracted": extracted,
        "email_subject": result.get("subject"),
        "email_test_override": test_override,
        "email_delivered_to_extracted": bool(
            sent
            and extracted
            and delivered_to
            and str(delivered_to).strip().lower() == str(extracted).strip().lower()
        ),
    }


async def schedule_outreach_for_contact(
    contact: dict[str, Any],
    *,
    online_mode: bool = True,
    on_save: bool = False,
    contact_id: str | None = None,
    skip_whatsapp: bool = False,
    skip_email: bool = False,
    log_context: str = "outreach",
    scanner_email: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    outreach_kwargs = {
        "online_mode": online_mode,
        "on_zoho_sync": on_save,  # email/whatsapp services still accept this flag name
        "contact_id": contact_id,
    }
    whatsapp_result: dict[str, Any] = {
        "attempted": False,
        "sent": False,
        "error": "Skipped by request." if skip_whatsapp else None,
    }
    email_result: dict[str, Any] = {
        "attempted": False,
        "sent": False,
        "error": "Skipped by request." if skip_email else None,
    }

    tasks: list[tuple[str, Any]] = []
    if not skip_whatsapp:
        tasks.append(
            (
                "whatsapp",
                schedule_whatsapp_for_contact(contact, **outreach_kwargs, log_context=log_context),
            )
        )
    if not skip_email:
        tasks.append(
            (
                "email",
                schedule_email_for_contact(
                    contact,
                    **outreach_kwargs,
                    scanner_email=scanner_email,
                ),
            )
        )

    if tasks:
        results = await asyncio.gather(*(task for _, task in tasks))
        for (channel, _), result in zip(tasks, results):
            if channel == "whatsapp":
                whatsapp_result = result
            else:
                email_result = result

    if contact_id:
        await asyncio.to_thread(
            storage.update_outreach_delivery,
            contact_id,
            email_status=_delivery_state(email_result),
            email_error=email_result.get("error"),
            whatsapp_status=_delivery_state(whatsapp_result),
            whatsapp_error=whatsapp_result.get("error"),
        )

    return whatsapp_result, email_result


def payload_to_outreach_contact(data: dict[str, Any]) -> dict[str, Any]:
    email = str(
        data.get("email")
        or data.get("emailAddress")
        or data.get("secondaryEmail")
        or data.get("secondaryEmailAddress")
        or ""
    ).strip()
    phone = str(data.get("phone") or data.get("phoneNumber") or "").strip()
    website = str(data.get("website") or data.get("url") or "").strip()
    secondary_website = str(data.get("secondaryWebsite") or "").strip()
    address = str(data.get("address") or "").strip()
    secondary_address = str(data.get("secondaryAddress") or "").strip()
    full_name = str(
        data.get("fullName")
        or data.get("name")
        or f"{data.get('firstName', '')} {data.get('lastName', '')}".strip()
    ).strip()
    return {
        "fullName": full_name,
        "name": full_name,
        "firstName": data.get("firstName", ""),
        "lastName": data.get("lastName", ""),
        "designation": data.get("designation") or data.get("title", ""),
        "company": data.get("company", ""),
        "companyName": data.get("company", ""),
        "email": email,
        "emailAddress": email,
        "emails": [e for e in (email, str(data.get("secondaryEmail") or "").strip()) if e],
        "phone": phone,
        "countryCode": str(data.get("countryCode") or "").strip(),
        "countryName": str(data.get("countryName") or "").strip(),
        "phones": [p for p in (phone, str(data.get("secondaryPhone") or "").strip()) if p],
        "website": website or secondary_website,
        "secondaryWebsite": secondary_website,
        "address": address or secondary_address,
        "secondaryAddress": secondary_address,
        "eventName": str(data.get("eventName") or "").strip(),
        "eventDay": str(data.get("eventDay") or "Day 1").strip() or "Day 1",
    }


async def run_post_save_outreach(
    *,
    contact_id: str | None = None,
    contact: dict[str, Any] | None = None,
    skip_whatsapp: bool = False,
    skip_email: bool = False,
    log_context: str = "post-save-outreach",
    scanner_email: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run thank-you WhatsApp/email after a contact is saved to PostgreSQL."""
    skipped_whatsapp: dict[str, Any] = {
        "sent": False,
        "error": "Skipped by request (skipWhatsApp=true).",
    }
    skipped_email: dict[str, Any] = {
        "sent": False,
        "error": "Skipped by request (skipEmail=true).",
    }

    try:
        if contact_id:
            stored = storage.get_contact(contact_id)
            if not stored:
                logger.warning("Outreach skipped: contact %s not found.", contact_id)
                return skipped_whatsapp, skipped_email
            return await schedule_outreach_for_contact(
                stored,
                on_save=True,
                contact_id=contact_id,
                skip_whatsapp=skip_whatsapp,
                skip_email=skip_email,
                log_context=log_context,
                scanner_email=scanner_email,
            )

        if contact:
            return await schedule_outreach_for_contact(
                contact,
                on_save=True,
                contact_id=None,
                skip_whatsapp=skip_whatsapp,
                skip_email=skip_email,
                log_context=log_context,
                scanner_email=scanner_email,
            )
    except Exception as exc:
        logger.error("Outreach failed: %s", exc, exc_info=True)
        err = {"sent": False, "error": str(exc)}
        return err, err

    return skipped_whatsapp, skipped_email


def fire_post_save_outreach(
    *,
    contact_id: str | None = None,
    contact: dict[str, Any] | None = None,
    skip_whatsapp: bool = False,
    skip_email: bool = False,
    scanner_email: str | None = None,
) -> None:
    """Fire-and-forget outreach after contact save."""
    async def _run() -> None:
        await run_post_save_outreach(
            contact_id=contact_id,
            contact=contact,
            skip_whatsapp=skip_whatsapp,
            skip_email=skip_email,
            scanner_email=scanner_email,
        )

    asyncio.create_task(_run())
