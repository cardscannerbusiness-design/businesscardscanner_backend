"""Ensure the Meta app is subscribed to WABA webhooks (required for inbound messages)."""
from __future__ import annotations

import logging
from typing import Any

import requests

from services.whatsapp_service import (
    ACCESS_TOKEN,
    GRAPH_API_VERSION,
    WABA_ID,
    _active_waba_id,
    _active_whatsapp_credentials,
)

logger = logging.getLogger(__name__)


def _graph_auth() -> tuple[str, str, str]:
    """Prefer CMS runtime credentials when active."""
    try:
        token, _phone_id, version = _active_whatsapp_credentials()
        waba = _active_waba_id()
        if token and waba:
            return token, waba, version or GRAPH_API_VERSION
    except Exception:
        pass
    return ACCESS_TOKEN, WABA_ID, GRAPH_API_VERSION


def get_waba_subscription_status() -> dict[str, Any]:
    token, waba, version = _graph_auth()
    if not token or not waba:
        return {"subscribed": False, "reason": "WhatsApp not configured."}

    url = f"https://graph.facebook.com/{version}/{waba}/subscribed_apps"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        response.raise_for_status()
        apps = response.json().get("data") or []
        app_ids = [
            str((item.get("whatsapp_business_api_data") or {}).get("id") or item.get("id") or "")
            for item in apps
        ]
        app_ids = [app_id for app_id in app_ids if app_id]
        return {
            "subscribed": bool(app_ids),
            "app_count": len(app_ids),
            "app_ids": app_ids,
        }
    except requests.RequestException as exc:
        logger.warning("Could not read WABA subscribed_apps: %s", exc)
        return {"subscribed": False, "reason": str(exc)}


def ensure_waba_webhook_subscription() -> dict[str, Any]:
    """
    Meta requires POST /{waba-id}/subscribed_apps even when the callback URL is set
    in the app dashboard. Without this, inbound message webhooks are never delivered.
    """
    status = get_waba_subscription_status()
    if status.get("subscribed"):
        logger.info("WhatsApp WABA already subscribed to app webhooks (%s).", status.get("app_ids"))
        return {**status, "action": "already_subscribed"}

    token, waba, version = _graph_auth()
    if not token or not waba:
        logger.warning("Skipping WABA webhook subscribe — WhatsApp not configured.")
        return {"subscribed": False, "action": "skipped", "reason": "not configured"}

    url = f"https://graph.facebook.com/{version}/{waba}/subscribed_apps"
    try:
        response = requests.post(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        response.raise_for_status()
        body = response.json()
        if body.get("success"):
            refreshed = get_waba_subscription_status()
            logger.info("Subscribed app to WhatsApp WABA webhooks: %s", refreshed)
            return {**refreshed, "action": "subscribed"}
        return {"subscribed": False, "action": "failed", "response": body}
    except requests.RequestException as exc:
        logger.error("Failed to subscribe app to WABA webhooks: %s", exc)
        return {"subscribed": False, "action": "failed", "reason": str(exc)}
