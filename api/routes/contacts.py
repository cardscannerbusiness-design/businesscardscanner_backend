import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from api.auth_context import get_receive_email_from_request
from api.outreach import (
    email_response,
    is_online_mode,
    run_post_save_outreach,
    schedule_outreach_for_contact,
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
from utils.file_utils import cleanup_temp_file, save_temp_file, validate_file

router = APIRouter(tags=["Contacts"])
logger = logging.getLogger(__name__)


def _sheets_extras(data: dict[str, Any]) -> dict[str, Any]:
    """Scan metadata forwarded to the Google Sheets sync (never persisted)."""
    return {
        "ocrEngine": str(data.get("ocrEngine") or ""),
        "ocrConfidence": data.get("ocrConfidence"),
        "captureSource": str(data.get("captureSource") or ""),
    }


@router.post("/contacts/check-duplicates")
async def check_duplicates(
    request: DuplicateCheckRequest,
    user: dict = Depends(get_current_user),
):
    return {"duplicates": find_duplicate_contacts(request.model_dump(), user=user)}


@router.put("/contacts/{contact_id}")
async def update_existing_contact(
    contact_id: str,
    request: ContactUpdateRequest,
    user: dict = Depends(get_current_user),
):
    existing = storage.get_contact(contact_id, user=user)
    require_contact_access(user, existing)
    result = update_contact(contact_id, request.contact)
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
):
    if page is not None or limit is not None or q or event:
        return storage.list_contacts_page(
            user=user,
            page=page or 1,
            limit=limit or 10,
            q=q,
            event=event,
        )
    return storage.list_contacts(user=user)


@router.get("/api/storage/config")
async def storage_config(_user: dict = Depends(get_current_user)):
    from utils.storage_config import get_contact_storage_mode

    return {
        "storage": get_contact_storage_mode(),
        "database": storage.check_storage(),
    }


@router.get("/api/contacts", summary="List contacts (UI shape)")
async def list_contacts_api(
    user: dict = Depends(get_current_user),
    page: int | None = Query(None, ge=1),
    limit: int | None = Query(None, ge=1, le=100),
    q: str | None = Query(None, max_length=200),
    event: str | None = Query(None, max_length=200),
):
    if page is not None or limit is not None or q or event:
        return storage.list_contacts_page(
            user=user,
            page=page or 1,
            limit=limit or 10,
            q=q,
            event=event,
        )
    return storage.list_contacts(user=user)


@router.get("/api/contacts/{contact_id}")
async def get_contact_api(contact_id: str, user: dict = Depends(get_current_user)):
    contact = storage.get_contact(contact_id, user=user)
    return require_contact_access(user, contact)


@router.get("/api/contacts/{contact_id}/card-image", summary="Original card image")
async def get_contact_card_image(contact_id: str, user: dict = Depends(get_current_user)):
    """Serve the stored business-card image (base64 in PostgreSQL) as a file."""
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


@router.post("/api/contacts", summary="Save contact")
async def create_contact_json(
    body: LocalContactBody,
    request: Request,
    user: dict = Depends(get_current_user),
):
    try:
        payload = body.model_dump()
        payload["created_by_user_id"] = user["id"]
        result = storage.create_contact(payload)
        contact_id = result["id"]
        response: dict[str, Any] = {
            "success": True,
            "id": contact_id,
            "contact": storage.get_contact(contact_id, user=user),
            "database": "postgresql",
        }

        # Secondary sync: PostgreSQL commit succeeded — mirror to Google Sheets.
        fire_sheets_sync(contact_id, _sheets_extras(payload))

        if is_online_mode(body.connectionMode):
            try:
                whatsapp_result, email_result = await run_post_save_outreach(
                    contact_id=contact_id,
                    skip_whatsapp=body.skipWhatsApp,
                    skip_email=body.skipEmail,
                    log_context="create-contact",
                    scanner_email=get_receive_email_from_request(request),
                )
                response.update(whatsapp_response(whatsapp_result))
                response.update(email_response(email_result))
                # Return the contact row after delivery statuses are persisted.
                response["contact"] = storage.get_contact(contact_id, user=user)
            except Exception as exc:
                logger.error("Outreach after save failed for %s: %s", contact_id, exc, exc_info=True)
                response["outreachError"] = str(exc)

        return response
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
        existing = storage.get_contact(contact_id, user=user)
        require_contact_access(user, existing)
        payload = body.model_dump()
        payload.pop("created_by_user_id", None)
        result = storage.update_contact(contact_id, payload)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error", "Contact not found"))
        fire_sheets_sync(contact_id, _sheets_extras(payload))
        return {"success": True, "id": contact_id, "contact": storage.get_contact(contact_id, user=user)}
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
    contact = storage.get_contact(contact_id, user=user)
    require_contact_access(user, contact)
    result = storage.delete_contact(contact_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("message", "Contact not found"))
    return result


@router.post("/contacts/seed-sample")
async def seed_offline_sample(_user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))):
    return seed_offline_sample_if_empty()


@router.delete(
    "/contacts/{contact_id}",
    summary="Soft-delete a contact (Admin/SuperAdmin only)",
)
def remove_contact(
    contact_id: str,
    user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN)),
):
    contact = storage.get_contact(contact_id, user=user)
    require_contact_access(user, contact)
    return delete_contact(contact_id)
