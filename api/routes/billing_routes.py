"""Prepaid billing — start checkout for Admin companies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.constants import ROLE_ADMIN
from auth.dependencies import require_role
from services.billing_service import BillingError, start_prepaid_checkout

router = APIRouter(prefix="/api/billing", tags=["Billing"])


class PrepaidCheckoutRequest(BaseModel):
    package_id: str = Field(..., min_length=3, max_length=64)


@router.post(
    "/prepaid/checkout",
    summary="Start prepaid package checkout",
)
def prepaid_checkout(
    body: PrepaidCheckoutRequest,
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    try:
        return start_prepaid_checkout(user, body.package_id)
    except BillingError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
