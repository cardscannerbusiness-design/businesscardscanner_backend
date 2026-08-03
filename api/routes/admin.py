import logging

from fastapi import APIRouter, Depends, HTTPException

from api.schemas import WipeAllDataBody
from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN
from auth.dependencies import require_role
from auth.email_service import send_data_deletion_confirmation
from services import contact_storage as storage
from services.contact_service import delete_all_contacts

router = APIRouter(tags=["Admin"])
logger = logging.getLogger(__name__)


@router.post(
    "/admin/wipe-all-data",
    summary="Wipe all data (Admin/SuperAdmin only)",
    description="Soft-deletes all local PostgreSQL contacts. Requires ADMIN or SUPER_ADMIN role.",
)
def wipe_all_data(
    body: WipeAllDataBody,
    user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN)),
):
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Set confirm=true in the request body to wipe local database contacts.",
        )

    result = {
        "contacts": delete_all_contacts(),
        "storage": storage.storage_label(),
    }
    email = str(user.get("email") or "").strip()
    email_sent = False
    if email:
        try:
            send_result = send_data_deletion_confirmation(email, "organisation")
            email_sent = bool(send_result.get("sent"))
        except Exception:
            logger.exception("Organisation deletion confirmation email failed")
    return {"success": True, "email_sent": email_sent, **result}
