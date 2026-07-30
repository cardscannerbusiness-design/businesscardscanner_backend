import asyncio
import html
import logging
import os
import smtplib
import time
from email.message import EmailMessage
from typing import Any

from utils.parser_utils import is_valid_email

from services.email_cc_validation import validate_cc_address_list
from services.email_template_service import (
    cc_scanned_contact_email_context,
    render_cc_scanned_contact_email_html,
    render_thank_you_email_html,
    thank_you_email_context,
)

logger = logging.getLogger(__name__)

_RECENT_SENDS: dict[str, float] = {}
_SEND_DEDUPE_SECONDS = 120

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587

SUBJECT = "Thank You for Your Interest"
CC_SCANNED_SUBJECT_PREFIX = "Scanned contact:"

# Brand palette aligned with NameCardScan UI (Inter / cyan-teal)
_BRAND_PRIMARY = "#0891b2"
_BRAND_PRIMARY_DARK = "#0e7490"
_BRAND_ACCENT = "#0e7490"
_BRAND_TEXT = "#1e293b"
_BRAND_MUTED = "#64748b"
_BRAND_SURFACE = "#f8fafc"
_BRAND_BORDER = "#e2e8f0"
_FONT_SANS = "'Inter',Arial,Helvetica,sans-serif"
_FONT_DISPLAY = "'Inter Tight','Inter',Arial,Helvetica,sans-serif"


def _normalize_env(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


# Email clients cannot reach localhost; always use the public production assets host.
_EMAIL_ASSETS_BASE = "https://api.namecardscan.com/assets"


def _assets_base() -> str:
    """Public /assets base URL for thank-you email (PDF, promo, icons)."""
    return _EMAIL_ASSETS_BASE


def _pdf_download_href() -> str:
    """Public PDF link for thank-you HTML (served from /assets)."""
    return f"{_assets_base()}/NCS-Corporate-Deck-V1.pdf"


def _normalize_gmail_app_password(value: str | None) -> str:
    """SMTP app passwords (e.g. Gmail's) are 16 chars; remove spaces from .env (e.g. 'abcd efgh')."""
    return _normalize_env(value).replace(" ", "")


GMAIL_USER = _normalize_env(os.getenv("GMAIL_USER"))
GMAIL_APP_PASSWORD = _normalize_gmail_app_password(os.getenv("GMAIL_APP_PASSWORD"))

# SMTP is the only email transport. GMAIL_USER / GMAIL_APP_PASSWORD are the default
# credentials; SMTP_USER / SMTP_PASSWORD / SMTP_HOST / SMTP_PORT override them for any
# other SMTP provider without touching code.
SMTP_HOST = _normalize_env(os.getenv("SMTP_HOST")) or DEFAULT_SMTP_HOST
SMTP_PORT = int(_normalize_env(os.getenv("SMTP_PORT")) or str(DEFAULT_SMTP_PORT))
SMTP_USER = _normalize_env(os.getenv("SMTP_USER")) or GMAIL_USER
SMTP_PASSWORD = _normalize_gmail_app_password(os.getenv("SMTP_PASSWORD")) or GMAIL_APP_PASSWORD

BUSINESS_COMPANY_NAME = _normalize_env(os.getenv("BUSINESS_COMPANY_NAME")) or "NameCardScan"
BUSINESS_PHONE = _normalize_env(os.getenv("BUSINESS_PHONE")) or ""
BUSINESS_WEBSITE = _normalize_env(os.getenv("BUSINESS_WEBSITE")) or ""
BUSINESS_EMAIL = _normalize_env(os.getenv("BUSINESS_EMAIL")) or SMTP_USER
BUSINESS_EVENT_NAME = (
    _normalize_env(os.getenv("BUSINESS_EVENT_NAME")) or "the exhibition"
)
EMAIL_TEST_RECIPIENT = _normalize_env(os.getenv("EMAIL_TEST_RECIPIENT"))
_RENDER_SMTP_HINT = (
    "Some hosts block outbound SMTP on port 587. "
    "Use a host/network that permits SMTP, or an SMTP relay that allows it."
)

_SMTP_AUTH_HELP = (
    "SMTP authentication failed. If using Gmail, create a Google App Password "
    "(not your login password): enable 2-Step Verification, then visit "
    "https://myaccount.google.com/apppasswords and set GMAIL_APP_PASSWORD "
    "to the 16-character password for GMAIL_USER."
)


def _auto_send_enabled() -> bool:
    return _normalize_env(os.getenv("EMAIL_AUTO_SEND_ON_SCAN")).lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def is_smtp_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD)


# Backward-compatible alias — SMTP is the only transport now.
def is_gmail_configured() -> bool:
    return is_smtp_configured()


def smtp_sender_email() -> str:
    return BUSINESS_EMAIL or SMTP_USER


def receive_inbox_email() -> str | None:
    """Fallback receive inbox from SUPERADMIN_EMAIL when hierarchy lookup is unavailable."""
    inbox = _normalize_env(os.getenv("SUPERADMIN_EMAIL"))
    return inbox or None


def _cc_recipients_for_scan(receive_email: str | None = None) -> list[str]:
    """Receive template recipient — Admin (for User scans) or SuperAdmin (for Admin scans)."""
    email = str(receive_email or "").strip()
    return [email] if email else []


def is_email_configured() -> bool:
    return is_smtp_configured()


def is_email_test_recipient_configured() -> bool:
    """True when EMAIL_TEST_RECIPIENT env redirects all sends (including auto-send on scan)."""
    return bool(EMAIL_TEST_RECIPIENT)


def is_test_recipient_mode() -> bool:
    """Alias used by outreach API responses."""
    return is_email_test_recipient_configured()


def get_email_provider() -> str | None:
    """SMTP (smtplib) is the only email transport."""
    if is_smtp_configured():
        return "smtp"
    return None


def _format_from_address(email: str, name: str | None = None) -> str:
    display = name or BUSINESS_COMPANY_NAME
    if display:
        return f"{display} <{email}>"
    return email


def _redact_email(email: str | None) -> str:
    """Mask email for logs (e.g. u***@example.com)."""
    value = str(email or "").strip()
    if not value or "@" not in value:
        return "(none)"
    local, _, domain = value.partition("@")
    if len(local) <= 1:
        masked_local = "*"
    else:
        masked_local = f"{local[0]}***"
    return f"{masked_local}@{domain}"


def _prepare_cc_addresses(
    cc_addresses: list[str] | None,
    *,
    to_address: str,
) -> tuple[list[str], list[dict[str, str]]]:
    """Validate CC list; return valid addresses and invalid entries with reasons."""
    validation = validate_cc_address_list(cc_addresses, to_address=to_address)
    if validation.invalid:
        for entry in validation.invalid:
            logger.warning(
                "CC address rejected: %s — %s (to=%s)",
                _redact_email(entry.get("address")),
                entry.get("reason"),
                _redact_email(to_address),
            )
    return validation.valid, validation.invalid


def _send_via_smtp_relay(
    to_address: str,
    *,
    subject: str,
    plain_body: str,
    html_body: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_address: str,
    reply_to: str,
    provider_label: str,
    cc_addresses: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "recipient_email": to_address,
        "cc_emails": [],
        "error": None,
    }

    cc_list, cc_invalid = _prepare_cc_addresses(cc_addresses, to_address=to_address)
    result["cc_emails"] = cc_list
    if cc_invalid:
        result["cc_invalid"] = cc_invalid

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = to_address
    message["Reply-To"] = reply_to
    if cc_list:
        message["Cc"] = ", ".join(cc_list)
    _attach_multipart_body(message, plain_body, html_body)

    recipients = [to_address, *cc_list]

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(message, to_addrs=recipients)
    except smtplib.SMTPAuthenticationError as exc:
        result["error"] = f"{provider_label} authentication failed: {exc}"
        logger.error("SMTP auth failed for %s: %s", to_address, exc, exc_info=True)
        return result
    except smtplib.SMTPRecipientsRefused as exc:
        result["error"] = f"{provider_label} rejected recipient {to_address}: {exc.recipients}"
        logger.error("SMTP recipient refused: %s", result["error"], exc_info=True)
        return result
    except smtplib.SMTPException as exc:
        result["error"] = f"SMTP error while sending to {to_address}: {exc}"
        logger.error("SMTP error: %s", result["error"], exc_info=True)
        return result
    except OSError as exc:
        result["error"] = f"Network error connecting to {provider_label}: {exc}. {_RENDER_SMTP_HINT}"
        logger.error("SMTP network error: %s", result["error"], exc_info=True)
        return result
    except Exception as exc:
        result["error"] = f"Unexpected SMTP error: {exc}"
        logger.error("SMTP unexpected error: %s", result["error"], exc_info=True)
        return result

    logger.info("SUCCESS: Thank-you email sent via %s to %s", provider_label, to_address)
    result["success"] = True
    return result


def _send_via_smtp(
    to_address: str,
    *,
    subject: str,
    plain_body: str,
    html_body: str,
    cc_addresses: list[str] | None = None,
) -> dict[str, Any]:
    """Send an email over SMTP (smtplib) using the configured credentials."""
    if not is_smtp_configured():
        return {
            "success": False,
            "recipient_email": to_address,
            "error": "SMTP is not configured. Set GMAIL_USER + GMAIL_APP_PASSWORD (or SMTP_USER + SMTP_PASSWORD) in .env.",
        }
    sender = smtp_sender_email()
    result = _send_via_smtp_relay(
        to_address,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        smtp_user=SMTP_USER,
        smtp_password=SMTP_PASSWORD,
        from_address=_format_from_address(sender),
        reply_to=BUSINESS_EMAIL or sender,
        provider_label="SMTP",
        cc_addresses=cc_addresses,
    )
    if not result["success"] and "authentication failed" in str(result.get("error", "")).lower():
        result["error"] = _SMTP_AUTH_HELP
    return result


def _deliver_email(
    to_address: str,
    *,
    subject: str,
    plain_body: str,
    html_body: str,
    cc_addresses: list[str] | None = None,
) -> dict[str, Any]:
    if is_smtp_configured():
        return _send_via_smtp(
            to_address,
            subject=subject,
            plain_body=plain_body,
            html_body=html_body,
            cc_addresses=cc_addresses,
        )
    return {
        "success": False,
        "recipient_email": to_address,
        "error": (
            "Email is not configured. Set GMAIL_USER + GMAIL_APP_PASSWORD "
            "(or SMTP_USER + SMTP_PASSWORD) in .env."
        ),
    }


def extract_primary_email(contact: dict[str, Any]) -> str:
    """Extract the primary email address from parsed contact data."""
    for key in ("email", "emailAddress", "primaryEmail"):
        value = str(contact.get(key) or "").strip()
        if value:
            return value

    for key in ("secondaryEmail", "secondaryEmailAddress"):
        value = str(contact.get(key) or "").strip()
        if value:
            return value

    for key in ("emails", "emailAddresses"):
        values = contact.get(key) or []
        if isinstance(values, list):
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
    return ""


def validate_email_address(email: str) -> tuple[bool, str]:
    """Validate that an email address is present and properly formatted."""
    normalized = str(email or "").strip()
    if not normalized:
        return False, "Email address is required."
    if not is_valid_email(normalized):
        return False, f"Invalid email format: '{normalized}'"
    return True, normalized


def extract_contact_name(contact: dict[str, Any]) -> str:
    """Extract display name from parsed contact data."""
    for key in ("fullName", "name"):
        value = str(contact.get(key) or "").strip()
        if value:
            return value

    first = str(contact.get("firstName") or "").strip()
    last = str(contact.get("lastName") or "").strip()
    combined = f"{first} {last}".strip()
    return combined


def _greeting_name(recipient_name: str | None) -> str:
    name = str(recipient_name or "").strip()
    if not name:
        return "Valued Customer"
    return name.split()[0] if name else "Valued Customer"


def build_thank_you_email_plain(recipient_name: str | None = None) -> str:
    """Build the plain-text fallback for the business thank-you email."""
    greeting = _greeting_name(recipient_name)
    event_name = BUSINESS_EVENT_NAME
    return (
        f"Hi {greeting},\n\n"
        f"Dhana here. It was great meeting you at {event_name}.\n\n"
        "Quick question: After collecting business cards, how many prospects actually "
        "become customers?\n\n"
        "Most businesses at exhibitions and events struggle with:\n"
        "• Delayed follow-ups\n"
        "• Poor brand recall\n"
        "• Manual business card data entry\n"
        "• No clear Return on Exhibition Spend (ROES)\n\n"
        "That’s why we built Name Card Scan (NCS).\n\n"
        "With NCS, you can:\n"
        "• Scan or upload business cards\n"
        "• Instantly capture contact details\n"
        "• Automatically send your WhatsApp & Email introduction, digital business "
        "card, brochures (PDF) and videos\n"
        "• Store all data in Google Drive\n"
        "• Push leads directly to Zoho CRM via API\n\n"
        "NCS helps you engage prospects while your brand is still fresh in their minds.\n\n"
        "ROES = Prospect → Qualified Lead → Sale → Customer\n\n"
        "Reply YES and let my team help you get started with NCS to improve your ROES.\n\n"
        "Regards,\n"
        "Dhana\n"
        "Founder, Name Card Scan\n"
        "dhana@ulavitech.com\n"
        "+91 8838747273 | +65 97310793\n"
    )


def _contact_detail_rows() -> str:
    """Build footer contact rows for the HTML table template."""
    rows: list[str] = []
    details: list[tuple[str, str, str | None]] = []

    if BUSINESS_PHONE:
        details.append(("Phone", BUSINESS_PHONE, f"tel:{BUSINESS_PHONE.replace(' ', '')}"))
    if BUSINESS_WEBSITE:
        website = BUSINESS_WEBSITE if BUSINESS_WEBSITE.startswith("http") else f"https://{BUSINESS_WEBSITE}"
        details.append(("Website", BUSINESS_WEBSITE, website))
    if BUSINESS_EMAIL:
        details.append(("Email", BUSINESS_EMAIL, f"mailto:{BUSINESS_EMAIL}"))

    for label, value, href in details:
        safe_label = html.escape(label)
        safe_value = html.escape(value)
        if href:
            safe_href = html.escape(href, quote=True)
            cell = (
                f'<a href="{safe_href}" style="color:{_BRAND_PRIMARY};'
                f'text-decoration:none;font-weight:500;">{safe_value}</a>'
            )
        else:
            cell = safe_value
        rows.append(
            f'<tr>'
            f'<td style="padding:6px 0;color:{_BRAND_MUTED};font-size:13px;'
            f"font-family:{_FONT_SANS};"
            f'width:72px;vertical-align:top;">{safe_label}</td>'
            f'<td style="padding:6px 0;color:{_BRAND_TEXT};font-size:13px;'
            f"font-family:{_FONT_SANS};"
            f'vertical-align:top;">{cell}</td>'
            f"</tr>"
        )
    return "\n".join(rows)


def build_thank_you_email_html(recipient_name: str | None = None) -> str:
    """Build a table-based HTML email body compatible with major email clients."""
    reply_addr = BUSINESS_EMAIL or GMAIL_USER or ""
    context = thank_you_email_context(
        greeting=_greeting_name(recipient_name),
        company=BUSINESS_COMPANY_NAME,
        subject=SUBJECT,
        reply_href=f"mailto:{reply_addr}",
        contact_rows=_contact_detail_rows(),
        year=time.strftime("%Y"),
        brand_primary=_BRAND_PRIMARY,
        brand_primary_dark=_BRAND_PRIMARY_DARK,
        brand_accent=_BRAND_ACCENT,
        brand_text=_BRAND_TEXT,
        brand_muted=_BRAND_MUTED,
        brand_surface=_BRAND_SURFACE,
        brand_border=_BRAND_BORDER,
        pdf_download_href=_pdf_download_href(),
        assets_base=_assets_base(),
        event_name=BUSINESS_EVENT_NAME,
    )
    return render_thank_you_email_html(context)


def build_thank_you_email_body(recipient_name: str | None = None) -> tuple[str, str]:
    """Return (plain_text, html) bodies for the business thank-you email."""
    return (
        build_thank_you_email_plain(recipient_name),
        build_thank_you_email_html(recipient_name),
    )


def _contact_field(contact: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(contact.get(key) or "").strip()
        if value:
            return value
    return ""


def _scanned_contact_detail_rows(contact: dict[str, Any]) -> str:
    """Build table rows for the CC scanned-contact template."""
    rows_spec: list[tuple[str, str]] = []
    field_map: list[tuple[str, tuple[str, ...]]] = [
        ("Name", ("fullName", "name")),
        ("Email", ("email", "emailAddress")),
        ("Phone", ("phone",)),
        ("Company", ("company", "companyName")),
        ("Title", ("designation", "title")),
        ("Event", ("eventName",)),
        ("Website", ("website", "secondaryWebsite")),
        ("Address", ("address", "secondaryAddress")),
        ("Notes", ("notes",)),
    ]
    for label, keys in field_map:
        value = _contact_field(contact, *keys)
        if value:
            rows_spec.append((label, value))

    if not rows_spec:
        rows_spec.append(("Details", "No scanned fields were available."))

    rendered: list[str] = []
    for label, value in rows_spec:
        safe_label = html.escape(label)
        safe_value = html.escape(value)
        rendered.append(
            f'<tr>'
            f'<td style="padding:10px 16px;color:{_BRAND_MUTED};font-size:13px;'
            f"font-family:{_FONT_SANS};"
            f'width:120px;vertical-align:top;border-bottom:1px solid {_BRAND_BORDER};">{safe_label}</td>'
            f'<td style="padding:10px 16px;color:{_BRAND_TEXT};font-size:13px;'
            f"font-family:{_FONT_SANS};"
            f'vertical-align:top;border-bottom:1px solid {_BRAND_BORDER};">{safe_value}</td>'
            f"</tr>"
        )
    return "\n".join(rendered)


def build_cc_scanned_contact_subject(contact: dict[str, Any]) -> str:
    name = extract_contact_name(contact) or "New contact"
    return f"{CC_SCANNED_SUBJECT_PREFIX} {name}"


def build_cc_scanned_contact_body(contact: dict[str, Any]) -> tuple[str, str]:
    """Return (plain_text, html) for CC recipients — scanned details only."""
    subject = build_cc_scanned_contact_subject(contact)
    detail_rows = _scanned_contact_detail_rows(contact)
    context = cc_scanned_contact_email_context(
        company=BUSINESS_COMPANY_NAME,
        subject=subject,
        detail_rows=detail_rows,
        year=time.strftime("%Y"),
        brand_primary=_BRAND_PRIMARY,
        brand_primary_dark=_BRAND_PRIMARY_DARK,
        brand_text=_BRAND_TEXT,
        brand_muted=_BRAND_MUTED,
        brand_surface=_BRAND_SURFACE,
        brand_border=_BRAND_BORDER,
    )
    html_body = render_cc_scanned_contact_email_html(context)

    plain_lines = [f"{label}: {value}" for label, value in [
        ("Name", _contact_field(contact, "fullName", "name")),
        ("Email", _contact_field(contact, "email", "emailAddress")),
        ("Phone", _contact_field(contact, "phone")),
        ("Company", _contact_field(contact, "company", "companyName")),
        ("Title", _contact_field(contact, "designation", "title")),
        ("Event", _contact_field(contact, "eventName")),
        ("Website", _contact_field(contact, "website", "secondaryWebsite")),
        ("Address", _contact_field(contact, "address", "secondaryAddress")),
        ("Notes", _contact_field(contact, "notes")),
    ] if value]
    plain_body = "Scanned contact details\n\n" + "\n".join(plain_lines or ["No scanned fields were available."])
    return plain_body, html_body


def _send_cc_scanned_details_emails(
    cc_addresses: list[str],
    *,
    contact: dict[str, Any],
) -> list[dict[str, Any]]:
    """Send the CC-only scanned-details template as separate emails (SMTP)."""
    provider = get_email_provider()
    if provider != "smtp" or not cc_addresses:
        return []

    plain_body, html_body = build_cc_scanned_contact_body(contact)
    subject = build_cc_scanned_contact_subject(contact)
    results: list[dict[str, Any]] = []

    for cc_email in cc_addresses:
        logger.info(
            "Sending CC scanned-details email via %s to=%s",
            provider,
            _redact_email(cc_email),
        )
        delivery = _deliver_email(
            cc_email,
            subject=subject,
            plain_body=plain_body,
            html_body=html_body,
            cc_addresses=None,
        )
        results.append({"email": cc_email, **delivery})
        if delivery.get("success"):
            logger.info("CC scanned-details email confirmed to=%s", _redact_email(cc_email))
        else:
            logger.error(
                "CC scanned-details email failed to=%s: %s",
                _redact_email(cc_email),
                delivery.get("error"),
            )
    return results


def _attach_multipart_body(message: EmailMessage, plain: str, html_body: str) -> None:
    """Attach plain-text and HTML alternatives to an EmailMessage."""
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")


def _resolve_recipient(extracted_email: str, *, test_override: str | None = None) -> str:
    """Resolve the final recipient, applying test override when configured."""
    override = test_override or EMAIL_TEST_RECIPIENT
    if override:
        logger.info(
            "Email test override active: sending to %s instead of %s",
            override,
            extracted_email or "(none)",
        )
        return override
    return extracted_email


def send_business_thank_you_email(
    recipient_email: str,
    *,
    recipient_name: str | None = None,
    test_override: str | None = None,
    cc_addresses: list[str] | None = None,
    contact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Validate, compose, and send the business thank-you email.

    To = scanned contact (User). CC = scanning CardSync user (Owner) when provided.
    """
    result: dict[str, Any] = {
        "success": False,
        "recipient_email": None,
        "cc_emails": [],
        "extracted_email": recipient_email,
        "subject": SUBJECT,
        "error": None,
    }

    is_valid, validated_or_error = validate_email_address(recipient_email)
    if not is_valid:
        logger.warning("Email send aborted: %s", validated_or_error)
        result["error"] = validated_or_error
        return result

    validated_email: str = validated_or_error
    to_address = _resolve_recipient(validated_email, test_override=test_override)
    is_valid_to, to_validated_or_error = validate_email_address(to_address)
    if not is_valid_to:
        logger.warning("Email send aborted: %s", to_validated_or_error)
        result["error"] = to_validated_or_error
        return result

    to_address = to_validated_or_error
    result["recipient_email"] = to_address

    if not is_email_configured():
        error = (
            "Email is not configured. Set GMAIL_USER + GMAIL_APP_PASSWORD "
            "(or SMTP_USER + SMTP_PASSWORD) in .env."
        )
        logger.error("Email send failed: %s", error)
        result["error"] = error
        return result

    plain_body, html_body = build_thank_you_email_body(recipient_name)
    provider = get_email_provider()
    cc_list, cc_invalid = _prepare_cc_addresses(cc_addresses, to_address=to_address)
    result["cc_emails"] = cc_list
    if cc_invalid:
        result["cc_invalid"] = cc_invalid
    logger.info(
        "Sending business thank-you email via %s -> to (User)=%s cc (Owner)=%s subject=%r",
        provider,
        _redact_email(to_address),
        [_redact_email(e) for e in cc_list] or "(none)",
        SUBJECT,
    )

    delivery = _deliver_email(
        to_address,
        subject=SUBJECT,
        plain_body=plain_body,
        html_body=html_body,
        cc_addresses=None,
    )

    result["success"] = delivery.get("success", False)
    result["cc_emails"] = cc_list if provider == "smtp" else []
    if cc_invalid and "cc_invalid" not in result:
        result["cc_invalid"] = cc_invalid
    result["error"] = delivery.get("error")

    if result["success"] and cc_list and contact and provider == "smtp":
        cc_results = _send_cc_scanned_details_emails(cc_list, contact=contact)
        result["cc_delivery"] = cc_results
        cc_failures = [r for r in cc_results if not r.get("success")]
        if cc_failures:
            logger.warning(
                "Primary email sent to=%s but %s CC notification(s) failed.",
                _redact_email(to_address),
                len(cc_failures),
            )

    if result["success"]:
        logger.info(
            "Email delivery confirmed via %s to=%s cc=%s",
            provider,
            _redact_email(to_address),
            [_redact_email(e) for e in result["cc_emails"]],
        )
    else:
        logger.error(
            "Email delivery failed via %s to=%s: %s",
            provider,
            _redact_email(to_address),
            result["error"],
        )
    return result


def send_thank_you_to_contact(
    contact: dict[str, Any],
    *,
    test_override: str | None = None,
    cc_addresses: list[str] | None = None,
) -> dict[str, Any]:
    """Extract email from contact data and send the business thank-you email."""
    extracted = extract_primary_email(contact)
    if not extracted:
        raise ValueError("No primary email address found on the contact.")

    contact_name = extract_contact_name(contact)
    logger.info(
        "Email dynamic send -> extracted email=%s name=%s",
        extracted,
        contact_name or "unknown",
    )
    return send_business_thank_you_email(
        extracted,
        recipient_name=contact_name or None,
        test_override=test_override,
        cc_addresses=cc_addresses,
        contact=contact,
    )


def _should_send_to_email(email: str, *, bypass_dedupe: bool = False) -> bool:
    normalized = email.strip().lower()
    if not normalized:
        return False

    if bypass_dedupe:
        _RECENT_SENDS[normalized] = time.time()
        return True

    now = time.time()
    last_sent = _RECENT_SENDS.get(normalized, 0)
    if now - last_sent < _SEND_DEDUPE_SECONDS:
        logger.info("Skipping duplicate email send to %s within dedupe window.", normalized)
        return False

    _RECENT_SENDS[normalized] = now
    return True


async def schedule_email_for_contact(
    contact: dict[str, Any],
    *,
    online_mode: bool = True,
    on_zoho_sync: bool = False,
    contact_id: str | None = None,
    skip_if_already_sent: bool = True,
    test_override: str | None = None,
    scanner_email: str | None = None,
) -> dict[str, Any]:
    """
    Send a business thank-you email to the contact's primary email address.

    Returns a result dict with attempted/sent/error fields.
    """
    skipped: dict[str, Any] = {
        "attempted": False,
        "sent": False,
        "queued": False,
        "error": None,
        "recipient_email": None,
        "cc_emails": [],
        "extracted_email": None,
        "subject": SUBJECT,
    }

    if not _auto_send_enabled():
        logger.debug("Email auto-send disabled via EMAIL_AUTO_SEND_ON_SCAN.")
        skipped["error"] = "Email auto-send is disabled."
        return skipped

    if not on_zoho_sync and not online_mode:
        logger.info("Email auto-send skipped: offline mode (will send after database sync).")
        skipped["error"] = "Offline mode — email will send after the contact syncs to the database."
        return skipped

    if not is_email_configured():
        logger.warning("Email auto-send skipped: SMTP is not configured.")
        skipped["error"] = (
            "Email is not configured. Set GMAIL_USER + GMAIL_APP_PASSWORD "
            "(or SMTP_USER + SMTP_PASSWORD) in .env."
        )
        return skipped

    if contact_id and skip_if_already_sent:
        from services import contact_storage as storage

        existing = storage.get_contact(contact_id)
        if existing and storage.has_email_sent(existing):
            logger.info("Email auto-send skipped: already sent for contact %s.", contact_id)
            skipped["error"] = "Thank-you email was already sent for this contact."
            return skipped

    extracted = extract_primary_email(contact)
    skipped["extracted_email"] = extracted or None
    if not extracted:
        logger.info("Email auto-send skipped: no primary email on scanned contact.")
        skipped["error"] = "No primary email address found on the contact."
        return skipped

    is_valid, validated_or_error = validate_email_address(extracted)
    if not is_valid:
        logger.info("Email auto-send skipped: %s", validated_or_error)
        skipped["error"] = validated_or_error
        return skipped

    contact_name = extract_contact_name(contact)
    recipient = _resolve_recipient(validated_or_error, test_override=test_override)
    raw_cc = _cc_recipients_for_scan(scanner_email)
    cc_list, cc_invalid = _prepare_cc_addresses(raw_cc or None, to_address=recipient)
    skipped["recipient_email"] = recipient
    skipped["cc_emails"] = cc_list
    if cc_invalid:
        skipped["cc_invalid"] = cc_invalid
    logger.info(
        "Email dynamic send -> to (contact)=%s receive/cc (manager)=%s name=%s",
        _redact_email(recipient),
        [_redact_email(e) for e in cc_list] or "(none)",
        contact_name or "unknown",
    )

    if not _should_send_to_email(recipient, bypass_dedupe=is_test_recipient_mode()):
        skipped["error"] = "Duplicate email send skipped for this address."
        return skipped

    try:
        delivery = await asyncio.to_thread(
            send_business_thank_you_email,
            extracted,
            recipient_name=contact_name or None,
            test_override=test_override,
            cc_addresses=raw_cc,
            contact=contact,
        )
        if delivery["success"]:
            logger.info(
                "Business thank-you email sent to=%s cc=%s (extracted=%s)",
                _redact_email(delivery["recipient_email"]),
                [_redact_email(e) for e in (delivery.get("cc_emails") or cc_list)],
                _redact_email(extracted),
            )
            if contact_id:
                from services import contact_storage as storage

                await asyncio.to_thread(storage.mark_email_sent, contact_id)
            return {
                "attempted": True,
                "sent": True,
                "queued": False,
                "error": None,
                "recipient_email": delivery["recipient_email"],
                "cc_emails": delivery.get("cc_emails") or cc_list,
                "cc_invalid": delivery.get("cc_invalid") or cc_invalid,
                "extracted_email": extracted,
                "subject": SUBJECT,
            }

        error_message = delivery.get("error") or "Unknown email send failure."
        logger.error(
            "Email auto-send failed for %s: %s",
            _redact_email(extracted),
            error_message,
        )
        return {
            "attempted": True,
            "sent": False,
            "queued": False,
            "error": error_message,
            "recipient_email": recipient,
            "cc_emails": delivery.get("cc_emails") or cc_list,
            "cc_invalid": delivery.get("cc_invalid") or cc_invalid,
            "extracted_email": extracted,
            "subject": SUBJECT,
        }
    except Exception as exc:
        error_message = str(exc)
        logger.error(
            "Email auto-send failed for %s: %s",
            _redact_email(extracted),
            error_message,
            exc_info=True,
        )
        return {
            "attempted": True,
            "sent": False,
            "queued": False,
            "error": error_message,
            "recipient_email": recipient,
            "cc_emails": cc_list,
            "cc_invalid": cc_invalid,
            "extracted_email": extracted,
            "subject": SUBJECT,
        }


class EmailQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.worker_task = None
        self.is_running = False

    async def start(self):
        """Starts the background worker processing the queue."""
        if not self.is_running:
            self.is_running = True
            self.worker_task = asyncio.create_task(self._worker())
            logger.info("Email background worker started.")

    async def stop(self):
        """Stops the background worker gracefully."""
        self.is_running = False
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
            logger.info("Email background worker stopped.")

    async def enqueue_message(self, email: str, message: str | None = None):
        """Adds a message to the queue to be sent asynchronously."""
        item = {"email": email, "message": message}
        await self.queue.put(item)
        logger.info("Email enqueued for %s. Queue size: %s", email, self.queue.qsize())

    async def enqueue_thank_you(self, email: str):
        """Enqueue the standard business thank-you email."""
        await self.enqueue_message(email, message=None)

    async def _worker(self):
        """Background worker that continuously processes the queue."""
        while self.is_running:
            try:
                item = await self.queue.get()
                email = item["email"]
                custom_message = item.get("message")

                logger.info("Processing email message to %s...", email)

                if custom_message:
                    result = await asyncio.to_thread(
                        _send_custom_email,
                        email,
                        custom_message,
                    )
                else:
                    result = await asyncio.to_thread(send_business_thank_you_email, email)

                if result["success"]:
                    logger.info("SUCCESS: Email message sent to %s", result["recipient_email"])
                else:
                    logger.error(
                        "FAILED: Email message to %s — %s",
                        email,
                        result.get("error"),
                    )

                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error processing email message: %s", exc, exc_info=True)
                self.queue.task_done()


def _send_custom_email(
    recipient_email: str,
    body: str,
    *,
    subject: str = SUBJECT,
    html_body: str | None = None,
) -> dict[str, Any]:
    """Send a custom plain-text email via configured provider."""
    result: dict[str, Any] = {
        "success": False,
        "recipient_email": None,
        "extracted_email": recipient_email,
        "subject": subject,
        "error": None,
    }

    is_valid, validated_or_error = validate_email_address(recipient_email)
    if not is_valid:
        result["error"] = validated_or_error
        return result

    to_address = validated_or_error
    result["recipient_email"] = to_address

    if not is_email_configured():
        result["error"] = (
            "Email is not configured. Set GMAIL_USER + GMAIL_APP_PASSWORD "
            "(or SMTP_USER + SMTP_PASSWORD) in .env."
        )
        return result

    delivery = _deliver_email(
        to_address,
        subject=subject,
        plain_body=body,
        html_body=html_body or f"<pre>{html.escape(body)}</pre>",
    )

    result["success"] = delivery.get("success", False)
    result["error"] = delivery.get("error")
    return result


def build_password_reset_otp_email(otp: str) -> tuple[str, str]:
    """Plain + HTML bodies for password reset OTP."""
    company = BUSINESS_COMPANY_NAME
    plain = (
        f"{company} — password reset\n\n"
        f"Your verification code is: {otp}\n\n"
        f"This code expires in 10 minutes. If you did not request a password reset, "
        f"you can ignore this email.\n"
    )
    safe_otp = html.escape(otp)
    safe_company = html.escape(company)
    html_body = f"""<!DOCTYPE html>
<html lang="en"><body style="font-family:Arial,sans-serif;background:#f4f7fb;padding:24px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;">
    <p style="margin:0 0 8px;font-size:13px;color:#64748b;text-transform:uppercase;letter-spacing:1px;">
      {safe_company}
    </p>
    <h1 style="margin:0 0 16px;font-size:22px;color:#1e3a5f;">Reset your password</h1>
    <p style="margin:0 0 20px;color:#475569;line-height:1.6;">
      Enter this verification code in the app to set a new password:
    </p>
    <div style="margin:0 0 20px;padding:16px 20px;border-radius:10px;background:#f1f5f9;text-align:center;
      font-size:32px;font-weight:700;letter-spacing:8px;color:#0891b2;">
      {safe_otp}
    </div>
    <p style="margin:0;color:#64748b;font-size:13px;line-height:1.6;">
      This code expires in 10 minutes. If you did not request this, you can safely ignore this email.
    </p>
  </div>
</body></html>"""
    return plain, html_body


def send_password_reset_otp_email(recipient_email: str, otp: str) -> dict[str, Any]:
    subject = f"{BUSINESS_COMPANY_NAME} — password reset code"
    plain, html_body = build_password_reset_otp_email(otp)
    return _send_custom_email(recipient_email, plain, subject=subject, html_body=html_body)


email_queue = EmailQueue()
