from fastapi import APIRouter

from api.routes.admin import router as admin_router
from api.routes.auth_password_reset import router as auth_password_reset_router
from api.routes.auth_routes import router as auth_router
from api.routes.audit_routes import router as audit_router
from api.routes.company_routes import router as company_router
from api.routes.contacts import router as contacts_router
from api.routes.event_routes import router as event_router
from api.routes.google_oauth_routes import router as google_oauth_router
from api.routes.integrations import router as integrations_router
from api.routes.invitation_routes import router as invitation_router
from api.routes.ocr import router as ocr_router
from api.routes.offline_queue_routes import router as offline_queue_router
from api.routes.profile_routes import router as profile_router
from api.routes.session_routes import router as session_router
from api.routes.user_routes import router as user_router


def build_api_router() -> APIRouter:
    root = APIRouter()
    root.include_router(integrations_router)
    root.include_router(auth_password_reset_router)
    root.include_router(contacts_router)
    root.include_router(ocr_router)
    root.include_router(admin_router)
    root.include_router(auth_router)
    root.include_router(user_router)
    root.include_router(company_router)
    root.include_router(event_router)
    root.include_router(session_router)
    root.include_router(profile_router)
    root.include_router(audit_router)
    root.include_router(invitation_router)
    root.include_router(offline_queue_router)
    root.include_router(google_oauth_router)
    return root
