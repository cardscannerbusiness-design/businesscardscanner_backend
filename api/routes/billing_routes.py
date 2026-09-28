"""Prepaid billing — start checkout for Admin companies and verify provider webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth.constants import ROLE_ADMIN
from auth.dependencies import require_role
from services.billing_service import (
    BillingError,
    handle_razorpay_webhook,
    handle_stripe_webhook,
    list_company_payments,
    start_prepaid_checkout,
)
from services.feature_control import require_feature
from services.storage_service import resolve_company_id_for_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["Billing"])

# Temporary lock. Set PAYMENTS_ENABLED=true to restore checkout without code changes.
_PAYMENTS_ON = {"1", "true", "yes", "on"}


def payments_enabled() -> bool:
    return os.getenv("PAYMENTS_ENABLED", "").strip().lower() in _PAYMENTS_ON


class PrepaidCheckoutRequest(BaseModel):
    package_id: str = Field(..., min_length=3, max_length=64)


def _parse_json_body(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload.") from exc
    return payload if isinstance(payload, dict) else {}


def _verify_razorpay_signature(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _verify_stripe_signature(secret: str, body: bytes, header: str) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    timestamp = parts.get("t", "")
    provided = parts.get("v1", "")
    signed = f"{timestamp}.".encode("utf-8") + body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


@router.post(
    "/prepaid/checkout",
    summary="Start prepaid package checkout",
)
def prepaid_checkout(
    body: PrepaidCheckoutRequest,
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    if not payments_enabled():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "PAYMENTS_DISABLED",
                "message": "Online payments are currently unavailable.",
            },
        )
    require_feature(user, "subscription")
    try:
        return start_prepaid_checkout(user, body.package_id)
    except BillingError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.get(
    "/history",
    summary="Payment history for the authenticated Admin company",
)
def billing_history(user: dict = Depends(require_role(ROLE_ADMIN))):
    require_feature(user, "subscription")
    company_id = resolve_company_id_for_user(user)
    return list_company_payments(str(company_id) if company_id else None)


@router.post("/webhooks/razorpay", summary="Razorpay payment webhook")
async def razorpay_webhook(request: Request):
    secret = (os.getenv("RAZORPAY_WEBHOOK_SECRET") or "").strip()
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature") or ""
    env = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "").lower()
    if secret:
        if not _verify_razorpay_signature(secret, body, signature):
            logger.warning("Razorpay webhook signature mismatch")
            raise HTTPException(status_code=400, detail="Invalid webhook signature.")
    elif env in {"production", "prod"}:
        raise HTTPException(status_code=503, detail="Razorpay webhook secret is not configured.")
    payload = _parse_json_body(body)
    try:
        return handle_razorpay_webhook(payload)
    except BillingError as exc:
        logger.warning("Razorpay webhook not fulfilled: %s", exc.message)
        return {"success": False, "code": exc.code, "message": exc.message}


@router.post("/webhooks/stripe", summary="Stripe payment webhook")
async def stripe_webhook(request: Request):
    secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    body = await request.body()
    signature = request.headers.get("Stripe-Signature") or ""
    env = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "").lower()
    if secret:
        if not _verify_stripe_signature(secret, body, signature):
            logger.warning("Stripe webhook signature mismatch")
            raise HTTPException(status_code=400, detail="Invalid webhook signature.")
    elif env in {"production", "prod"}:
        raise HTTPException(status_code=503, detail="Stripe webhook secret is not configured.")
    payload = _parse_json_body(body)
    try:
        return handle_stripe_webhook(payload)
    except BillingError as exc:
        logger.warning("Stripe webhook not fulfilled: %s", exc.message)
        return {"success": False, "code": exc.code, "message": exc.message}
