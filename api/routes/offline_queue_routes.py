"""Role-scoped registry for viewing device Offline Queue records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from psycopg2.extras import Json

from auth.dependencies import get_current_user
from db.pool import db_cursor

router = APIRouter(prefix="/api/offline-queue", tags=["Offline Queue"])


class QueueRecord(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    contact_data: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "retrying", "synced", "failed"]
    retry_count: int = Field(default=0, ge=0)
    created_at: datetime
    last_attempt: datetime | None = None
    error_message: str | None = None


class QueueSnapshot(BaseModel):
    items: list[QueueRecord] = Field(default_factory=list, max_length=1000)


@router.put("/snapshot", summary="Report the current device queue")
def report_queue_snapshot(
    body: QueueSnapshot,
    user: dict = Depends(get_current_user),
):
    """Mirror one user's current queue without changing synchronization."""
    user_id = user["id"]
    company_id = user.get("company_id")
    queue_ids = [item.id for item in body.items]

    with db_cursor() as cur:
        for item in body.items:
            cur.execute(
                """
                INSERT INTO offline_queue_records (
                    queue_id, created_by_user_id, owner_company_id, status,
                    retry_count, contact_data, error_message, queued_at,
                    last_attempt, reported_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (queue_id, created_by_user_id) DO UPDATE SET
                    owner_company_id = EXCLUDED.owner_company_id,
                    status = EXCLUDED.status,
                    retry_count = EXCLUDED.retry_count,
                    contact_data = EXCLUDED.contact_data,
                    error_message = EXCLUDED.error_message,
                    queued_at = EXCLUDED.queued_at,
                    last_attempt = EXCLUDED.last_attempt,
                    reported_at = NOW()
                """,
                (
                    item.id,
                    user_id,
                    company_id,
                    item.status,
                    item.retry_count,
                    Json(item.contact_data),
                    item.error_message,
                    item.created_at,
                    item.last_attempt,
                ),
            )

        if queue_ids:
            cur.execute(
                """
                DELETE FROM offline_queue_records
                WHERE created_by_user_id = %s AND NOT (queue_id = ANY(%s))
                """,
                (user_id, queue_ids),
            )
        else:
            cur.execute(
                "DELETE FROM offline_queue_records WHERE created_by_user_id = %s",
                (user_id,),
            )

    return {"success": True, "count": len(body.items)}


@router.get("", summary="List role-scoped Offline Queue records")
def list_offline_queue(
    user: dict = Depends(get_current_user),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=200),
):
    """SuperAdmin sees all; Admin sees company; User sees their own."""
    role = str(user.get("role") or "")
    conditions: list[str] = []
    params: list[Any] = []

    if role == "SUPER_ADMIN":
        conditions.append(
            "(oq.created_by_user_id = %s OR oq.owner_company_id IS NULL)"
        )
        params.append(user["id"])
        # Restrict further: only rows created by SuperAdmin accounts.
        conditions.append(
            """EXISTS (
                SELECT 1 FROM users su
                JOIN roles sr ON sr.id = su.role_id
                WHERE su.id = oq.created_by_user_id AND sr.name = 'SUPER_ADMIN'
            )"""
        )
    elif role == "USER":
        conditions.append("oq.created_by_user_id = %s")
        params.append(user["id"])
    elif role == "ADMIN":
        if user.get("company_id"):
            conditions.append("oq.owner_company_id = %s")
            params.append(user["company_id"])
        else:
            conditions.append("oq.created_by_user_id = %s")
            params.append(user["id"])
    else:
        # Fail closed for any unexpected role value.
        conditions.append("oq.created_by_user_id = %s")
        params.append(user["id"])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * limit
    with db_cursor(commit=False) as cur:
        cur.execute(
            f"SELECT COUNT(*) AS count FROM offline_queue_records oq {where_clause}",
            params,
        )
        total = int(cur.fetchone()["count"])

        cur.execute(
            f"""
            SELECT oq.queue_id, oq.status, oq.retry_count, oq.contact_data,
                   oq.error_message, oq.queued_at, oq.last_attempt, oq.reported_at,
                   oq.created_by_user_id, oq.owner_company_id,
                   u.username AS created_by_username,
                   TRIM(COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, ''))
                       AS created_by_name,
                   COALESCE(comp.company_name, '') AS owner_company_name
            FROM offline_queue_records oq
            JOIN users u ON u.id = oq.created_by_user_id
            LEFT JOIN companies comp ON comp.id = oq.owner_company_id
            {where_clause}
            ORDER BY oq.queued_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = [dict(row) for row in cur.fetchall()]

    for row in rows:
        for key in ("queued_at", "last_attempt", "reported_at"):
            if row.get(key) and hasattr(row[key], "isoformat"):
                row[key] = row[key].isoformat()
        for key in ("created_by_user_id", "owner_company_id"):
            if row.get(key) is not None:
                row[key] = str(row[key])
    return {"items": rows, "total": total, "page": page, "limit": limit}
