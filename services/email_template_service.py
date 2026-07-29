"""Load and render reusable HTML email templates."""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


@lru_cache(maxsize=4)
def _load_template(name: str) -> str:
    path = _TEMPLATES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"Email template not found: {path}")
    return path.read_text(encoding="utf-8")


def render_thank_you_email_html(context: dict[str, str]) -> str:
    """Render thank-you.html with pre-escaped token values."""
    template = _load_template("thank-you.html")
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def render_cc_scanned_contact_email_html(context: dict[str, str]) -> str:
    """Render cc_scanned_contact_email.html with pre-escaped token values."""
    template = _load_template("cc_scanned_contact_email.html")
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def cc_scanned_contact_email_context(
    *,
    company: str,
    subject: str,
    detail_rows: str,
    year: str,
    brand_primary: str,
    brand_primary_dark: str,
    brand_text: str,
    brand_muted: str,
    brand_surface: str,
    brand_border: str,
) -> dict[str, str]:
    """Build escaped placeholder map for the CC scanned-contact template."""
    return {
        "COMPANY": html.escape(company),
        "SUBJECT": html.escape(subject),
        "DETAIL_ROWS": detail_rows,
        "YEAR": html.escape(year),
        "BRAND_PRIMARY": brand_primary,
        "BRAND_PRIMARY_DARK": brand_primary_dark,
        "BRAND_TEXT": brand_text,
        "BRAND_MUTED": brand_muted,
        "BRAND_SURFACE": brand_surface,
        "BRAND_BORDER": brand_border,
    }


def thank_you_email_context(
    *,
    greeting: str,
    company: str,
    subject: str,
    reply_href: str,
    contact_rows: str,
    year: str,
    brand_primary: str,
    brand_primary_dark: str,
    brand_accent: str,
    brand_text: str,
    brand_muted: str,
    brand_surface: str,
    brand_border: str,
    pdf_download_href: str = "",
    assets_base: str = "",
    event_name: str = "",
) -> dict[str, str]:
    """Build escaped placeholder map for the thank-you template."""
    return {
        "GREETING": html.escape(greeting),
        "COMPANY": html.escape(company),
        "SUBJECT": html.escape(subject),
        "REPLY_HREF": html.escape(reply_href, quote=True),
        "CONTACT_ROWS": contact_rows,
        "YEAR": html.escape(year),
        "EVENT_NAME": html.escape(event_name),
        "BRAND_PRIMARY": brand_primary,
        "BRAND_PRIMARY_DARK": brand_primary_dark,
        "BRAND_ACCENT": brand_accent,
        "BRAND_TEXT": brand_text,
        "BRAND_MUTED": brand_muted,
        "BRAND_SURFACE": brand_surface,
        "BRAND_BORDER": brand_border,
        "PDF_DOWNLOAD_HREF": html.escape(pdf_download_href, quote=True),
        "ASSETS_BASE": html.escape(assets_base, quote=True),
    }
