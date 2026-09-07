"""Validation via phonenumbers metadata."""

from __future__ import annotations

import phonenumbers


def validate_phone_number(number: phonenumbers.PhoneNumber) -> dict[str, bool]:
    return {
        "is_valid": phonenumbers.is_valid_number(number),
        "is_possible": phonenumbers.is_possible_number(number),
    }
