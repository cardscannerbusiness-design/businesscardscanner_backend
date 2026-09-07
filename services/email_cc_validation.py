"""CC address validation for thank-you emails (SMTP)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from utils.parser_utils import is_valid_email


def _validate_email(email: str) -> tuple[bool, str]:
    normalized = str(email or "").strip()
    if not normalized:
        return False, "Email address is required."
    if not is_valid_email(normalized):
        return False, f"Invalid email format: '{normalized}'"
    return True, normalized


@dataclass
class CcValidationResult:
    valid: list[str] = field(default_factory=list)
    invalid: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": list(self.valid),
            "invalid": list(self.invalid),
        }


def validate_cc_address_list(
    cc_addresses: list[str] | None,
    *,
    to_address: str,
    allow_same_as_to: bool = False,
) -> CcValidationResult:
    """
    Validate CC / receive-notification recipients.
    Invalid entries are skipped; valid ones are deduplicated.

    When allow_same_as_to is False (SMTP Cc: header), addresses matching To are rejected.
    When True (separate scanned-details email), same-as-To is allowed so Receive still
    gets the data copy when contact email equals CMS Receive email.
    """
    result = CcValidationResult()
    if not cc_addresses:
        return result

    to_normalized = str(to_address or "").strip().lower()
    seen: set[str] = set()

    for raw in cc_addresses:
        candidate = str(raw or "").strip()
        if not candidate:
            continue

        is_valid, validated_or_error = _validate_email(candidate)
        if not is_valid:
            result.invalid.append(
                {"address": candidate, "reason": str(validated_or_error)},
            )
            continue

        email = str(validated_or_error).strip()
        key = email.lower()
        if key == to_normalized and not allow_same_as_to:
            result.invalid.append(
                {"address": candidate, "reason": "CC address cannot match the To recipient."},
            )
            continue
        if key in seen:
            result.invalid.append(
                {"address": candidate, "reason": "Duplicate CC address."},
            )
            continue

        seen.add(key)
        result.valid.append(email)

    return result


def normalize_cc_addresses(
    cc_addresses: list[str] | None,
    *,
    to_address: str,
    allow_same_as_to: bool = False,
) -> list[str]:
    """Return only valid, deduplicated CC emails (backward-compatible helper)."""
    return validate_cc_address_list(
        cc_addresses,
        to_address=to_address,
        allow_same_as_to=allow_same_as_to,
    ).valid
