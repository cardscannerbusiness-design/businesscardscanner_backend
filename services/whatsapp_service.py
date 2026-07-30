import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_RECENT_SENDS: dict[str, float] = {}
_SEND_DEDUPE_SECONDS = 120


_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_CARD_RECEIVED_TEMPLATE_PATH = _BACKEND_ROOT / "scripts" / "whatsapp_card_received_template.json"


def _load_template_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Could not load WhatsApp template JSON from %s: %s", path, exc)
        return None


_CARD_RECEIVED_TEMPLATE_DEF = _load_template_json(_CARD_RECEIVED_TEMPLATE_PATH) or {}


def _auto_send_enabled() -> bool:
    return _normalize_env(os.getenv("WHATSAPP_AUTO_SEND_ON_SCAN")).lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _normalize_env(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


ACCESS_TOKEN = _normalize_env(os.getenv("WHATSAPP_ACCESS_TOKEN"))
PHONE_NUMBER_ID = _normalize_env(os.getenv("WHATSAPP_PHONE_NUMBER_ID"))
GRAPH_API_VERSION = _normalize_env(os.getenv("WHATSAPP_GRAPH_API_VERSION")) or "v25.0"
TEMPLATE_NAME = _normalize_env(os.getenv("WHATSAPP_TEMPLATE_NAME")) or "hello_world"
TEMPLATE_LANGUAGE_CODE = _normalize_env(os.getenv("WHATSAPP_TEMPLATE_LANGUAGE_CODE")) or "en_US"
SCAN_THANKS_TEMPLATE_NAME = (
    _normalize_env(os.getenv("WHATSAPP_SCAN_TEMPLATE_NAME")) or "cardsync_scan_thanks"
)
_CARD_RECEIVED_TEMPLATE_NAME_FROM_JSON = _CARD_RECEIVED_TEMPLATE_DEF.get("name")
BUSINESS_CARD_TEMPLATE_NAME = (
    _normalize_env(os.getenv("WHATSAPP_BUSINESS_CARD_TEMPLATE_NAME"))
    or _CARD_RECEIVED_TEMPLATE_NAME_FROM_JSON
    or "cardsync_card_received"
)
CARD_RECEIVED_TEMPLATE_NAME = (
    _normalize_env(os.getenv("WHATSAPP_CARD_RECEIVED_TEMPLATE_NAME")) or BUSINESS_CARD_TEMPLATE_NAME
)
WABA_ID = _normalize_env(os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID"))

_TEMPLATE_LANG_CACHE: dict[str, str] = {}


def is_whatsapp_configured() -> bool:
    return bool(ACCESS_TOKEN and PHONE_NUMBER_ID)


_BUSINESS_PHONE_CACHE: dict[str, Any] = {"fetched_at": 0.0, "data": None}


def get_whatsapp_config_summary() -> dict[str, Any]:
    link = get_whatsapp_chat_link_config()
    return {
        "phone_number_id": PHONE_NUMBER_ID or None,
        "card_received_template": CARD_RECEIVED_TEMPLATE_NAME,
        "scan_template": SCAN_THANKS_TEMPLATE_NAME,
        "language_code_env": TEMPLATE_LANGUAGE_CODE,
        "language_code_resolved": resolve_template_language(CARD_RECEIVED_TEMPLATE_NAME),
        "business_phone": link.get("business_phone"),
        "verified_name": link.get("verified_name"),
    }


def _digits_only(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _fetch_business_phone_from_graph() -> dict[str, Any] | None:
    if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
        return None
    try:
        url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            params={
                "fields": "display_phone_number,verified_name,status,name_status,quality_rating",
            },
            timeout=20,
        )
        if response.status_code >= 400:
            logger.warning("Could not fetch WhatsApp phone metadata: %s", response.text)
            return None
        data = response.json()
        display = _digits_only(str(data.get("display_phone_number") or ""))
        if not display:
            return None
        return {
            "business_phone": display,
            "display_phone_number": data.get("display_phone_number"),
            "verified_name": data.get("verified_name"),
            "status": data.get("status"),
            "name_status": data.get("name_status"),
            "quality_rating": data.get("quality_rating"),
        }
    except Exception as exc:
        logger.warning("WhatsApp business phone lookup failed: %s", exc)
        return None


def get_whatsapp_chat_link_config(
    prefill_text: str = "Hi, verify my number",
) -> dict[str, Any]:
    """Config for wa.me user-initiated chat (opens in recipient Chats, not Updates)."""
    env_phone = _digits_only(_normalize_env(os.getenv("WHATSAPP_BUSINESS_PHONE")))
    now = time.time()
    cached = _BUSINESS_PHONE_CACHE.get("data")
    if cached and now - float(_BUSINESS_PHONE_CACHE.get("fetched_at") or 0) < 3600:
        meta = cached
    else:
        meta = _fetch_business_phone_from_graph()
        _BUSINESS_PHONE_CACHE["data"] = meta
        _BUSINESS_PHONE_CACHE["fetched_at"] = now

    business_phone = env_phone or (meta or {}).get("business_phone") or ""
    prefill = (prefill_text or "Hi, verify my number").strip() or "Hi, verify my number"
    wa_me_url = ""
    if business_phone:
        from urllib.parse import quote

        wa_me_url = f"https://wa.me/{business_phone}?text={quote(prefill)}"

    return {
        "business_phone": business_phone,
        "display_phone_number": (meta or {}).get("display_phone_number"),
        "verified_name": (meta or {}).get("verified_name") or "BusinessCardScanner",
        "prefill_text": prefill,
        "wa_me_url": wa_me_url,
        "configured": bool(business_phone and is_whatsapp_configured()),
    }


def resolve_template_language(template_name: str) -> str:
    """Use the language Meta approved for this template (e.g. en vs en_US)."""
    name = (template_name or "").strip()
    if not name:
        return TEMPLATE_LANGUAGE_CODE
    if name in _TEMPLATE_LANG_CACHE:
        return _TEMPLATE_LANG_CACHE[name]
    json_name = _CARD_RECEIVED_TEMPLATE_DEF.get("name")
    json_lang = _CARD_RECEIVED_TEMPLATE_DEF.get("language")
    if json_name and json_lang and name == json_name:
        return str(json_lang)
    if not ACCESS_TOKEN or not WABA_ID:
        return TEMPLATE_LANGUAGE_CODE
    try:
        url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WABA_ID}/message_templates"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            params={"name": name, "limit": 1},
            timeout=20,
        )
        data = response.json().get("data") or []
        if data and data[0].get("language"):
            lang = str(data[0]["language"])
            _TEMPLATE_LANG_CACHE[name] = lang
            return lang
    except Exception as exc:
        logger.warning("Could not resolve language for template %s: %s", name, exc)
    return TEMPLATE_LANGUAGE_CODE


def normalize_whatsapp_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        raise ValueError("Phone number is required.")
    if len(digits) == 10 and digits[0] in "6789":
        return f"91{digits}"
    return digits


def _meta_from_graph_message_response(response_json: dict[str, Any]) -> dict[str, Any]:
    """Meta includes wa_id in contacts[] when the recipient is on WhatsApp."""
    contacts = response_json.get("contacts") or []
    wa_id = str((contacts[0] or {}).get("wa_id") or "").strip() if contacts else ""
    return {
        "wa_id": wa_id or None,
        "recipient_has_whatsapp": bool(wa_id),
    }


def is_whatsapp_not_registered_error(error_message: str) -> bool:
    text = (error_message or "").lower()
    return "131026" in text or "not registered on whatsapp" in text


def evaluate_recipient_before_send(phone: str) -> dict[str, Any]:
    """
    Pre-send checks. recipient_has_whatsapp defaults to False until Meta returns wa_id.
    contact_chat_verified = contact messaged our business first (separate from has WhatsApp).
    """
    from services.whatsapp_inbound import is_phone_whatsapp_verified

    result: dict[str, Any] = {
        "recipient_has_whatsapp": False,
        "contact_chat_verified": False,
        "normalized_phone": "",
        "can_send": False,
        "error": None,
    }
    try:
        normalized = normalize_whatsapp_phone(phone)
    except ValueError as exc:
        result["error"] = str(exc)
        return result

    digits = re.sub(r"\D", "", normalized)
    result["normalized_phone"] = normalized
    result["contact_chat_verified"] = is_phone_whatsapp_verified(phone)
    if len(digits) < 10 or len(digits) > 15:
        result["error"] = f"Invalid phone length for WhatsApp ({len(digits)} digits)."
        return result

    result["can_send"] = True
    return result


def _base_whatsapp_send_result() -> dict[str, Any]:
    return {
        "attempted": False,
        "sent": False,
        "queued": False,
        "error": None,
        "message_id": None,
        "recipient_phone": None,
        "recipient_name": None,
        "message": None,
        "send_mode": None,
        "recipient_has_whatsapp": False,
        "contact_chat_verified": False,
        "wa_id": None,
    }


def _format_whatsapp_error(error_json: Any) -> str:
    if not error_json:
        return "Unknown WhatsApp API error."

    if isinstance(error_json, dict):
        error = error_json.get("error") or error_json
        code = error.get("code")
        message = error.get("message")
        subcode = error.get("error_subcode")

        if code == 190:
            if subcode == 463:
                return (
                    "WhatsApp OAuth error 190/463: the access token session has expired. "
                    "Generate a fresh permanent System User token in Meta Business Manager."
                )
            return (
                "WhatsApp OAuth error 190: invalid or expired access token. "
                "Generate a fresh WHATSAPP_ACCESS_TOKEN from Meta Business Manager."
            )

        if message:
            details = f"{message}"
            if code == 131026:
                return (
                    "Recipient phone number is not registered on WhatsApp (code=131026). "
                    "Meta will not deliver messages to this number."
                )
            if code == 131030:
                return (
                    "Recipient phone number is not in Meta's WhatsApp test list. "
                    "In development mode Meta only delivers to pre-approved test numbers. "
                    "Move your WhatsApp app to Live mode in Meta Business Manager to send "
                    "dynamically to any scanned card holder number."
                )
            if code:
                details = f"{details} (code={code}"
                if subcode:
                    details += f", subcode={subcode}"
                details += ")"
            return details

    return "Unknown WhatsApp API error."


def _post_message(payload: dict[str, Any]) -> dict[str, Any]:
    if not is_whatsapp_configured():
        raise RuntimeError(
            "Missing WhatsApp config. Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID in .env."
        )

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, json=payload, timeout=30)

    try:
        response_json = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"WhatsApp send failed: invalid JSON response (status {response.status_code}). "
            f"Response body: {response.text}"
        ) from exc

    if response.status_code >= 400:
        raise RuntimeError("WhatsApp send failed: " + _format_whatsapp_error(response_json))

    graph_meta = _meta_from_graph_message_response(response_json)
    response_json["_graph_meta"] = graph_meta
    return response_json


def send_whatsapp_text(phone: str, message: str) -> dict[str, Any]:
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_whatsapp_phone(phone),
        "type": "text",
        "text": {"body": message},
    }
    return _post_message(payload)


def send_whatsapp_template(
    phone: str,
    template_name: str | None = None,
    language_code: str | None = None,
    components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_name = template_name or TEMPLATE_NAME
    resolved_lang = language_code or resolve_template_language(resolved_name)
    template: dict[str, Any] = {
        "name": resolved_name,
        "language": {"code": resolved_lang},
    }
    if components:
        template["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_whatsapp_phone(phone),
        "type": "template",
        "template": template,
    }
    return _post_message(payload)


def send_business_card_template(
    phone: str,
    contact_name: str,
    company: str = "",
    title: str = "",
) -> dict[str, Any]:
    contact = {
        "fullName": contact_name,
        "company": company,
        "designation": title,
    }
    return send_whatsapp_template(
        phone,
        template_name=BUSINESS_CARD_TEMPLATE_NAME,
        language_code=TEMPLATE_LANGUAGE_CODE,
        components=build_contact_saved_template_components(contact),
    )


def extract_contact_name(contact: dict[str, Any]) -> str:
    for key in ("fullName", "name"):
        value = str(contact.get(key) or "").strip()
        if value:
            return value

    first = str(contact.get("firstName") or "").strip()
    last = str(contact.get("lastName") or "").strip()
    combined = f"{first} {last}".strip()
    if combined:
        return combined
    return ""


def extract_company_name(contact: dict[str, Any]) -> str:
    for key in ("company", "companyName"):
        value = str(contact.get(key) or "").strip()
        if value:
            return value
    return ""


def extract_designation(contact: dict[str, Any]) -> str:
    for key in ("designation", "title", "jobTitle", "role"):
        value = str(contact.get(key) or "").strip()
        if value:
            return value
    return ""


def extract_primary_email(contact: dict[str, Any]) -> str:
    for key in ("email", "emailAddress", "primaryEmail"):
        value = str(contact.get(key) or "").strip()
        if value:
            return value
    for key in ("emails",):
        values = contact.get(key) or []
        if isinstance(values, list):
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
    return ""


def extract_website(contact: dict[str, Any]) -> str:
    for key in ("website", "secondaryWebsite", "url", "websiteUrl"):
        value = str(contact.get(key) or "").strip()
        if value:
            return value
    for key in ("websites",):
        values = contact.get(key) or []
        if isinstance(values, list):
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
    return ""


def extract_event_name(contact: dict[str, Any]) -> str:
    for key in ("eventName", "event", "event_name", "meetingAt", "meeting_at"):
        value = contact.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return _normalize_env(os.getenv("WHATSAPP_TEMPLATE_EVENT_NAME")) or "the exhibition"


def extract_address(contact: dict[str, Any]) -> str:
    for key in ("address", "secondaryAddress", "street", "location"):
        value = str(contact.get(key) or "").strip()
        if value:
            return value
    for key in ("addresses",):
        values = contact.get(key) or []
        if isinstance(values, list):
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
    return ""


def _template_param(text: str, fallback: str = "—", max_len: int = 200) -> str:
    # WhatsApp template parameters must not contain newlines or tabs.
    cleaned = re.sub(r"\s*[\r\n\t]+\s*", ", ", str(text or "")).strip()
    if not cleaned:
        return fallback
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1] + "…"
    return cleaned


def build_scan_thank_you_text(
    contact_name: str,
    company: str = "",
    designation: str = "",
) -> str:
    first_name = (contact_name or "there").strip().split()[0]
    company_bit = f" from {company}" if company else ""
    title_bit = f" ({designation})" if designation else ""
    return (
        f"Hi {first_name}, thank you for sharing your business card{company_bit}{title_bit}. "
        "We have saved your details in NameCardScan."
    )


def _extract_variable_positions(template_def: dict[str, Any]) -> list[int]:
    """Return sorted {{N}} placeholder indices from a template definition."""
    positions: set[int] = set()
    for comp in template_def.get("components", []):
        if comp.get("type", "").upper() != "BODY":
            continue
        text = comp.get("text", "")
        for match in re.finditer(r"\{\{\s*(\d+)\s*\}\}", text):
            positions.add(int(match.group(1)))
    return sorted(positions)


def _resolve_public_asset_url(url: str | None) -> str | None:
    """Expand __BACKEND_BASE_URL__ placeholders using BACKEND_BASE_URL from env."""
    if not url:
        return None
    raw = str(url).strip()
    if "__BACKEND_BASE_URL__" not in raw:
        return raw
    from config.urls import get_backend_base_url

    return raw.replace("__BACKEND_BASE_URL__", get_backend_base_url())


def _is_publicly_reachable_url(url: str) -> bool:
    """Meta must be able to download header media — local/LAN hosts can't work."""
    lowered = url.lower()
    return not any(
        host in lowered
        for host in ("localhost", "127.0.0.1", "0.0.0.0", "://192.168.", "://10.")
    )


def _local_path_for_static_url(url: str) -> Path | None:
    """Map a /static/... URL back to the file inside the backend static folder."""
    marker = "/static/"
    idx = url.find(marker)
    if idx == -1:
        return None
    rel = url[idx + len(marker):].split("?")[0]
    static_root = (_BACKEND_ROOT / "static").resolve()
    path = (static_root / rel).resolve()
    if not str(path).startswith(str(static_root)):
        return None
    return path if path.is_file() else None


_MEDIA_CACHE_PATH = _BACKEND_ROOT / "data" / "whatsapp_media_cache.json"
# Meta media ids are valid for 30 days; refresh a little earlier.
_MEDIA_ID_TTL_SECONDS = 25 * 24 * 3600


def _load_media_cache() -> dict[str, Any]:
    try:
        with _MEDIA_CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_media_cache(cache: dict[str, Any]) -> None:
    try:
        _MEDIA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _MEDIA_CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as exc:
        logger.warning("Could not persist WhatsApp media cache: %s", exc)


def _mime_for_media_file(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".3gp": "video/3gpp",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")


def _get_or_upload_header_media(file_path: Path) -> str | None:
    """Upload a local media file to Meta once and reuse the returned media id."""
    cache = _load_media_cache()
    entry = cache.get(file_path.name) or {}
    media_id = str(entry.get("media_id") or "")
    uploaded_at = float(entry.get("uploaded_at") or 0)
    if media_id and (time.time() - uploaded_at) < _MEDIA_ID_TTL_SECONDS:
        return media_id

    if not is_whatsapp_configured():
        return None
    mime = _mime_for_media_file(file_path)
    try:
        url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/media"
        with file_path.open("rb") as f:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (file_path.name, f, mime)},
                timeout=120,
            )
        data = response.json()
        if response.status_code >= 400:
            logger.warning("WhatsApp media upload failed for %s: %s", file_path.name, data)
            return None
        media_id = str(data.get("id") or "")
        if media_id:
            cache[file_path.name] = {"media_id": media_id, "uploaded_at": time.time()}
            _save_media_cache(cache)
            logger.info(
                "[WhatsApp] Uploaded header media %s as media id %s",
                file_path.name,
                media_id,
            )
        return media_id or None
    except Exception as exc:
        logger.warning("WhatsApp media upload error for %s: %s", file_path, exc)
        return None


def _header_media_component(
    media_type: str,
    *,
    media_id: str | None = None,
    link: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if media_id:
        payload["id"] = media_id
    if link:
        payload["link"] = link
    if filename and media_type == "document":
        payload["filename"] = filename
    return {
        "type": "header",
        "parameters": [{"type": media_type, media_type: payload}],
    }


def _build_header_component(template_def: dict[str, Any]) -> dict[str, Any] | None:
    """Build a Meta template header component from the JSON definition."""
    for comp in template_def.get("components", []):
        if comp.get("type", "").upper() != "HEADER":
            continue
        fmt = (comp.get("format") or "").upper()
        if fmt == "DOCUMENT":
            doc_url = _resolve_public_asset_url(
                template_def.get("header_document_url") or comp.get("document_url")
            )
            if not doc_url:
                return None
            filename = str(doc_url).split("/")[-1] or "document.pdf"

            if not _is_publicly_reachable_url(doc_url):
                # Meta cannot download from localhost — upload the file and send by media id.
                local_path = _local_path_for_static_url(doc_url)
                media_id = _get_or_upload_header_media(local_path) if local_path else None
                if media_id:
                    return _header_media_component(
                        "document", media_id=media_id, filename=filename
                    )
                logger.warning(
                    "WhatsApp header document %s is not publicly reachable and media "
                    "upload failed — sending template without guaranteed delivery.",
                    doc_url,
                )

            return _header_media_component("document", link=doc_url, filename=filename)
        if fmt == "VIDEO":
            video_url = _resolve_public_asset_url(
                template_def.get("header_video_url")
                or comp.get("video_url")
                or template_def.get("header_handle")
                or (comp.get("example") or {}).get("header_handle", [None])[0]
            )
            if not video_url:
                return None

            if not _is_publicly_reachable_url(video_url):
                local_path = _local_path_for_static_url(video_url)
                media_id = _get_or_upload_header_media(local_path) if local_path else None
                if media_id:
                    return _header_media_component("video", media_id=media_id)
                logger.warning(
                    "WhatsApp header video %s is not publicly reachable and media "
                    "upload failed — sending template without video header.",
                    video_url,
                )
                return None

            # Prefer uploading a local copy when available; otherwise pass the public link.
            local_path = _local_path_for_static_url(video_url)
            media_id = _get_or_upload_header_media(local_path) if local_path else None
            if media_id:
                return _header_media_component("video", media_id=media_id)
            return _header_media_component("video", link=video_url)
        if fmt == "TEXT":
            text = comp.get("text")
            if text is None:
                return None
            return {
                "type": "header",
                "parameters": [{"type": "text", "text": str(text)}],
            }
    return None


def build_card_received_template_components(contact: dict[str, Any]) -> list[dict[str, Any]]:
    """Map scanned card fields to the approved card-received template variables."""
    contact_name = extract_contact_name(contact)
    first_name = (contact_name or "there").strip().split()[0]
    event_name = extract_event_name(contact)

    positions = _extract_variable_positions(_CARD_RECEIVED_TEMPLATE_DEF) or [1]
    if len(positions) == 1:
        # Single-variable templates expect the full name.
        field_by_position: dict[int, str] = {
            1: _template_param(contact_name, "there"),
        }
    elif positions == [1, 2]:
        # card_final_ula: Hi {{1}} … meeting you at {{2}}.
        field_by_position = {
            1: _template_param(first_name, "there"),
            2: _template_param(event_name, "the exhibition"),
        }
    else:
        field_by_position = {
            1: _template_param(first_name, "there"),
            2: _template_param(contact_name),
            3: _template_param(extract_company_name(contact)),
            4: _template_param(extract_designation(contact)),
            5: _template_param(extract_primary_email(contact)),
            6: _template_param(extract_website(contact)),
            7: _template_param(extract_address(contact)),
        }

    parameters = [
        {"type": "text", "text": field_by_position.get(pos, "—")}
        for pos in positions
    ]
    components: list[dict[str, Any]] = []
    header = _build_header_component(_CARD_RECEIVED_TEMPLATE_DEF)
    if header:
        components.append(header)
    components.append({"type": "body", "parameters": parameters})
    return components


def build_contact_saved_template_components(contact: dict[str, Any]) -> list[dict[str, Any]]:
    """Map scanned card fields to cardsync_contact_saved (1 variable in Meta)."""
    contact_name = extract_contact_name(contact)
    first_name = (contact_name or "there").strip().split()[0]
    return [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": _template_param(first_name, "there")}],
        }
    ]


def build_scan_thanks_template_components(contact: dict[str, Any]) -> list[dict[str, Any]]:
    """Map scanned card fields to single-variable scan thank-you templates."""
    contact_name = extract_contact_name(contact)
    first_name = (contact_name or "there").strip().split()[0]
    return [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": _template_param(first_name, "there")}],
        }
    ]


def _requires_template_message(error: Exception) -> bool:
    error_text = str(error).lower()
    return any(
        token in error_text
        for token in ("template", "24 hour", "24-hour", "session", "re-engagement")
    )


def _template_components(template_name: str, contact: dict[str, Any]) -> list[dict[str, Any]]:
    """Pick body parameters that match the approved template."""
    name = (template_name or "").strip()
    if name in {TEMPLATE_NAME, "hello_world"}:
        return []
    if name == "cardsync_contact_saved":
        return build_contact_saved_template_components(contact)
    if name in {
        CARD_RECEIVED_TEMPLATE_NAME,
        SCAN_THANKS_TEMPLATE_NAME,
        BUSINESS_CARD_TEMPLATE_NAME,
        "cardsync_card_received",
        _CARD_RECEIVED_TEMPLATE_NAME_FROM_JSON,
    }:
        return build_card_received_template_components(contact)
    if name == "3p_direct_integration_test_template":
        contact_name = extract_contact_name(contact)
        first_name = (contact_name or "there").strip().split()[0]
        return [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": _template_param(first_name, "there")}],
            }
        ]
    return build_scan_thanks_template_components(contact)


def _ordered_outbound_template_names() -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in (
        CARD_RECEIVED_TEMPLATE_NAME,
        SCAN_THANKS_TEMPLATE_NAME,
        BUSINESS_CARD_TEMPLATE_NAME,
        "cardsync_contact_saved",
        TEMPLATE_NAME,
    ):
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def send_scan_thank_you_to_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """Send a personalized thank-you to the card holder's extracted phone number."""
    phone = extract_primary_phone(contact)
    if not phone:
        raise ValueError("No primary phone number found on the contact.")

    contact_name = extract_contact_name(contact)
    company = extract_company_name(contact)
    designation = extract_designation(contact)
    message = build_scan_thank_you_text(contact_name, company, designation)
    normalized_phone = normalize_whatsapp_phone(phone)

    # Business API outbound: approved templates with card fields first.
    # Free-text only works inside an existing 24-hour customer session.
    result = None
    send_mode = ""
    last_error: Exception | None = None

    components_by_template = _template_components(CARD_RECEIVED_TEMPLATE_NAME, contact)
    try:
        result = send_whatsapp_template(
            phone,
            template_name=CARD_RECEIVED_TEMPLATE_NAME,
            components=components_by_template,
        )
        send_mode = CARD_RECEIVED_TEMPLATE_NAME
    except RuntimeError as primary_exc:
        last_error = primary_exc
        logger.warning(
            "WhatsApp primary template %s failed for %s: %s",
            CARD_RECEIVED_TEMPLATE_NAME,
            normalized_phone,
            primary_exc,
        )
        for template_name in _ordered_outbound_template_names():
            if template_name == CARD_RECEIVED_TEMPLATE_NAME:
                continue
            try:
                result = send_whatsapp_template(
                    phone,
                    template_name=template_name,
                    components=_template_components(template_name, contact),
                )
                send_mode = template_name
                logger.info(
                    "WhatsApp fallback template %s sent to %s (primary %s failed).",
                    template_name,
                    normalized_phone,
                    CARD_RECEIVED_TEMPLATE_NAME,
                )
                break
            except RuntimeError as exc:
                last_error = exc
                logger.info(
                    "WhatsApp fallback template %s failed for %s: %s",
                    template_name,
                    normalized_phone,
                    exc,
                )

    if result is None:
        raise last_error or RuntimeError("WhatsApp send failed for all configured templates.")

    graph_meta = result.get("_graph_meta") or _meta_from_graph_message_response(result)
    return {
        "result": result,
        "phone": phone,
        "normalized_phone": normalized_phone,
        "recipient_name": contact_name or "there",
        "message": message,
        "send_mode": send_mode,
        "recipient_has_whatsapp": bool(graph_meta.get("recipient_has_whatsapp", False)),
        "wa_id": graph_meta.get("wa_id"),
    }


def extract_primary_phone(contact: dict[str, Any]) -> str:
    phone = ""
    for key in ("phone", "phoneNumber", "primaryPhone"):
        value = str(contact.get(key) or "").strip()
        if value:
            phone = value
            break

    if not phone:
        for key in ("phones", "mobileNumbers", "telephoneNumbers"):
            values = contact.get(key) or []
            if isinstance(values, list):
                for value in values:
                    text = str(value or "").strip()
                    if text:
                        phone = text
                        break
            if phone:
                break

    if not phone:
        return ""

    country = str(contact.get("countryCode") or "").strip()
    if not country:
        return phone

    local_digits = re.sub(r"\D", "", phone)
    cc_digits = re.sub(r"\D", "", country)
    if not local_digits:
        return ""
    if cc_digits and local_digits.startswith(cc_digits):
        return f"+{local_digits}"
    if cc_digits:
        return f"+{cc_digits}{local_digits}"
    return phone


def _should_send_to_phone(phone: str) -> bool:
    try:
        normalized = normalize_whatsapp_phone(phone)
    except ValueError:
        return False

    now = time.time()
    last_sent = _RECENT_SENDS.get(normalized, 0)
    if now - last_sent < _SEND_DEDUPE_SECONDS:
        logger.info("Skipping duplicate WhatsApp send to %s within dedupe window.", normalized)
        return False

    _RECENT_SENDS[normalized] = now
    return True


def _log_whatsapp_line(
    status: str,
    phone: str,
    *,
    context: str = "",
    error: str | None = None,
    template: str | None = None,
    wa_id: str | None = None,
    message_id: str | None = None,
    chat_verified: bool | None = None,
) -> None:
    parts = [f"[WhatsApp] {status}", f"phone={phone}"]
    if context:
        parts.append(f"context={context}")
    if template:
        parts.append(f"template={template}")
    if wa_id:
        parts.append(f"wa_id={wa_id}")
    if message_id:
        parts.append(f"wamid={message_id}")
    if chat_verified is not None:
        parts.append(f"chat_verified={chat_verified}")
    if error:
        parts.append(f"error={error}")
    line = " | ".join(parts)
    if status in {"SKIP", "FAIL"}:
        logger.warning(line)
    else:
        logger.info(line)


async def schedule_whatsapp_for_contact(
    contact: dict[str, Any],
    *,
    online_mode: bool = True,
    on_zoho_sync: bool = False,
    contact_id: str | None = None,
    skip_if_already_sent: bool = True,
    log_context: str = "schedule",
) -> dict[str, Any]:
    """
    Send a dummy WhatsApp template to the contact's primary phone.

    Returns a result dict with attempted/sent/error/message_id fields.
    """
    skipped = _base_whatsapp_send_result()

    if not _auto_send_enabled():
        skipped["error"] = "WhatsApp auto-send is disabled."
        _log_whatsapp_line("SKIP", "?", context=log_context, error=skipped["error"])
        return skipped

    if not on_zoho_sync and not online_mode:
        skipped["error"] = "Offline mode — WhatsApp will send when you sync to Zoho."
        _log_whatsapp_line("SKIP", "?", context=log_context, error=skipped["error"])
        return skipped

    if not is_whatsapp_configured():
        skipped["error"] = "WhatsApp is not configured in .env."
        _log_whatsapp_line("SKIP", "?", context=log_context, error=skipped["error"])
        return skipped

    phone = extract_primary_phone(contact)

    if contact_id and skip_if_already_sent:
        from services import contact_storage as storage

        existing = storage.get_contact(contact_id)
        if existing and storage.has_whatsapp_sent(existing):
            skipped["error"] = "WhatsApp template was already sent for this contact."
            _log_whatsapp_line("SKIP", phone or "?", context=log_context, error=skipped["error"])
            return skipped

    if not phone:
        skipped["error"] = "No primary phone number found on the contact."
        _log_whatsapp_line("SKIP", "?", context=log_context, error=skipped["error"])
        return skipped

    contact_name = extract_contact_name(contact)
    recipient_check = evaluate_recipient_before_send(phone)
    skipped["recipient_phone"] = phone
    skipped["recipient_name"] = contact_name or None
    skipped["message"] = build_scan_thank_you_text(contact_name, extract_company_name(contact))
    skipped["contact_chat_verified"] = recipient_check["contact_chat_verified"]
    skipped["recipient_has_whatsapp"] = recipient_check["recipient_has_whatsapp"]

    if not recipient_check["can_send"]:
        skipped["error"] = recipient_check["error"] or "Phone cannot receive WhatsApp."
        _log_whatsapp_line("SKIP", phone, context=log_context, error=skipped["error"])
        return skipped

    if not _should_send_to_phone(phone):
        skipped["error"] = "Duplicate WhatsApp send skipped for this number."
        _log_whatsapp_line("SKIP", phone, context=log_context, error=skipped["error"])
        return skipped

    _log_whatsapp_line("SEND", phone, context=log_context, template=CARD_RECEIVED_TEMPLATE_NAME)
    try:
        delivery = await asyncio.to_thread(send_scan_thank_you_to_contact, contact)
        result = delivery["result"]
        message_id = (result.get("messages") or [{}])[0].get("id")
        has_whatsapp = bool(delivery.get("recipient_has_whatsapp", False))
        if contact_id and has_whatsapp:
            from services import contact_storage as storage

            await asyncio.to_thread(storage.mark_whatsapp_sent, contact_id)
        ok = {
            "attempted": True,
            "sent": has_whatsapp,
            "queued": False,
            "error": None if has_whatsapp else "Meta accepted send but recipient wa_id missing.",
            "message_id": message_id,
            "recipient_phone": delivery["phone"],
            "recipient_name": delivery["recipient_name"],
            "message": delivery["message"],
            "send_mode": delivery["send_mode"],
            "recipient_has_whatsapp": has_whatsapp,
            "contact_chat_verified": recipient_check["contact_chat_verified"],
            "wa_id": delivery.get("wa_id"),
        }
        ok["template_category"] = "UTILITY"
        ok["really_sent_to_meta"] = has_whatsapp
        ok["silent_api_failure"] = not has_whatsapp
        ok["phone_delivery_status"] = "pending_webhook" if has_whatsapp else "not_sent"
        _log_whatsapp_line(
            "SENT" if has_whatsapp else "FAIL",
            phone,
            context=log_context,
            template=delivery["send_mode"],
            wa_id=delivery.get("wa_id"),
            message_id=message_id,
            chat_verified=recipient_check["contact_chat_verified"],
            error=None if has_whatsapp else ok["error"],
        )
        return ok
    except Exception as exc:
        error_message = str(exc)
        _log_whatsapp_line("FAIL", phone, context=log_context, error=error_message)
        logger.error("WhatsApp auto-send failed for %s: %s", phone, error_message, exc_info=True)
        not_on_whatsapp = is_whatsapp_not_registered_error(error_message)
        failed = {
            "attempted": True,
            "sent": False,
            "queued": False,
            "error": error_message,
            "message_id": None,
            "recipient_phone": phone,
            "recipient_name": contact_name or None,
            "message": build_scan_thank_you_text(contact_name, extract_company_name(contact)),
            "send_mode": None,
            "recipient_has_whatsapp": False if not_on_whatsapp else recipient_check["recipient_has_whatsapp"],
            "contact_chat_verified": recipient_check["contact_chat_verified"],
            "wa_id": None,
        }
        return failed


# Backwards-compatible alias
async def schedule_whatsapp_for_scanned_contact(contact: dict[str, Any], **kwargs) -> dict[str, Any]:
    return await schedule_whatsapp_for_contact(contact, **kwargs)


def send_whatsapp_message(phone: str, message: str) -> dict[str, Any]:
    """Send a text message; fall back to the default template if outside the 24h session window."""
    try:
        return send_whatsapp_text(phone, message)
    except RuntimeError as exc:
        error_text = str(exc).lower()
        if "template" not in error_text and "24" not in error_text and "session" not in error_text:
            raise
        logger.info("Text message blocked by WhatsApp session rules; falling back to template.")
        return send_whatsapp_template(phone)


class WhatsAppQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.worker_task = None
        self.is_running = False

    async def start(self):
        if not self.is_running:
            self.is_running = True
            self.worker_task = asyncio.create_task(self._worker())
            logger.info("WhatsApp background worker started.")

    async def stop(self):
        self.is_running = False
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
            logger.info("WhatsApp background worker stopped.")

    async def enqueue_message(self, phone: str, message: str):
        item = {"type": "text", "phone": phone, "message": message}
        await self.queue.put(item)
        logger.info("Message enqueued for %s. Queue size: %s", phone, self.queue.qsize())

    async def enqueue_template(
        self,
        phone: str,
        template_name: str | None = None,
        language_code: str | None = None,
        contact_id: str | None = None,
    ):
        item = {
            "type": "template",
            "phone": phone,
            "template_name": template_name,
            "language_code": language_code,
            "contact_id": contact_id,
        }
        await self.queue.put(item)
        logger.info("Template enqueued for %s. Queue size: %s", phone, self.queue.qsize())

    async def _worker(self):
        while self.is_running:
            try:
                item = await self.queue.get()
                phone = item["phone"]
                item_type = item.get("type", "text")

                logger.info("Processing WhatsApp %s message to %s...", item_type, phone)
                if item_type == "template":
                    result = await asyncio.to_thread(
                        send_whatsapp_template,
                        phone,
                        item.get("template_name"),
                        item.get("language_code"),
                    )
                else:
                    result = await asyncio.to_thread(
                        send_whatsapp_message,
                        phone,
                        item.get("message", ""),
                    )
                message_id = (result.get("messages") or [{}])[0].get("id")
                logger.info("WhatsApp message sent to %s (id=%s)", phone, message_id)
                contact_id = item.get("contact_id")
                if contact_id:
                    from services import contact_storage as storage

                    await asyncio.to_thread(storage.mark_whatsapp_sent, contact_id)
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error processing WhatsApp message: %s", e, exc_info=True)
                self.queue.task_done()


whatsapp_queue = WhatsAppQueue()
