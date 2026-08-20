"""Prepaid checkout start — Razorpay/Stripe when configured, otherwise a recorded intent.

Does not activate scans until a provider webhook/confirm lands. Selecting a package
always persists a payment_intents row so SuperAdmin can see demand.
"""

from __future__ import annotations

import base64
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN
from db.pool import db_cursor

logger = logging.getLogger(__name__)

PREPAID_PACKAGES: dict[str, dict[str, Any]] = {
    "PREPAID_STARTER": {"name": "Starter", "amount_inr": 750, "scan_capacity": 1000, "validity_days": 5},
    "PREPAID_GROWTH": {"name": "Growth", "amount_inr": 1500, "scan_capacity": 2000, "validity_days": 10},
    "PREPAID_PRO": {"name": "Pro", "amount_inr": 2300, "scan_capacity": 3000, "validity_days": 25},
    "PREPAID_EVENT_PLUS": {"name": "Event Plus", "amount_inr": 3300, "scan_capacity": 5000, "validity_days": 30},
}


class BillingError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def start_prepaid_checkout(user: dict[str, Any], package_id: str) -> dict[str, Any]:
    role = str(user.get("role") or "")
    if role == ROLE_SUPER_ADMIN:
        raise BillingError(
            "NOT_REQUIRED",
            "SuperAdmin accounts are unlimited and do not require prepaid payment.",
            400,
        )
    if role != ROLE_ADMIN:
        raise BillingError("FORBIDDEN", "Only a company Admin can start prepaid checkout.", 403)

    company_id = user.get("company_id")
    if not company_id:
        raise BillingError("NO_COMPANY", "This account is not linked to a company.", 400)

    pkg = PREPAID_PACKAGES.get((package_id or "").strip().upper())
    if not pkg:
        raise BillingError("INVALID_PACKAGE", "Unknown prepaid package.", 422)

    razorpay_key = (os.getenv("RAZORPAY_KEY_ID") or "").strip()
    razorpay_secret = (os.getenv("RAZORPAY_KEY_SECRET") or "").strip()
    stripe_secret = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    frontend = (os.getenv("FRONTEND_URL") or os.getenv("APP_URL") or "").rstrip("/")

    intent_id = str(uuid.uuid4())
    now = _now()
    provider = "manual"
    status = "created"
    provider_ref = ""
    checkout_url = ""
    error_message = ""

    if razorpay_key and razorpay_secret:
        provider = "razorpay"
        try:
            auth = base64.b64encode(f"{razorpay_key}:{razorpay_secret}".encode()).decode()
            payload = {
                "amount": int(pkg["amount_inr"]) * 100,
                "currency": "INR",
                "receipt": f"ncs_{intent_id[:12]}",
                "notes": {
                    "package_id": package_id,
                    "company_id": str(company_id),
                    "user_id": str(user["id"]),
                },
            }
            with httpx.Client(timeout=20.0) as client:
                res = client.post(
                    "https://api.razorpay.com/v1/orders",
                    headers={"Authorization": f"Basic {auth}"},
                    json=payload,
                )
            data = res.json() if res.content else {}
            if res.status_code >= 400:
                error_message = str(data.get("error", {}).get("description") or res.text)[:500]
                status = "provider_error"
            else:
                provider_ref = str(data.get("id") or "")
                status = "pending"
                checkout_url = f"{frontend}/subscription?intent={intent_id}" if frontend else ""
        except Exception as exc:
            logger.exception("Razorpay order failed")
            status = "provider_error"
            error_message = str(exc)[:500]
    elif stripe_secret:
        provider = "stripe"
        try:
            success = f"{frontend}/subscription?paid=1" if frontend else "https://namecardscan.com/subscription?paid=1"
            cancel = f"{frontend}/subscription?canceled=1" if frontend else "https://namecardscan.com/subscription?canceled=1"
            with httpx.Client(timeout=20.0) as client:
                res = client.post(
                    "https://api.stripe.com/v1/checkout/sessions",
                    headers={"Authorization": f"Bearer {stripe_secret}"},
                    data={
                        "mode": "payment",
                        "success_url": success,
                        "cancel_url": cancel,
                        "line_items[0][quantity]": "1",
                        "line_items[0][price_data][currency]": "inr",
                        "line_items[0][price_data][unit_amount]": str(int(pkg["amount_inr"]) * 100),
                        "line_items[0][price_data][product_data][name]": f"NameCardScan {pkg['name']}",
                        "metadata[package_id]": package_id,
                        "metadata[company_id]": str(company_id),
                        "metadata[intent_id]": intent_id,
                    },
                )
            data = res.json() if res.content else {}
            if res.status_code >= 400:
                error_message = str(data.get("error", {}).get("message") or res.text)[:500]
                status = "provider_error"
            else:
                provider_ref = str(data.get("id") or "")
                checkout_url = str(data.get("url") or "")
                status = "pending"
        except Exception as exc:
            logger.exception("Stripe checkout failed")
            status = "provider_error"
            error_message = str(exc)[:500]
    else:
        status = "awaiting_gateway"
        error_message = (
            "Payment gateway is not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET "
            "(or STRIPE_SECRET_KEY) on the backend to enable live checkout."
        )

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO payment_intents (
                id, company_id, user_id, package_id, provider, status, amount_inr,
                scan_capacity, validity_days, provider_ref, checkout_url, error_message,
                created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                intent_id,
                company_id,
                user["id"],
                package_id,
                provider,
                status,
                pkg["amount_inr"],
                pkg["scan_capacity"],
                pkg["validity_days"],
                provider_ref,
                checkout_url,
                error_message,
                now,
                now,
            ),
        )

    message = {
        "pending": f"Checkout started for {pkg['name']}. Complete payment to unlock {pkg['scan_capacity']} scans.",
        "awaiting_gateway": error_message,
        "provider_error": "The payment provider could not start checkout. The request was recorded.",
        "created": "Prepaid request recorded.",
    }.get(status, "Prepaid request recorded.")

    return {
        "success": status in ("pending", "awaiting_gateway", "created"),
        "intent_id": intent_id,
        "package_id": package_id,
        "package_name": pkg["name"],
        "amount_inr": pkg["amount_inr"],
        "scan_capacity": pkg["scan_capacity"],
        "validity_days": pkg["validity_days"],
        "provider": provider,
        "status": status,
        "provider_ref": provider_ref,
        "checkout_url": checkout_url,
        "razorpay_key_id": razorpay_key if provider == "razorpay" and status == "pending" else "",
        "message": message,
    }
