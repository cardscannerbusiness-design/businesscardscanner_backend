"""Google Sheets secondary sync — mirrors saved contacts into spreadsheets.

PostgreSQL remains the single source of truth. After a contact row is
committed, the same record is upserted (update-by-Contact-ID, else append)
into Google Sheets for reporting/sharing.

Storage layout (role-based):
    Workbook = one spreadsheet per Admin/company (all Users under that Admin
               share it). Super Admin saves go to a dedicated Super Admin sheet.
    Worksheet (tab title) = Event Day

New workbooks are created in the Admin's own Google Drive via OAuth (free),
then shared with the service account for sync writes. Missing worksheets are
created automatically. Existing contact rows are never wiped.

Drive access: Admin owns the sheet; Super Admin gets Editor; company Users get
Viewer (read-only) so the sheet appears in their Drive / Sheets apps.

Configuration (environment variables only — nothing hardcoded):
    GOOGLE_SHEET_ID              Optional fallback spreadsheet when company /
                                 Super Admin sheet cannot be resolved.
    GOOGLE_SHEET_NAME            Fallback tab when event day is empty
                                 (default: "Day 1").
    GOOGLE_DRIVE_FOLDER_ID       Optional Drive folder for newly created
                                 workbooks (share this folder with the
                                 service account).
    GOOGLE_SERVICE_ACCOUNT_JSON  Service-account credentials: either the raw
                                 JSON string or a path to the JSON key file.
    SUPERADMIN_EMAIL             Used when sharing company sheets with the
                                 platform Super Admin.

Behaviour:
    * Runs fire-and-forget in a worker thread — never blocks or fails a save.
    * Update-by-ID is preferred; append only when the Contact ID is not found,
      so edits do not create duplicate rows.
    * Failures are logged and queued in-process; the next successful sync
      drains the retry queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import jwt
import requests

# Backend package root (…/BusinessCardScanner_Backend)
_BACKEND_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)

_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
_DRIVE_API = "https://www.googleapis.com/drive/v3"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = " ".join(
    [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
)

# Column layout — order defines the sheet. Keep header text stable.
HEADERS: list[str] = [
    # Contact information
    "Contact ID",
    "Full Name",
    "Company",
    "Designation",
    "Country Code",
    "Country Name",
    "Primary Phone",
    "Secondary Phone",
    "Primary Email",
    "Secondary Email",
    "Website",
    "Primary Address",
    "Secondary Address",
    "Notes",
    # Business information
    "Event Name",
    "Event Day",
    "Company ID",
    "Company Name",
    "Created By",
    "Created By Role",
    "Created Date",
    "Updated Date",
    # OCR information
    "OCR Engine",
    "OCR Confidence",
    "Capture Source",
    # Image information
    "Original Image URL",
    "Image File Name",
    # Application information
    "Contact Status",
    "Scan Status",
    "Created Timestamp",
    "Updated Timestamp",
]

_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (1, 3)  # seconds between attempts

# In-process retry queue: contact ids whose sheet sync failed.
_pending_lock = threading.Lock()
_pending_retry: dict[str, dict[str, Any]] = {}

# Cached access token (service-account tokens last ~1 hour).
_token_lock = threading.Lock()
_cached_token: dict[str, Any] = {"token": None, "expires_at": 0.0}

# cache key ("company:<uuid>" | "superadmin") → spreadsheet id
_workbook_cache_lock = threading.Lock()
_workbook_cache: dict[str, str] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

def _sheet_id() -> str:
    return os.getenv("GOOGLE_SHEET_ID", "").strip()


def _sheet_name() -> str:
    return os.getenv("GOOGLE_SHEET_NAME", "").strip() or "Day 1"


def _drive_folder_id() -> str:
    return os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()


def _resolve_service_account_path(raw: str) -> Path | None:
    """Resolve a credentials file path for local Windows and EC2 Linux layouts."""
    candidates: list[Path] = [Path(raw)]
    if not Path(raw).is_absolute():
        candidates.append(_BACKEND_ROOT / raw)
        candidates.append(_BACKEND_ROOT / "secrets" / Path(raw).name)
        candidates.append(Path.cwd() / raw)
        candidates.append(Path.cwd() / "secrets" / Path(raw).name)
    # If someone pasted a Windows path on Linux, also try secrets/<filename>
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


def _load_service_account() -> dict[str, Any] | None:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        resolved = _resolve_service_account_path(raw)
        if resolved is not None:
            with resolved.open(encoding="utf-8") as handle:
                return json.load(handle)
        logger.warning(
            "GOOGLE_SERVICE_ACCOUNT_JSON path not found (%s). "
            "On EC2 put the JSON under secrets/ and set "
            "GOOGLE_SERVICE_ACCOUNT_JSON=secrets/<filename>.json",
            raw,
        )
    except Exception as exc:
        logger.warning("Could not load Google service account credentials: %s", exc)
    return None


def is_sheets_configured() -> bool:
    """Configured when a service account is available.

    GOOGLE_SHEET_ID is optional — used only as a last-resort fallback when a
    company / Super Admin workbook cannot be resolved.
    """
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return False
    if raw.startswith("{"):
        return True
    return _resolve_service_account_path(raw) is not None


def _superadmin_share_email() -> str | None:
    """Email used for Drive Editor share on Admin company sheets."""
    email = (os.getenv("SUPERADMIN_EMAIL") or "").strip().lower()
    return email or None


# ─────────────────────────────────────────────────────────────────────────────
# Auth (service-account JWT → OAuth token; PyJWT signs RS256)
# ─────────────────────────────────────────────────────────────────────────────

def _get_access_token() -> str | None:
    with _token_lock:
        if _cached_token["token"] and time.time() < _cached_token["expires_at"] - 60:
            return _cached_token["token"]

    creds = _load_service_account()
    if not creds:
        return None

    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": creds.get("client_email"),
            "scope": _SCOPE,
            "aud": _TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        },
        creds.get("private_key"),
        algorithm="RS256",
    )
    response = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    with _token_lock:
        _cached_token["token"] = token
        _cached_token["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
    return token


def _auth_headers() -> dict[str, str] | None:
    token = _get_access_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────────────
# Row building
# ─────────────────────────────────────────────────────────────────────────────

def _column_letter(index: int) -> str:
    """1-based column index → A1 letter (1 → A, 27 → AA)."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


_LAST_COL = _column_letter(len(HEADERS))


def _card_image_url(contact: dict[str, Any]) -> str:
    if not contact.get("cardImageBase64"):
        return ""
    try:
        from config.urls import try_backend_base_url

        base = try_backend_base_url()
    except Exception:
        return ""
    if not base:
        return ""
    return f"{base}/api/contacts/{contact.get('id')}/card-image"


def _image_file_name(contact: dict[str, Any]) -> str:
    data_url = str(contact.get("cardImageBase64") or "")
    if not data_url:
        return ""
    ext = "png" if "image/png" in data_url[:40] else "jpg"
    return f"card-{contact.get('id')}.{ext}"


def _sanitize_sheet_title(title: str, *, fallback: str = "Day 1") -> str:
    """Google Sheets tab titles: max 100 chars; no \\ / ? * [ ]."""
    cleaned = re.sub(r"[\\/?*\[\]]+", " ", (title or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = fallback
    return cleaned[:100]


def contact_to_row(contact: dict[str, Any], extras: dict[str, Any] | None = None) -> list[str]:
    """Map the existing contact model (from `_row_to_contact`) to sheet columns."""
    extras = extras or {}
    created = str(contact.get("created_at") or "")
    updated = str(contact.get("updatedAt") or "")
    confidence = extras.get("ocrConfidence")
    confidence_str = f"{float(confidence):.2f}" if confidence not in (None, "") else ""

    return [
        str(contact.get("id") or ""),
        str(contact.get("fullName") or contact.get("name") or ""),
        str(contact.get("company") or ""),
        str(contact.get("designation") or ""),
        str(contact.get("countryCode") or ""),
        str(contact.get("countryName") or ""),
        str(contact.get("phone") or ""),
        str(contact.get("secondaryPhone") or ""),
        str(contact.get("email") or ""),
        str(contact.get("secondaryEmail") or ""),
        str(contact.get("website") or ""),
        str(contact.get("address") or ""),
        str(contact.get("secondaryAddress") or ""),
        str(contact.get("notes") or ""),
        str(contact.get("eventName") or ""),
        str(contact.get("eventDay") or "Day 1"),
        str(contact.get("owner_company_id") or contact.get("company_id") or ""),
        str(contact.get("owner_company_name") or contact.get("company") or ""),
        str(contact.get("user_name") or ""),
        str(contact.get("created_by_role") or ""),
        created[:10],
        updated[:10],
        str(extras.get("ocrEngine") or ""),
        confidence_str,
        str(extras.get("captureSource") or ""),
        _card_image_url(contact),
        _image_file_name(contact),
        str(contact.get("status") or ""),
        str(contact.get("syncStatus") or ""),
        created,
        updated,
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Sheets / Drive API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _values_get(headers: dict[str, str], spreadsheet_id: str, range_: str) -> list[list[str]]:
    url = f"{_SHEETS_API}/{spreadsheet_id}/values/{quote(range_, safe='!:')}"
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json().get("values", [])


def _values_update(
    headers: dict[str, str],
    spreadsheet_id: str,
    range_: str,
    values: list[list[str]],
) -> None:
    url = (
        f"{_SHEETS_API}/{spreadsheet_id}/values/{quote(range_, safe='!:')}"
        "?valueInputOption=RAW"
    )
    response = requests.put(url, headers=headers, json={"values": values}, timeout=20)
    response.raise_for_status()


def _values_append(
    headers: dict[str, str],
    spreadsheet_id: str,
    range_: str,
    values: list[list[str]],
) -> None:
    url = (
        f"{_SHEETS_API}/{spreadsheet_id}/values/{quote(range_, safe='!:')}:append"
        "?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    )
    response = requests.post(url, headers=headers, json={"values": values}, timeout=20)
    response.raise_for_status()


def _spreadsheet_meta(headers: dict[str, str], spreadsheet_id: str) -> dict[str, Any]:
    url = f"{_SHEETS_API}/{spreadsheet_id}?fields=sheets.properties"
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


def _list_sheet_titles(headers: dict[str, str], spreadsheet_id: str) -> list[str]:
    meta = _spreadsheet_meta(headers, spreadsheet_id)
    titles: list[str] = []
    for sheet in meta.get("sheets") or []:
        props = sheet.get("properties") or {}
        title = str(props.get("title") or "").strip()
        if title:
            titles.append(title)
    return titles


def _create_worksheet(headers: dict[str, str], spreadsheet_id: str, title: str) -> None:
    url = f"{_SHEETS_API}/{spreadsheet_id}:batchUpdate"
    response = requests.post(
        url,
        headers=headers,
        json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        timeout=20,
    )
    response.raise_for_status()


def _ensure_worksheet(
    headers: dict[str, str],
    spreadsheet_id: str,
    worksheet: str,
) -> str:
    """Ensure *worksheet* exists in the spreadsheet; return the canonical title."""
    title = _sanitize_sheet_title(worksheet)
    existing = _list_sheet_titles(headers, spreadsheet_id)
    for name in existing:
        if name.casefold() == title.casefold():
            return name
    _create_worksheet(headers, spreadsheet_id, title)
    logger.info("Google Sheets: created worksheet %r in spreadsheet %s.", title, spreadsheet_id)
    return title


def _ensure_header_row(headers: dict[str, str], spreadsheet_id: str, worksheet: str) -> None:
    existing = _values_get(headers, spreadsheet_id, f"'{worksheet}'!A1:{_LAST_COL}1")
    if not existing or not existing[0] or existing[0][0] != HEADERS[0]:
        _values_update(headers, spreadsheet_id, f"'{worksheet}'!A1:{_LAST_COL}1", [HEADERS])


def _find_row_by_contact_id(
    headers: dict[str, str],
    spreadsheet_id: str,
    worksheet: str,
    contact_id: str,
) -> int | None:
    """Return the 1-based sheet row holding this Contact ID, or None."""
    id_column = _values_get(headers, spreadsheet_id, f"'{worksheet}'!A:A")
    for index, row in enumerate(id_column, start=1):
        if row and str(row[0]).strip() == contact_id:
            return index
    return None


def _drive_find_spreadsheet(headers: dict[str, str], title: str) -> str | None:
    folder = _drive_folder_id()
    escaped = title.replace("\\", "\\\\").replace("'", "\\'")
    query_parts = [
        "mimeType='application/vnd.google-apps.spreadsheet'",
        "trashed=false",
        f"name='{escaped}'",
    ]
    if folder:
        query_parts.append(f"'{folder}' in parents")
    query = " and ".join(query_parts)
    response = requests.get(
        f"{_DRIVE_API}/files",
        headers=headers,
        params={
            "q": query,
            "spaces": "drive",
            "fields": "files(id,name)",
            "pageSize": 10,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        },
        timeout=20,
    )
    response.raise_for_status()
    files = response.json().get("files") or []
    if not files:
        return None
    return str(files[0].get("id") or "") or None


def _create_spreadsheet(headers: dict[str, str], title: str, first_sheet: str) -> str:
    """Create a new workbook titled *title* with an initial worksheet."""
    sheet_title = _sanitize_sheet_title(first_sheet)
    response = requests.post(
        _SHEETS_API,
        headers=headers,
        json={
            "properties": {"title": title},
            "sheets": [{"properties": {"title": sheet_title}}],
        },
        timeout=30,
    )
    response.raise_for_status()
    spreadsheet_id = str(response.json().get("spreadsheetId") or "")
    if not spreadsheet_id:
        raise RuntimeError("Sheets API did not return a spreadsheetId.")

    folder = _drive_folder_id()
    if folder:
        try:
            # Move into the shared folder (remove root parent when present).
            meta = requests.get(
                f"{_DRIVE_API}/files/{spreadsheet_id}",
                headers=headers,
                params={"fields": "parents", "supportsAllDrives": "true"},
                timeout=15,
            )
            meta.raise_for_status()
            parents = meta.json().get("parents") or []
            params = {
                "addParents": folder,
                "supportsAllDrives": "true",
            }
            if parents:
                params["removeParents"] = ",".join(parents)
            move = requests.patch(
                f"{_DRIVE_API}/files/{spreadsheet_id}",
                headers=headers,
                params=params,
                timeout=20,
            )
            move.raise_for_status()
        except Exception as exc:
            logger.warning(
                "Created spreadsheet %s but could not move it into folder %s: %s",
                spreadsheet_id,
                folder,
                exc,
            )

    logger.info("Google Sheets: created workbook %r (%s).", title, spreadsheet_id)
    return spreadsheet_id


def _drive_share(
    headers: dict[str, str],
    file_id: str,
    email: str,
    *,
    role: str = "writer",
) -> None:
    """Grant Drive access to *email*. Idempotent — ignores already-shared.

    role: "writer" (Editor) or "reader" (Viewer).
    """
    address = (email or "").strip().lower()
    drive_role = (role or "writer").strip().lower()
    if drive_role not in ("writer", "reader"):
        drive_role = "writer"
    if not address or not file_id:
        return
    response = requests.post(
        f"{_DRIVE_API}/files/{file_id}/permissions",
        headers=headers,
        params={
            "supportsAllDrives": "true",
            "sendNotificationEmail": "false",
        },
        json={
            "type": "user",
            "role": drive_role,
            "emailAddress": address,
        },
        timeout=20,
    )
    if response.status_code in (200, 201):
        logger.info(
            "Google Drive: shared %s as %s with %s.",
            file_id,
            drive_role,
            address,
        )
        return
    # alreadyExists / duplicate permission
    body = ""
    try:
        body = response.text or ""
    except Exception:
        body = ""
    if response.status_code == 400 and "already" in body.lower():
        return
    if response.status_code == 403 and "already" in body.lower():
        return
    try:
        err = response.json()
        reason = str((err.get("error") or {}).get("errors") or "")
        message = str((err.get("error") or {}).get("message") or "")
        if "alreadyExists" in reason or "already" in message.lower():
            return
    except Exception:
        pass
    response.raise_for_status()


def _drive_share_writer(headers: dict[str, str], file_id: str, email: str) -> None:
    """Grant Editor access to *email*. Idempotent — ignores already-shared."""
    _drive_share(headers, file_id, email, role="writer")


def _drive_share_viewer(headers: dict[str, str], file_id: str, email: str) -> None:
    """Grant Viewer (read-only) access to *email*. Idempotent."""
    _drive_share(headers, file_id, email, role="reader")


def _cache_get(key: str) -> str | None:
    with _workbook_cache_lock:
        return _workbook_cache.get(key)


def _cache_set(key: str, spreadsheet_id: str) -> None:
    with _workbook_cache_lock:
        _workbook_cache[key] = spreadsheet_id


def _load_company_sheet_meta(company_id: str) -> dict[str, Any] | None:
    """Return company_name, google_sheet_id, admin_email, user_emails for a company."""
    from db.pool import db_cursor

    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT c.company_name,
                   c.google_sheet_id,
                   a.email AS admin_email
            FROM companies c
            LEFT JOIN users a ON a.id = c.admin_id AND a.deleted_at IS NULL
            WHERE c.id = %s
            """,
            (company_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        cur.execute(
            """
            SELECT u.email
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE u.company_id = %s
              AND r.name = 'USER'
              AND u.deleted_at IS NULL
              AND u.is_active = TRUE
            """,
            (company_id,),
        )
        user_rows = cur.fetchall() or []

    user_emails: list[str] = []
    for urec in user_rows:
        email = str(urec.get("email") or "").strip().lower()
        if email and email not in user_emails:
            user_emails.append(email)

    return {
        "company_name": str(row.get("company_name") or "").strip() or "Company",
        "google_sheet_id": str(row.get("google_sheet_id") or "").strip() or None,
        "admin_email": str(row.get("admin_email") or "").strip().lower() or None,
        "user_emails": user_emails,
    }


def _persist_company_sheet_id(company_id: str, spreadsheet_id: str) -> None:
    from db.pool import db_cursor

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE companies
            SET google_sheet_id = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (spreadsheet_id, company_id),
        )


def _load_superadmin_sheet_meta() -> dict[str, Any] | None:
    """Return id, email, google_sheet_id for the platform Super Admin user."""
    from db.pool import db_cursor

    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.id, u.email, u.google_sheet_id
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE r.name = 'SUPER_ADMIN'
              AND u.deleted_at IS NULL
            ORDER BY u.created_at ASC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": str(row.get("id") or ""),
        "email": str(row.get("email") or "").strip().lower() or None,
        "google_sheet_id": str(row.get("google_sheet_id") or "").strip() or None,
    }


def _persist_user_sheet_id(user_id: str, spreadsheet_id: str) -> None:
    from db.pool import db_cursor

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE users
            SET google_sheet_id = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (spreadsheet_id, user_id),
        )


def _spreadsheet_reachable(headers: dict[str, str], spreadsheet_id: str) -> bool:
    try:
        _spreadsheet_meta(headers, spreadsheet_id)
        return True
    except Exception as exc:
        logger.warning(
            "Google Sheets: stored spreadsheet %s is not reachable: %s",
            spreadsheet_id,
            exc,
        )
        return False


def ensure_company_sheet(company_id: str, *, first_sheet: str | None = None) -> str:
    """Ensure the Admin/company workbook exists, is shared, and return its id.

    Creates the spreadsheet in the Admin's Google Drive via OAuth when missing
    (free — uses Admin storage). Then shares:
      - service account = Editor (so sync can write)
      - Super Admin = Editor
      - company Users = Viewer
    Admin already owns the file.
    """
    company_id = (company_id or "").strip()
    if not company_id:
        raise RuntimeError("ensure_company_sheet requires a company_id.")

    cache_key = f"company:{company_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    sa_auth = _auth_headers()
    if not sa_auth:
        raise RuntimeError("Google Sheets auth failed (no service-account access token).")

    meta = _load_company_sheet_meta(company_id)
    if not meta:
        raise RuntimeError(f"Company {company_id} not found.")

    spreadsheet_id = meta.get("google_sheet_id")
    if spreadsheet_id and _spreadsheet_reachable(sa_auth, str(spreadsheet_id)):
        spreadsheet_id = str(spreadsheet_id)
    else:
        # Create in Admin's Drive via OAuth (avoids SA storageQuotaExceeded).
        from services import google_oauth_service as oauth

        admin_oauth = oauth.load_company_admin_oauth(company_id)
        if not admin_oauth or not admin_oauth.get("refresh_token"):
            raise RuntimeError(
                "Admin has not connected Google Drive. "
                "Ask the Admin to open Settings → Connect Google Drive."
            )
        access = oauth.refresh_access_token(str(admin_oauth["refresh_token"]))
        title = f"{meta['company_name']} — Contacts"
        spreadsheet_id = oauth.create_spreadsheet_with_oauth(
            access, title, first_sheet or _sheet_name()
        )
        _persist_company_sheet_id(company_id, spreadsheet_id)

        # Share service account so background sync can write.
        sa_email = oauth.service_account_client_email()
        if sa_email:
            try:
                _drive_share_writer(
                    oauth.oauth_auth_headers(access), spreadsheet_id, sa_email
                )
            except Exception as exc:
                logger.warning(
                    "Could not share new sheet %s with service account %s: %s",
                    spreadsheet_id,
                    sa_email,
                    exc,
                )

    # Share: Super Admin = Editor; company Users = Viewer (Admin already owns).
    editor_emails: list[str] = []
    sa_email_env = _superadmin_share_email()
    if sa_email_env:
        editor_emails.append(sa_email_env)
    # Prefer Admin OAuth token for sharing when available; else SA token.
    share_headers = sa_auth
    try:
        from services import google_oauth_service as oauth

        admin_oauth = oauth.load_company_admin_oauth(company_id)
        if admin_oauth and admin_oauth.get("refresh_token"):
            access = oauth.refresh_access_token(str(admin_oauth["refresh_token"]))
            share_headers = oauth.oauth_auth_headers(access)
    except Exception as exc:
        logger.warning("Using service-account token for shares: %s", exc)

    for email in editor_emails:
        # Don't need to share Admin with themselves (owner).
        if meta.get("admin_email") and email == meta.get("admin_email"):
            continue
        try:
            _drive_share_writer(share_headers, spreadsheet_id, email)
        except Exception as exc:
            logger.warning(
                "Google Drive: could not share company sheet %s with editor %s: %s",
                spreadsheet_id,
                email,
                exc,
            )

    viewer_emails = [
        e for e in (meta.get("user_emails") or [])
        if e and e not in editor_emails and e != meta.get("admin_email")
    ]
    for email in viewer_emails:
        try:
            _drive_share_viewer(share_headers, spreadsheet_id, email)
        except Exception as exc:
            logger.warning(
                "Google Drive: could not share company sheet %s with viewer %s: %s",
                spreadsheet_id,
                email,
                exc,
            )

    _cache_set(cache_key, spreadsheet_id)
    return spreadsheet_id


def ensure_superadmin_sheet(*, first_sheet: str | None = None) -> str:
    """Ensure the Super Admin workbook exists (created via Super Admin OAuth)."""
    cache_key = "superadmin"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    sa_auth = _auth_headers()
    if not sa_auth:
        raise RuntimeError("Google Sheets auth failed (no service-account access token).")

    meta = _load_superadmin_sheet_meta()
    spreadsheet_id = meta.get("google_sheet_id") if meta else None
    if spreadsheet_id and _spreadsheet_reachable(sa_auth, str(spreadsheet_id)):
        spreadsheet_id = str(spreadsheet_id)
    else:
        from services import google_oauth_service as oauth

        user_id = str((meta or {}).get("id") or "")
        refresh = oauth.load_user_refresh_token(user_id) if user_id else None
        if not refresh:
            raise RuntimeError(
                "Super Admin has not connected Google Drive. "
                "Open Settings → Connect Google Drive."
            )
        access = oauth.refresh_access_token(refresh)
        spreadsheet_id = oauth.create_spreadsheet_with_oauth(
            access, "Super Admin — Contacts", first_sheet or _sheet_name()
        )
        if user_id:
            _persist_user_sheet_id(user_id, spreadsheet_id)
        sa_email = oauth.service_account_client_email()
        if sa_email:
            try:
                _drive_share_writer(
                    oauth.oauth_auth_headers(access), spreadsheet_id, sa_email
                )
            except Exception as exc:
                logger.warning(
                    "Could not share Super Admin sheet with service account: %s",
                    exc,
                )

    _cache_set(cache_key, spreadsheet_id)
    return spreadsheet_id


def fire_ensure_company_sheet(company_id: str) -> None:
    """Fire-and-forget company sheet create/share (Admin or User invite accept)."""
    if not is_sheets_configured() or not company_id:
        return

    def _run() -> None:
        try:
            # Clear cache so newly accepted Users are included in viewer shares.
            with _workbook_cache_lock:
                _workbook_cache.pop(f"company:{company_id}", None)
            ensure_company_sheet(company_id)
        except Exception as exc:
            logger.warning(
                "Google Sheets: ensure_company_sheet failed for %s: %s",
                company_id,
                exc,
            )

    threading.Thread(target=_run, daemon=True).start()


def _resolve_workbook_id(
    headers: dict[str, str],
    contact: dict[str, Any],
    event_day: str,
) -> str:
    """Resolve the role-based workbook for this contact (company or Super Admin)."""
    role = str(contact.get("created_by_role") or "").strip().upper()
    company_id = str(
        contact.get("owner_company_id") or contact.get("company_id") or ""
    ).strip()

    if role == "SUPER_ADMIN" or (not company_id and role not in ("ADMIN", "USER")):
        try:
            return ensure_superadmin_sheet(first_sheet=event_day)
        except Exception as exc:
            fallback = _sheet_id()
            if fallback:
                logger.warning(
                    "Super Admin sheet ensure failed (%s); using GOOGLE_SHEET_ID.",
                    exc,
                )
                return fallback
            raise

    if company_id:
        try:
            return ensure_company_sheet(company_id, first_sheet=event_day)
        except Exception as exc:
            fallback = _sheet_id()
            if fallback:
                logger.warning(
                    "Company sheet ensure failed for %s (%s); using GOOGLE_SHEET_ID.",
                    company_id,
                    exc,
                )
                return fallback
            raise

    fallback = _sheet_id()
    if fallback:
        return fallback
    raise RuntimeError(
        "Cannot resolve Google Sheet: contact has no company and "
        "GOOGLE_SHEET_ID is not configured."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sync entry points
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_row(contact: dict[str, Any], extras: dict[str, Any] | None) -> None:
    auth = _auth_headers()
    if not auth:
        raise RuntimeError("Google Sheets auth failed (no access token).")

    event_day = _sanitize_sheet_title(
        str(contact.get("eventDay") or "").strip() or _sheet_name(),
        fallback=_sheet_name(),
    )

    spreadsheet_id = _resolve_workbook_id(auth, contact, event_day)
    worksheet = _ensure_worksheet(auth, spreadsheet_id, event_day)
    _ensure_header_row(auth, spreadsheet_id, worksheet)

    contact_id = str(contact.get("id") or "")
    company_label = str(
        contact.get("owner_company_id") or contact.get("company_id") or spreadsheet_id
    )
    row = contact_to_row(contact, extras)
    existing_row = _find_row_by_contact_id(auth, spreadsheet_id, worksheet, contact_id)
    if existing_row:
        _values_update(
            auth,
            spreadsheet_id,
            f"'{worksheet}'!A{existing_row}:{_LAST_COL}{existing_row}",
            [row],
        )
        logger.info(
            "Google Sheets: updated row %s for contact %s in %r / %r.",
            existing_row,
            contact_id,
            company_label,
            worksheet,
        )
    else:
        _values_append(auth, spreadsheet_id, f"'{worksheet}'!A:{_LAST_COL}", [row])
        logger.info(
            "Google Sheets: appended row for contact %s in %r / %r.",
            contact_id,
            company_label,
            worksheet,
        )


def sync_contact_to_sheet(
    contact: dict[str, Any],
    extras: dict[str, Any] | None = None,
) -> bool:
    """Upsert one contact into the sheet. Returns True on success.

    Never raises — Sheets is a secondary layer and must not affect saves.
    """
    if not is_sheets_configured():
        logger.debug("Google Sheets sync skipped: not configured.")
        return False

    contact_id = str(contact.get("id") or "")
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            _upsert_row(contact, extras)
            with _pending_lock:
                _pending_retry.pop(contact_id, None)
            return True
        except Exception as exc:
            last_error = exc
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)])

    logger.error(
        "Google Sheets sync failed for contact %s after %s attempts: %s "
        "(contact is safe in PostgreSQL; queued for retry on next sync).",
        contact_id,
        _MAX_ATTEMPTS,
        last_error,
    )
    with _pending_lock:
        _pending_retry[contact_id] = {"extras": extras or {}}
    return False


def sync_contact_by_id(contact_id: str, extras: dict[str, Any] | None = None) -> bool:
    """Fetch the committed contact from PostgreSQL and upsert it into the sheet."""
    if not is_sheets_configured():
        return False
    from services import contact_storage as storage

    contact = storage.get_contact(contact_id)
    if not contact:
        logger.warning("Google Sheets sync skipped: contact %s not found in PostgreSQL.", contact_id)
        return False

    ok = sync_contact_to_sheet(contact, extras)
    if ok:
        _drain_pending_retries(exclude_id=contact_id)
    return ok


def _drain_pending_retries(exclude_id: str | None = None) -> None:
    with _pending_lock:
        pending = {cid: meta for cid, meta in _pending_retry.items() if cid != exclude_id}
    if not pending:
        return
    from services import contact_storage as storage

    for cid, meta in pending.items():
        contact = storage.get_contact(cid)
        if contact:
            sync_contact_to_sheet(contact, meta.get("extras"))
        else:
            with _pending_lock:
                _pending_retry.pop(cid, None)


# Strong references so fire-and-forget tasks are not garbage-collected mid-run.
_background_tasks: set[asyncio.Task] = set()


def fire_sheets_sync(contact_id: str, extras: dict[str, Any] | None = None) -> None:
    """Fire-and-forget sheet sync after a successful PostgreSQL commit.

    Runs in a worker thread via asyncio so the API response is never blocked.
    """
    if not is_sheets_configured() or not contact_id:
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(sync_contact_by_id, contact_id, extras)
        except Exception as exc:
            logger.error("Google Sheets background sync crashed for %s: %s", contact_id, exc)

    try:
        asyncio.get_running_loop()
        task = asyncio.create_task(_run())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        # No running loop (sync context / tests) — run inline but still guarded.
        threading.Thread(
            target=sync_contact_by_id, args=(contact_id, extras), daemon=True
        ).start()
