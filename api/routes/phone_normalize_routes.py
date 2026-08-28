"""REST endpoints for universal international phone normalization."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from utils.international_phone import (
    get_all_countries,
    get_supported_country_count,
    normalize_international_phone,
    search_countries,
)

router = APIRouter(prefix="/api/phone", tags=["international-phone"])


class NormalizePhoneRequest(BaseModel):
    phone: str = Field(..., description="Raw phone input (international +... or national)")
    country: str | None = Field(
        None,
        description="ISO 3166-1 alpha-2 required for national numbers without +",
    )


@router.get("/countries")
def list_countries(q: str | None = None) -> dict[str, Any]:
    countries = search_countries(q) if q else get_all_countries()
    return {
        "count": len(countries),
        "total_supported": get_supported_country_count(),
        "countries": countries,
    }


@router.post("/normalize")
def normalize_phone(body: NormalizePhoneRequest) -> dict[str, Any]:
    return normalize_international_phone(body.phone, body.country)
