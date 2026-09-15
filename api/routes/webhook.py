"""Meta WhatsApp Cloud API webhook — verification (GET) and event delivery (POST)."""
import hashlib
import hmac
import json
import logging
import os

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import PlainTextResponse

from services.whatsapp_delivery_store import record_message_status
from services.whatsapp_inbound import handle_inbound_whatsapp_message

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhooks"])


def _normalize_env(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


VERIFY_TOKEN = _normalize_env(os.getenv("WHATSAPP_VERIFY_TOKEN")) or "cardsync_whatsapp_verify_token"
APP_SECRET = _normalize_env(os.getenv("WHATSAPP_APP_SECRET"))


def get_webhook_verify_token() -> str:
    return VERIFY_TOKEN


def _verify_meta_signature(raw_body: bytes, signature_header: str) -> bool:
    if not APP_SECRET:
        return True
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header[7:], expected)


def _log_whatsapp_events(payload: dict) -> None:
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for status in value.get("statuses") or []:
                mid = str(status.get("id") or "").strip()
                state = str(status.get("status") or "").strip().lower()
                errors = status.get("errors") or []
                if mid and state:
                    record_message_status(
                        mid,
                        state,
                        recipient_id=str(status.get("recipient_id") or "") or None,
                        errors=errors if isinstance(errors, list) else [errors],
                        timestamp=str(status.get("timestamp") or "") or None,
                    )
                if state == "failed":
                    logger.error(
                        "WhatsApp delivery failed id=%s recipient=%s errors=%s",
                        mid or status.get("id"),
                        status.get("recipient_id"),
                        errors,
                    )
                elif state in {"sent", "delivered", "read"}:
                    logger.info(
                        "WhatsApp delivery status=%s id=%s recipient=%s",
                        state,
                        mid or status.get("id"),
                        status.get("recipient_id"),
                    )


async def _process_inbound_messages(payload: dict) -> None:
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") != "messages":
                continue
            for sender, message in _iter_inbound_messages(value):
                await handle_inbound_whatsapp_message(sender, message)


def _iter_inbound_messages(value: dict):
    for message in value.get("messages") or []:
        sender = message.get("from")
        if sender:
            yield str(sender), message


@router.get(
    "/webhook",
    summary="Meta webhook verification",
    description=(
        "Meta calls this when you click **Verify and save** in the WhatsApp app settings. "
        "Returns `hub.challenge` when `hub.verify_token` matches `WHATSAPP_VERIFY_TOKEN` in `.env`."
    ),
    response_class=PlainTextResponse,
)
async def verify_webhook(
    hub_mode: str | None = Query(
        None,
        alias="hub.mode",
        description='Must be `"subscribe"` during Meta verification.',
    ),
    hub_verify_token: str | None = Query(
        None,
        alias="hub.verify_token",
        description="Must match `WHATSAPP_VERIFY_TOKEN` (default: cardsync_whatsapp_verify_token).",
    ),
    hub_challenge: str | None = Query(
        None,
        alias="hub.challenge",
        description="Echoed back as plain text when verification succeeds.",
    ),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN and hub_challenge:
        logger.info("WhatsApp webhook verified (challenge accepted).")
        return PlainTextResponse(content=hub_challenge)

    logger.warning(
        "WhatsApp webhook verification failed (mode=%s, token_match=%s).",
        hub_mode,
        hub_verify_token == VERIFY_TOKEN,
    )
    return PlainTextResponse(content="Verification failed", status_code=403)


@router.post(
    "/webhook",
    summary="Meta webhook events",
    description=(
        "Receives WhatsApp message and delivery-status events from Meta. "
        "Validates `X-Hub-Signature-256` when `WHATSAPP_APP_SECRET` is set. "
        "Always returns HTTP 200 on success so Meta does not retry."
    ),
)
async def receive_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(
        None,
        description="HMAC SHA-256 signature from Meta (required when WHATSAPP_APP_SECRET is set).",
    ),
):
    raw_body = await request.body()
    signature = x_hub_signature_256 or ""

    if APP_SECRET and not _verify_meta_signature(raw_body, signature):
        logger.error(
            "WhatsApp webhook rejected: invalid X-Hub-Signature-256. "
            "Set WHATSAPP_APP_SECRET to the App Secret from "
            "Meta Developers → your app → Settings → Basic (no quotes)."
        )
        return PlainTextResponse(content="Invalid signature", status_code=403)

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return PlainTextResponse(content="Invalid JSON", status_code=400)

    if payload.get("object") == "whatsapp_business_account":
        _log_whatsapp_events(payload)
        # Process before returning 200 so verify state updates reliably (no lost background tasks).
        await _process_inbound_messages(payload)

    return Response(status_code=200)
