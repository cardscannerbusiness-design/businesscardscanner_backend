"""Company Display Name + Display Picture (CMS business identity).

Separate from Email Display Name (From header only).
"""

from __future__ import annotations

import re
from pathlib import Path

DISPLAY_NAME_MAX_LEN = 255
# Meta business profile images: JPEG/PNG; keep uploads modest for Graph upload.
DISPLAY_PICTURE_MAX_BYTES = 5 * 1024 * 1024
DISPLAY_PICTURE_ALLOWED_TYPES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }
)
DISPLAY_PICTURE_ALLOWED_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
COMPANY_PROFILES_DIR = _BACKEND_ROOT / "assets" / "company-profiles"


def normalize_display_name(value: str | None, *, max_len: int = DISPLAY_NAME_MAX_LEN) -> str:
    """Strip control chars; collapse whitespace; cap length."""
    raw = str(value or "")
    cleaned: list[str] = []
    for ch in raw:
        if ord(ch) < 32:
            cleaned.append(" ")
            continue
        cleaned.append(ch)
    text = " ".join("".join(cleaned).split())
    return text[:max_len].strip()


def guess_image_content_type(filename: str | None, content_type: str | None) -> str:
    ctype = str(content_type or "").strip().lower().split(";")[0].strip()
    if ctype in DISPLAY_PICTURE_ALLOWED_TYPES:
        return "image/jpeg" if ctype == "image/jpg" else ctype
    ext = Path(filename or "").suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return ctype


def validate_display_picture(
    *,
    filename: str | None,
    content_type: str | None,
    size: int,
) -> str:
    """Return normalized MIME type or raise ValueError."""
    if size <= 0:
        raise ValueError("Image file is empty.")
    if size > DISPLAY_PICTURE_MAX_BYTES:
        raise ValueError(
            f"Image must be {DISPLAY_PICTURE_MAX_BYTES // (1024 * 1024)} MB or smaller."
        )
    ctype = guess_image_content_type(filename, content_type)
    ext = Path(filename or "").suffix.lower()
    if ctype not in DISPLAY_PICTURE_ALLOWED_TYPES and ext not in DISPLAY_PICTURE_ALLOWED_EXTS:
        raise ValueError("Invalid image format. Use JPEG, PNG, or WebP.")
    if ctype not in DISPLAY_PICTURE_ALLOWED_TYPES:
        raise ValueError("Invalid image format. Use JPEG, PNG, or WebP.")
    return "image/jpeg" if ctype == "image/jpg" else ctype


def extension_for_content_type(content_type: str) -> str:
    ctype = str(content_type or "").lower()
    if ctype == "image/png":
        return ".png"
    if ctype == "image/webp":
        return ".webp"
    return ".jpg"


def company_profile_relative_path(company_id: str, content_type: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(company_id or ""))
    if not safe_id:
        raise ValueError("Invalid company id for profile picture storage.")
    ext = extension_for_content_type(content_type)
    return f"company-profiles/{safe_id}{ext}"


def absolute_profile_path(relative_path: str) -> Path:
    rel = str(relative_path or "").lstrip("/").replace("\\", "/")
    if ".." in rel.split("/"):
        raise ValueError("Invalid profile picture path.")
    return _BACKEND_ROOT / "assets" / rel


def public_display_picture_url(relative_or_url: str | None) -> str:
    """Return a browser-loadable URL for a stored relative path or absolute URL."""
    import os

    raw = str(relative_or_url or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    rel = raw.lstrip("/")
    if rel.startswith("assets/"):
        rel = rel[len("assets/") :]
    base = (
        os.getenv("BACKEND_BASE_URL")
        or os.getenv("PUBLIC_API_URL")
        or os.getenv("API_BASE_URL")
        or ""
    ).strip().strip('"').strip("'").rstrip("/")
    if base:
        return f"{base}/assets/{rel}"
    return f"/assets/{rel}"
