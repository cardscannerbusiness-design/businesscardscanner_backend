"""Generic calling-code dedupe helpers — country-agnostic, metadata-driven.

Never invents a missing country code. Never hardcodes India/UAE/etc.
Duplicate CC stripping is only suggested; callers must confirm with libphonenumber.
"""

from __future__ import annotations

import re


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def strip_duplicate_calling_code_prefix(national_digits: str, calling_code_digits: str) -> str:
    """
    Candidate strip of a repeated calling-code prefix from national digits.

    Does NOT validate by itself — callers must confirm with phonenumbers that
    the stripped form is a possible/valid number and the original is not.
    """
    national = digits_only(national_digits)
    cc = digits_only(calling_code_digits)
    if not national or not cc:
        return national
    if national.startswith(cc) and len(national) > len(cc):
        return national[len(cc) :]
    return national


def candidate_deduped_international_digits(
    digits: str,
    calling_codes_longest_first: list[str],
) -> tuple[str, str] | None:
    """
    If `digits` matches <CC><CC><NATIONAL>, return (cc, national).

    Uses longest-first calling codes from international metadata.
    Returns None when no duplicated-CC pattern is present.
    """
    cleaned = digits_only(digits)
    if not cleaned:
        return None
    for code in calling_codes_longest_first:
        if not cleaned.startswith(code):
            continue
        rest = cleaned[len(code) :]
        # Longer metadata prefix matched but is not duplicated — try shorter codes.
        if not rest.startswith(code):
            continue
        national = rest[len(code) :]
        if len(national) < 3:
            continue
        return code, national
    return None
