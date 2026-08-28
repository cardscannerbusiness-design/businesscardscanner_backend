"""Main normalization API — country-agnostic, uses full libphonenumber metadata."""

from __future__ import annotations

from typing import Any

from utils.international_phone.countries import find_country_by_iso
from utils.international_phone.formatter import e164_to_whatsapp_recipient_digits, format_phone_to_e164
from utils.international_phone.parser import (
    looks_international,
    parse_international_phone,
    parse_national_phone,
    prepare_phone_input,
)
from utils.international_phone.validator import validate_phone_number


def _failure(error: str) -> dict[str, Any]:
    return {"is_valid": False, "error": error}


def _success_from_parsed(parsed) -> dict[str, Any]:
    validation = validate_phone_number(parsed)
    if not validation["is_valid"]:
        return _failure("Invalid phone number.")

    formatted = format_phone_to_e164(parsed)
    if not formatted["country"]:
        return _failure("Unable to determine the country from this number.")

    return {
        "is_valid": True,
        "country": formatted["country"],
        "country_calling_code": formatted["country_calling_code"],
        "national_number": formatted["national_number"],
        "e164": formatted["e164"],
        "whatsapp_recipient": e164_to_whatsapp_recipient_digits(formatted["e164"]),
    }


def normalize_international_phone(phone: str, country: str | None = None) -> dict[str, Any]:
    """
    Normalize any supported international phone number to E.164.

    International (+...) numbers ignore the supplied country.
    National numbers require a valid ISO country code.
    """
    prepared = prepare_phone_input(phone)
    if not prepared:
        return _failure("Phone number is required.")

    if looks_international(prepared):
        parsed = parse_international_phone(prepared)
        if parsed is None:
            return _failure("Invalid international phone number.")
        return _success_from_parsed(parsed)

    iso = (country or "").strip().upper()
    if not iso:
        return _failure("Please select a country for a national phone number.")

    if find_country_by_iso(iso) is None:
        return _failure("Unable to normalize the phone number.")

    parsed = parse_national_phone(prepared, iso)
    if parsed is None:
        return _failure("Invalid phone number.")

    return _success_from_parsed(parsed)
