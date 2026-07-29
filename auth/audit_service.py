"""Audit log — records security-relevant events to the audit_logs table."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from db.pool import db_cursor

logger = logging.getLogger(__name__)


def log_action(
    user_id: str | None,
    action: str,
    *,
    ip: str = "",
    browser: str = "",
    user_agent: str = "",
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    """Insert a single audit log entry."""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_logs (user_id, action, ip, browser, user_agent, old_value, new_value)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    action,
                    ip,
                    browser,
                    user_agent,
                    json.dumps(old_value) if old_value else None,
                    json.dumps(new_value) if new_value else None,
                ),
            )
    except Exception as exc:
        # Audit logging must never break the main flow
        logger.warning("Failed to write audit log (%s): %s", action, exc)


# Internal bookkeeping events that only add noise to the audit UI.
NOISY_ACTIONS = ("token_refreshed",)


def get_logs(
    *,
    user_id: str | None = None,
    action: str | None = None,
    company_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """Return a paginated list of audit log entries with optional filters."""
    conditions: list[str] = []
    params: list[Any] = []

    if user_id:
        conditions.append("al.user_id = %s")
        params.append(user_id)
    if action:
        conditions.append("al.action = %s")
        params.append(action)
    else:
        conditions.append("al.action NOT IN %s")
        params.append(NOISY_ACTIONS)
    if company_id:
        conditions.append("u.company_id = %s")
        params.append(company_id)
    if date_from:
        conditions.append("al.created_at >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("al.created_at <= %s")
        params.append(date_to)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    # Always join the actor so the UI can show a useful identity instead of a
    # UUID. IP and user-agent remain stored internally for security analysis.
    join_clause = """
        LEFT JOIN users u ON u.id = al.user_id
        LEFT JOIN roles r ON r.id = u.role_id
    """

    offset = (page - 1) * limit

    with db_cursor(commit=False) as cur:
        # Count
        cur.execute(
            f"SELECT COUNT(*) AS total FROM audit_logs al {join_clause} {where_clause}",
            params,
        )
        total = cur.fetchone()["total"]

        # Page
        cur.execute(
            f"""
            SELECT al.id, al.user_id, al.action, al.ip, al.browser, al.user_agent,
                   al.old_value, al.new_value, al.created_at,
                   COALESCE(
                       NULLIF(TRIM(COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')), ''),
                       NULLIF(TRIM(u.email), ''),
                       ''
                   ) AS actor_name,
                   u.username AS actor_username,
                   u.email AS actor_email,
                   r.name AS actor_role
            FROM audit_logs al
            {join_clause}
            {where_clause}
            ORDER BY al.created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()

    items = []
    for row in rows:
        row = dict(row)
        # Convert datetime for JSON serialization
        if row.get("created_at") and hasattr(row["created_at"], "isoformat"):
            row["created_at"] = row["created_at"].isoformat()
        items.append(row)

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if limit else 0,
    }
