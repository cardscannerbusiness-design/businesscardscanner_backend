"""Per-user Display Name + Profile Picture (keyed by users.id).

Never store these on companies or a shared path — each authenticated account
owns its own identity under profile-pictures/{user_id}/.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from db.pool import db_cursor
from services.company_display_identity import (
    DISPLAY_NAME_MAX_LEN,
    DISPLAY_PICTURE_ALLOWED_EXTS,
    DISPLAY_PICTURE_ALLOWED_TYPES,
    DISPLAY_PICTURE_MAX_BYTES,
    extension_for_content_type,
    guess_image_content_type,
    normalize_display_name,
    public_display_picture_url,
    validate_display_picture,
)

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PICTURES_ROOT = _BACKEND_ROOT / "assets" / "profile-pictures"

# Re-export validation helpers for callers / tests
__all__ = [
    "DISPLAY_NAME_MAX_LEN",
    "DISPLAY_PICTURE_MAX_BYTES",
    "DISPLAY_PICTURE_ALLOWED_TYPES",
    "normalize_display_name",
    "validate_display_picture",
    "user_profile_relative_path",
    "public_profile_image_url",
    "get_user_profile_identity",
    "set_user_display_name",
    "set_user_profile_picture",
    "effective_display_name",
]


def user_profile_relative_path(user_id: str, content_type: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(user_id or ""))
    if not safe_id:
        raise ValueError("Invalid user id for profile picture storage.")
    ext = extension_for_content_type(content_type)
    return f"profile-pictures/{safe_id}/profile{ext}"


def absolute_user_profile_path(relative_path: str) -> Path:
    rel = str(relative_path or "").lstrip("/").replace("\\", "/")
    parts = rel.split("/")
    if ".." in parts or not rel.startswith("profile-pictures/"):
        raise ValueError("Invalid profile picture path.")
    return _BACKEND_ROOT / "assets" / rel


def public_profile_image_url(relative_or_url: str | None, *, cache_bust: str | None = None) -> str:
    url = public_display_picture_url(relative_or_url)
    if not url:
        return ""
    if cache_bust:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}v={cache_bust}"
    return url


def effective_display_name(
    *,
    display_name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> str:
    custom = normalize_display_name(display_name)
    if custom:
        return custom
    return " ".join(str(p or "").strip() for p in (first_name, last_name) if str(p or "").strip()).strip()


def get_user_profile_identity(user_id: str | None) -> dict[str, Any] | None:
    if not user_id:
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, display_name, first_name, last_name, profile_image, updated_at
            FROM users
            WHERE id = %s AND deleted_at IS NULL
            """,
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    updated = row.get("updated_at")
    bust = ""
    if updated is not None and hasattr(updated, "timestamp"):
        bust = str(int(updated.timestamp()))
    elif updated:
        bust = str(updated)
    return {
        "user_id": str(row["id"]),
        "display_name": str(row.get("display_name") or "").strip(),
        "first_name": str(row.get("first_name") or "").strip(),
        "last_name": str(row.get("last_name") or "").strip(),
        "effective_display_name": effective_display_name(
            display_name=row.get("display_name"),
            first_name=row.get("first_name"),
            last_name=row.get("last_name"),
        ),
        "profile_image": public_profile_image_url(row.get("profile_image"), cache_bust=bust or None),
        "profile_image_path": str(row.get("profile_image") or "").strip(),
    }


def set_user_display_name(user_id: str, display_name: str | None) -> dict[str, Any]:
    cleaned = normalize_display_name(display_name)
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE users
            SET display_name = %s, updated_at = NOW()
            WHERE id = %s AND deleted_at IS NULL
            """,
            (cleaned, user_id),
        )
        if cur.rowcount == 0:
            raise ValueError("User not found")
    result = get_user_profile_identity(user_id)
    if not result:
        raise RuntimeError("Failed to reload user after saving display name")
    return result


def set_user_profile_picture(
    user_id: str,
    *,
    image_bytes: bytes,
    filename: str | None,
    content_type: str | None,
) -> dict[str, Any]:
    ctype = validate_display_picture(
        filename=filename,
        content_type=content_type,
        size=len(image_bytes or b""),
    )
    relative = user_profile_relative_path(str(user_id), ctype)
    dest = absolute_user_profile_path(relative)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Remove other extensions for this user so stale files are not served.
    for old in dest.parent.glob("profile.*"):
        try:
            if old.resolve() != dest.resolve():
                old.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove old profile picture %s", old)

    dest.write_bytes(image_bytes)

    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE users
            SET profile_image = %s, updated_at = NOW()
            WHERE id = %s AND deleted_at IS NULL
            """,
            (relative, user_id),
        )
        if cur.rowcount == 0:
            raise ValueError("User not found")

    result = get_user_profile_identity(user_id)
    if not result:
        raise RuntimeError("Failed to reload user after saving profile picture")
    return result
