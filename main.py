"""
CardSync AI — FastAPI backend entry point.

Run locally:
  cd backend
  python -m venv .venv
  .venv\\Scripts\\activate
  pip install -r requirements.txt
  python run.py
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer
from fastapi.staticfiles import StaticFiles

from utils.env_loader import load_env

load_env()

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    for name in (
        "httpx",
        "httpcore",
        "uvicorn.access",
        "services",
        "api",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("services.whatsapp_service").setLevel(logging.INFO)


_configure_logging()
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

from api.routes import build_api_router  # noqa: E402
from api.routes.webhook import router as webhook_router  # noqa: E402
from config.settings import (  # noqa: E402
    APP_DESCRIPTION,
    APP_TITLE,
    APP_VERSION,
    CORS_ORIGIN_REGEX,
    get_allowed_origins,
)
from auth.middleware import RBACMiddleware  # noqa: E402
from services.email_service import email_queue  # noqa: E402
from services.whatsapp_service import whatsapp_queue  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    from services.whatsapp_webhook_setup import ensure_waba_webhook_subscription

    # ── Database init ─────────────────────────────────────────────────
    from db.pool import init_pool, close_pool
    from db.schema import ensure_schema
    from db.seed import run_seed

    # Pool must be ready before any request; do not swallow init failures.
    init_pool()
    try:
        ensure_schema()
        run_seed()
    except Exception as exc:
        logger.warning("Database schema/seed skipped: %s", exc)

    # ── Background queues ─────────────────────────────────────────────
    await whatsapp_queue.start()
    await email_queue.start()
    try:
        sub = await asyncio.to_thread(ensure_waba_webhook_subscription)
        if not sub.get("subscribed"):
            logger.warning("WhatsApp WABA webhook subscription: %s", sub)
    except Exception as exc:
        logger.warning("WhatsApp WABA webhook subscription check failed: %s", exc)
    yield
    await whatsapp_queue.stop()
    await email_queue.stop()
    close_pool()


# ── Swagger security scheme ───────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False)

app = FastAPI(
    title=APP_TITLE,
    description=APP_DESCRIPTION,
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=[
        {"name": "Health", "description": "Service health and connectivity checks"},
        {"name": "Auth", "description": "Login, logout, refresh, forgot/reset password, email verification"},
        {"name": "Users", "description": "User management (SuperAdmin + Admin)"},
        {"name": "Companies", "description": "Company management (SuperAdmin)"},
        {"name": "Sessions", "description": "Active session and device management"},
        {"name": "Profile", "description": "Self-service profile, password, and email"},
        {"name": "Audit", "description": "Audit log viewer (SuperAdmin + Admin)"},
        {"name": "Contacts", "description": "PostgreSQL contact CRUD and duplicates"},
        {"name": "Integrations", "description": "WhatsApp and email queue integrations"},
        {"name": "Webhooks", "description": "Meta WhatsApp webhook verification and events"},
        {"name": "Admin", "description": "Destructive admin operations (wipe data)"},
    ],
)

allowed_origins = get_allowed_origins()
cors_regex = CORS_ORIGIN_REGEX or None

# Auth runs inside CORS so 401/403 responses still include Access-Control-Allow-Origin.
app.add_middleware(RBACMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=cors_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(build_api_router())
app.include_router(webhook_router)


# ---------------------------------------------------------------------------
# Custom OpenAPI — inject Bearer security scheme + lock icons on protected routes
# ---------------------------------------------------------------------------
from auth.constants import is_public_path  # noqa: E402

_PUBLIC_OPENAPI = {"/", "/health", "/webhook"}


def _patch_openapi_schema() -> None:
    """Post-process the generated OpenAPI schema to add JWT Bearer auth."""
    schema = app.openapi_schema
    if schema is None:
        schema = app.openapi()

    # Register the Bearer security scheme
    schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schema["components"]["securitySchemes"]["Bearer"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Paste your JWT access token (no 'Bearer ' prefix).",
    }

    # Mark each operation: public → security=[], protected → security=[{"Bearer": []}]
    for path, path_item in schema.get("paths", {}).items():
        is_pub = is_public_path(path) or path in _PUBLIC_OPENAPI
        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if operation is None:
                continue
            if "security" not in operation:
                if is_pub:
                    operation["security"] = []
                else:
                    operation["security"] = [{"Bearer": []}]

    app.openapi_schema = schema


# ---------------------------------------------------------------------------
# Override app.openapi so the patched schema is used by /docs and /openapi.json
# ---------------------------------------------------------------------------
_original_openapi = app.openapi


def custom_openapi():
    if app.openapi_schema is None:
        _original_openapi()
        _patch_openapi_schema()
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]


@app.get(
    "/",
    tags=["Health"],
    summary="API root",
    description="Lightweight API index. Use `/health` for full service status.",
)
def root():
    return {
        "ok": True,
        "service": "cardsync-backend",
        "health": "/health",
        "docs": "/docs",
        "webhook": "/webhook",
    }


@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)


@app.get(
    "/health",
    tags=["Health"],
    summary="Health check",
    description=(
        "Reports PostgreSQL storage, email (SMTP), WhatsApp, and OCR status. "
        "OCR: Textract (online, POST /api/ocr) + PaddleOCR (offline, browser)."
    ),
)
def health_check():
    from services.contact_storage import check_storage, storage_label
    from services.email_service import (
        SMTP_HOST,
        SMTP_USER,
        get_email_provider,
        is_email_configured,
        is_email_test_recipient_configured,
        is_smtp_configured,
        smtp_sender_email,
    )
    from api.routes.webhook import get_webhook_verify_token
    from services.whatsapp_service import get_whatsapp_config_summary, is_whatsapp_configured
    from services.whatsapp_webhook_setup import get_waba_subscription_status

    provider = get_email_provider()
    db_status = check_storage()
    return {
        "ok": bool(db_status.get("ok")),
        "service": "cardsync-backend",
        "storage": storage_label(),
        "database": db_status,
        "ocr": {
            "location": "backend+browser",
            "note": "Textract (online) + PaddleOCR (offline). POST /api/ocr for online extraction.",
        },
        "email": {
            "configured": is_email_configured(),
            "provider": provider,
            "smtp_configured": is_smtp_configured(),
            "smtp_host": SMTP_HOST,
            "test_recipient_env_set": is_email_test_recipient_configured(),
            "from": smtp_sender_email() or SMTP_USER or None,
        },
        "whatsapp": {
            "configured": is_whatsapp_configured(),
            "webhook_path": "/webhook",
            "verify_token_set": bool(get_webhook_verify_token()),
            **(get_whatsapp_config_summary() if is_whatsapp_configured() else {}),
            "waba_webhook_subscribed": (
                get_waba_subscription_status() if is_whatsapp_configured() else {"subscribed": False}
            ),
        },
    }
