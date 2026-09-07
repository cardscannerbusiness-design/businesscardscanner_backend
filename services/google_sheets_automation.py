"""Per-event Google Sheets workbook creation (new managed events).

Contact sync is handled separately in google_sheets_service and is not modified
here. This module only creates a brand-new workbook for a managed event id.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from db.pool import db_cursor
from services import google_oauth_service as oauth
from services.google_sheets_service import (
    _drive_share_viewer,
    _drive_share_writer,
    _ensure_header_row,
    _ensure_worksheet,
    _load_company_sheet_meta,
    _sanitize_sheet_title,
    _superadmin_share_email,
)

logger = logging.getLogger(__name__)

_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"


def _unique_day_titles(day_titles: list[str] | None) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in day_titles or []:
        title = _sanitize_sheet_title(str(raw or ""))
        key = title.casefold()
        count = seen.get(key, 0)
        if count:
            title = _sanitize_sheet_title(f"{title} ({count + 1})")
        seen[key] = count + 1
        out.append(title)
    return out or ["Day 1"]


class _SheetsCreateCall:
    def __init__(self, access_token: str, body: dict[str, Any]):
        self._access_token = access_token
        self._body = body

    def execute(self) -> dict[str, Any]:
        response = requests.post(
            _SHEETS_API,
            headers=oauth.oauth_auth_headers(self._access_token),
            json=self._body,
            timeout=30,
        )
        if response.status_code >= 400:
            logger.error("OAuth spreadsheet create failed: %s", response.text[:500])
            raise oauth.GoogleOAuthError(
                "SHEET_CREATE_FAILED",
                "Could not create Google Sheet in your Drive.",
                502,
            )
        return response.json()


class _SpreadsheetsResource:
    def __init__(self, access_token: str):
        self._access_token = access_token

    def create(self, body: dict[str, Any] | None = None, **_kwargs: Any) -> _SheetsCreateCall:
        return _SheetsCreateCall(self._access_token, body or {})


class _SheetsV4Service:
    def __init__(self, access_token: str):
        self._access_token = access_token

    def spreadsheets(self) -> _SpreadsheetsResource:
        return _SpreadsheetsResource(self._access_token)


def _sheets_v4_service(access_token: str) -> _SheetsV4Service:
    """Sheets API client authenticated with a user OAuth access token."""
    return _SheetsV4Service(access_token)


def _create_fresh_workbook(
    access_token: str,
    event_name: str,
    day_titles: list[str],
) -> tuple[str, str, list[str]]:
    """Create a new spreadsheet with one tab per event day. Never reuses an existing file."""
    logger.info("[GSHEET] calling fresh workbook creation")
    titles = _unique_day_titles(day_titles)
    name = str(event_name or "").strip() or "Event"
    body = {
        "properties": {
            "title": f"NCS-{name}",
        },
        "sheets": [
            {"properties": {"title": title, "index": i}}
            for i, title in enumerate(titles)
        ],
    }
    service = _sheets_v4_service(access_token)
    response = service.spreadsheets().create(body=body).execute()
    spreadsheet_id = str(response.get("spreadsheetId") or "")
    if not spreadsheet_id:
        raise oauth.GoogleOAuthError("SHEET_CREATE_FAILED", "No spreadsheetId returned.", 502)
    spreadsheet_url = str(
        response.get("spreadsheetUrl")
        or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    )
    logger.info("[GSHEET] Google response spreadsheet_id=%s", spreadsheet_id)
    return spreadsheet_id, spreadsheet_url, titles


def _share_event_workbook(
    headers: dict[str, str],
    spreadsheet_id: str,
    company_id: str | None,
) -> None:
    """Share with the service account (editor) and company users (existing helpers)."""
    sa_email = oauth.service_account_client_email()
    if sa_email:
        try:
            _drive_share_writer(headers, spreadsheet_id, sa_email)
        except Exception as exc:
            logger.warning(
                "[GSHEET] could not share event workbook %s with service account %s: %s",
                spreadsheet_id,
                sa_email,
                exc,
            )

    editor_emails: list[str] = []
    superadmin = _superadmin_share_email()
    if superadmin:
        editor_emails.append(superadmin)

    admin_email: str | None = None
    viewer_emails: list[str] = []
    cid = str(company_id or "").strip()
    if cid:
        try:
            meta = _load_company_sheet_meta(cid) or {}
            admin_email = str(meta.get("admin_email") or "").strip().lower() or None
            viewer_emails = list(meta.get("user_emails") or [])
        except Exception as exc:
            logger.warning("[GSHEET] could not load company share list: %s", exc)

    for email in editor_emails:
        if admin_email and email == admin_email:
            continue
        try:
            _drive_share_writer(headers, spreadsheet_id, email)
        except Exception as exc:
            logger.warning(
                "[GSHEET] could not share event workbook %s with editor %s: %s",
                spreadsheet_id,
                email,
                exc,
            )

    skip = set(editor_emails)
    if admin_email:
        skip.add(admin_email)
    for email in viewer_emails:
        if not email or email in skip:
            continue
        try:
            _drive_share_viewer(headers, spreadsheet_id, email)
        except Exception as exc:
            logger.warning(
                "[GSHEET] could not share event workbook %s with viewer %s: %s",
                spreadsheet_id,
                email,
                exc,
            )


def _persist_event_workbook(
    event_id: str,
    spreadsheet_id: str,
    spreadsheet_url: str,
) -> None:
    logger.info("[GSHEET] saving workbook metadata for event_id=%s", event_id)
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE managed_events
            SET spreadsheet_id = %s,
                spreadsheet_url = %s,
                google_sheet_id = %s,
                google_sheet_url = %s,
                updated_at = NOW()
            WHERE id = %s
              AND deleted_at IS NULL
            """,
            (
                spreadsheet_id,
                spreadsheet_url,
                spreadsheet_id,
                spreadsheet_url,
                event_id,
            ),
        )
    logger.info("[GSHEET] workbook metadata saved")


def automate_event_sheets(
    *,
    event_id: str,
    event_name: str,
    company_id: str | None,
    user_id: str,
    day_titles: list[str],
    reuse_existing: bool = False,
) -> dict[str, Any]:
    """Create a new per-event workbook and persist ids on *event_id*.

    ``reuse_existing=False`` (new events) never reads or passes an existing
    spreadsheet_id / google_sheet_id — a fresh workbook is always created.
    """
    logger.info("[GSHEET] reuse_existing=%s", reuse_existing)
    if reuse_existing:
        raise RuntimeError(
            "automate_event_sheets reuse_existing=True is not used for new events."
        )

    access = oauth._oauth_access_token(company_id=company_id, user_id=user_id)
    spreadsheet_id, spreadsheet_url, titles = _create_fresh_workbook(
        access, event_name, day_titles
    )
    headers = oauth.oauth_auth_headers(access)
    for title in titles:
        try:
            worksheet = _ensure_worksheet(headers, spreadsheet_id, title)
            _ensure_header_row(headers, spreadsheet_id, worksheet)
        except Exception:
            logger.exception("[GSHEET] header row failed for tab %r", title)

    _share_event_workbook(headers, spreadsheet_id, company_id)
    _persist_event_workbook(event_id, spreadsheet_id, spreadsheet_url)
    return {
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": spreadsheet_url,
        "google_sheet_id": spreadsheet_id,
        "google_sheet_url": spreadsheet_url,
    }
