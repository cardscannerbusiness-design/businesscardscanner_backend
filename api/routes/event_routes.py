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
    ROLE_SUPER_ADMIN,
)
from auth.dependencies import get_current_user, require_role
from db.pool import db_cursor

router = APIRouter(prefix="/api/events", tags=["Events"])
logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALLOWED_STATUS = {"active", "inactive", "completed"}


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
    for key in ("id", "created_by", "updated_by"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("start_date", "end_date"):
        if out.get(key) and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    for key in ("created_at", "updated_at", "deleted_at"):
        if out.get(key) and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    return out


def _validate_date_range(start: date | None, end: date | None) -> None:
    if start and end and end < start:
        raise HTTPException(status_code=422, detail="end_date cannot be before start_date.")


@router.get(
    "",
    summary="List managed events",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def list_events(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    q: str = Query("", max_length=200),
    status: str = Query("", max_length=32),
):
    offset = (page - 1) * limit
    clauses = ["deleted_at IS NULL"]
    params: list = []

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
            SELECT id, name, description, location, start_date, end_date, status,
                   created_by, updated_by, created_at, updated_at
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
    "/{event_id}",
    summary="Get managed event",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def get_event(event_id: str):
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, name, description, location, start_date, end_date, status,
                   created_by, updated_by, created_at, updated_at
            FROM managed_events
            WHERE id = %s AND deleted_at IS NULL
            """,
            (event_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found.")
    return _serialize_event(dict(row))


@router.post(
    "",
    summary="Create managed event",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def create_event(body: CreateManagedEventRequest, request: Request):
    user = get_current_user(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Event name is required.")

    start = _parse_date(body.start_date, "start_date")
    end = _parse_date(body.end_date, "end_date")
    _validate_date_range(start, end)
    status = (body.status or "active").strip().lower()
    if status not in _ALLOWED_STATUS:
        raise HTTPException(status_code=422, detail="Invalid status.")

    now = datetime.now(timezone.utc)
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM managed_events
            WHERE deleted_at IS NULL AND LOWER(name) = LOWER(%s)
            """,
            (name,),
        )
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="An event with this name already exists.")

        cur.execute(
            """
            INSERT INTO managed_events (
                name, description, location, start_date, end_date, status,
                created_by, updated_by, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, name, description, location, start_date, end_date, status,
                      created_by, updated_by, created_at, updated_at
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
                now,
                now,
            ),
        )
        row = cur.fetchone()

    audit_service.log_action(
        user["id"],
        AUDIT_EVENT_CREATED,
        ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
        new_value={"event_id": str(row["id"]), "name": name},
    )
    logger.info("Managed event created: %s by %s", row["id"], user["id"])
    return _serialize_event(dict(row))


@router.put(
    "/{event_id}",
    summary="Update managed event",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def update_event(event_id: str, body: UpdateManagedEventRequest, request: Request):
    user = get_current_user(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update.")

    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM managed_events WHERE id = %s AND deleted_at IS NULL",
            (event_id,),
        )
        existing = cur.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Event not found.")
    existing = dict(existing)

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
            cur.execute(
                """
                SELECT 1 FROM managed_events
                WHERE deleted_at IS NULL AND LOWER(name) = LOWER(%s) AND id <> %s
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
            RETURNING id, name, description, location, start_date, end_date, status,
                      created_by, updated_by, created_at, updated_at
            """,
            params,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Event not found.")

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
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def delete_event(event_id: str, request: Request):
    user = get_current_user(request)
    now = datetime.now(timezone.utc)
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE managed_events
            SET deleted_at = %s, updated_at = %s, updated_by = %s, status = 'inactive'
            WHERE id = %s AND deleted_at IS NULL
            RETURNING id, name
            """,
            (now, now, user["id"], event_id),
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
