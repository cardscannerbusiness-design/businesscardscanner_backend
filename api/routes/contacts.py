import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile

from api.auth_context import get_receive_email_from_request
from api.outreach import (
    email_response,
    is_online_mode,
    run_post_save_outreach,
    schedule_outreach_for_contact,
    track_background_task,
    whatsapp_response,
)
from api.schemas import (
    ContactUpdateRequest,
    DuplicateCheckRequest,
    LocalContactBody,
    SyncStatusBody,
)
from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN
from auth.dependencies import get_current_user, require_role
from auth.ownership import require_contact_access
from services import contact_storage as storage
from services.contact_service import (
    delete_contact,
    find_duplicate_contacts,
    save_contact,
    seed_offline_sample_if_empty,
    update_contact,
)
from services.contact_storage import ContactStorageError
from services.google_sheets_service import fire_sheets_sync
from services.local_db_service import LocalDbError
from services.storage_service import (
    StorageLimitExceededError,
    get_storage_usage as get_company_quota_usage,
    resolve_company_id_for_user,
)
from services.entitlement_service import (
    CardLimitExceededError,
    ContactsFrozenError,
    EntitlementDeniedError,
    assert_can_access_contacts,
    assert_can_process_card,
)
from utils.file_utils import cleanup_temp_file, save_temp_file, validate_file

router = APIRouter(tags=["Contacts"])
logger = logging.getLogger(__name__)

# How long the JSON /api/contacts endpoint waits for the initial online-save
# outreach (email + WhatsApp) to finish before responding. Matches the Resend
# path which fully awaits provider results; kept bounded so a stalled provider
# cannot hang the HTTP response forever. On timeout the shielded task keeps
# running and persists the real status to the same contact row (no second send).
_INITIAL_OUTREACH_WAIT_SECONDS: float = 45.0


async def _await_online_outreach_into_response(
    response: dict[str, Any],
    *,
    contact_id: str,
    body: LocalContactBody,
    request: Request,
    user: dict,
) -> None:
    """Await email/WhatsApp like RESEND, then attach results + refreshed contact.

    This is the missing piece vs ``/api/outreach/thank-you``: the create endpoint
    must return the real per-channel send outcome (and persist it) before the
    client navigates to Contacts, otherwise the UI shows Pending until Resend.
    """
    outreach_task = asyncio.create_task(
        run_post_save_outreach(
            contact_id=contact_id,
            skip_whatsapp=body.skipWhatsApp,
            skip_email=body.skipEmail,
            scanner_email=get_receive_email_from_request(request),
            user=user,
            initial_save=True,
        )
    )
    track_background_task(outreach_task)

    try:
        whatsapp_result, email_result = await asyncio.wait_for(
            asyncio.shield(outreach_task),
            timeout=_INITIAL_OUTREACH_WAIT_SECONDS,
        )
        # Same response shape as form-data create + thank-you Resend.
        response.update(whatsapp_response(whatsapp_result))
        response.update(email_response(email_result))
    except asyncio.TimeoutError:
        # Provider still working — do NOT mark attempted=true (that looks like
        # a definitive Failed). Leave Pending; background task will persist.
        logger.info(
            "[CONTACT] Outreach exceeded %.0fs — returning Pending; "
            "task continues in background contact_id=%s",
            _INITIAL_OUTREACH_WAIT_SECONDS,
            contact_id,
        )
        response["whatsapp_sent"] = False
        response["whatsapp_attempted"] = False
        response["email_sent"] = False
        response["email_attempted"] = False
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "[CONTACT] Awaited outreach crashed for %s: %s",
            contact_id,
            exc,
        )
        response["whatsapp_sent"] = False
        response["whatsapp_attempted"] = not body.skipWhatsApp
        response["whatsapp_error"] = str(exc)
        response["email_sent"] = False
        response["email_attempted"] = not body.skipEmail
        response["email_error"] = str(exc)

    # Refresh so nested contact carries emailDeliveryStatus / whatsappDeliveryStatus.
    refreshed = storage.get_contact(contact_id, user=user)
    if refreshed:
        response["contact"] = refreshed


def _raise_entitlement(exc: EntitlementDeniedError) -> None:
    logger.warning(
        "[ENTITLEMENT] API rejected error=%s detail=%s",
        exc.code,
        exc.to_response(),
    )
    raise HTTPException(status_code=403, detail=exc.to_response()) from exc


def _raise_card_limit(exc: CardLimitExceededError) -> None:
    _raise_entitlement(exc)


def _require_contacts_access(user: dict[str, Any]) -> None:
    try:
        assert_can_access_contacts(resolve_company_id_for_user(user), user=user)
    except ContactsFrozenError as exc:
        _raise_entitlement(exc)


def _raise_storage_limit(exc: StorageLimitExceededError) -> None:
    logger.warning(
        "[STORAGE] API rejected upload error=%s detail=%s",
        exc.code,
        exc.to_response(),
    )
    raise HTTPException(status_code=403, detail=exc.to_response()) from exc


def _sheets_extras(data: dict[str, Any]) -> dict[str, Any]:
    """Scan metadata forwarded to the Google Sheets sync (never persisted)."""
    return {
        "ocrEngine": str(data.get("ocrEngine") or ""),
        "ocrConfidence": data.get("ocrConfidence"),
        "captureSource": str(data.get("captureSource") or ""),
    }


def _parse_contact_created_at(contact: dict[str, Any]) -> datetime | None:
    raw = contact.get("createdAt") or contact.get("created_at")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _find_recent_duplicate_contact(
    payload: dict[str, Any],
    user: dict[str, Any],
    *,
    window_seconds: int = 90,
) -> dict[str, Any] | None:
    """Return a matching contact created moments ago (double-submit / retry guard).

    Older intentional duplicates (Save as new) are not in this window and proceed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    for entry in find_duplicate_contacts(payload, user=user):
        contact = entry.get("contact") if isinstance(entry, dict) else None
        if not isinstance(contact, dict):
            continue
        created = _parse_contact_created_at(contact)
        if created and created >= cutoff:
            return contact
    return None


@router.post("/contacts/check-duplicates")
async def check_duplicates(
    request: DuplicateCheckRequest,
    user: dict = Depends(get_current_user),
):
    # After Freemium exhaustion do not leak stored contacts. Empty list lets
    # Capture continue (IndexedDB save) without exposing PostgreSQL rows.
    try:
        assert_can_access_contacts(resolve_company_id_for_user(user), user=user)
    except ContactsFrozenError:
        return {"duplicates": []}
    return {"duplicates": find_duplicate_contacts(request.model_dump(), user=user)}


@router.put("/contacts/{contact_id}")
async def update_existing_contact(
    contact_id: str,
    request: ContactUpdateRequest,
    user: dict = Depends(get_current_user),
):
    _require_contacts_access(user)
    existing = storage.get_contact(contact_id, user=user)
    require_contact_access(user, existing)
    try:
        result = update_contact(contact_id, request.contact)
    except StorageLimitExceededError as exc:
        _raise_storage_limit(exc)
    except LocalDbError as exc:
        logger.exception("[STORAGE] Contact update failed contact_id=%s", contact_id)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Contact not found"))
    fire_sheets_sync(contact_id, _sheets_extras(request.contact))
    return result


@router.post("/contacts")
async def create_contact(
    request: Request,
    contact: str = Form(...),
    card: Optional[UploadFile] = File(None),
    user: dict = Depends(get_current_user),
):
    try:
        contact_data = json.loads(contact)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid contact JSON") from exc

    contact_data["created_by_user_id"] = user["id"]

    temp_path = None
    try:
        if card and card.filename:
            if not validate_file(card):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid card image. Supported types: JPG, JPEG, PNG.",
                )
            temp_path = await save_temp_file(card)

        try:
            result = save_contact(contact_data, image_path=temp_path)
        except CardLimitExceededError as exc:
            _raise_card_limit(exc)
        except StorageLimitExceededError as exc:
            _raise_storage_limit(exc)
        except LocalDbError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        if not result.get("success"):
            raise HTTPException(status_code=500, detail="Failed to save contact")

        fire_sheets_sync(str(result.get("id") or ""), _sheets_extras(contact_data))

        whatsapp_result, email_result = await schedule_outreach_for_contact(
            contact_data,
            online_mode=is_online_mode(contact_data.get("connectionMode")),
            contact_id=result.get("id"),
            skip_whatsapp=bool(contact_data.get("skipWhatsApp")),
            skip_email=bool(contact_data.get("skipEmail")),
            scanner_email=get_receive_email_from_request(request),
            user=user,
            initial_save=True,
        )
        return {
            **result,
            **whatsapp_response(whatsapp_result),
            **email_response(email_result),
        }
    finally:
        if temp_path:
            cleanup_temp_file(temp_path)


@router.get("/contacts", summary="List all local database contacts")
async def fetch_contacts(
    user: dict = Depends(get_current_user),
    page: int | None = Query(None, ge=1),
    limit: int | None = Query(None, ge=1, le=100),
    q: str | None = Query(None, max_length=200),
    event: str | None = Query(None, max_length=200),
    eventId: str | None = Query(None, max_length=100),
):
    _require_contacts_access(user)
    if page is not None or limit is not None or q or event or eventId:
        return storage.list_contacts_page(
            user=user,
            page=page or 1,
            limit=limit or 10,
            q=q,
            event=event,
            event_id=eventId,
        )
    return storage.list_contacts(user=user)


@router.get("/api/storage/config")
async def storage_config(user: dict = Depends(get_current_user)):
    import asyncio

    from utils.storage_config import get_contact_storage_mode

    # Heavy aggregates must not block the asyncio event loop — that froze
    # concurrent OCR requests after Storage Usage was added to this endpoint.
    def _build() -> dict[str, Any]:
        response: dict[str, Any] = {
            "storage": get_contact_storage_mode(),
            "database": storage.check_storage(),
            "usage": storage.get_storage_usage(user=user),
        }
        company_id = resolve_company_id_for_user(user)
        if company_id:
            try:
                from services.entitlement_service import apply_user_scan_overlay

                response["quota"] = apply_user_scan_overlay(
                    get_company_quota_usage(company_id), user
                )
            except Exception as exc:
                logger.exception(
                    "[STORAGE] Could not load company storage quota company_id=%s: %s",
                    company_id,
                    exc,
                )
        return response

    return await asyncio.to_thread(_build)


@router.get(
    "/api/storage/usage",
    summary="Company storage quota usage",
    tags=["Storage"],
    responses={
        200: {
            "description": "Current plan storage usage for the authenticated user's company",
            "content": {
                "application/json": {
                    "example": {
                        "plan": "FREEMIUM",
                        "storage_limit_bytes": 1048576,
                        "used_storage_bytes": 422576,
                        "remaining_storage_bytes": 626000,
                        "used_percentage": 40.3,
                        "used_mb": 0.4,
                        "limit_mb": 1.0,
                        "remaining_mb": 0.6,
                        "can_upload": True,
                        "warning_level": "NORMAL",
                    }
                }
            },
        },
        400: {"description": "User has no company (e.g. Super Admin without company)"},
        403: {
            "description": "Returned by contact create/update when quota is exceeded",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "success": False,
                            "error": "STORAGE_LIMIT_EXCEEDED",
                            "message": "Storage limit reached. Upgrade your plan to continue.",
                            "used_storage_bytes": 1048576,
                            "storage_limit_bytes": 1048576,
                            "image_size_bytes": 50000,
                        }
                    }
                }
            },
        },
    },
)
async def storage_usage(response: Response, user: dict = Depends(get_current_user)):
    """Return plan, used/limit/remaining bytes, can_upload, and warning_level."""
    from auth.constants import ROLE_SUPER_ADMIN
    from services.entitlement_service import apply_user_scan_overlay, entitlement_fields_for_usage

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"

    if user.get("role") == ROLE_SUPER_ADMIN:
        unlimited = {
            "plan": "UNLIMITED",
            "plan_name": "Unlimited",
            "storage_limit_bytes": None,
            "used_storage_bytes": 0,
            "remaining_storage_bytes": None,
            "used_percentage": 0,
            "used_mb": 0,
            "limit_mb": None,
            "remaining_mb": None,
            "can_upload": True,
            "warning_level": "NORMAL",
        }
        unlimited.update(entitlement_fields_for_usage(None))
        unlimited["plan"] = "UNLIMITED"
        unlimited["plan_name"] = "Unlimited"
        return apply_user_scan_overlay(unlimited, user)

    company_id = resolve_company_id_for_user(user)
    if not company_id:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": "NO_COMPANY",
                "message": "No company is associated with this account.",
            },
        )
    try:
        return apply_user_scan_overlay(get_company_quota_usage(company_id), user)
    except Exception as exc:
        logger.exception("[STORAGE] Storage usage request failed company_id=%s", company_id)
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error": "STORAGE_USAGE_FAILED",
                "message": "Failed to load storage usage.",
            },
        ) from exc


@router.get("/api/contacts", summary="List contacts (UI shape)")
async def list_contacts_api(
    user: dict = Depends(get_current_user),
    page: int | None = Query(None, ge=1),
    limit: int | None = Query(None, ge=1, le=100),
    q: str | None = Query(None, max_length=200),
    event: str | None = Query(None, max_length=200),
    eventId: str | None = Query(None, max_length=100),
):
    _require_contacts_access(user)
    if page is not None or limit is not None or q or event or eventId:
        return storage.list_contacts_page(
            user=user,
            page=page or 1,
            limit=limit or 10,
            q=q,
            event=event,
            event_id=eventId,
        )
    return storage.list_contacts(user=user)


@router.get("/api/contacts/{contact_id}")
async def get_contact_api(contact_id: str, user: dict = Depends(get_current_user)):
    _require_contacts_access(user)
    contact = storage.get_contact(contact_id, user=user)
    return require_contact_access(user, contact)


@router.get("/api/contacts/{contact_id}/card-image", summary="Original card image")
async def get_contact_card_image(contact_id: str, user: dict = Depends(get_current_user)):
    """Serve the stored business-card image (base64 in PostgreSQL) as a file."""
    _require_contacts_access(user)
    import base64

    from fastapi.responses import Response

    contact = storage.get_contact(contact_id, user=user)
    require_contact_access(user, contact)

    data_url = str(contact.get("cardImageBase64") or "")
    if not data_url:
        raise HTTPException(status_code=404, detail="No card image stored for this contact")

    media_type = "image/jpeg"
    encoded = data_url
    if data_url.startswith("data:"):
        header, _, encoded = data_url.partition(",")
        media_type = header.removeprefix("data:").split(";", 1)[0] or media_type
    try:
        image_bytes = base64.b64decode(encoded)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Stored card image is corrupted") from exc

    return Response(content=image_bytes, media_type=media_type)


@router.post(
    "/api/contacts",
    summary="Save contact",
    responses={
        403: {
            "description": "Company storage quota exceeded",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "success": False,
                            "error": "STORAGE_LIMIT_EXCEEDED",
                            "message": "Storage limit reached. Upgrade your plan to continue.",
                        }
                    }
                }
            },
        }
    },
)
async def create_contact_json(
    body: LocalContactBody,
    request: Request,
    user: dict = Depends(get_current_user),
):
    try:
        # Freeze PostgreSQL persist after Freemium exhaustion (IndexedDB still allowed).
        # Check before duplicate lookup so contact rows are not leaked.
        try:
            assert_can_process_card(resolve_company_id_for_user(user), user=user)
        except CardLimitExceededError as exc:
            _raise_card_limit(exc)

        payload = body.model_dump()
        payload["created_by_user_id"] = user["id"]

        # Idempotency: a parallel/retried POST for the same card must not insert again.
        recent = _find_recent_duplicate_contact(payload, user)
        if recent and recent.get("id"):
            contact_id = str(recent["id"])
            logger.info(
                "[CONTACT] Reusing recent duplicate contact_id=%s (idempotent create)",
                contact_id,
            )
            response: dict[str, Any] = {
                "success": True,
                "id": contact_id,
                "contact": storage.get_contact(contact_id, user=user),
                "database": "postgresql",
                "idempotentReuse": True,
            }
            if is_online_mode(body.connectionMode):
                # Same as fresh create / Resend: await + return real delivery status.
                # skip_if_already_sent inside outreach prevents a duplicate send when
                # the original create already delivered.
                await _await_online_outreach_into_response(
                    response,
                    contact_id=contact_id,
                    body=body,
                    request=request,
                    user=user,
                )
            fire_sheets_sync(contact_id, _sheets_extras(payload))
            return response

        result = storage.create_contact(payload)
        contact_id = result["id"]
        response = {
            "success": True,
            "id": contact_id,
            "contact": storage.get_contact(contact_id, user=user),
            "database": "postgresql",
        }

        # Secondary sync: PostgreSQL commit succeeded — mirror to Google Sheets.
        fire_sheets_sync(contact_id, _sheets_extras(payload))

        if is_online_mode(body.connectionMode):
            await _await_online_outreach_into_response(
                response,
                contact_id=contact_id,
                body=body,
                request=request,
                user=user,
            )

        return response
    except CardLimitExceededError as exc:
        _raise_card_limit(exc)
    except StorageLimitExceededError as exc:
        _raise_storage_limit(exc)
    except ContactStorageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except LocalDbError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.put("/api/contacts/{contact_id}")
async def update_contact_json(
    contact_id: str,
    body: LocalContactBody,
    user: dict = Depends(get_current_user),
):
    try:
        _require_contacts_access(user)
        existing = storage.get_contact(contact_id, user=user)
        require_contact_access(user, existing)
        payload = body.model_dump()
        payload.pop("created_by_user_id", None)
        result = storage.update_contact(contact_id, payload)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error", "Contact not found"))
        fire_sheets_sync(contact_id, _sheets_extras(payload))
        return {"success": True, "id": contact_id, "contact": storage.get_contact(contact_id, user=user)}
    except StorageLimitExceededError as exc:
        _raise_storage_limit(exc)
    except ContactStorageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except LocalDbError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.patch("/api/contacts/{contact_id}/sync-status")
async def patch_contact_sync_status(
    contact_id: str,
    body: SyncStatusBody,
    user: dict = Depends(get_current_user),
):
    _require_contacts_access(user)
    existing = storage.get_contact(contact_id, user=user)
    require_contact_access(user, existing)
    storage.patch_sync_status(
        contact_id,
        sync_status=body.syncStatus,
    )
    contact = storage.get_contact(contact_id, user=user)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"success": True, "contact": contact}


@router.delete(
    "/api/contacts/{contact_id}",
    summary="Soft-delete a contact (Admin/SuperAdmin only)",
)
async def delete_contact_api(
    contact_id: str,
    user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN)),
):
    _require_contacts_access(user)
    contact = storage.get_contact(contact_id, user=user)
    require_contact_access(user, contact)
    result = storage.delete_contact(contact_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("message", "Contact not found"))
    return result


@router.post("/contacts/seed-sample")
async def seed_offline_sample(user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))):
    _require_contacts_access(user)
    return seed_offline_sample_if_empty()


@router.delete(
    "/contacts/{contact_id}",
    summary="Soft-delete a contact (Admin/SuperAdmin only)",
)
def remove_contact(
    contact_id: str,
    user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN)),
):
    _require_contacts_access(user)
    contact = storage.get_contact(contact_id, user=user)
    require_contact_access(user, contact)
    return delete_contact(contact_id)
