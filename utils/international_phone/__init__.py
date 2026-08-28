"""Universal international phone handling — isolated from existing WhatsApp code."""

from utils.international_phone.countries import (
    get_all_countries,
    get_supported_country_count,
    search_countries,
)
from utils.international_phone.formatter import e164_to_whatsapp_recipient_digits, format_phone_to_e164
from utils.international_phone.normalizer import normalize_international_phone
from utils.international_phone.parser import looks_international, prepare_phone_input
from utils.international_phone.whatsapp_adapter import (
    apply_international_phone_to_contact,
    prepare_whatsapp_recipient,
    resolve_country_iso_from_contact,
)

__all__ = [
    "apply_international_phone_to_contact",
    "e164_to_whatsapp_recipient_digits",
    "format_phone_to_e164",
    "get_all_countries",
    "get_supported_country_count",
    "looks_international",
    "normalize_international_phone",
    "prepare_phone_input",
    "prepare_whatsapp_recipient",
    "resolve_country_iso_from_contact",
    "search_countries",
]
