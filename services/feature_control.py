"""Centralized CMS feature-control checks for customer APIs.

Plan quota (cards remaining) is entitlement_service. Feature locks are CMS
owner block/unblock flags on the company. Callers must check both.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from services.cms_app_access import (
    ACTION_KEYS,
    FEATURE_KEYS,
    effective_channel_locks,
    normalize_action_locks,
)

FEATURE_BLOCKED_MESSAGE = (
    "This feature is currently unavailable. Please contact your administrator."
)


class FeatureBlockedError(Exception):
    code = "FEATURE_BLOCKED"

    def __init__(self, feature: str, action: str | None = None):
        self.feature = str(feature or "").strip()
        self.action = str(action or "").strip() or None
        self.message = FEATURE_BLOCKED_MESSAGE
        super().__init__(self.message)

    def to_response(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "success": False,
            "error": self.code,
            "code": self.code,
            "message": self.message,
            "feature": self.feature,
        }
        if self.action:
            body["action"] = self.action
        return body


def _company_id_for(user: dict[str, Any] | None, company_id: str | None = None) -> str | None:
    if company_id:
        return str(company_id)
    if not user:
        return None
    from services.storage_service import resolve_company_id_for_user

    resolved = resolve_company_id_for_user(user)
    return str(resolved) if resolved else None


def load_company_feature_control(company_id: str | None) -> dict[str, Any]:
    """OR company JSONB locks with mirrored admin_env_settings locks."""
    if not company_id:
        return {
            "locks": {key: False for key in FEATURE_KEYS},
            "actions": {key: False for key in ACTION_KEYS},
        }
    from db.pool import db_cursor
    from services.admin_env_service import get_channel_locks_for_company

    raw = None
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT cms_channel_locks FROM companies WHERE id = %s", (company_id,))
            row = cur.fetchone() or {}
        raw = row.get("cms_channel_locks") if isinstance(row, dict) else None
    except Exception:
        raw = None
    company_locks = effective_channel_locks(raw, False)
    company_actions = normalize_action_locks(raw)
    settings_locks = get_channel_locks_for_company(company_id)
    settings_actions = normalize_action_locks(settings_locks)
    locks = {
        key: bool(company_locks.get(key) or settings_locks.get(key))
        for key in FEATURE_KEYS
    }
    actions = {
        key: bool(company_actions.get(key) or settings_actions.get(key) or settings_locks.get(key))
        for key in ACTION_KEYS
    }
    if locks.get("email"):
        actions["email.send"] = True
    if locks.get("whatsapp"):
        actions["whatsapp.send"] = True
    return {"locks": locks, "actions": actions}


def is_feature_blocked(
    company_id: str | None,
    feature: str,
    action: str | None = None,
) -> bool:
    control = load_company_feature_control(company_id)
    feature_key = str(feature or "").strip()
    if feature_key and control["locks"].get(feature_key):
        return True
    if action:
        action_key = action if "." in action else f"{feature_key}.{action}"
        return bool(control["actions"].get(action_key))
    return False


def assert_feature_enabled(
    user: dict[str, Any] | None,
    feature: str,
    action: str | None = None,
    *,
    company_id: str | None = None,
) -> None:
    if user and str(user.get("role") or "") == "SUPER_ADMIN":
        return
    cid = _company_id_for(user, company_id)
    if is_feature_blocked(cid, feature, action):
        raise FeatureBlockedError(feature, action)


def require_feature(
    user: dict[str, Any] | None,
    feature: str,
    action: str | None = None,
    *,
    company_id: str | None = None,
) -> None:
    try:
        assert_feature_enabled(user, feature, action, company_id=company_id)
    except FeatureBlockedError as exc:
        raise HTTPException(status_code=403, detail=exc.to_response()) from exc
