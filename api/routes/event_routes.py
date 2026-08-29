"""Managed event CRUD — Super Admin only."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.schemas import CreateManagedEventRequest, UpdateManagedEventRequest
from auth import audit_service
from auth.constants import (
    AUDIT_EVENT_CREATED,
    AUDIT_EVENT_DELETED,
    AUDIT_EVENT_UPDATED,
    ROLE_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_USER,
)
from auth.dependencies import get_current_user, require_role
from db.pool import db_cursor

router = APIRouter(prefix="/api/events", tags=["Events"])
logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALLOWED_STATUS = {"active", "inactive", "completed"}

_EVENT_COLUMNS = """
    id, name, description, location, start_date, end_date, status,
    created_by, updated_by, company_id, created_at, updated_at,
    spreadsheet_id, spreadsheet_url, google_sheet_id, google_sheet_url
"""


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None or value.strip() == "":
        return None
    raw = value.strip()
    if not _DATE_RE.match(raw):
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD.")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field}.") from exc


def _serialize_event(row: dict) -> dict:
    out = dict(row)
    for key in ("id", "created_by", "updated_by", "company_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("start_date", "end_date"):
        if out.get(key) and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    for key in ("created_at", "updated_at", "deleted_at"):
        if out.get(key) and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    for key in ("spreadsheet_id", "spreadsheet_url", "google_sheet_id", "google_sheet_url"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    return out


def _validate_date_range(start: date | None, end: date | None) -> None:
    if start and end and end < start:
        raise HTTPException(status_code=422, detail="end_date cannot be before start_date.")


def _tenant_company_id(user: dict) -> str | None:
    if user.get("role") == ROLE_SUPER_ADMIN:
        return None
    raw = user.get("company_id")
    return str(raw) if raw else None


def _event_scope_sql(user: dict) -> tuple[str, list]:
    """Tenant filter: SuperAdmin sees only SuperAdmin-owned events (company_id IS NULL)."""
    if user.get("role") == ROLE_SUPER_ADMIN:
        return "company_id IS NULL", []
    company_id = _tenant_company_id(user)
    if company_id:
        return "company_id = %s", [company_id]
    return "created_by = %s", [user["id"]]


def _event_day_names(
    body: CreateManagedEventRequest,
    start: date | None,
    end: date | None,
) -> list[str]:
    names = [str(day).strip() for day in (getattr(body, "days", None) or []) if str(day).strip()]
    if names:
        return names
    if start and end:
        span = (end - start).days + 1
        span = max(1, min(span, 60))
        return [f"Day {i}" for i in range(1, span + 1)]
    return ["Day 1"]


def _replace_event_days(cur, event_id: str, day_names: list[str]) -> None:
    cur.execute("DELETE FROM event_days WHERE event_id = %s", (event_id,))
    titles = day_names or ["Day 1"]
    for index, name in enumerate(titles):
        cur.execute(
            """
            INSERT INTO event_days (event_id, name, sort_order)
            VALUES (%s, %s, %s)
            """,
            (event_id, name[:100], index),
        )


def _load_event_day_names(event_id: str, row: dict | None = None) -> list[str]:
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT name FROM event_days WHERE event_id = %s ORDER BY sort_order",
            (event_id,),
        )
        rows = cur.fetchall() or []
    names = [str(item.get("name") or "").strip() for item in rows]
    names = [name for name in names if name]
    if names:
        return names
    if row:
        start = row.get("start_date")
        end = row.get("end_date")
        if start and end:
            try:
                start_d = start if isinstance(start, date) else date.fromisoformat(str(start)[:10])
                end_d = end if isinstance(end, date) else date.fromisoformat(str(end)[:10])
                span = (end_d - start_d).days + 1
                span = max(1, min(span, 60))
                return [f"Day {i}" for i in range(1, span + 1)]
            except Exception:
                pass
    return ["Day 1"]


def _reload_event(event_id: str) -> dict | None:
    with db_cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT {_EVENT_COLUMNS}
            FROM managed_events
            WHERE id = %s AND deleted_at IS NULL
            """,
            (event_id,),
        )
        loaded = cur.fetchone()
    return dict(loaded) if loaded else None


def _event_workbook_missing(row: dict) -> bool:
    sheet_id = str(row.get("spreadsheet_id") or row.get("google_sheet_id") or "").strip()
    sheet_url = str(row.get("spreadsheet_url") or row.get("google_sheet_url") or "").strip()
    return not sheet_id and not sheet_url


def _create_and_save_event_workbook(
    *,
    event_id: str,
    event_name: str,
    company_id: str | None,
    user_id: str,
    day_titles: list[str] | None = None,
    reuse_existing: bool = False,
) -> None:
    from services.google_sheets_automation import automate_event_sheets

    logger.info("[GSHEET] reuse_existing=%s", reuse_existing)
    titles = day_titles or _load_event_day_names(event_id)
    logger.info("[GSHEET] days=%s", titles)
    automate_event_sheets(
        event_id=event_id,
        event_name=event_name,
        company_id=company_id,
        user_id=user_id,
        day_titles=titles,
        reuse_existing=reuse_existing,
    )


def _ensure_event_workbook_if_missing(row: dict, user: dict) -> dict:
    """Create an event-specific workbook only when this event has no sheet metadata."""
    if not _event_workbook_missing(row):
        return row
    event_id = str(row.get("id") or "").strip()
    if not event_id:
        return row
    company_id = row.get("company_id")
    company_id = str(company_id) if company_id else None
    try:
        _create_and_save_event_workbook(
            event_id=event_id,
            event_name=str(row.get("name") or ""),
            company_id=company_id,
            user_id=str(user.get("id") or ""),
            day_titles=_load_event_day_names(event_id, row),
            reuse_existing=False,
        )
        refreshed = _reload_event(event_id)
        if refreshed:
            return refreshed
    except Exception:
        logger.exception("[GSHEET] ensure workbook failed for event_id=%s", event_id)
    return row


def _deactivate_other_active_events(cur, keep_id: str, now: datetime, company_id: str | None) -> None:
    """Ensure only one managed event is active per tenant (or SuperAdmin-owned set)."""
    if company_id:
        cur.execute(
            """
            UPDATE managed_events
            SET status = 'inactive', updated_at = %s
            WHERE deleted_at IS NULL
              AND status = 'active'
              AND id <> %s
              AND company_id = %s
            """,
            (now, keep_id, company_id),
        )
        return
    cur.execute(
        """
        UPDATE managed_events
        SET status = 'inactive', updated_at = %s
        WHERE deleted_at IS NULL
          AND status = 'active'
          AND id <> %s
          AND company_id IS NULL
        """,
        (now, keep_id),
    )


@router.get(
    "",
    summary="List managed events for the caller's company (SuperAdmin: own events only)",
)
def list_events(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    q: str = Query("", max_length=200),
    status: str = Query("", max_length=32),
    user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER)),
):
    del request
    offset = (page - 1) * limit
    scope_sql, scope_params = _event_scope_sql(user)
    clauses = ["deleted_at IS NULL", scope_sql]
    params: list = [*scope_params]

    if q.strip():
        clauses.append("(name ILIKE %s OR description ILIKE %s OR location ILIKE %s)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like])
    if status.strip():
        if status.strip() not in _ALLOWED_STATUS:
            raise HTTPException(status_code=422, detail="Invalid status filter.")
        clauses.append("status = %s")
        params.append(status.strip())

    where = " AND ".join(clauses)
    with db_cursor(commit=False) as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM managed_events WHERE {where}", params)
        total = cur.fetchone()["total"]
        cur.execute(
            f"""
            SELECT {_EVENT_COLUMNS}
            FROM managed_events
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        rows = cur.fetchall()

    return {
        "items": [_serialize_event(dict(row)) for row in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get(
    "/active",
    summary="Get the currently active managed event for this company",
)
def get_active_event(user: dict = Depends(get_current_user)):
    """Return the tenant-active event used to tag scans on Extraction."""
    scope_sql, scope_params = _event_scope_sql(user)
    with db_cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT {_EVENT_COLUMNS}
            FROM managed_events
            WHERE deleted_at IS NULL AND status = 'active' AND {scope_sql}
            ORDER BY updated_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """,
            scope_params,
        )
        row = cur.fetchone()
    if not row:
        return {"event": None}
    event_row = _ensure_event_workbook_if_missing(dict(row), user)
    return {"event": _serialize_event(event_row)}


@router.get(
    "/{event_id}",
    summary="Get managed event",
)
def get_event(
    event_id: str,
    user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER)),
):
    scope_sql, scope_params = _event_scope_sql(user)
    with db_cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT {_EVENT_COLUMNS}
            FROM managed_events
            WHERE id = %s AND deleted_at IS NULL AND {scope_sql}
            """,
            (event_id, *scope_params),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found.")
    return _serialize_event(dict(row))


@router.post(
    "",
    summary="Create managed event",
)
def create_event(
    body: CreateManagedEventRequest,
    request: Request,
    user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER)),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Event name is required.")

    start = _parse_date(body.start_date, "start_date")
    end = _parse_date(body.end_date, "end_date")
    _validate_date_range(start, end)
    status = (body.status or "active").strip().lower()
    if status not in _ALLOWED_STATUS:
        raise HTTPException(status_code=422, detail="Invalid status.")

    company_id = _tenant_company_id(user)
    now = datetime.now(timezone.utc)
    day_names = _event_day_names(body, start, end)
    with db_cursor() as cur:
        if company_id:
            cur.execute(
                """
                SELECT 1 FROM managed_events
                WHERE deleted_at IS NULL AND company_id = %s AND LOWER(name) = LOWER(%s)
                """,
                (company_id, name),
            )
        else:
            cur.execute(
                """
                SELECT 1 FROM managed_events
                WHERE deleted_at IS NULL AND company_id IS NULL AND LOWER(name) = LOWER(%s)
                """,
                (name,),
            )
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="An event with this name already exists.")

        cur.execute(
            """
            INSERT INTO managed_events (
                name, description, location, start_date, end_date, status,
                created_by, updated_by, company_id, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING """ + _EVENT_COLUMNS + """
            """,
            (
                name,
                (body.description or "").strip(),
                (body.location or "").strip(),
                start,
                end,
                status,
                user["id"],
                user["id"],
                company_id,
                now,
                now,
            ),
        )
        row = cur.fetchone()
        event_id = str(row["id"])
        _replace_event_days(cur, event_id, day_names)
        if status == "active" and row:
            _deactivate_other_active_events(cur, event_id, now, company_id)

    logger.info("[GSHEET] event creation started: event_id=%s", event_id)
    logger.info("[GSHEET] create_google_workbook=True")
    try:
        _create_and_save_event_workbook(
            event_id=event_id,
            event_name=name,
            company_id=company_id,
            user_id=str(user["id"]),
            day_titles=day_names,
            reuse_existing=False,
        )
        refreshed = _reload_event(event_id)
        if refreshed:
            row = refreshed
    except Exception:
        logger.exception("[GSHEET] creation failed:")

    audit_service.log_action(
        user["id"],
        AUDIT_EVENT_CREATED,
        ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
        new_value={"event_id": event_id, "name": name, "company_id": company_id},
    )
    logger.info("Managed event created: %s by %s", event_id, user["id"])
    return _serialize_event(dict(row))


@router.put(
    "/{event_id}",
    summary="Update managed event",
)
def update_event(
    event_id: str,
    body: UpdateManagedEventRequest,
    request: Request,
    user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN)),
):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update.")

    scope_sql, scope_params = _event_scope_sql(user)
    with db_cursor(commit=False) as cur:
        cur.execute(
            f"SELECT * FROM managed_events WHERE id = %s AND deleted_at IS NULL AND {scope_sql}",
            (event_id, *scope_params),
        )
        existing = cur.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Event not found.")
    existing = dict(existing)
    company_id = existing.get("company_id")
    company_id = str(company_id) if company_id else None

    name = updates.get("name", existing["name"])
    if isinstance(name, str):
        name = name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Event name is required.")
        updates["name"] = name

    start = existing.get("start_date")
    end = existing.get("end_date")
    if "start_date" in updates:
        start = _parse_date(updates["start_date"], "start_date")
        updates["start_date"] = start
    if "end_date" in updates:
        end = _parse_date(updates["end_date"], "end_date")
        updates["end_date"] = end
    _validate_date_range(start, end)

    if "status" in updates:
        status = str(updates["status"]).strip().lower()
        if status not in _ALLOWED_STATUS:
            raise HTTPException(status_code=422, detail="Invalid status.")
        updates["status"] = status

    for text_key in ("description", "location"):
        if text_key in updates and isinstance(updates[text_key], str):
            updates[text_key] = updates[text_key].strip()

    updates["updated_by"] = user["id"]
    updates["updated_at"] = datetime.now(timezone.utc)

    set_parts = []
    params: list = []
    for col, val in updates.items():
        set_parts.append(f"{col} = %s")
        params.append(val)
    params.append(event_id)

    with db_cursor() as cur:
        if "name" in updates:
            if company_id:
                cur.execute(
                    """
                    SELECT 1 FROM managed_events
                    WHERE deleted_at IS NULL AND LOWER(name) = LOWER(%s) AND id <> %s AND company_id = %s
                    """,
                    (updates["name"], event_id, company_id),
                )
            else:
                cur.execute(
                    """
                    SELECT 1 FROM managed_events
                    WHERE deleted_at IS NULL AND LOWER(name) = LOWER(%s) AND id <> %s AND company_id IS NULL
                    """,
                    (updates["name"], event_id),
                )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="An event with this name already exists.")

        cur.execute(
            f"""
            UPDATE managed_events
            SET {', '.join(set_parts)}
            WHERE id = %s AND deleted_at IS NULL
            RETURNING {_EVENT_COLUMNS}
            """,
            params,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Event not found.")
        if str(row.get("status") or "").lower() == "active":
            _deactivate_other_active_events(cur, str(row["id"]), updates["updated_at"], company_id)

    audit_service.log_action(
        user["id"],
        AUDIT_EVENT_UPDATED,
        ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
        new_value={"event_id": event_id, "fields": list(updates.keys())},
    )
    return _serialize_event(dict(row))


@router.delete(
    "/{event_id}",
    summary="Soft delete managed event",
)
def delete_event(
    event_id: str,
    request: Request,
    user: dict = Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN)),
):
    now = datetime.now(timezone.utc)
    scope_sql, scope_params = _event_scope_sql(user)
    with db_cursor() as cur:
        cur.execute(
            f"""
            UPDATE managed_events
            SET deleted_at = %s, updated_at = %s, updated_by = %s, status = 'inactive'
            WHERE id = %s AND deleted_at IS NULL AND {scope_sql}
            RETURNING id, name
            """,
            (now, now, user["id"], event_id, *scope_params),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found.")

    audit_service.log_action(
        user["id"],
        AUDIT_EVENT_DELETED,
        ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
        new_value={"event_id": event_id, "name": row["name"]},
    )
    return {"success": True, "message": "Event deleted.", "id": str(row["id"])}
