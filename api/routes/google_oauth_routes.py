"""Google OAuth routes — Admin / Super Admin connect Drive and create company sheets."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN
from auth.dependencies import require_role
from config.urls import get_frontend_base_url
from services import google_oauth_service as oauth
from services import google_sheets_service as sheets

router = APIRouter(prefix="/api/google", tags=["Google Drive"])
logger = logging.getLogger(__name__)


def _frontend_settings_redirect(**params: str) -> RedirectResponse:
    try:
        base = get_frontend_base_url()
    except Exception:
        base = "http://localhost:5173"
    qs = urlencode({k: v for k, v in params.items() if v})
    url = f"{base}/settings" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=url, status_code=302)


@router.get("/oauth/status", summary="Google Drive connection status")
def oauth_status(request: Request):
    user = require_role(ROLE_ADMIN, ROLE_SUPER_ADMIN)(request)
    return oauth.get_oauth_status(str(user["id"]))


@router.get("/oauth/start", summary="Start Google OAuth (Connect Google Drive)")
def oauth_start(request: Request):
    user = require_role(ROLE_ADMIN, ROLE_SUPER_ADMIN)(request)
    try:
        url = oauth.build_authorize_url(
            user_id=str(user["id"]),
            role=str(user.get("role") or ""),
        )
    except oauth.GoogleOAuthError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})
    if not url:
        # Soft degradation — never crash; UI disables Connect when oauth_configured is false.
        return {
            "oauth_configured": False,
            "authorize_url": None,
            "message": (
                "Google OAuth is not configured on the server. "
                "Set GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / "
                "GOOGLE_OAUTH_REDIRECT_URI or place secrets/client_secret.json."
            ),
        }
    return {"oauth_configured": True, "authorize_url": url}


@router.get("/oauth/callback", summary="Google OAuth callback (public)")
def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return _frontend_settings_redirect(google="error", message=error)
    if not code or not state:
        return _frontend_settings_redirect(google="error", message="missing_code")
    try:
        decoded = oauth.decode_oauth_state(state)
        tokens = oauth.exchange_code_for_tokens(code)
        access = str(tokens.get("access_token") or "")
        refresh = str(tokens.get("refresh_token") or "").strip() or None
        email = oauth.fetch_google_email(access) if access else None
        oauth.persist_oauth_tokens(
            user_id=decoded["user_id"],
            refresh_token=refresh,
            connected_email=email,
        )
        # Best-effort: create sheet immediately after connect.
        role = str(decoded.get("role") or "").upper()
        if role == ROLE_SUPER_ADMIN:
            try:
                sheets.ensure_superadmin_sheet()
            except Exception as exc:
                logger.warning("Sheet ensure after Super Admin OAuth: %s", exc)
        else:
            from db.pool import db_cursor

            with db_cursor(commit=False) as cur:
                cur.execute(
                    "SELECT company_id FROM users WHERE id = %s",
                    (decoded["user_id"],),
                )
                row = cur.fetchone()
            company_id = str((row or {}).get("company_id") or "").strip()
            if company_id:
                try:
                    with sheets._workbook_cache_lock:
                        sheets._workbook_cache.pop(f"company:{company_id}", None)
                    sheets.ensure_company_sheet(company_id)
                except Exception as exc:
                    logger.warning("Sheet ensure after Admin OAuth: %s", exc)
    except oauth.GoogleOAuthError as exc:
        return _frontend_settings_redirect(google="error", message=exc.code)
    except Exception as exc:
        logger.exception("OAuth callback failed: %s", exc)
        return _frontend_settings_redirect(google="error", message="callback_failed")
    return _frontend_settings_redirect(google="connected")


@router.post("/oauth/disconnect", summary="Disconnect Google Drive")
def oauth_disconnect(request: Request):
    user = require_role(ROLE_ADMIN, ROLE_SUPER_ADMIN)(request)
    oauth.clear_oauth_tokens(str(user["id"]))
    return {"success": True, "detail": "Google Drive disconnected."}


@router.post("/sheets/ensure", summary="Create / refresh company or Super Admin sheet")
def ensure_sheet(request: Request):
    user = require_role(ROLE_ADMIN, ROLE_SUPER_ADMIN)(request)
    role = str(user.get("role") or "").upper()
    try:
        if role == ROLE_SUPER_ADMIN:
            sheet_id = sheets.ensure_superadmin_sheet()
        else:
            company_id = str(user.get("company_id") or "").strip()
            if not company_id:
                raise HTTPException(status_code=400, detail="Admin has no company.")
            with sheets._workbook_cache_lock:
                sheets._workbook_cache.pop(f"company:{company_id}", None)
            sheet_id = sheets.ensure_company_sheet(company_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except oauth.GoogleOAuthError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})
    except Exception as exc:
        logger.exception("ensure_sheet failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not create or open Google Sheet.") from exc
    return {
        "success": True,
        "spreadsheet_id": sheet_id,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
    }
