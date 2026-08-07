"""Load and render reusable HTML email templates."""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


@lru_cache(maxsize=8)
def _load_template(name: str) -> str:
    path = _TEMPLATES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"Email template not found: {path}")
    return path.read_text(encoding="utf-8")


def clear_template_cache() -> None:
    """Clear cached template files (call after CMS body updates if needed)."""
    _load_template.cache_clear()


def get_thank_you_shell() -> str:
    return _load_template("thank-you.html")


def get_thank_you_body_default() -> str:
    return _load_template("thank-you-body-default.html")


def get_thank_you_body_cms_default() -> str:
    """Starter body fragment for CMS per-Admin editing."""
    path = _TEMPLATES_DIR / "thank-you-body-cms-default.html"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return get_thank_you_body_default()


def _apply_tokens(template: str, context: dict[str, str]) -> str:
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def render_thank_you_email_html(context: dict[str, str]) -> str:
    """Render thank-you shell + body.

    Fixed chrome lives in thank-you.html ({{BODY_HTML}}).
    Body comes from context['BODY_HTML'] or thank-you-body-default.html.
    """
    shell = get_thank_you_shell()
    body = (context.get("BODY_HTML") or "").strip() or get_thank_you_body_default()
    ctx = {k: v for k, v in context.items() if k != "BODY_HTML"}
    body = _apply_tokens(body, ctx)
    return _apply_tokens(shell, {**ctx, "BODY_HTML": body})


def render_cc_scanned_contact_email_html(context: dict[str, str]) -> str:
    """Render cc_scanned_contact_email.html with pre-escaped token values."""
    template = _load_template("cc_scanned_contact_email.html")
    return _apply_tokens(template, context)


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
    body_html: str = "",
    phone: str = "",
    email: str = "",
    website: str = "",
    sign_off_name: str = "",
    numbered_tokens: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build escaped placeholder map for the thank-you template."""
    greeting_esc = html.escape(greeting)
    phone_esc = html.escape(phone)
    email_esc = html.escape(email)
    website_esc = html.escape(website, quote=True)
    sign_esc = html.escape(sign_off_name or "Team")
    ctx: dict[str, str] = {
        "GREETING": greeting_esc,
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
        "BODY_HTML": body_html,
        # Defaults; overridden by CMS token_map when provided
        "1": greeting_esc,
        "2": phone_esc,
        "3": email_esc,
        "4": website_esc,
        "5": sign_esc,
    }
    if numbered_tokens:
        for num, value in numbered_tokens.items():
            ctx[str(num)] = html.escape(value or "")
    return ctx
