"""
Minimal adapter to supply WhatsApp-ready recipients without modifying whatsapp_service.

Use before calling existing WhatsApp functions when migrating to universal phone handling.
"""

from __future__ import annotations

import re
from typing import Any

from phonenumbers import region_codes_for_country_code

from utils.international_phone.countries import find_country_by_iso
from utils.international_phone.normalizer import normalize_international_phone


def prepare_whatsapp_recipient(phone: str, country: str | None = None) -> dict[str, Any]:
    """
    Validate and normalize phone for WhatsApp delivery.

    Returns normalized E.164 plus digits-only recipient for Meta API.
    Does not call WhatsApp or verify WhatsApp account availability.
    """
    result = normalize_international_phone(phone, country)
    if not result.get("is_valid"):
        return {
            "ok": False,
            "error": result.get("error"),
            "e164": None,
            "whatsapp_recipient": None,
            "country": None,
            "country_calling_code": None,
        }
    return {
        "ok": True,
        "error": None,
        "e164": result["e164"],
        "whatsapp_recipient": result["whatsapp_recipient"],
        "country": result["country"],
        "country_calling_code": result["country_calling_code"],
    }


def resolve_country_iso_from_contact(contact: dict[str, Any]) -> str | None:
    """Resolve ISO country from contact payload (new countryIso or legacy countryCode)."""
    iso = str(contact.get("countryIso") or "").strip().upper()
    if len(iso) == 2 and find_country_by_iso(iso):
        return iso

    dial = str(contact.get("countryCode") or "").strip()
    cc_digits = re.sub(r"\D", "", dial)
    if cc_digits:
        try:
            regions = region_codes_for_country_code(int(cc_digits))
        except Exception:
            regions = []
        if len(regions) == 1:
            return regions[0]
    return None


def _phone_and_country_for_normalize(
    phone: str,
    country_iso: str | None,
    dial_code: str,
) -> tuple[str, str | None]:
    raw = (phone or "").strip()
    if not raw:
        return "", country_iso

    if raw.startswith("+") or raw.startswith("00"):
        return raw, None

    digits = re.sub(r"\D", "", raw)
    cc_digits = re.sub(r"\D", "", dial_code)
    if digits and cc_digits and digits.startswith(cc_digits) and len(digits) > len(cc_digits):
        return f"+{digits}", None

    return raw, country_iso


def _normalize_phone_field(
    phone: str,
    country_iso: str | None,
    dial_code: str,
) -> dict[str, Any] | None:
    raw = (phone or "").strip()
    if not raw:
        return None
    phone_input, parse_country = _phone_and_country_for_normalize(raw, country_iso, dial_code)
    prep = prepare_whatsapp_recipient(phone_input, parse_country)
    return prep if prep.get("ok") else None


def apply_international_phone_to_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize primary/secondary phones on a contact dict before WhatsApp outreach.

    Returns a shallow copy. Invalid numbers are left unchanged so existing WhatsApp
    handling can still surface Meta-side errors separately.
    """
    updated = dict(contact)
    country_iso = resolve_country_iso_from_contact(updated)
    dial_code = str(updated.get("countryCode") or "")

    primary_prep = _normalize_phone_field(str(contact.get("phone") or ""), country_iso, dial_code)
    if primary_prep:
        updated["phone"] = primary_prep["whatsapp_recipient"]
        if primary_prep.get("country"):
            updated["countryIso"] = primary_prep["country"]
        if primary_prep.get("country_calling_code"):
            updated["countryCode"] = f"+{primary_prep['country_calling_code']}"

    secondary_prep = _normalize_phone_field(
        str(contact.get("secondaryPhone") or ""),
        country_iso,
        dial_code,
    )
    if secondary_prep:
        updated["secondaryPhone"] = secondary_prep["whatsapp_recipient"]

    phones = [
        p.strip()
        for p in (updated.get("phone"), updated.get("secondaryPhone"))
        if p and str(p).strip()
    ]
    if phones:
        updated["phones"] = phones

    return updated
