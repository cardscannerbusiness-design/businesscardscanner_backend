"""Dynamic country metadata from phonenumbers (Google libphonenumber)."""

from __future__ import annotations

from functools import lru_cache
from typing import TypedDict

import phonenumbers
from phonenumbers import PhoneMetadata


class InternationalCountry(TypedDict):
    iso: str
    name: str
    calling_code: str


def _region_display_name(iso: str) -> str:
    try:
        import babel
        from babel import Locale

        return Locale.parse("en").territories.get(iso, iso) or iso
    except Exception:
        return iso


@lru_cache(maxsize=1)
def get_all_countries() -> list[InternationalCountry]:
    """All supported regions from phonenumbers metadata — not a hand-maintained list."""
    countries: list[InternationalCountry] = []
    for iso in sorted(phonenumbers.SUPPORTED_REGIONS):
        if iso == "001":
            continue
        calling = phonenumbers.country_code_for_region(iso)
        if calling == 0:
            continue
        countries.append(
            {
                "iso": iso,
                "name": _region_display_name(iso),
                "calling_code": f"+{calling}",
            }
        )
    return countries


def get_supported_country_count() -> int:
    return len(get_all_countries())


def find_country_by_iso(iso: str | None) -> InternationalCountry | None:
    if not iso:
        return None
    normalized = iso.strip().upper()
    for country in get_all_countries():
        if country["iso"] == normalized:
            return country
    return None


def search_countries(query: str) -> list[InternationalCountry]:
    q = (query or "").strip().lower()
    if not q:
        return get_all_countries()
    q_digits = "".join(ch for ch in q if ch.isdigit())
    results: list[InternationalCountry] = []
    for country in get_all_countries():
        dial_digits = country["calling_code"].lstrip("+")
        if (
            q in country["name"].lower()
            or q in country["iso"].lower()
            or q in country["calling_code"].lower()
            or (q_digits and q_digits in dial_digits)
        ):
            results.append(country)
    return results


def get_metadata_for_region(iso: str) -> PhoneMetadata | None:
    return phonenumbers.PhoneMetadata.metadata_for_region(iso.upper(), None)
