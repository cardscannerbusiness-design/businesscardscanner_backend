"""E.164 formatting via phonenumbers."""

from __future__ import annotations

import phonenumbers
from phonenumbers import PhoneNumberFormat


def format_phone_to_e164(number: phonenumbers.PhoneNumber) -> dict[str, str]:
    region = phonenumbers.region_code_for_number(number) or ""
    return {
        "country": region,
        "country_calling_code": str(number.country_code),
        "national_number": str(number.national_number),
        "e164": phonenumbers.format_number(number, PhoneNumberFormat.E164),
    }


def e164_to_whatsapp_recipient_digits(e164: str) -> str:
    """Meta WhatsApp API uses digits-only recipient derived from E.164."""
    return "".join(ch for ch in e164 if ch.isdigit())
