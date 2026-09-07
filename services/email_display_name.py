"""Company-specific From display name (does not change mailbox, SMTP, or Reply-To)."""

from __future__ import annotations

EMAIL_DISPLAY_NAME_MAX_LEN = 255


def normalize_email_display_name(value: str | None, *, max_len: int = EMAIL_DISPLAY_NAME_MAX_LEN) -> str:
    """Strip control chars / angle brackets; collapse whitespace; cap length."""
    raw = str(value or "")
    cleaned = []
    for ch in raw:
        if ch in "<>":
            continue
        if ord(ch) < 32:
            cleaned.append(" ")
            continue
        cleaned.append(ch)
    text = " ".join("".join(cleaned).split())
    return text[:max_len].strip()
