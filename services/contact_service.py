import logging
from datetime import datetime

from services import contact_storage as storage
from services.contact_storage import ContactStorageError

logger = logging.getLogger(__name__)


def seed_offline_sample_if_empty():
    existing = get_all_contacts()
    return {
        "seeded": False,
        "message": "Sample seeding disabled — no hardcoded contacts",
        "count": len(existing),
    }


def save_contact(contact_data, image_path=None):
    contact_data = dict(contact_data)
    contact_data.setdefault("created_at", datetime.utcnow().isoformat())
    contact_data.setdefault("source", "localdb")
    contact_data.setdefault("status", "synced")
    contact_data.setdefault("syncStatus", "synced")

    try:
        return storage.create_contact(contact_data, image_path=image_path)
    except ContactStorageError as exc:
        logger.error("Error saving contact: %s", exc)
        raise


def get_all_contacts(user: dict | None = None):
    try:
        return storage.list_contacts(user=user)
    except Exception as exc:
        logger.error("Error reading contacts: %s", exc)
        return []


def delete_all_contacts():
    return storage.delete_all_contacts()


def delete_contact(contact_id: str):
    return storage.delete_contact(contact_id)


def get_contact_by_id(contact_id: str, user: dict | None = None):
    try:
        return storage.get_contact(contact_id, user=user)
    except Exception as exc:
        logger.error("Error reading contact: %s", exc)
        return None


def _normalize_phone(phone: str) -> str:
    return "".join(c for c in str(phone or "") if c.isdigit())


def _full_phone_digits(contact_data: dict) -> str:
    """Combine country dial code + local phone for duplicate matching."""
    local = _normalize_phone(
        contact_data.get("phone") or contact_data.get("phoneNumber") or ""
    )
    if not local:
        return ""
    cc = _normalize_phone(contact_data.get("countryCode") or "")
    if cc and not local.startswith(cc):
        return f"{cc}{local}"
    return local


def find_duplicate_contacts(contact_data: dict, user: dict | None = None) -> list:
    """Find duplicates within the caller's visible contact set (RBAC-scoped)."""
    email = str(contact_data.get("email") or contact_data.get("emailAddress") or "").strip().lower()
    phone = _full_phone_digits(contact_data)
    name = str(contact_data.get("fullName") or contact_data.get("name") or "").strip().lower()
    company = str(contact_data.get("company") or contact_data.get("companyName") or "").strip().lower()

    duplicates = []
    seen_ids = set()

    for contact in get_all_contacts(user=user):
        cid = contact.get("id")
        if not cid or cid in seen_ids:
            continue

        matched_by = []
        c_email = str(contact.get("email") or "").strip().lower()
        c_phone = _full_phone_digits(contact)
        c_name = str(contact.get("fullName") or contact.get("name") or "").strip().lower()
        c_company = str(contact.get("company") or "").strip().lower()

        if email and c_email and email == c_email:
            matched_by.append("email")
        if phone and c_phone and (phone == c_phone or phone.endswith(c_phone) or c_phone.endswith(phone)):
            matched_by.append("phone")
        if name and company and c_name == name and c_company == company:
            matched_by.append("name_company")

        if matched_by:
            seen_ids.add(cid)
            duplicates.append({"contact": contact, "matchedBy": matched_by})

    return duplicates


def update_contact(contact_id: str, contact_data: dict):
    contact_data = dict(contact_data)
    contact_data["updated_at"] = datetime.utcnow().isoformat()
    result = storage.update_contact(contact_id, contact_data)
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Contact not found")}
    return result
