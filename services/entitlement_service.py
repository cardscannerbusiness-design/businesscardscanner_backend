"""Company card-count entitlement — Freemium (10 cards) with room for PAYG/Prepaid.

Storage quota in storage_service.py stays independent. This module is the
single backend decision for:

  * can this company persist another card to PostgreSQL?
  * is WhatsApp allowed?
  * is Email allowed?
  * is Contacts (read/list) allowed?

OCR / Capture must keep working after Freemium exhaustion. Extra cards are
saved on the device (IndexedDB), not in PostgreSQL, until entitlement is
restored (e.g. Pay-as-you-go).

Runtime values always come from companies.card_limit / cards_used.
Do not hard-code 10 at call sites — use DEFAULT_FREEMIUM_CARD_LIMIT.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PLAN_NAME = "FREEMIUM"
USAGE_TYPE_CARD_PROCESS = "CARD_PROCESS"

# Single authoritative Freemium allowance. Env override for test/ops only.
try:
    DEFAULT_FREEMIUM_CARD_LIMIT = max(0, int(os.getenv("FREEMIUM_CARD_LIMIT", "10")))
except ValueError:
    DEFAULT_FREEMIUM_CARD_LIMIT = 10

_LOCK_TIMEOUT_MS = 5_000
_STATEMENT_TIMEOUT_MS = 15_000

_COMPANY_LOCK_COLUMNS = (
    "id",
    "plan_name",
    "card_limit",
    "cards_used",
    "entitlement_started_at",
    "entitlement_exhausted_at",
)

def _limit_label() -> str:
    return f"{DEFAULT_FREEMIUM_CARD_LIMIT}-card"


CARD_LIMIT_MESSAGE = (
    f"Your {_limit_label()} Freemium limit has been reached. "
    "This card can still be saved on this device. Complete payment to unlock Contacts."
)
CONTACTS_FROZEN_MESSAGE = (
    f"Contacts is locked because your {_limit_label()} Freemium limit has been reached. "
    "Complete payment to unlock it."
)
WHATSAPP_FROZEN_MESSAGE = (
    f"WhatsApp is locked because your {_limit_label()} Freemium limit has been reached. "
    "Complete payment to unlock it."
)
EMAIL_FROZEN_MESSAGE = (
    f"Email is locked because your {_limit_label()} Freemium limit has been reached. "
    "Complete payment to unlock it."
)
OUTREACH_BLOCKED_MESSAGE = (
    f"WhatsApp and Email are locked because your {_limit_label()} Freemium limit "
    "has been reached. Complete payment to unlock them."
)


class EntitlementDeniedError(Exception):
    """Business-plan restriction (not a technical failure)."""

    code = "ENTITLEMENT_DENIED"

    def __init__(
        self,
        message: str,
        *,
        company_id: str | None = None,
        cards_used: int | None = None,
        card_limit: int | None = None,
    ):
        self.message = message
        self.company_id = company_id
        self.cards_used = cards_used
        self.card_limit = card_limit
        super().__init__(message)

    def to_response(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "success": False,
            "error": self.code,
            "message": self.message,
        }
        if self.cards_used is not None:
            body["cards_used"] = self.cards_used
        if self.card_limit is not None:
            body["card_limit"] = self.card_limit
        if self.company_id is not None:
            body["company_id"] = self.company_id
        return body


class CardLimitExceededError(EntitlementDeniedError):
    """PostgreSQL persist blocked because cards_used >= card_limit."""

    code = "CONTACTS_FROZEN"

    def __init__(
        self,
        message: str = CARD_LIMIT_MESSAGE,
        *,
        company_id: str | None = None,
        cards_used: int | None = None,
        card_limit: int | None = None,
    ):
        super().__init__(
            message,
            company_id=company_id,
            cards_used=cards_used,
            card_limit=card_limit,
        )


class ContactsFrozenError(EntitlementDeniedError):
    """Contacts list/detail APIs blocked after Freemium exhaustion."""

    code = "CONTACTS_FROZEN"

    def __init__(
        self,
        message: str = CONTACTS_FROZEN_MESSAGE,
        *,
        company_id: str | None = None,
        cards_used: int | None = None,
        card_limit: int | None = None,
    ):
        super().__init__(
            message,
            company_id=company_id,
            cards_used=cards_used,
            card_limit=card_limit,
        )


class OutreachFrozenError(EntitlementDeniedError):
    """WhatsApp / Email send blocked after Freemium exhaustion."""

    code = "OUTREACH_FROZEN"

    def __init__(
        self,
        message: str = OUTREACH_BLOCKED_MESSAGE,
        *,
        company_id: str | None = None,
        cards_used: int | None = None,
        card_limit: int | None = None,
        channel: str | None = None,
    ):
        self.channel = channel
        super().__init__(
            message,
            company_id=company_id,
            cards_used=cards_used,
            card_limit=card_limit,
        )

    def to_response(self) -> dict[str, Any]:
        body = super().to_response()
        if self.channel:
            body["channel"] = self.channel
        return body


def _row_as_dict(row: Any) -> dict[str, Any]:
    if row is None:
        raise LookupError("Company entitlement row is empty")
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, (tuple, list)) and len(row) >= 4:
        return dict(zip(_COMPANY_LOCK_COLUMNS, row[: len(_COMPANY_LOCK_COLUMNS)]))
    raise TypeError(f"Unsupported company entitlement row type: {type(row)!r}")


def _normalize(row: dict[str, Any] | None, company_id: str) -> dict[str, Any]:
    if not row:
        limit = DEFAULT_FREEMIUM_CARD_LIMIT
        used = 0
        plan = DEFAULT_PLAN_NAME
        started = None
        exhausted_at = None
        resolved_id = company_id
    else:
        plan = str(row.get("plan_name") or DEFAULT_PLAN_NAME).strip() or DEFAULT_PLAN_NAME
        raw_limit = row.get("card_limit")
        limit = DEFAULT_FREEMIUM_CARD_LIMIT if raw_limit is None else int(raw_limit)
        used = max(0, int(row.get("cards_used") or 0))
        started = row.get("entitlement_started_at")
        exhausted_at = row.get("entitlement_exhausted_at")
        resolved_id = str(row.get("id") or company_id)

    enforced = limit >= 0
    remaining = max(0, limit - used) if enforced else None
    exhausted = bool(enforced and remaining == 0)
    outreach_ok = (not enforced) or (remaining is not None and remaining > 0)
    contacts_ok = outreach_ok

    return {
        "company_id": resolved_id,
        "plan": plan,
        "plan_name": plan,
        "card_limit": limit if enforced else None,
        "cards_used": used,
        "cards_remaining": remaining if enforced else None,
        "freemium_exhausted": exhausted,
        "card_quota_enforced": enforced,
        "can_process_card": (not enforced) or (remaining is not None and remaining > 0),
        "whatsapp_allowed": outreach_ok,
        "email_allowed": outreach_ok,
        "contacts_allowed": contacts_ok,
        "entitlement_started_at": started,
        "entitlement_exhausted_at": exhausted_at,
    }


def get_entitlement(company_id: str | None) -> dict[str, Any]:
    """Public snapshot. Missing company → unrestricted (e.g. Super Admin)."""
    if not company_id:
        return {
            "company_id": None,
            "plan": "UNLIMITED",
            "plan_name": "Unlimited",
            "card_limit": None,
            "cards_used": 0,
            "cards_remaining": None,
            "freemium_exhausted": False,
            "card_quota_enforced": False,
            "can_process_card": True,
            "whatsapp_allowed": True,
            "email_allowed": True,
            "contacts_allowed": True,
            "entitlement_started_at": None,
            "entitlement_exhausted_at": None,
        }

    from db.pool import db_cursor

    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, plan_name, card_limit, cards_used,
                   entitlement_started_at, entitlement_exhausted_at
            FROM companies
            WHERE id = %s
            """,
            (company_id,),
        )
        row = cur.fetchone()
    return _normalize(_row_as_dict(row) if row else None, company_id)


def _apply_txn_timeouts(cur: Any) -> None:
    cur.execute(f"SET LOCAL lock_timeout = {_LOCK_TIMEOUT_MS}")
    cur.execute(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}")


def _lock_company(cur: Any, company_id: str) -> dict[str, Any]:
    _apply_txn_timeouts(cur)
    cur.execute(
        """
        SELECT id, plan_name, card_limit, cards_used,
               entitlement_started_at, entitlement_exhausted_at
        FROM companies
        WHERE id = %s
        FOR UPDATE
        """,
        (company_id,),
    )
    row = cur.fetchone()
    if not row:
        raise LookupError(f"Company not found: {company_id}")
    return _row_as_dict(row)


def assert_can_process_card(company_id: str | None) -> None:
    """Non-locking check: can this company persist another card to PostgreSQL?

    Do not call this from OCR — scanning must continue after Freemium exhaustion.
    """
    if not company_id:
        return
    info = get_entitlement(company_id)
    if info["can_process_card"]:
        return
    raise CardLimitExceededError(
        company_id=company_id,
        cards_used=info["cards_used"],
        card_limit=info["card_limit"],
    )


def assert_can_access_contacts(company_id: str | None) -> None:
    """Block Contacts list/detail APIs after Freemium exhaustion."""
    if not company_id:
        return
    info = get_entitlement(company_id)
    if info.get("contacts_allowed", True):
        return
    raise ContactsFrozenError(
        company_id=company_id,
        cards_used=info["cards_used"],
        card_limit=info["card_limit"],
    )


def assert_can_send_outreach(
    company_id: str | None,
    *,
    initial_save: bool = False,
    channel: str | None = None,
) -> None:
    """Reject WhatsApp/Email send after Freemium exhaustion (except the just-consumed final card)."""
    if can_send_outreach(company_id, initial_save=initial_save):
        return
    info = get_entitlement(company_id) if company_id else get_entitlement(None)
    if channel == "whatsapp":
        message = WHATSAPP_FROZEN_MESSAGE
    elif channel == "email":
        message = EMAIL_FROZEN_MESSAGE
    else:
        message = OUTREACH_BLOCKED_MESSAGE
    raise OutreachFrozenError(
        message,
        company_id=company_id,
        cards_used=info.get("cards_used"),
        card_limit=info.get("card_limit"),
        channel=channel,
    )


def assert_can_process_card_locked(cur: Any, company_id: str | None) -> dict[str, Any]:
    """Atomic check: SELECT … FOR UPDATE then compare cards_used vs card_limit."""
    if not company_id:
        return get_entitlement(None)
    row = _lock_company(cur, company_id)
    info = _normalize(row, company_id)
    if info["can_process_card"]:
        return info
    raise CardLimitExceededError(
        company_id=company_id,
        cards_used=info["cards_used"],
        card_limit=info["card_limit"],
    )


def consume_card_locked(
    cur: Any,
    company_id: str | None,
    *,
    contact_id: str | None = None,
    user_id: str | None = None,
    plan_name: str | None = None,
) -> dict[str, Any]:
    """Increment cards_used under the current transaction. Caller must hold the row lock."""
    if not company_id:
        return get_entitlement(None)

    cur.execute(
        """
        UPDATE companies
        SET cards_used = cards_used + 1,
            entitlement_started_at = COALESCE(entitlement_started_at, NOW()),
            entitlement_exhausted_at = CASE
                WHEN card_limit IS NOT NULL AND cards_used + 1 >= card_limit THEN NOW()
                ELSE entitlement_exhausted_at
            END,
            updated_at = NOW()
        WHERE id = %s
          AND (card_limit IS NULL OR cards_used < card_limit)
        RETURNING id, plan_name, card_limit, cards_used,
                  entitlement_started_at, entitlement_exhausted_at
        """,
        (company_id,),
    )
    row = cur.fetchone()
    if not row:
        info = get_entitlement(company_id)
        raise CardLimitExceededError(
            company_id=company_id,
            cards_used=info.get("cards_used"),
            card_limit=info.get("card_limit"),
        )

    info = _normalize(_row_as_dict(row), company_id)
    # Plain INSERT — do not use ON CONFLICT. A unique index is not a table
    # constraint, and missing inference aborted the whole contact save.
    cur.execute(
        """
        INSERT INTO usage_events (
            company_id, user_id, contact_id, usage_type, quantity, plan_name
        ) VALUES (%s, %s, %s, %s, 1, %s)
        """,
        (
            company_id,
            user_id,
            contact_id,
            USAGE_TYPE_CARD_PROCESS,
            plan_name or info.get("plan_name") or DEFAULT_PLAN_NAME,
        ),
    )
    logger.info(
        "[ENTITLEMENT] Card consumed company_id=%s used=%s limit=%s contact_id=%s",
        company_id,
        info["cards_used"],
        info["card_limit"],
        contact_id,
    )
    return info


def can_send_outreach(
    company_id: str | None,
    *,
    initial_save: bool = False,
) -> bool:
    """WhatsApp/Email eligibility.

    initial_save=True allows the just-consumed final Freemium card to still
    send thank-you messages (remaining is already 0 after consume).
    Resend / later calls use initial_save=False and are blocked when exhausted.
    """
    if not company_id:
        return True
    info = get_entitlement(company_id)
    if not info["card_quota_enforced"]:
        return True
    if info["cards_remaining"] and info["cards_remaining"] > 0:
        return True
    if initial_save and info["cards_used"] > 0 and info["card_limit"] is not None:
        return info["cards_used"] <= int(info["card_limit"])
    return False


def entitlement_fields_for_usage(company_id: str | None) -> dict[str, Any]:
    """Subset merged into GET /api/storage/usage."""
    info = get_entitlement(company_id)
    return {
        "card_limit": info["card_limit"],
        "cards_used": info["cards_used"],
        "cards_remaining": info["cards_remaining"],
        "freemium_exhausted": info["freemium_exhausted"],
        "card_quota_enforced": info["card_quota_enforced"],
        "can_process_card": info["can_process_card"],
        "whatsapp_allowed": info["whatsapp_allowed"],
        "email_allowed": info["email_allowed"],
        "contacts_allowed": info["contacts_allowed"],
        "entitlement_started_at": info["entitlement_started_at"],
        "entitlement_exhausted_at": info["entitlement_exhausted_at"],
    }
