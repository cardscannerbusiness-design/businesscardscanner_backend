"""Company storage quota service — single source of truth for plan limits.

All quota checks and used_storage_bytes updates go through this module so
contact create/delete routes never duplicate storage math.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Default Freemium plan: 20 MB
DEFAULT_PLAN_NAME = "FREEMIUM"
DEFAULT_STORAGE_LIMIT_BYTES = 20 * 1024 * 1024  # 20_971_520

# Prevent SELECT … FOR UPDATE from waiting forever under contention.
_LOCK_TIMEOUT_MS = 5_000
_STATEMENT_TIMEOUT_MS = 15_000

_COMPANY_LOCK_COLUMNS = ("id", "plan_name", "storage_limit_bytes", "used_storage_bytes")


def _log_step(tag: str, message: str, *, started_at: float | None = None, **fields: Any) -> float:
    """Structured step log with optional duration since started_at. Returns now."""
    now = time.perf_counter()
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    if started_at is None:
        logger.info("[%s] %s %s", tag, message, extra)
    else:
        ms = (now - started_at) * 1000
        logger.info("[%s] %s duration_ms=%.1f %s", tag, message, ms, extra)
    return now


def _row_as_dict(row: Any) -> dict[str, Any]:
    """Normalize RealDictRow / mapping / tuple rows from company lock queries."""
    if row is None:
        raise LookupError("Company row is empty")
    if isinstance(row, dict):
        return dict(row)
    # Plain cursor returns a tuple in SELECT column order.
    if isinstance(row, (tuple, list)) and len(row) >= len(_COMPANY_LOCK_COLUMNS):
        return dict(zip(_COMPANY_LOCK_COLUMNS, row[: len(_COMPANY_LOCK_COLUMNS)]))
    raise TypeError(f"Unsupported company row type: {type(row)!r}")


class StorageLimitExceededError(Exception):
    """Raised when used_storage_bytes + image_size would exceed the plan limit."""

    code = "STORAGE_LIMIT_EXCEEDED"

    def __init__(
        self,
        message: str = "Storage limit reached. Upgrade your plan to continue.",
    ):
        self.message = message
        super().__init__(message)

    def to_response(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": self.code,
            "message": self.message,
        }


def calculate_image_size_bytes(card_image_base64: str | None) -> int:
    """Return decoded byte size of a data-URL or raw base64 card image."""
    raw = str(card_image_base64 or "").strip()
    if not raw:
        return 0
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    raw = "".join(raw.split())
    if not raw:
        return 0
    try:
        return len(base64.b64decode(raw, validate=False))
    except Exception:
        # Approximate if decode fails (corrupt padding, etc.)
        return max(0, (len(raw) * 3) // 4)


def _bytes_to_mb(value: int | float) -> float:
    return round(float(value) / (1024 * 1024), 2)


def _normalize_row(row: dict[str, Any] | None, company_id: str) -> dict[str, Any]:
    if not row:
        return {
            "company_id": company_id,
            "plan": DEFAULT_PLAN_NAME,
            "plan_name": DEFAULT_PLAN_NAME,
            "storage_limit_bytes": DEFAULT_STORAGE_LIMIT_BYTES,
            "used_storage_bytes": 0,
            "remaining_storage_bytes": DEFAULT_STORAGE_LIMIT_BYTES,
            "used_percentage": 0.0,
            "used_mb": 0.0,
            "limit_mb": _bytes_to_mb(DEFAULT_STORAGE_LIMIT_BYTES),
            "remaining_mb": _bytes_to_mb(DEFAULT_STORAGE_LIMIT_BYTES),
        }

    plan = str(row.get("plan_name") or DEFAULT_PLAN_NAME).strip() or DEFAULT_PLAN_NAME
    limit_bytes = int(row.get("storage_limit_bytes") or DEFAULT_STORAGE_LIMIT_BYTES)
    if limit_bytes < 0:
        limit_bytes = DEFAULT_STORAGE_LIMIT_BYTES
    used_bytes = max(0, int(row.get("used_storage_bytes") or 0))
    remaining = max(0, limit_bytes - used_bytes)
    used_pct = round((used_bytes / limit_bytes) * 100, 1) if limit_bytes > 0 else 0.0

    return {
        "company_id": str(row.get("id") or company_id),
        "plan": plan,
        "plan_name": plan,
        "storage_limit_bytes": limit_bytes,
        "used_storage_bytes": used_bytes,
        "remaining_storage_bytes": remaining,
        "used_percentage": used_pct,
        "used_mb": _bytes_to_mb(used_bytes),
        "limit_mb": _bytes_to_mb(limit_bytes),
        "remaining_mb": _bytes_to_mb(remaining),
    }


def get_company_storage(company_id: str) -> dict[str, Any]:
    """Fetch plan / limit / used for a company (no row lock)."""
    from db.pool import db_cursor

    started = _log_step("STORAGE", "Fetch Company", company_id=company_id)
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, plan_name, storage_limit_bytes, used_storage_bytes
            FROM companies
            WHERE id = %s
            """,
            (company_id,),
        )
        row = cur.fetchone()
    info = _normalize_row(_row_as_dict(row) if row else None, company_id)
    _log_step(
        "STORAGE",
        "Fetch Company Completed",
        started_at=started,
        company_id=company_id,
        used=info["used_storage_bytes"],
        limit=info["storage_limit_bytes"],
    )
    return info


def get_storage_usage(company_id: str) -> dict[str, Any]:
    """Public usage snapshot for GET /api/storage/usage."""
    return get_company_storage(company_id)


def can_upload(company_id: str, image_size_bytes: int) -> bool:
    """Return True if the company can accept an upload of image_size_bytes."""
    size = max(0, int(image_size_bytes or 0))
    if size == 0:
        return True
    info = get_company_storage(company_id)
    return (info["used_storage_bytes"] + size) <= info["storage_limit_bytes"]


def _apply_txn_timeouts(cur: Any) -> None:
    """Bound lock/statement waits so FOR UPDATE cannot hang the request forever."""
    # Plain integers are milliseconds in PostgreSQL.
    cur.execute(f"SET LOCAL lock_timeout = {_LOCK_TIMEOUT_MS}")
    cur.execute(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}")


def _lock_company(cur: Any, company_id: str) -> dict[str, Any]:
    started = _log_step("STORAGE", "Company Lock Started", company_id=company_id)
    try:
        _apply_txn_timeouts(cur)
        cur.execute(
            """
            SELECT id, plan_name, storage_limit_bytes, used_storage_bytes
            FROM companies
            WHERE id = %s
            FOR UPDATE
            """,
            (company_id,),
        )
        row = cur.fetchone()
    except Exception as exc:
        # psycopg2: QueryCanceled / lock_not_available when lock_timeout fires
        err = str(exc).lower()
        _log_step(
            "STORAGE",
            "Company Lock Failed",
            started_at=started,
            company_id=company_id,
            error=type(exc).__name__,
        )
        logger.exception("[STORAGE] Company lock error company_id=%s", company_id)
        if "lock" in err or "canceling statement" in err or "timeout" in err:
            raise TimeoutError(
                f"Timed out waiting for company storage lock ({company_id}). Retry shortly."
            ) from exc
        raise
    if not row:
        raise LookupError(f"Company not found: {company_id}")
    result = _row_as_dict(row)
    _log_step(
        "STORAGE",
        "Company Lock Acquired",
        started_at=started,
        company_id=company_id,
    )
    return result


def assert_can_upload(company_id: str | None, image_size_bytes: int) -> None:
    """Raise StorageLimitExceededError when the upload would exceed the plan.

    When company_id is missing (e.g. Super Admin with no company), the check is skipped.
    """
    size = max(0, int(image_size_bytes or 0))
    if not company_id or size == 0:
        return

    info = get_company_storage(company_id)
    if (info["used_storage_bytes"] + size) <= info["storage_limit_bytes"]:
        logger.info(
            "Storage upload accepted company=%s size=%s used=%s limit=%s",
            company_id,
            size,
            info["used_storage_bytes"],
            info["storage_limit_bytes"],
        )
        return

    logger.warning(
        "Storage upload rejected company=%s size=%s used=%s limit=%s",
        company_id,
        size,
        info["used_storage_bytes"],
        info["storage_limit_bytes"],
    )
    raise StorageLimitExceededError()


def assert_can_upload_locked(cur: Any, company_id: str | None, image_size_bytes: int) -> None:
    """Atomic quota check: SELECT … FOR UPDATE then compare used + size vs limit."""
    size = max(0, int(image_size_bytes or 0))
    if not company_id or size == 0:
        return

    started = _log_step(
        "STORAGE",
        "Storage Validation Started",
        company_id=company_id,
        size=size,
    )
    row = _lock_company(cur, company_id)
    info = _normalize_row(row, company_id)
    if (info["used_storage_bytes"] + size) <= info["storage_limit_bytes"]:
        _log_step(
            "STORAGE",
            "Storage Validation Passed",
            started_at=started,
            company_id=company_id,
            size=size,
            used=info["used_storage_bytes"],
            limit=info["storage_limit_bytes"],
        )
        return

    _log_step(
        "STORAGE",
        "Storage Validation Rejected",
        started_at=started,
        company_id=company_id,
        size=size,
        used=info["used_storage_bytes"],
        limit=info["storage_limit_bytes"],
    )
    raise StorageLimitExceededError()


def update_storage_after_upload(
    company_id: str,
    image_size_bytes: int,
    *,
    cur: Any | None = None,
) -> None:
    """Increment companies.used_storage_bytes after a successful contact image save."""
    size = max(0, int(image_size_bytes or 0))
    if not company_id or size == 0:
        return

    def _run(active_cur: Any) -> None:
        active_cur.execute(
            """
            UPDATE companies
            SET used_storage_bytes = used_storage_bytes + %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (size, company_id),
        )
        _log_step(
            "STORAGE",
            "Updating Used Storage",
            company_id=company_id,
            delta=size,
        )

    if cur is not None:
        _run(cur)
        return

    from db.pool import db_cursor

    with db_cursor(commit=True) as active_cur:
        _run(active_cur)


def release_storage_after_delete(
    company_id: str,
    image_size_bytes: int,
    *,
    cur: Any | None = None,
) -> None:
    """Decrement companies.used_storage_bytes after contact soft-delete (never below 0)."""
    size = max(0, int(image_size_bytes or 0))
    if not company_id or size == 0:
        return

    def _run(active_cur: Any) -> None:
        active_cur.execute(
            """
            UPDATE companies
            SET used_storage_bytes = GREATEST(0, used_storage_bytes - %s),
                updated_at = NOW()
            WHERE id = %s
            """,
            (size, company_id),
        )
        logger.info(
            "Storage released (delete) company=%s delta=%s",
            company_id,
            size,
        )

    if cur is not None:
        _run(cur)
        return

    from db.pool import db_cursor

    with db_cursor(commit=True) as active_cur:
        _run(active_cur)


def resolve_company_id_for_user(user: dict[str, Any] | None) -> str | None:
    """Best-effort company id from the authenticated user dict."""
    if not user:
        return None
    raw = user.get("company_id")
    if raw:
        return str(raw)
    return None
