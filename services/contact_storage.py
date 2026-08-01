"""PostgreSQL contact storage facade.

Server-side persistence is PostgreSQL only. Browser IndexedDB remains the
offline queue on the frontend and is not a backend storage mode.
"""
from typing import Any

from utils.storage_config import get_contact_storage_mode, is_client_side_storage


class ContactStorageError(Exception):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def storage_label() -> str:
    return get_contact_storage_mode()


def check_storage() -> dict[str, Any]:
    if is_client_side_storage():
        return {
            "ok": False,
            "storage": "indexeddb",
            "error": "DATABASE_URL is not set. Configure PostgreSQL for server persistence.",
        }
    from services.local_db_service import check_database

    result = check_database()
    result["storage"] = "postgresql"
    return result


def get_storage_usage(user: dict[str, Any] | None = None) -> dict[str, Any]:
    if is_client_side_storage():
        return {
            "cards_scanned": 0,
            "contacts": 0,
            "images_stored": 0,
            "image_bytes": 0,
            "database_bytes": None,
        }
    from services import local_db_service as local_db

    return local_db.get_storage_usage(user=user)


def list_contacts(user: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if is_client_side_storage():
        return []
    from services import local_db_service as local_db

    return local_db.list_contacts(user=user)


def list_contacts_page(
    user: dict[str, Any] | None = None,
    *,
    page: int = 1,
    limit: int = 10,
    q: str | None = None,
    event: str | None = None,
) -> dict[str, Any]:
    if is_client_side_storage():
        return {"items": [], "total": 0, "page": page, "limit": limit}
    from services import local_db_service as local_db

    return local_db.list_contacts_page(user=user, page=page, limit=limit, q=q, event=event)


def get_contact(contact_id: str, user: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if is_client_side_storage():
        return None
    from services import local_db_service as local_db

    return local_db.get_contact(contact_id, user=user)


def create_contact(contact_data: dict[str, Any], image_path: str | None = None) -> dict[str, Any]:
    if is_client_side_storage():
        raise ContactStorageError(
            "PostgreSQL is not configured. Set DATABASE_URL in backend/.env.",
            status_code=503,
        )
    from services import local_db_service as local_db

    return local_db.create_contact(contact_data, image_path=image_path)


def update_contact(contact_id: str, contact_data: dict[str, Any]) -> dict[str, Any]:
    if is_client_side_storage():
        raise ContactStorageError("PostgreSQL is not configured.", status_code=503)
    from services import local_db_service as local_db

    return local_db.update_contact(contact_id, contact_data)


def delete_contact(contact_id: str) -> dict[str, Any]:
    if is_client_side_storage():
        raise ContactStorageError("PostgreSQL is not configured.", status_code=503)
    from services import local_db_service as local_db

    return local_db.delete_contact(contact_id)


def delete_all_contacts() -> dict[str, Any]:
    if is_client_side_storage():
        return {"deleted": 0, "note": "PostgreSQL not configured"}
    from services import local_db_service as local_db

    return local_db.delete_all_local_db_contacts()


def patch_sync_status(
    contact_id: str,
    sync_status: str,
    zoho_lead_id: str | None = None,
) -> None:
    if is_client_side_storage():
        return
    from services import local_db_service as local_db

    local_db.patch_sync_status(contact_id, sync_status=sync_status, zoho_lead_id=zoho_lead_id)


def mark_whatsapp_sent(contact_id: str) -> None:
    if is_client_side_storage():
        return
    from services import local_db_service as local_db

    local_db.mark_whatsapp_sent(contact_id)


def mark_email_sent(contact_id: str) -> None:
    if is_client_side_storage():
        return
    from services import local_db_service as local_db

    local_db.mark_email_sent(contact_id)


def update_outreach_delivery(
    contact_id: str,
    *,
    email_status: str | None = None,
    email_error: str | None = None,
    whatsapp_status: str | None = None,
    whatsapp_error: str | None = None,
) -> None:
    if is_client_side_storage():
        return
    from services import local_db_service as local_db

    local_db.update_outreach_delivery(
        contact_id,
        email_status=email_status,
        email_error=email_error,
        whatsapp_status=whatsapp_status,
        whatsapp_error=whatsapp_error,
    )


def has_whatsapp_sent(contact: dict[str, Any]) -> bool:
    from services import local_db_service as local_db

    return local_db.has_whatsapp_sent(contact)


def has_email_sent(contact: dict[str, Any]) -> bool:
    from services import local_db_service as local_db

    return local_db.has_email_sent(contact)
