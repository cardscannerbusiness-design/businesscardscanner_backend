"""Parse international and national phone numbers."""

from __future__ import annotations

import phonenumbers


def looks_international(raw: str) -> bool:
    trimmed = (raw or "").strip()
    return trimmed.startswith("+") or trimmed.startswith("00")


def prepare_phone_input(raw: str) -> str:
    trimmed = (raw or "").strip()
    if not trimmed:
        return ""
    if trimmed.startswith("00") and len(trimmed) > 2:
        return f"+{trimmed[2:]}"
    return trimmed


def parse_international_phone(raw: str) -> phonenumbers.PhoneNumber | None:
    prepared = prepare_phone_input(raw)
    if not prepared.startswith("+"):
        return None
    try:
        return phonenumbers.parse(prepared, None)
    except phonenumbers.NumberParseException:
        return None


def parse_national_phone(raw: str, country_iso: str) -> phonenumbers.PhoneNumber | None:
    prepared = (raw or "").strip()
    if not prepared or prepared.startswith("+"):
        return None
    try:
        return phonenumbers.parse(prepared, country_iso.upper())
    except phonenumbers.NumberParseException:
        return None
