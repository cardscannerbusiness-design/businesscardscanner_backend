"""Google OAuth for Admin / Super Admin — create sheets in their own Drive (free).

Service-account create fails on personal Gmail (storageQuotaExceeded). Admins
connect Google once via OAuth; new workbooks are created in *their* Drive and
then shared with the service account (writer) so existing sheet sync still works.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import jwt
import requests

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
_DRIVE_API = "https://www.googleapis.com/drive/v3"

_SCOPES = " ".join(
    [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/userinfo.email",
        "openid",
    ]
)

_oauth_client_cache: dict[str, str] | None = None


class GoogleOAuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _resolve_oauth_json_path(raw: str) -> Path | None:
    candidates: list[Path] = [Path(raw)]
    if not Path(raw).is_absolute():
        candidates.append(_BACKEND_ROOT / raw)
        candidates.append(_BACKEND_ROOT / "secrets" / Path(raw).name)
    name = Path(raw.replace("\\", "/")).name
    if name.lower().endswith(".json"):
        candidates.append(_BACKEND_ROOT / "secrets" / name)
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _find_downloaded_client_secret() -> Path | None:
    secrets_dir = _BACKEND_ROOT / "secrets"
    if not secrets_dir.is_dir():
        return None
    # Prefer Google Cloud Console downloads (client_secret_*.json), then exact name.
    matches = sorted(secrets_dir.glob("client_secret_*.json"))
    if matches:
        return matches[0]
    exact = secrets_dir / "client_secret.json"
    try:
        if exact.is_file():
            return exact
    except OSError:
        return None
    return None


def warn_if_oauth_not_configured() -> None:
    """Log a one-time warning when Google OAuth credentials are missing (never crash)."""
    try:
        if not is_oauth_configured():
            logger.warning(
                "Google OAuth is not configured. Set GOOGLE_OAUTH_CLIENT_ID / "
                "GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REDIRECT_URI, or place "
                "secrets/client_secret.json (or secrets/client_secret_*.json). "
                "Google Drive connect will stay disabled until configured."
            )
    except Exception as exc:
        logger.warning("Google OAuth config check failed: %s", exc)


def _load_oauth_client() -> dict[str, str]:
    """Load client_id/secret from env or secrets/client_secret_*.json."""
    global _oauth_client_cache
    if _oauth_client_cache is not None:
        return _oauth_client_cache

    client_id = (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
    redirect = (os.getenv("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()

    raw_json = (os.getenv("GOOGLE_OAUTH_CLIENT_JSON") or "").strip()
    json_path: Path | None = None
    if raw_json:
        json_path = _resolve_oauth_json_path(raw_json)
    if json_path is None:
        json_path = _find_downloaded_client_secret()

    if json_path is not None:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            web = data.get("web") if isinstance(data, dict) else None
            if isinstance(web, dict):
                # Prefer downloaded client JSON over possibly mistyped .env values.
                json_id = str(web.get("client_id") or "").strip()
                json_secret = str(web.get("client_secret") or "").strip()
                if json_id:
                    client_id = json_id
                if json_secret:
                    client_secret = json_secret
                if not redirect:
                    uris = web.get("redirect_uris") or []
                    # Prefer local callback when running locally.
                    backend = (os.getenv("BACKEND_BASE_URL") or "").strip()
                    if "127.0.0.1" in backend or "localhost" in backend:
                        for uri in uris:
                            if "127.0.0.1" in str(uri) or "localhost" in str(uri):
                                redirect = str(uri).strip()
                                break
                    if not redirect and uris:
                        # Prefer production api domain if present.
                        for uri in uris:
                            if "api.namecardscan.com" in str(uri):
                                redirect = str(uri).strip()
                                break
                        if not redirect:
                            redirect = str(uris[0]).strip()
            logger.info("Google OAuth client loaded from %s", json_path)
        except Exception as exc:
            logger.warning("Could not load Google OAuth client JSON %s: %s", json_path, exc)

    _oauth_client_cache = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
    }
    return _oauth_client_cache


def is_oauth_configured() -> bool:
    cfg = _load_oauth_client()
    return bool(cfg.get("client_id") and cfg.get("client_secret") and cfg.get("redirect_uri"))


def _client_id() -> str:
    return _load_oauth_client().get("client_id") or ""


def _client_secret() -> str:
    return _load_oauth_client().get("client_secret") or ""


def _redirect_uri() -> str:
    return _load_oauth_client().get("redirect_uri") or ""


def _state_secret() -> str:
    return (os.getenv("JWT_SECRET_KEY") or "oauth-state").strip()


def build_authorize_url(*, user_id: str, role: str) -> str | None:
    """Build Google authorize URL, or return None when OAuth is not configured."""
    if not is_oauth_configured():
        logger.warning(
            "Google OAuth start requested but OAuth is not configured "
            "(env vars or secrets/client_secret*.json missing)."
        )
        return None
    state = jwt.encode(
        {
            "uid": user_id,
            "role": role,
            "nonce": secrets.token_urlsafe(8),
            "exp": int(time.time()) + 600,
        },
        _state_secret(),
        algorithm="HS256",
    )
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def decode_oauth_state(state: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(state, _state_secret(), algorithms=["HS256"])
    except Exception as exc:
        raise GoogleOAuthError("INVALID_STATE", f"Invalid OAuth state: {exc}", 400) from exc
    uid = str(payload.get("uid") or "").strip()
    if not uid:
        raise GoogleOAuthError("INVALID_STATE", "OAuth state missing user.", 400)
    return {"user_id": uid, "role": str(payload.get("role") or "")}


def exchange_code_for_tokens(code: str) -> dict[str, Any]:
    response = requests.post(
        _TOKEN_URL,
        data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        logger.warning("Google OAuth token exchange failed: %s", response.text[:400])
        raise GoogleOAuthError(
            "TOKEN_EXCHANGE_FAILED",
            "Failed to exchange Google authorization code.",
            400,
        )
    return response.json()


def refresh_access_token(refresh_token: str) -> str:
    response = requests.post(
        _TOKEN_URL,
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        logger.warning("Google OAuth refresh failed: %s", response.text[:400])
        raise GoogleOAuthError(
            "TOKEN_REFRESH_FAILED",
            "Google Drive connection expired. Please reconnect.",
            401,
        )
    token = response.json().get("access_token")
    if not token:
        raise GoogleOAuthError("TOKEN_REFRESH_FAILED", "No access token returned.", 401)
    return str(token)


def fetch_google_email(access_token: str) -> str | None:
    response = requests.get(
        _USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if response.status_code >= 400:
        return None
    return str(response.json().get("email") or "").strip().lower() or None


def persist_oauth_tokens(
    *,
    user_id: str,
    refresh_token: str | None,
    connected_email: str | None,
) -> None:
    from db.pool import db_cursor

    with db_cursor(commit=True) as cur:
        if refresh_token:
            cur.execute(
                """
                UPDATE users
                SET google_refresh_token = %s,
                    google_connected_email = COALESCE(%s, google_connected_email),
                    google_connected_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (refresh_token, connected_email, user_id),
            )
        else:
            # Google sometimes omits refresh_token on re-consent; keep existing refresh.
            cur.execute(
                """
                UPDATE users
                SET google_connected_email = COALESCE(%s, google_connected_email),
                    google_connected_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (connected_email, user_id),
            )


def clear_oauth_tokens(user_id: str) -> None:
    from db.pool import db_cursor

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE users
            SET google_refresh_token = NULL,
                google_connected_email = NULL,
                google_connected_at = NULL,
                updated_at = NOW()
            WHERE id = %s
            """,
            (user_id,),
        )


def get_oauth_status(user_id: str) -> dict[str, Any]:
    from db.pool import db_cursor

    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT google_refresh_token IS NOT NULL AS connected,
                   google_connected_email,
                   google_connected_at,
                   google_sheet_id,
                   company_id
            FROM users
            WHERE id = %s AND deleted_at IS NULL
            """,
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"connected": False, "oauth_configured": is_oauth_configured()}
    company_sheet_id = None
    company_id = row.get("company_id")
    if company_id:
        with db_cursor(commit=False) as cur:
            cur.execute(
                "SELECT google_sheet_id FROM companies WHERE id = %s",
                (str(company_id),),
            )
            crow = cur.fetchone()
            if crow:
                company_sheet_id = str(crow.get("google_sheet_id") or "").strip() or None
    return {
        "oauth_configured": is_oauth_configured(),
        "connected": bool(row.get("connected")),
        "google_email": str(row.get("google_connected_email") or "").strip() or None,
        "connected_at": row.get("google_connected_at").isoformat()
        if row.get("google_connected_at")
        else None,
        "user_sheet_id": str(row.get("google_sheet_id") or "").strip() or None,
        "company_sheet_id": company_sheet_id,
        "sheet_url": (
            f"https://docs.google.com/spreadsheets/d/{company_sheet_id}/edit"
            if company_sheet_id
            else (
                f"https://docs.google.com/spreadsheets/d/{row.get('google_sheet_id')}/edit"
                if row.get("google_sheet_id")
                else None
            )
        ),
    }


def load_user_refresh_token(user_id: str) -> str | None:
    from db.pool import db_cursor

    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT google_refresh_token
            FROM users
            WHERE id = %s AND deleted_at IS NULL
            """,
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    token = str(row.get("google_refresh_token") or "").strip()
    return token or None


def load_company_admin_oauth(company_id: str) -> dict[str, Any] | None:
    """Return admin user id + refresh token for a company."""
    from db.pool import db_cursor

    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.id AS admin_id,
                   u.google_refresh_token,
                   u.email AS admin_email,
                   u.google_connected_email
            FROM companies c
            JOIN users u ON u.id = c.admin_id AND u.deleted_at IS NULL
            WHERE c.id = %s
            """,
            (company_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    refresh = str(row.get("google_refresh_token") or "").strip()
    return {
        "admin_id": str(row.get("admin_id") or ""),
        "refresh_token": refresh or None,
        "admin_email": str(row.get("admin_email") or "").strip().lower() or None,
        "google_connected_email": str(row.get("google_connected_email") or "").strip().lower()
        or None,
    }


def oauth_auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def create_spreadsheet_with_oauth(
    access_token: str,
    title: str,
    first_sheet: str = "Day 1",
) -> str:
    """Create a spreadsheet in the connected user's Drive. Returns spreadsheet id."""
    from services.google_sheets_service import _sanitize_sheet_title

    sheet_title = _sanitize_sheet_title(first_sheet)
    response = requests.post(
        _SHEETS_API,
        headers=oauth_auth_headers(access_token),
        json={
            "properties": {"title": title},
            "sheets": [{"properties": {"title": sheet_title}}],
        },
        timeout=30,
    )
    if response.status_code >= 400:
        logger.error("OAuth spreadsheet create failed: %s", response.text[:500])
        raise GoogleOAuthError(
            "SHEET_CREATE_FAILED",
            "Could not create Google Sheet in your Drive.",
            502,
        )
    spreadsheet_id = str(response.json().get("spreadsheetId") or "")
    if not spreadsheet_id:
        raise GoogleOAuthError("SHEET_CREATE_FAILED", "No spreadsheetId returned.", 502)
    logger.info("Google OAuth: created workbook %r (%s).", title, spreadsheet_id)
    return spreadsheet_id


def service_account_client_email() -> str | None:
    from services.google_sheets_service import _load_service_account

    creds = _load_service_account() or {}
    email = str(creds.get("client_email") or "").strip().lower()
    return email or None
