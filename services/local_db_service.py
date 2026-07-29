import base64
import logging
import mimetypes
import os
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator

from psycopg2.extras import RealDictCursor

from db.pool import get_connection, release_connection

logger = logging.getLogger(__name__)


def _postgres_url() -> str:
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        return ""
    # Prisma adds ?schema=public — psycopg2 does not accept that query string.
    return raw.split("?", 1)[0]


class LocalDbError(Exception):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def check_database() -> dict:
    if not _postgres_url():
        return {
            "ok": False,
            "error": "DATABASE_URL is missing in .env (PostgreSQL connection string).",
        }
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return {"ok": True, "database": "postgresql"}
    except Exception as exc:
        logger.warning("PostgreSQL health check failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@contextmanager
def _connect() -> Generator:
    """Acquire a pooled PostgreSQL connection (shared with auth module)."""
    try:
        conn = get_connection()
    except RuntimeError as exc:
        raise LocalDbError(
            "PostgreSQL pool is not initialized. Ensure DATABASE_URL is set and the app has started.",
            status_code=503,
        ) from exc
    try:
        yield conn
        if not conn.closed:
            conn.commit()
    except Exception:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        release_connection(conn)


WHATSAPP_SENT_MARKER = "[whatsapp:sent]"
EMAIL_SENT_MARKER = "[email:sent]"


def has_whatsapp_sent(contact: dict[str, Any]) -> bool:
    return WHATSAPP_SENT_MARKER in str(contact.get("notes") or "")


def has_email_sent(contact: dict[str, Any]) -> bool:
    return EMAIL_SENT_MARKER in str(contact.get("notes") or "")


def mark_whatsapp_sent(contact_id: str) -> None:
    contact = get_contact(contact_id)
    if not contact or has_whatsapp_sent(contact):
        return

    new_notes = f"{str(contact.get('notes') or '').strip()}\n{WHATSAPP_SENT_MARKER}".strip()
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE contacts SET notes = %s, "updatedAt" = %s WHERE id = %s',
                    (new_notes, datetime.utcnow(), contact_id),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to mark WhatsApp sent for %s: %s", contact_id, exc)


def mark_email_sent(contact_id: str) -> None:
    contact = get_contact(contact_id)
    if not contact or has_email_sent(contact):
        return

    new_notes = f"{str(contact.get('notes') or '').strip()}\n{EMAIL_SENT_MARKER}".strip()
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE contacts SET notes = %s, "updatedAt" = %s WHERE id = %s',
                    (new_notes, datetime.utcnow(), contact_id),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to mark email sent for %s: %s", contact_id, exc)


def update_outreach_delivery(
    contact_id: str,
    *,
    email_status: str | None = None,
    email_error: str | None = None,
    whatsapp_status: str | None = None,
    whatsapp_error: str | None = None,
) -> None:
    """Persist delivery outcomes so every role sees the same indication."""
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE contacts
                    SET email_delivery_status = COALESCE(%s, email_delivery_status),
                        email_delivery_error = CASE WHEN %s IS NULL THEN email_delivery_error ELSE %s END,
                        whatsapp_delivery_status = COALESCE(%s, whatsapp_delivery_status),
                        whatsapp_delivery_error = CASE WHEN %s IS NULL THEN whatsapp_delivery_error ELSE %s END,
                        "updatedAt" = %s
                    WHERE id = %s
                    """,
                    (
                        email_status,
                        email_status,
                        email_error,
                        whatsapp_status,
                        whatsapp_status,
                        whatsapp_error,
                        datetime.utcnow(),
                        contact_id,
                    ),
                )
    except Exception as exc:
        logger.warning("Failed to persist outreach delivery for %s: %s", contact_id, exc)


def _row_to_contact(row: dict[str, Any]) -> dict[str, Any]:
    # PostgreSQL is the system of record. Legacy Zoho values map to synced.
    sync_status = str(row.get("syncStatus") or "synced")
    if sync_status in ("synced_zoho", "local_only"):
        sync_status = "synced"
    if sync_status == "failed":
        status = "failed"
    else:
        status = "synced"
        sync_status = "synced"

    name = row.get("fullName") or ""
    created = row.get("createdAt")
    updated = row.get("updatedAt")
    if hasattr(created, "isoformat"):
        created = created.isoformat()
    if hasattr(updated, "isoformat"):
        updated = updated.isoformat()

    result = {
        "id": str(row["id"]),
        "name": name,
        "fullName": name,
        "firstName": row.get("firstName") or "",
        "lastName": row.get("lastName") or "",
        "designation": row.get("designation") or "",
        "title": row.get("designation") or "",
        "company": row.get("company") or "",
        "phone": row.get("phone") or "",
        "secondaryPhone": row.get("secondaryPhone") or "",
        "countryCode": row.get("countryCode") or "",
        "countryName": row.get("countryName") or "",
        "email": row.get("email") or "",
        "secondaryEmail": row.get("secondaryEmail") or "",
        "website": row.get("website") or "",
        "secondaryWebsite": row.get("secondaryWebsite") or "",
        "address": row.get("address") or "",
        "secondaryAddress": row.get("secondaryAddress") or "",
        "socialLinks": row.get("socialLinks") or "",
        "gstNumber": row.get("gstNumber") or "",
        "notes": row.get("notes") or "",
        "eventName": row.get("eventName") or "",
        "eventDay": row.get("eventDay") or "Day 1",
        "eventId": row.get("eventId"),
        "cardImageBase64": row.get("cardImageBase64"),
        "syncStatus": sync_status,
        "source": "localdb",
        "status": status,
        "created_at": created or "",
        "updatedAt": updated or "",
        "lastSync": (
            "Synced"
            if status == "synced"
            else str(updated or "Sync failed")
        ),
        "channels": {"whatsapp": bool(row.get("phone")), "email": bool(row.get("email"))},
        "whatsappSent": has_whatsapp_sent({"notes": row.get("notes") or ""}),
        "emailSent": has_email_sent({"notes": row.get("notes") or ""}),
        "emailDeliveryStatus": row.get("email_delivery_status"),
        "emailDeliveryError": row.get("email_delivery_error"),
        "whatsappDeliveryStatus": row.get("whatsapp_delivery_status"),
        "whatsappDeliveryError": row.get("whatsapp_delivery_error"),
    }

    # Ownership fields (from JOIN with users/companies or stored columns)
    if "admin_name" in row:
        result["admin_name"] = row.get("admin_name") or ""
    if "owner_company_name" in row:
        result["owner_company_name"] = row.get("owner_company_name") or ""
    if "user_name" in row:
        result["user_name"] = row.get("user_name") or ""
    if "user_username" in row:
        result["user_username"] = row.get("user_username") or ""
    if "created_by_user_id" in row:
        result["created_by_user_id"] = str(row.get("created_by_user_id") or "")
    owner_company = row.get("owner_company_id")
    if owner_company is None:
        owner_company = row.get("joined_owner_company_id")
    if owner_company is not None:
        result["owner_company_id"] = str(owner_company)
        result["company_id"] = str(owner_company)
    if row.get("created_by_role"):
        result["created_by_role"] = str(row.get("created_by_role") or "")

    return result


def _payload_to_local_body(
    contact_data: dict[str, Any],
    card_image_base64: str | None = None,
) -> dict[str, Any]:
    name = contact_data.get("fullName") or contact_data.get("name") or ""
    first = contact_data.get("firstName") or ""
    last = contact_data.get("lastName") or ""
    if not last and name:
        parts = str(name).strip().split(None, 1)
        first = first or (parts[0] if parts else "")
        last = parts[1] if len(parts) > 1 else (parts[0] if parts else "")

    # Anything written to PostgreSQL is synced to the app database.
    # Browser offline queue uses "pending" until it POSTs here.
    sync_status = str(contact_data.get("syncStatus") or "synced").strip()
    if sync_status in ("synced_zoho", "local_only", ""):
        sync_status = "synced"
    if sync_status not in ("synced", "failed"):
        sync_status = "synced"

    return {
        "fullName": str(name).strip() or "Contact",
        "firstName": str(first).strip(),
        "lastName": str(last).strip(),
        "designation": str(
            contact_data.get("designation") or contact_data.get("title") or ""
        ).strip(),
        "company": str(contact_data.get("company") or "").strip(),
        "phone": str(contact_data.get("phone") or contact_data.get("phoneNumber") or "").strip(),
        "secondaryPhone": str(contact_data.get("secondaryPhone") or "").strip(),
        "countryCode": str(contact_data.get("countryCode") or "").strip(),
        "countryName": str(contact_data.get("countryName") or "").strip(),
        "email": str(
            contact_data.get("email") or contact_data.get("emailAddress") or ""
        ).strip(),
        "secondaryEmail": str(contact_data.get("secondaryEmail") or "").strip(),
        "website": str(contact_data.get("website") or "").strip(),
        "secondaryWebsite": str(contact_data.get("secondaryWebsite") or "").strip(),
        "address": str(contact_data.get("address") or "").strip(),
        "secondaryAddress": str(contact_data.get("secondaryAddress") or "").strip(),
        "socialLinks": str(contact_data.get("socialLinks") or "").strip(),
        "gstNumber": str(contact_data.get("gstNumber") or "").strip(),
        "notes": str(contact_data.get("notes") or "").strip(),
        "eventName": str(contact_data.get("eventName") or "").strip(),
        "eventDay": str(contact_data.get("eventDay") or "Day 1").strip() or "Day 1",
        "eventId": (str(contact_data.get("eventId")).strip() if contact_data.get("eventId") else None),
        "cardImageBase64": card_image_base64 or contact_data.get("cardImageBase64"),
        "syncStatus": sync_status,
    }


def image_path_to_base64(image_path: str | None) -> str | None:
    if not image_path or not os.path.isfile(image_path):
        return None
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    with open(image_path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def list_contacts(user: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """List contacts with ownership info. Optionally filter by user role/company."""
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                base_query, params = _contacts_list_sql(user)
                base_query += ' ORDER BY c."createdAt" DESC'
                cur.execute(base_query, params)
                rows = cur.fetchall()
        return [_row_to_contact(dict(row)) for row in rows]
    except LocalDbError:
        raise
    except Exception as exc:
        logger.error("PostgreSQL list failed: %s", exc)
        return []


def list_contacts_page(
    user: dict[str, Any] | None = None,
    *,
    page: int = 1,
    limit: int = 10,
    q: str | None = None,
    event: str | None = None,
) -> dict[str, Any]:
    """Paginated contacts list — same RBAC as list_contacts, with optional search."""
    page = max(1, int(page or 1))
    limit = min(100, max(1, int(limit or 10)))
    offset = (page - 1) * limit
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                base_query, params = _contacts_list_sql(user, q=q, event=event)
                count_query = f"SELECT COUNT(*) AS count FROM ({base_query}) AS scoped"
                cur.execute(count_query, params)
                total = int(cur.fetchone()["count"])

                page_query = base_query + ' ORDER BY c."createdAt" DESC LIMIT %s OFFSET %s'
                cur.execute(page_query, params + [limit, offset])
                rows = cur.fetchall()
        return {
            "items": [_row_to_contact(dict(row)) for row in rows],
            "total": total,
            "page": page,
            "limit": limit,
        }
    except LocalDbError:
        raise
    except Exception as exc:
        logger.error("PostgreSQL paged list failed: %s", exc)
        return {"items": [], "total": 0, "page": page, "limit": limit}


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contacts_list_sql(
    user: dict[str, Any] | None = None,
    *,
    q: str | None = None,
    event: str | None = None,
) -> tuple[str, list[Any]]:
    base_query = """
        SELECT c.*,
               COALESCE(
                   NULLIF(TRIM(u.first_name || ' ' || u.last_name), ''),
                   NULLIF(TRIM(u.email), ''),
                   ''
               ) AS user_name,
               COALESCE(u.username, '') AS user_username,
               COALESCE(
                   NULLIF(TRIM(admin_u.first_name || ' ' || admin_u.last_name), ''),
                   NULLIF(TRIM(admin_u.email), ''),
                   CASE
                       WHEN c.created_by_role = 'ADMIN'
                       THEN COALESCE(
                           NULLIF(TRIM(u.first_name || ' ' || u.last_name), ''),
                           NULLIF(TRIM(u.email), ''),
                           ''
                       )
                       ELSE ''
                   END,
                   ''
               ) AS admin_name,
               COALESCE(comp.company_name, '') AS owner_company_name,
               COALESCE(c.owner_company_id, u.company_id) AS joined_owner_company_id
        FROM contacts c
        LEFT JOIN users u ON c.created_by_user_id = u.id
        LEFT JOIN users admin_u ON admin_u.id = u.admin_id
        LEFT JOIN companies comp ON COALESCE(c.owner_company_id, u.company_id) = comp.id
        WHERE (c.is_deleted = FALSE OR c.is_deleted IS NULL)
    """
    params: list[Any] = []

    if user:
        role = user.get("role", "")
        company_id = user.get("company_id")
        user_id = user.get("id")
        if role == "USER" and user_id:
            base_query += " AND c.created_by_user_id = %s"
            params.append(user_id)
        elif role == "ADMIN":
            if company_id:
                base_query += " AND COALESCE(c.owner_company_id, u.company_id) = %s"
                params.append(company_id)
            elif user_id:
                base_query += " AND c.created_by_user_id = %s"
                params.append(user_id)

    search = (q or "").strip()
    if search:
        like = f"%{_escape_like(search)}%"
        base_query += """
            AND (
                COALESCE(c."fullName", '') ILIKE %s ESCAPE '\\'
                OR COALESCE(c.company, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(c.email, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(c.phone, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(c."secondaryPhone", '') ILIKE %s ESCAPE '\\'
                OR COALESCE(c."secondaryEmail", '') ILIKE %s ESCAPE '\\'
                OR COALESCE(c."eventName", '') ILIKE %s ESCAPE '\\'
                OR COALESCE(c.designation, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(c.notes, '') ILIKE %s ESCAPE '\\'
            )
        """
        params.extend([like] * 9)

    event_name = (event or "").strip()
    if event_name:
        base_query += ' AND LOWER(TRIM(COALESCE(c."eventName", \'\'))) = LOWER(TRIM(%s))'
        params.append(event_name)

    return base_query, params


def get_contact(contact_id: str, user: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Fetch a single contact. When *user* is provided, apply RBAC ownership filter."""
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = """
                    SELECT c.*,
                           COALESCE(
                               NULLIF(TRIM(u.first_name || ' ' || u.last_name), ''),
                               NULLIF(TRIM(u.email), ''),
                               ''
                           ) AS user_name,
                           COALESCE(u.username, '') AS user_username,
                           COALESCE(
                               NULLIF(TRIM(admin_u.first_name || ' ' || admin_u.last_name), ''),
                               NULLIF(TRIM(admin_u.email), ''),
                               CASE
                                   WHEN c.created_by_role = 'ADMIN'
                                   THEN COALESCE(
                                       NULLIF(TRIM(u.first_name || ' ' || u.last_name), ''),
                                       NULLIF(TRIM(u.email), ''),
                                       ''
                                   )
                                   ELSE ''
                               END,
                               ''
                           ) AS admin_name,
                           COALESCE(comp.company_name, '') AS owner_company_name,
                           COALESCE(c.owner_company_id, u.company_id) AS joined_owner_company_id
                    FROM contacts c
                    LEFT JOIN users u ON c.created_by_user_id = u.id
                    LEFT JOIN users admin_u ON admin_u.id = u.admin_id
                    LEFT JOIN companies comp ON COALESCE(c.owner_company_id, u.company_id) = comp.id
                    WHERE c.id = %s AND (c.is_deleted = FALSE OR c.is_deleted IS NULL)
                """
                params: list[Any] = [contact_id]

                if user:
                    role = user.get("role", "")
                    company_id = user.get("company_id")
                    user_id = user.get("id")
                    if role == "USER" and user_id:
                        query += " AND c.created_by_user_id = %s"
                        params.append(user_id)
                    elif role == "ADMIN":
                        if company_id:
                            query += " AND COALESCE(c.owner_company_id, u.company_id) = %s"
                            params.append(company_id)
                        elif user_id:
                            query += " AND c.created_by_user_id = %s"
                            params.append(user_id)
                    # SUPER_ADMIN: no extra filter

                cur.execute(query, params)
                row = cur.fetchone()
        if not row:
            return None
        return _row_to_contact(dict(row))
    except LocalDbError:
        raise
    except Exception as exc:
        raise LocalDbError(str(exc)) from exc


def _resolve_creator_meta(created_by_user_id: str | None) -> tuple[str | None, str]:
    """Return (owner_company_id, created_by_role) for the creating user."""
    if not created_by_user_id:
        return None, ""
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT u.company_id, r.name AS role_name
                    FROM users u
                    LEFT JOIN roles r ON r.id = u.role_id
                    WHERE u.id = %s
                    """,
                    (created_by_user_id,),
                )
                row = cur.fetchone()
        if not row:
            return None, ""
        company_id = str(row["company_id"]) if row.get("company_id") else None
        role_name = str(row.get("role_name") or "")
        return company_id, role_name
    except Exception as exc:
        logger.warning("Failed to resolve creator meta for %s: %s", created_by_user_id, exc)
        return None, ""


def create_contact(
    contact_data: dict[str, Any],
    image_path: str | None = None,
) -> dict[str, Any]:
    body = _payload_to_local_body(
        contact_data,
        image_path_to_base64(image_path),
    )
    contact_id = str(uuid.uuid4())
    now = datetime.utcnow()
    created_by_user_id = contact_data.get("created_by_user_id")
    owner_company_id, created_by_role = _resolve_creator_meta(
        str(created_by_user_id) if created_by_user_id else None
    )

    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO contacts (
                        id, "fullName", "firstName", "lastName", designation, company,
                        phone, "secondaryPhone", "countryCode", "countryName",
                        email, "secondaryEmail", website,
                        "secondaryWebsite", address, "secondaryAddress", "socialLinks",
                        "gstNumber", notes, "eventName", "eventDay", "eventId", "cardImageBase64",
                        "syncStatus", "createdAt", "updatedAt", created_by_user_id,
                        owner_company_id, created_by_role
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        contact_id,
                        body["fullName"],
                        body["firstName"],
                        body["lastName"],
                        body["designation"],
                        body["company"],
                        body["phone"],
                        body["secondaryPhone"],
                        body["countryCode"],
                        body["countryName"],
                        body["email"],
                        body["secondaryEmail"],
                        body["website"],
                        body["secondaryWebsite"],
                        body["address"],
                        body["secondaryAddress"],
                        body["socialLinks"],
                        body["gstNumber"],
                        body["notes"],
                        body["eventName"],
                        body["eventDay"],
                        body.get("eventId"),
                        body["cardImageBase64"],
                        body["syncStatus"],
                        now,
                        now,
                        created_by_user_id,
                        owner_company_id,
                        created_by_role,
                    ),
                )
            conn.commit()
    except LocalDbError:
        raise
    except Exception as exc:
        raise LocalDbError(f"Failed to save contact: {exc}") from exc

    return {"success": True, "id": contact_id, "database": "postgresql"}


def update_contact(contact_id: str, contact_data: dict[str, Any]) -> dict[str, Any]:
    body = _payload_to_local_body(contact_data)
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Preserve system delivery markers that live in notes so edits
                # cannot wipe WhatsApp/email sent history.
                cur.execute('SELECT notes FROM contacts WHERE id = %s', (contact_id,))
                existing = cur.fetchone()
                existing_notes = str((existing or {}).get("notes") or "")
                markers = [
                    marker
                    for marker in (WHATSAPP_SENT_MARKER, EMAIL_SENT_MARKER)
                    if marker in existing_notes
                ]
                notes = str(body["notes"] or "")
                for marker in markers:
                    if marker not in notes:
                        notes = f"{notes}\n{marker}".strip() if notes else marker
                body["notes"] = notes

                cur.execute(
                    """
                    UPDATE contacts SET
                        "fullName" = %s, "firstName" = %s, "lastName" = %s,
                        designation = %s, company = %s, phone = %s, "secondaryPhone" = %s,
                        "countryCode" = %s, "countryName" = %s,
                        email = %s, "secondaryEmail" = %s, website = %s, "secondaryWebsite" = %s,
                        address = %s, "secondaryAddress" = %s, "socialLinks" = %s,
                        "gstNumber" = %s, notes = %s, "eventName" = %s, "eventDay" = %s,
                        "eventId" = COALESCE(%s, "eventId"),
                        "cardImageBase64" = COALESCE(%s, "cardImageBase64"),
                        "syncStatus" = %s, "updatedAt" = %s
                    WHERE id = %s
                    """,
                    (
                        body["fullName"],
                        body["firstName"],
                        body["lastName"],
                        body["designation"],
                        body["company"],
                        body["phone"],
                        body["secondaryPhone"],
                        body["countryCode"],
                        body["countryName"],
                        body["email"],
                        body["secondaryEmail"],
                        body["website"],
                        body["secondaryWebsite"],
                        body["address"],
                        body["secondaryAddress"],
                        body["socialLinks"],
                        body["gstNumber"],
                        body["notes"],
                        body["eventName"],
                        body["eventDay"],
                        body.get("eventId"),
                        body["cardImageBase64"],
                        body["syncStatus"],
                        datetime.utcnow(),
                        contact_id,
                    ),
                )
                if cur.rowcount == 0:
                    return {"success": False, "error": "Contact not found"}
            conn.commit()
    except LocalDbError:
        raise
    except Exception as exc:
        raise LocalDbError(str(exc)) from exc
    return {"success": True, "id": contact_id}


def soft_delete_contact(contact_id: str) -> dict[str, Any]:
    """Soft-delete: set is_deleted=TRUE and deleted_at=NOW(). Never permanently removes the row."""
    now = datetime.utcnow()
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE contacts
                    SET is_deleted = TRUE, deleted_at = %s, "updatedAt" = %s
                    WHERE id = %s AND (is_deleted = FALSE OR is_deleted IS NULL)
                    """,
                    (now, now, contact_id),
                )
                if cur.rowcount == 0:
                    return {"success": False, "message": "Contact not found or already deleted"}
            conn.commit()
    except LocalDbError:
        raise
    except Exception as exc:
        raise LocalDbError(str(exc)) from exc
    return {
        "success": True,
        "message": f"Contact {contact_id} soft-deleted",
        "deleted_at": now.isoformat(),
    }


# Legacy alias — some callers still import delete_contact
delete_contact = soft_delete_contact


def patch_sync_status(
    contact_id: str,
    *,
    sync_status: str,
    zoho_lead_id: str | None = None,
) -> None:
    del zoho_lead_id  # Deprecated Zoho field; column removed.
    normalized = "failed" if sync_status == "failed" else "synced"
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE contacts
                    SET "syncStatus" = %s, "updatedAt" = %s
                    WHERE id = %s
                    """,
                    (normalized, datetime.utcnow(), contact_id),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to patch sync status for %s: %s", contact_id, exc)


def delete_all_local_db_contacts() -> dict:
    """Soft-delete all contacts (never permanently removes rows)."""
    now = datetime.utcnow()
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE contacts
                    SET is_deleted = TRUE, deleted_at = %s, "updatedAt" = %s
                    WHERE (is_deleted = FALSE OR is_deleted IS NULL)
                    """,
                    (now, now),
                )
                deleted = cur.rowcount
            conn.commit()
        return {"success": True, "deleted": deleted}
    except LocalDbError as exc:
        return {"deleted": 0, "error": str(exc)}
    except Exception as exc:
        logger.warning("PostgreSQL wipe failed: %s", exc)
        return {"deleted": 0, "error": str(exc)}
