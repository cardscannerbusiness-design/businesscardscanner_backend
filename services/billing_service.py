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

# Stored on payment_intents for SuperAdmin/CMS diagnostics. Never returned to customers.
_GATEWAY_CONFIG_ERROR = (
    "Payment gateway is not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET "
    "(or STRIPE_SECRET_KEY) on the backend to enable live checkout."
)
CUSTOMER_GATEWAY_UNAVAILABLE = (
    "Payment could not be started. Please try again later or contact support."
)


def _customer_safe_error(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if "RAZORPAY_KEY" in upper or "STRIPE_SECRET" in upper or "KEY_SECRET" in upper:
        return CUSTOMER_GATEWAY_UNAVAILABLE
    return text

PREPAID_PACKAGES: dict[str, dict[str, Any]] = {
    "PREPAID_STARTER": {"name": "Starter", "amount_inr": 750, "scan_capacity": 1000, "validity_days": 365},
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
        error_message = _GATEWAY_CONFIG_ERROR

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
        "awaiting_gateway": CUSTOMER_GATEWAY_UNAVAILABLE,
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


def _row_to_payment_record(row: dict[str, Any]) -> dict[str, Any]:
    from services.cms_app_access import payment_status_view, classify_payment_status

    pkg = PREPAID_PACKAGES.get(str(row.get("package_id") or "").upper(), {})
    status = str(row.get("status") or "")
    view = payment_status_view(classify_payment_status(plan_name=None, intent_status=status))
    amount = int(row.get("amount_inr") or 0)
    created = row.get("created_at")
    updated = row.get("updated_at")
    validity_days = int(row.get("validity_days") or 0)
    valid_until = None
    if view["status_code"] == "paid" and updated and validity_days:
        try:
            from datetime import timedelta

            valid_until = (updated + timedelta(days=validity_days)).isoformat()
        except Exception:
            valid_until = None
    return {
        "id": str(row.get("id") or ""),
        "package_id": row.get("package_id") or "",
        "package_name": pkg.get("name") or str(row.get("package_id") or "Plan"),
        "provider": row.get("provider") or "",
        "status": status,
        "status_code": view["status_code"],
        "status_label": view["status_label"],
        "status_mark": view["status_mark"],
        "amount_inr": amount,
        "scan_capacity": int(row.get("scan_capacity") or 0),
        "validity_days": validity_days,
        "provider_ref": row.get("provider_ref") or "",
        "error_message": _customer_safe_error(str(row.get("error_message") or "")),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
        "valid_until": valid_until,
    }


def list_company_payments(company_id: str | None) -> dict[str, Any]:
    if not company_id:
        return {"items": [], "total": 0}
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, company_id, user_id, package_id, provider, status, amount_inr,
                   scan_capacity, validity_days, provider_ref, checkout_url, error_message,
                   created_at, updated_at
            FROM payment_intents
            WHERE company_id = %s
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (company_id,),
        )
        rows = cur.fetchall() or []
    items = [_row_to_payment_record(dict(row)) for row in rows]
    latest_paid = next((item for item in items if item["status_code"] == "paid"), None)
    return {"items": items, "total": len(items), "latest_paid": latest_paid}


def fulfill_paid_intent(
    *,
    intent_id: str | None = None,
    provider_ref: str | None = None,
    package_id: str | None = None,
    company_id: str | None = None,
) -> dict[str, Any]:
    """Mark an intent paid and apply the package to the company. Idempotent."""
    with db_cursor(commit=True) as cur:
        row = None
        if intent_id:
            cur.execute("SELECT * FROM payment_intents WHERE id = %s", (intent_id,))
            row = cur.fetchone()
        if not row and provider_ref:
            cur.execute(
                """
                SELECT * FROM payment_intents
                WHERE provider_ref = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (provider_ref,),
            )
            row = cur.fetchone()
        if not row and company_id and package_id:
            cur.execute(
                """
                SELECT * FROM payment_intents
                WHERE company_id = %s AND package_id = %s
                  AND status IN ('pending', 'created', 'awaiting_gateway', 'authorized')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (company_id, package_id),
            )
            row = cur.fetchone()
        if not row:
            raise BillingError("INTENT_NOT_FOUND", "Payment could not be matched.", 404)

        intent = dict(row)
        pkg_id = str(package_id or intent.get("package_id") or "").strip().upper()
        pkg = PREPAID_PACKAGES.get(pkg_id)
        cid = str(company_id or intent.get("company_id") or "")
        if not pkg or not cid:
            raise BillingError("INVALID_PACKAGE", "Paid package could not be applied.", 422)

        current_status = str(intent.get("status") or "").lower()
        already_paid = current_status in {"paid", "captured", "success", "succeeded", "completed"}
        if not already_paid:
            cur.execute(
                """
                UPDATE payment_intents
                SET status = 'paid', error_message = '', updated_at = NOW()
                WHERE id = %s
                """,
                (intent["id"],),
            )
            cur.execute(
                """
                UPDATE companies
                SET plan_name = %s, card_limit = %s, updated_at = NOW()
                WHERE id = %s AND COALESCE(status, 'active') <> 'deleted'
                """,
                (pkg_id, int(pkg["scan_capacity"]), cid),
            )
            logger.info(
                "Prepaid plan activated company_id=%s package=%s intent=%s",
                cid,
                pkg_id,
                intent["id"],
            )
        return {
            "success": True,
            "already_paid": already_paid,
            "intent_id": str(intent["id"]),
            "company_id": cid,
            "package_id": pkg_id,
            "plan_name": pkg_id,
            "card_limit": int(pkg["scan_capacity"]),
        }


def mark_intent_status(
    *,
    provider_ref: str | None,
    status: str,
    error_message: str = "",
) -> None:
    if not provider_ref:
        return
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE payment_intents
            SET status = %s, error_message = %s, updated_at = NOW()
            WHERE provider_ref = %s
              AND status NOT IN ('paid', 'captured', 'success', 'succeeded', 'completed')
            """,
            (status, error_message[:500], provider_ref),
        )


def handle_razorpay_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("event") or "")
    payment_entity = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    order_entity = ((payload.get("payload") or {}).get("order") or {}).get("entity") or {}
    notes = payment_entity.get("notes") or order_entity.get("notes") or {}
    if not isinstance(notes, dict):
        notes = {}
    order_id = str(payment_entity.get("order_id") or order_entity.get("id") or "")
    package_id = str(notes.get("package_id") or "")
    company_id = str(notes.get("company_id") or "")
    intent_id = str(notes.get("intent_id") or "")

    if event in {"payment.captured", "order.paid", "payment.authorized"}:
        return fulfill_paid_intent(
            intent_id=intent_id or None,
            provider_ref=order_id or None,
            package_id=package_id or None,
            company_id=company_id or None,
        )
    if event in {"payment.failed", "order.cancelled"}:
        mark_intent_status(
            provider_ref=order_id or None,
            status="failed" if "failed" in event else "cancelled",
            error_message=str(payment_entity.get("error_description") or event),
        )
        return {"success": True, "status": "failed" if "failed" in event else "cancelled"}
    return {"success": True, "ignored": event}


def handle_stripe_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    event_type = str(payload.get("type") or "")
    obj = (payload.get("data") or {}).get("object") or {}
    metadata = obj.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    session_id = str(obj.get("id") or "")
    if event_type in {"checkout.session.completed", "payment_intent.succeeded"}:
        return fulfill_paid_intent(
            intent_id=str(metadata.get("intent_id") or "") or None,
            provider_ref=session_id or None,
            package_id=str(metadata.get("package_id") or "") or None,
            company_id=str(metadata.get("company_id") or "") or None,
        )
    if event_type in {"checkout.session.expired", "payment_intent.payment_failed"}:
        mark_intent_status(
            provider_ref=session_id,
            status="expired" if "expired" in event_type else "failed",
            error_message=event_type,
        )
        return {"success": True, "status": "expired" if "expired" in event_type else "failed"}
    return {"success": True, "ignored": event_type}
