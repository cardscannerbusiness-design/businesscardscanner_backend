"""Map CMS {{N}} tokens to extracted review-page contact fields."""

from __future__ import annotations

from typing import Any

# Keys match BusinessCardScanner_Frontend review / leadFields (+ event/country).
REVIEW_FIELD_KEYS: tuple[tuple[str, str], ...] = (
    ("fullName", "Full Name"),
    ("firstName", "First Name"),
    ("lastName", "Last Name"),
    ("designation", "Designation"),
    ("companyName", "Company Name"),
    ("countryCode", "Country Code"),
    ("phoneNumber", "Primary Phone"),
    ("secondaryPhoneNumber", "Secondary Phone"),
    ("emailAddress", "Primary Email"),
    ("secondaryEmailAddress", "Secondary Email"),
    ("website", "Primary Website"),
    ("secondaryWebsite", "Secondary Website"),
    ("address", "Primary Address"),
    ("secondaryAddress", "Secondary Address"),
    ("socialLinks", "Social Media Links"),
    ("gstNumber", "GST / Tax Number"),
    ("eventName", "Event Name"),
    ("eventDay", "Event Day"),
    ("notes", "Notes"),
    ("senderName", "Sender / Sign-off (CMS Email)"),
)

_VALID_FIELDS = {key for key, _ in REVIEW_FIELD_KEYS}

DEFAULT_TOKEN_MAP: dict[str, str] = {
    "1": "fullName",
    "2": "phoneNumber",
    "3": "emailAddress",
    "4": "website",
    "5": "companyName",
}

# Preview samples for CMS live preview (not used on real send).
PREVIEW_SAMPLES: dict[str, str] = {
    "fullName": "Alex Rivera",
    "firstName": "Alex",
    "lastName": "Rivera",
    "designation": "Product Manager",
    "companyName": "Acme Corp",
    "countryCode": "+91",
    "phoneNumber": "+91 98765 43210",
    "secondaryPhoneNumber": "+91 91234 56789",
    "emailAddress": "partner@example.com",
    "secondaryEmailAddress": "alt@example.com",
    "website": "https://example.com",
    "secondaryWebsite": "https://acme.example",
    "address": "12 Market Street, Mumbai",
    "secondaryAddress": "Warehouse B, Pune",
    "socialLinks": "linkedin.com/in/alex",
    "gstNumber": "27AAAAA0000A1Z5",
    "eventName": "Tech Expo 2026",
    "eventDay": "Day 1",
    "notes": "Met at booth A12",
    "senderName": "B2B Team",
}

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "fullName": ("fullName", "name", "full_name"),
    "firstName": ("firstName", "first_name"),
    "lastName": ("lastName", "last_name"),
    "designation": ("designation", "title", "jobTitle"),
    "companyName": ("companyName", "company", "company_name"),
    "countryCode": ("countryCode", "country_code"),
    "phoneNumber": ("phoneNumber", "phone", "primaryPhone"),
    "secondaryPhoneNumber": ("secondaryPhoneNumber", "secondaryPhone", "secondary_phone"),
    "emailAddress": ("emailAddress", "email", "primaryEmail"),
    "secondaryEmailAddress": (
        "secondaryEmailAddress",
        "secondaryEmail",
        "secondary_email",
    ),
    "website": ("website", "url", "primaryWebsite"),
    "secondaryWebsite": ("secondaryWebsite", "secondary_website"),
    "address": ("address", "primaryAddress"),
    "secondaryAddress": ("secondaryAddress", "secondary_address"),
    "socialLinks": ("socialLinks", "social_links"),
    "gstNumber": ("gstNumber", "gst_number", "taxNumber"),
    "eventName": ("eventName", "event_name"),
    "eventDay": ("eventDay", "event_day"),
    "notes": ("notes", "note"),
}


def normalize_token_map(raw: Any) -> dict[str, str]:
    """Return {\"1\": \"fullName\", ...} with valid review field keys."""
    if not isinstance(raw, dict) or not raw:
        return dict(DEFAULT_TOKEN_MAP)

    out: dict[str, str] = {}
    for key, value in raw.items():
        num = str(key).strip()
        if not num.isdigit():
            continue
        field = str(value or "").strip()
        if field not in _VALID_FIELDS:
            continue
        out[num] = field

    return out or dict(DEFAULT_TOKEN_MAP)


def _pick(contact: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(contact.get(key) or "").strip()
        if value:
            return value
    # Secondary from list fields
    phones = contact.get("phones")
    if "phone" in keys and isinstance(phones, list) and phones:
        return str(phones[0] or "").strip()
    emails = contact.get("emails")
    if "email" in keys and isinstance(emails, list) and emails:
        return str(emails[0] or "").strip()
    return ""


def extract_review_field(
    contact: dict[str, Any] | None,
    field_key: str,
    *,
    sender_name: str = "",
) -> str:
    """Read one review-page field from a contact / outreach payload."""
    if field_key == "senderName":
        return (sender_name or "").strip() or "Team"

    if not contact:
        return ""

    aliases = _FIELD_ALIASES.get(field_key) or (field_key,)
    value = _pick(contact, *aliases)

    if field_key == "secondaryPhoneNumber" and not value:
        phones = contact.get("phones")
        if isinstance(phones, list) and len(phones) > 1:
            value = str(phones[1] or "").strip()

    if field_key == "secondaryEmailAddress" and not value:
        emails = contact.get("emails")
        if isinstance(emails, list) and len(emails) > 1:
            value = str(emails[1] or "").strip()

    return value


def resolve_token_values(
    contact: dict[str, Any] | None,
    templates: dict[str, Any] | None,
    *,
    sender_name: str = "",
) -> dict[str, str]:
    """Build {{N}} → actual scanned values using CMS token_map."""
    token_map = normalize_token_map((templates or {}).get("token_map"))
    return {
        num: extract_review_field(contact, field, sender_name=sender_name)
        for num, field in sorted(token_map.items(), key=lambda item: int(item[0]))
    }


def preview_token_values(
    templates: dict[str, Any] | None,
    *,
    sender_name: str = "",
) -> dict[str, str]:
    """CMS preview: sample values for each mapped review field."""
    token_map = normalize_token_map((templates or {}).get("token_map"))
    out: dict[str, str] = {}
    for num, field in token_map.items():
        if field == "senderName":
            out[num] = sender_name or PREVIEW_SAMPLES.get("senderName", "Team")
        else:
            out[num] = PREVIEW_SAMPLES.get(field, "")
    return out


def apply_numbered_tokens(text: str, values: dict[str, str]) -> str:
    rendered = text or ""
    for num, value in sorted(values.items(), key=lambda item: -int(item[0])):
        rendered = rendered.replace(f"{{{{{num}}}}}", value)
    return rendered
