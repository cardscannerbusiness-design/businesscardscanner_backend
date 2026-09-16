"""Parse international and national phone numbers (country-agnostic)."""

from __future__ import annotations

import phonenumbers

from utils.international_phone.cc_dedupe import (
    candidate_deduped_international_digits,
    digits_only,
    strip_duplicate_calling_code_prefix,
)


def looks_international(raw: str) -> bool:
    trimmed = (raw or "").strip()
    return trimmed.startswith("+") or trimmed.startswith("00")


def _calling_codes_longest_first() -> list[str]:
    codes = {
        str(code)
        for code in phonenumbers.COUNTRY_CODE_TO_REGION_CODE.keys()
        if code and code > 0
    }
    return sorted(codes, key=len, reverse=True)


def _is_possible(raw_e164: str) -> bool:
    try:
        parsed = phonenumbers.parse(raw_e164, None)
        return phonenumbers.is_possible_number(parsed)
    except phonenumbers.NumberParseException:
        return False


def _is_valid(raw_e164: str) -> bool:
    try:
        parsed = phonenumbers.parse(raw_e164, None)
        return phonenumbers.is_valid_number(parsed)
    except phonenumbers.NumberParseException:
        return False


def prepare_phone_input(raw: str) -> str:
    """
    Normalize 00→+ and collapse OCR-duplicated calling codes when confirmed.

    Duplicate stripping is applied only when:
    - digits match <CC><CC><NATIONAL> for a metadata calling code, AND
    - the deduped form is possible, AND
    - the original form is not a valid number (avoid blind edits).
    """
    trimmed = (raw or "").strip()
    if not trimmed:
        return ""
    if trimmed.startswith("00") and len(trimmed) > 2:
        trimmed = f"+{trimmed[2:]}"
    if not trimmed.startswith("+"):
        return trimmed

    digits = digits_only(trimmed)
    if not digits:
        return trimmed
    candidate = f"+{digits}"

    dup = candidate_deduped_international_digits(digits, _calling_codes_longest_first())
    if not dup:
        return candidate

    code, national = dup
    deduped = f"+{code}{national}"
    if _is_possible(deduped) and not _is_valid(candidate):
        return deduped
    return candidate


def parse_international_phone(raw: str) -> phonenumbers.PhoneNumber | None:
    prepared = prepare_phone_input(raw)
    if not prepared.startswith("+"):
        return None
    try:
        return phonenumbers.parse(prepared, None)
    except phonenumbers.NumberParseException:
        return None


def parse_national_phone(raw: str, country_iso: str) -> phonenumbers.PhoneNumber | None:
    """
    Parse a national number for a known ISO region.

    If the national digits accidentally repeat the region's calling code and the
    as-is parse is not valid, try the stripped form when it is possible/valid.
    """
    prepared = (raw or "").strip()
    if not prepared or prepared.startswith("+"):
        return None
    region = country_iso.upper()

    as_is: phonenumbers.PhoneNumber | None = None
    try:
        as_is = phonenumbers.parse(prepared, region)
        if phonenumbers.is_valid_number(as_is) or phonenumbers.is_possible_number(as_is):
            # Prefer as-is when already valid; for merely "possible", still allow
            # a validated strip below when digits clearly duplicate the CC.
            if phonenumbers.is_valid_number(as_is):
                return as_is
    except phonenumbers.NumberParseException:
        as_is = None

    try:
        cc = phonenumbers.country_code_for_region(region)
        if not cc:
            return as_is
        digits = digits_only(prepared)
        stripped = strip_duplicate_calling_code_prefix(digits, str(cc))
        if not stripped or stripped == digits:
            return as_is
        stripped_parsed = phonenumbers.parse(stripped, region)
        if phonenumbers.is_valid_number(stripped_parsed) or (
            phonenumbers.is_possible_number(stripped_parsed)
            and (as_is is None or not phonenumbers.is_valid_number(as_is))
        ):
            return stripped_parsed
    except phonenumbers.NumberParseException:
        return as_is
    except Exception:
        return as_is
    return as_is
