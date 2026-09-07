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
A per-user users.scans_unlimited flag is a USER-level exception: that user
gets the complete Freemium workflow (OCR/scan, card save, WhatsApp, Email,
Contacts) without changing company plan_name, card_limit, cards_used, or
storage. It must not unlimited the company or other users.
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
    "cms_channel_locks",
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
CMS_WHATSAPP_LOCKED_MESSAGE = (
    "WhatsApp is locked by Super Admin in CMS for your company."
)
CMS_EMAIL_LOCKED_MESSAGE = (
    "Email is locked by Super Admin in CMS for your company."
)
CMS_SHEETS_LOCKED_MESSAGE = (
    "Google Sheets is locked by Super Admin in CMS for your company."
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
    from services.cms_app_access import effective_channel_locks, payment_snapshot

    pay = payment_snapshot(
        plan_name=plan,
        intent_status=(row or {}).get("payment_intent_status"),
    )
    locks = effective_channel_locks((row or {}).get("cms_channel_locks"), pay["payment_done"])

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
        "whatsapp_allowed": outreach_ok and not locks["whatsapp"],
        "email_allowed": outreach_ok and not locks["email"],
        "contacts_allowed": contacts_ok,
        "google_sheets_allowed": not locks["google_sheets"],
        "cms_channel_locks": locks,
        "entitlement_started_at": started,
        "entitlement_exhausted_at": exhausted_at,
        "scans_unlimited": False,
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
            "google_sheets_allowed": True,
            "cms_channel_locks": {"whatsapp": False, "email": False, "google_sheets": False},
            "entitlement_started_at": None,
            "entitlement_exhausted_at": None,
            "scans_unlimited": False,
        }

    from db.pool import db_cursor

    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, plan_name, card_limit, cards_used,
                   entitlement_started_at, entitlement_exhausted_at,
                   cms_channel_locks,
                   (
                     SELECT status
                     FROM payment_intents
                     WHERE company_id = companies.id
                     ORDER BY updated_at DESC NULLS LAST
                     LIMIT 1
                   ) AS payment_intent_status
            FROM companies
            WHERE id = %s
            """,
            (company_id,),
        )
        row = cur.fetchone()
    return _normalize(_row_as_dict(row) if row else None, company_id)


def user_has_unlimited_scans(user: dict[str, Any] | None) -> bool:
    """True when the authenticated users.scans_unlimited flag is set.

    Reads the server-side user record only. Never use a client-supplied email
    or user id as the source of this decision.
    """
    if not user:
        return False
    return bool(user.get("scans_unlimited"))


def user_has_custom_card_limit(user: dict[str, Any] | None) -> bool:
    """True when this user uses a personal card cap (default 10 or CMS override)."""
    if not user or user_has_unlimited_scans(user):
        return False
    return True


def effective_user_card_limit(
    user: dict[str, Any] | None = None,
    *,
    user_card_limit: int | None = None,
    scans_unlimited: bool = False,
) -> int:
    """Personal scan cap for one user. Default is 10 cards for everyone."""
    if scans_unlimited or user_has_unlimited_scans(user):
        return DEFAULT_FREEMIUM_CARD_LIMIT
    if user_card_limit is not None:
        return max(1, int(user_card_limit))
    if user and user.get("user_card_limit") is not None:
        return max(1, int(user["user_card_limit"]))
    return DEFAULT_FREEMIUM_CARD_LIMIT


def is_default_user_card_limit(limit: int | None) -> bool:
    return limit is None or int(limit) == DEFAULT_FREEMIUM_CARD_LIMIT


def _normalize_user_card_entitlement(
    used: int,
    limit: int,
    company_id: str | None,
) -> dict[str, Any]:
    used = max(0, int(used))
    limit = max(1, int(limit))
    remaining = max(0, limit - used)
    exhausted = remaining == 0
    outreach_ok = not exhausted
    return {
        "company_id": company_id,
        "card_limit": limit,
        "cards_used": used,
        "cards_remaining": remaining,
        "freemium_exhausted": exhausted,
        "card_quota_enforced": True,
        "can_process_card": not exhausted,
        "whatsapp_allowed": outreach_ok,
        "email_allowed": outreach_ok,
        "contacts_allowed": outreach_ok,
        "user_card_limit": limit,
        "user_cards_used": used,
        "scans_unlimited": False,
    }


def resolve_user_card_entitlement(
    user: dict[str, Any] | None,
    company_info: dict[str, Any],
) -> dict[str, Any]:
    """Effective card entitlement for one authenticated user."""
    if user_has_unlimited_scans(user):
        return apply_user_scan_overlay(company_info, user)
    used = int(user.get("user_cards_used") or 0) if user else 0
    limit = effective_user_card_limit(user)
    out = dict(company_info)
    out.update(
        _normalize_user_card_entitlement(
            used,
            limit,
            company_info.get("company_id"),
        )
    )
    return out


def skip_card_quota_for_creator(
    role_name: str | None,
    scans_unlimited: bool,
    *,
    user_card_limit: int | None = None,
) -> bool:
    """Super Admin and per-user unlimited skip all card quota checks."""
    if str(role_name or "").upper() == "SUPER_ADMIN":
        return True
    if bool(scans_unlimited):
        return True
    return False


def uses_user_card_quota(
    role_name: str | None,
    scans_unlimited: bool,
    user_card_limit: int | None = None,
) -> bool:
    """True when card saves use the user's personal counter (default 10 for everyone)."""
    if skip_card_quota_for_creator(role_name, scans_unlimited, user_card_limit=user_card_limit):
        return False
    return True


def skip_storage_quota_for_creator(role_name: str | None) -> bool:
    """Storage-byte quota is role-based only; scans_unlimited does not skip it."""
    return str(role_name or "").upper() == "SUPER_ADMIN"


def apply_user_scan_overlay(
    info: dict[str, Any],
    user: dict[str, Any] | None = None,
    *,
    scans_unlimited: bool | None = None,
) -> dict[str, Any]:
    """Apply per-user entitlement overlay for usage APIs and outreach checks."""
    unlimited = (
        bool(scans_unlimited)
        if scans_unlimited is not None
        else user_has_unlimited_scans(user)
    )
    if unlimited:
        out = dict(info)
        out["scans_unlimited"] = True
        out["can_process_card"] = True
        out["whatsapp_allowed"] = True
        out["email_allowed"] = True
        out["contacts_allowed"] = True
        out["freemium_exhausted"] = False
        from services.admin_env_service import apply_channel_locks_to_entitlement

        return apply_channel_locks_to_entitlement(out, out.get("company_id"))
    if user_has_custom_card_limit(user):
        resolved = resolve_user_card_entitlement(user, info)
        from services.admin_env_service import apply_channel_locks_to_entitlement

        return apply_channel_locks_to_entitlement(resolved, resolved.get("company_id"))
    out = dict(info)
    out["scans_unlimited"] = False
    from services.admin_env_service import apply_channel_locks_to_entitlement

    return apply_channel_locks_to_entitlement(out, out.get("company_id"))


def assert_can_process_card(
    company_id: str | None,
    user: dict[str, Any] | None = None,
    *,
    scans_unlimited: bool = False,
    user_card_limit: int | None = None,
    user_cards_used: int = 0,
) -> None:
    """Non-locking check: can this company persist another card to PostgreSQL?

    Do not call this from OCR — scanning must continue after Freemium exhaustion.
    """
    if user_has_unlimited_scans(user) or scans_unlimited:
        return
    if user:
        limit = effective_user_card_limit(user, scans_unlimited=scans_unlimited)
        used = int(user.get("user_cards_used") or 0)
        info = _normalize_user_card_entitlement(used, limit, company_id)
        if info["can_process_card"]:
            return
        raise CardLimitExceededError(
            company_id=company_id,
            cards_used=info["cards_used"],
            card_limit=info["card_limit"],
        )
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


def _apply_txn_timeouts(cur: Any) -> None:
    cur.execute(f"SET LOCAL lock_timeout = {_LOCK_TIMEOUT_MS}")
    cur.execute(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}")


def _lock_company(cur: Any, company_id: str) -> dict[str, Any]:
    _apply_txn_timeouts(cur)
    cur.execute(
        """
        SELECT id, plan_name, card_limit, cards_used,
               entitlement_started_at, entitlement_exhausted_at,
               cms_channel_locks,
               (
                 SELECT status
                 FROM payment_intents
                 WHERE company_id = companies.id
                 ORDER BY updated_at DESC NULLS LAST
                 LIMIT 1
               ) AS payment_intent_status
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


def assert_can_access_contacts(
    company_id: str | None,
    user: dict[str, Any] | None = None,
    *,
    scans_unlimited: bool = False,
) -> None:
    """Block Contacts list/detail APIs after Freemium exhaustion."""
    if user_has_unlimited_scans(user) or scans_unlimited:
        return
    if user_has_custom_card_limit(user):
        base = {"company_id": company_id} if company_id else get_entitlement(None)
        info = resolve_user_card_entitlement(user, base)
        if info.get("contacts_allowed", True):
            return
        raise ContactsFrozenError(
            company_id=company_id,
            cards_used=info["cards_used"],
            card_limit=info["card_limit"],
        )
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
    user: dict[str, Any] | None = None,
    scans_unlimited: bool = False,
) -> None:
    """Reject WhatsApp/Email send after CMS lock or Freemium exhaustion."""
    from services.admin_env_service import channel_is_locked

    info = get_entitlement(company_id) if company_id else get_entitlement(None)
    locks = info.get("cms_channel_locks") or {}
    ch = str(channel or "").strip().lower()
    if (ch == "whatsapp" and locks.get("whatsapp")) or (
        ch == "whatsapp" and channel_is_locked(company_id, "whatsapp")
    ):
        raise OutreachFrozenError(
            CMS_WHATSAPP_LOCKED_MESSAGE,
            company_id=company_id,
            cards_used=info.get("cards_used"),
            card_limit=info.get("card_limit"),
            channel="whatsapp",
        )
    if (ch == "email" and locks.get("email")) or (
        ch == "email" and channel_is_locked(company_id, "email")
    ):
        raise OutreachFrozenError(
            CMS_EMAIL_LOCKED_MESSAGE,
            company_id=company_id,
            cards_used=info.get("cards_used"),
            card_limit=info.get("card_limit"),
            channel="email",
        )

    if can_send_outreach(
        company_id,
        initial_save=initial_save,
        user=user,
        scans_unlimited=scans_unlimited,
    ):
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


def assert_can_process_card_locked(
    cur: Any,
    company_id: str | None,
    *,
    scans_unlimited: bool = False,
    user: dict[str, Any] | None = None,
    user_id: str | None = None,
    user_card_limit: int | None = None,
    user_cards_used: int = 0,
) -> dict[str, Any]:
    """Atomic check: SELECT … FOR UPDATE then compare cards_used vs card_limit."""
    if user_has_unlimited_scans(user) or scans_unlimited:
        if not company_id:
            return apply_user_scan_overlay(get_entitlement(None), scans_unlimited=True)
        row = _lock_company(cur, company_id)
        return apply_user_scan_overlay(_normalize(row, company_id), scans_unlimited=True)
    if user_id and not (user_has_unlimited_scans(user) or scans_unlimited):
        effective_limit = effective_user_card_limit(
            user,
            user_card_limit=user_card_limit,
            scans_unlimited=scans_unlimited,
        )
        return assert_can_process_user_card_locked(
            cur,
            user_id,
            effective_limit,
            user_cards_used,
            company_id=company_id,
        )
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


def assert_can_process_user_card_locked(
    cur: Any,
    user_id: str,
    user_card_limit: int,
    user_cards_used: int,
    *,
    company_id: str | None = None,
) -> dict[str, Any]:
    _apply_txn_timeouts(cur)
    cur.execute(
        """
        SELECT COALESCE(user_cards_used, 0) AS user_cards_used, user_card_limit
        FROM users
        WHERE id = %s
          AND deleted_at IS NULL
        FOR UPDATE
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        raise LookupError(f"User not found: {user_id}")
    used = max(0, int(row.get("user_cards_used") or user_cards_used))
    limit = int(row.get("user_card_limit") or user_card_limit)
    info = _normalize_user_card_entitlement(used, limit, company_id)
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
    scans_unlimited: bool = False,
    user: dict[str, Any] | None = None,
    user_card_limit: int | None = None,
) -> dict[str, Any]:
    """Increment cards_used under the current transaction. Caller must hold the row lock."""
    if user_has_unlimited_scans(user) or scans_unlimited:
        # Extra cards for this user must not consume the company Freemium quota.
        if not company_id:
            return apply_user_scan_overlay(get_entitlement(None), scans_unlimited=True)
        return {
            "company_id": company_id,
            "can_process_card": True,
            "scans_unlimited": True,
        }
    if user_id and not (user_has_unlimited_scans(user) or scans_unlimited):
        effective_limit = effective_user_card_limit(
            user,
            user_card_limit=user_card_limit,
            scans_unlimited=scans_unlimited,
        )
        return consume_user_card_locked(
            cur,
            user_id,
            company_id=company_id,
            contact_id=contact_id,
            plan_name=plan_name,
            user_card_limit=effective_limit,
        )
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


def consume_user_card_locked(
    cur: Any,
    user_id: str,
    *,
    company_id: str | None = None,
    contact_id: str | None = None,
    plan_name: str | None = None,
    user_card_limit: int = DEFAULT_FREEMIUM_CARD_LIMIT,
) -> dict[str, Any]:
    """Increment a user's personal card counter without touching company cards_used."""
    cap = max(1, int(user_card_limit))
    cur.execute(
        """
        UPDATE users
        SET user_cards_used = COALESCE(user_cards_used, 0) + 1,
            user_card_limit = COALESCE(user_card_limit, %s),
            updated_at = NOW()
        WHERE id = %s
          AND COALESCE(user_cards_used, 0) < COALESCE(user_card_limit, %s)
        RETURNING user_cards_used, user_card_limit
        """,
        (cap, user_id, cap),
    )
    row = cur.fetchone()
    if not row:
        cur.execute(
            """
            SELECT COALESCE(user_cards_used, 0) AS user_cards_used, user_card_limit
            FROM users
            WHERE id = %s
            """,
            (user_id,),
        )
        current = cur.fetchone() or {}
        raise CardLimitExceededError(
            company_id=company_id,
            cards_used=int(current.get("user_cards_used") or 0),
            card_limit=int(current.get("user_card_limit") or cap),
        )

    used = int(row["user_cards_used"])
    limit = int(row["user_card_limit"])
    info = _normalize_user_card_entitlement(used, limit, company_id)
    if company_id:
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
                plan_name or DEFAULT_PLAN_NAME,
            ),
        )
    logger.info(
        "[ENTITLEMENT] User card consumed user_id=%s used=%s limit=%s contact_id=%s",
        user_id,
        used,
        limit,
        contact_id,
    )
    return info


def can_send_outreach(
    company_id: str | None,
    *,
    initial_save: bool = False,
    user: dict[str, Any] | None = None,
    scans_unlimited: bool = False,
) -> bool:
    """WhatsApp/Email eligibility.

    initial_save=True allows the just-consumed final Freemium card to still
    send thank-you messages (remaining is already 0 after consume).
    Resend / later calls use initial_save=False and are blocked when exhausted.
    A per-user scans_unlimited exception keeps outreach available for that user.
    """
    if user_has_unlimited_scans(user) or scans_unlimited:
        return True
    if user_has_custom_card_limit(user):
        base = {"company_id": company_id} if company_id else get_entitlement(None)
        info = resolve_user_card_entitlement(user, base)
        if not info["card_quota_enforced"]:
            return True
        if info["cards_remaining"] and info["cards_remaining"] > 0:
            return True
        if initial_save and info["cards_used"] > 0 and info["card_limit"] is not None:
            return info["cards_used"] <= int(info["card_limit"])
        return False
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
    from services.admin_env_service import apply_channel_locks_to_entitlement

    info = apply_channel_locks_to_entitlement(get_entitlement(company_id), company_id)
    return {
        "card_limit": info["card_limit"],
        "cards_used": info["cards_used"],
        "cards_remaining": info["cards_remaining"],
        "freemium_exhausted": info["freemium_exhausted"],
        "card_quota_enforced": info["card_quota_enforced"],
        "can_process_card": info["can_process_card"],
        "whatsapp_allowed": info["whatsapp_allowed"],
        "email_allowed": info["email_allowed"],
        "google_sheets_allowed": info.get("google_sheets_allowed", True),
        "cms_channel_locks": info.get("cms_channel_locks")
        or {"whatsapp": False, "email": False, "google_sheets": False},
        "cms_whatsapp_locked": bool(info.get("cms_whatsapp_locked")),
        "cms_email_locked": bool(info.get("cms_email_locked")),
        "cms_google_sheets_locked": bool(info.get("cms_google_sheets_locked")),
        "contacts_allowed": info["contacts_allowed"],
        "entitlement_started_at": info["entitlement_started_at"],
        "entitlement_exhausted_at": info["entitlement_exhausted_at"],
        "scans_unlimited": False,
    }
