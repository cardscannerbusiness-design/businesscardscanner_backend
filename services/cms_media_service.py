"""CMS media library — unified uploads for email / WhatsApp templates.

Files live under assets/cms-media/{admin_id}/ and are served via /assets/...
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services.company_display_identity import public_display_picture_url

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
CMS_MEDIA_ROOT = _BACKEND_ROOT / "assets" / "cms-media"

# Same public host used by thank-you email assets / existing CMS templates.
_PUBLIC_ASSETS_BASE = "https://api.namecardscan.com/assets"

CMS_MEDIA_MAX_BYTES = 25 * 1024 * 1024

CMS_MEDIA_ALLOWED: dict[str, frozenset[str]] = {
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/png": frozenset({".png"}),
    "image/webp": frozenset({".webp"}),
    "image/gif": frozenset({".gif"}),
    "video/mp4": frozenset({".mp4"}),
    "application/pdf": frozenset({".pdf"}),
}

_EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
}


def _safe_admin_id(admin_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(admin_id or ""))
    if not safe:
        raise ValueError("Invalid admin id for media storage.")
    return safe


def _admin_dir(admin_id: str) -> Path:
    return CMS_MEDIA_ROOT / _safe_admin_id(admin_id)


def _guess_content_type(filename: str | None, content_type: str | None) -> str:
    ctype = str(content_type or "").strip().lower().split(";")[0].strip()
    if ctype == "image/jpg":
        ctype = "image/jpeg"
    if ctype in CMS_MEDIA_ALLOWED:
        return ctype
    ext = Path(filename or "").suffix.lower()
    return _EXT_TO_MIME.get(ext, ctype)


def _extension_for(filename: str | None, content_type: str) -> str:
    ext = Path(filename or "").suffix.lower()
    allowed_exts = CMS_MEDIA_ALLOWED.get(content_type, frozenset())
    if ext in allowed_exts:
        return ext
    if content_type == "image/jpeg":
        return ".jpg"
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type == "image/gif":
        return ".gif"
    if content_type == "video/mp4":
        return ".mp4"
    if content_type == "application/pdf":
        return ".pdf"
    raise ValueError("Unsupported media type.")


def validate_cms_media(
    *,
    filename: str | None,
    content_type: str | None,
    size: int,
) -> str:
    """Return normalized MIME type or raise ValueError."""
    if size <= 0:
        raise ValueError("File is empty.")
    if size > CMS_MEDIA_MAX_BYTES:
        raise ValueError(
            f"File must be {CMS_MEDIA_MAX_BYTES // (1024 * 1024)} MB or smaller."
        )
    ctype = _guess_content_type(filename, content_type)
    if ctype not in CMS_MEDIA_ALLOWED:
        raise ValueError(
            "Unsupported file type. Use JPEG, PNG, WebP, GIF, MP4, or PDF."
        )
    ext = Path(filename or "").suffix.lower()
    if ext and ext not in CMS_MEDIA_ALLOWED[ctype] and ext not in _EXT_TO_MIME:
        raise ValueError(
            "Unsupported file type. Use JPEG, PNG, WebP, GIF, MP4, or PDF."
        )
    return ctype


def _safe_original_stem(filename: str | None) -> str:
    stem = Path(filename or "media").stem
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-._")
    return (cleaned or "media")[:80]


def relative_path_for(admin_id: str, stored_name: str) -> str:
    return f"cms-media/{_safe_admin_id(admin_id)}/{stored_name}"


def absolute_media_path(relative_path: str) -> Path:
    rel = str(relative_path or "").lstrip("/").replace("\\", "/")
    if rel.startswith("assets/"):
        rel = rel[len("assets/") :]
    parts = rel.split("/")
    if ".." in parts or not rel.startswith("cms-media/"):
        raise ValueError("Invalid media path.")
    return _BACKEND_ROOT / "assets" / rel


def public_cms_media_url(relative_path: str) -> str:
    """Public absolute URL for template embeds (email + WhatsApp).

    Prefer BACKEND_BASE_URL when it is publicly reachable; otherwise use the
    production assets host so Meta / email clients can fetch the file. CMS
    preview already rewrites api.namecardscan.com/assets → /assets via proxy.
    """
    url = public_display_picture_url(relative_path)
    lowered = url.lower()
    if (
        not url
        or url.startswith("/")
        or "localhost" in lowered
        or "127.0.0.1" in lowered
        or "0.0.0.0" in lowered
    ):
        rel = str(relative_path or "").lstrip("/")
        if rel.startswith("assets/"):
            rel = rel[len("assets/") :]
        return f"{_PUBLIC_ASSETS_BASE}/{rel}"
    return url


def _kind_for(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "document"


def _item_from_file(admin_id: str, path: Path) -> dict:
    rel = relative_path_for(admin_id, path.name)
    ext = path.suffix.lower()
    ctype = _EXT_TO_MIME.get(ext, "application/octet-stream")
    stat = path.stat()
    return {
        "id": path.name,
        "filename": path.name,
        "original_name": path.name,
        "content_type": ctype,
        "kind": _kind_for(ctype),
        "size": stat.st_size,
        "relative_path": rel,
        "url": public_cms_media_url(rel),
        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def list_cms_media(admin_id: str) -> list[dict]:
    folder = _admin_dir(admin_id)
    if not folder.is_dir():
        return []
    items: list[dict] = []
    for path in folder.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            items.append(_item_from_file(admin_id, path))
        except OSError as exc:
            logger.warning("Skipping media file %s: %s", path, exc)
    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return items


def save_cms_media(
    admin_id: str,
    *,
    file_bytes: bytes,
    filename: str | None,
    content_type: str | None,
) -> dict:
    ctype = validate_cms_media(
        filename=filename,
        content_type=content_type,
        size=len(file_bytes),
    )
    ext = _extension_for(filename, ctype)
    stem = _safe_original_stem(filename)
    stored_name = f"{stem}-{uuid.uuid4().hex[:10]}{ext}"
    folder = _admin_dir(admin_id)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / stored_name
    dest.write_bytes(file_bytes)
    logger.info(
        "CMS media saved admin=%s file=%s bytes=%s type=%s",
        admin_id,
        stored_name,
        len(file_bytes),
        ctype,
    )
    return _item_from_file(admin_id, dest)


def delete_cms_media(admin_id: str, filename: str) -> dict:
    safe_name = Path(str(filename or "")).name
    if not safe_name or safe_name != filename or ".." in safe_name:
        raise ValueError("Invalid media filename.")
    path = _admin_dir(admin_id) / safe_name
    if not path.is_file():
        raise ValueError("Media file not found.")
    item = _item_from_file(admin_id, path)
    path.unlink()
    logger.info("CMS media deleted admin=%s file=%s", admin_id, safe_name)
    return item
