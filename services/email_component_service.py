"""Validate and render structured email-template components."""

from __future__ import annotations

import re
from html import escape
from typing import Any, Callable

from services.cms_media_service import get_account_media

COMPONENT_TYPES = {
    "header",
    "text",
    "image",
    "image_text",
    "video",
    "brochure",
    "button",
    "divider",
    "footer",
}

CANVAS_WIDTH = 600
BLOCK_GAP = 24
PAD_X = 36
FONT = "Arial, Helvetica, sans-serif"
TEXT_COLOR = "#1e293b"
MUTED_COLOR = "#64748b"
BORDER = "#e2e8f0"
BUTTON_BG = "#2563eb"
SURFACE = "#f8fafc"

_ALIGN = {"left", "center", "right"}
_IMAGE_SIZE = {"full", "medium", "small"}
_LOGO_WIDTH = {"auto", "100", "150", "200"}
_BUTTON_STYLE = {"primary", "secondary", "outline"}
_IMAGE_TEXT_LAYOUT = {"image_left", "image_right"}
_DIVIDER_SPACE = {"16", "24", "32"}


class EmailComponentError(ValueError):
    pass


def _web_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith(("https://", "http://")):
        return url
    return ""


def _choice(value: Any, allowed: set[str], default: str) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in allowed else default


def _plain(value: Any, limit: int = 255) -> str:
    return str(value or "").strip()[:limit]


def _require_media(account_id: str, media_id: Any, kind: str | None = None) -> dict:
    value = str(media_id or "").strip()
    if not value:
        raise EmailComponentError("A media selection is required.")
    media = get_account_media(account_id, value)
    if not media:
        raise EmailComponentError("Selected media is missing or belongs to another account.")
    if kind and media.get("kind") != kind:
        raise EmailComponentError(f"Selected media must be a {kind}.")
    return media


def _optional_media(account_id: str, media_id: Any, kind: str | None = None) -> dict | None:
    if not str(media_id or "").strip():
        return None
    return _require_media(account_id, media_id, kind)


def _inject_style(html: str, tag: str, style: str) -> str:
    def repl(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        if re.search(r"\sstyle\s*=", attrs, flags=re.IGNORECASE):
            return re.sub(
                r'style\s*=\s*"([^"]*)"',
                lambda found: f'style="{style};{found.group(1)}"',
                match.group(0),
                count=1,
                flags=re.IGNORECASE,
            )
        if attrs:
            return f'<{tag}{attrs} style="{style}">'
        return f'<{tag} style="{style}">'

    return re.sub(rf"<{tag}(\s[^>]*)?>", repl, html, flags=re.IGNORECASE)


def _style_rich_text(content: str) -> str:
    html = str(content or "").strip()
    if not html:
        return ""
    lowered = html.lower()
    if "<p" not in lowered and "<div" not in lowered and "<ul" not in lowered and "<ol" not in lowered:
        html = f"<p>{html}</p>"
    html = _inject_style(html, "p", "margin:0 0 14px;line-height:1.7;color:#1e293b;font-size:16px;")
    html = _inject_style(html, "div", "margin:0 0 14px;line-height:1.7;color:#1e293b;font-size:16px;")
    html = _inject_style(html, "ul", "margin:0 0 14px;padding-left:22px;line-height:1.7;color:#1e293b;")
    html = _inject_style(html, "ol", "margin:0 0 14px;padding-left:22px;line-height:1.7;color:#1e293b;")
    html = _inject_style(html, "li", "margin:0 0 6px;")
    return html


def _row(inner: str, *, padding: str | None = None, extra: str = "", align: str = "left") -> str:
    pad = padding if padding is not None else f"0 {PAD_X}px {BLOCK_GAP}px"
    return (
        f'<tr><td align="{align}" style="padding:{pad};font-family:{FONT};{extra}">'
        f"{inner}</td></tr>"
    )


def _button_html(label: str, url: str, style: str, align: str) -> str:
    if style == "secondary":
        look = "background:#0f172a;color:#ffffff;border:1px solid #0f172a;"
    elif style == "outline":
        look = "background:#ffffff;color:#2563eb;border:2px solid #2563eb;"
    else:
        look = f"background:{BUTTON_BG};color:#ffffff;border:1px solid {BUTTON_BG};"
    return (
        f'<div style="text-align:{align};">'
        f'<a href="{escape(url, quote=True)}" target="_blank" '
        f'style="display:inline-block;{look}padding:12px 26px;border-radius:8px;'
        f'text-decoration:none;font-weight:700;font-size:14px;letter-spacing:0.02em;'
        f'font-family:{FONT};">{escape(label)}</a></div>'
    )


def _img_width(size: str) -> str:
    if size == "small":
        return "220"
    if size == "medium":
        return "420"
    return "568"


def normalize_components(
    account_id: str,
    raw_components: Any,
    *,
    sanitize_text: Callable[[Any], str],
) -> list[dict[str, Any]]:
    if not isinstance(raw_components, list) or not raw_components:
        raise EmailComponentError("Add at least one email content component.")

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_components):
        if not isinstance(raw, dict):
            raise EmailComponentError(f"Component {index + 1} is invalid.")
        kind = str(raw.get("type") or "").strip().lower()
        if kind not in COMPONENT_TYPES:
            raise EmailComponentError(f"Unsupported component type: {kind or 'empty'}.")

        component_id = str(raw.get("id") or f"component-{index + 1}")[:80]
        if kind == "text":
            content = sanitize_text(raw.get("content"))
            if not content:
                raise EmailComponentError(f"Text component {index + 1} is empty.")
            normalized.append({"id": component_id, "type": kind, "content": content})
        elif kind == "header":
            logo_id = str(raw.get("logo_media_id") or raw.get("media_id") or "").strip()
            if logo_id:
                _require_media(account_id, logo_id, "image")
            company_name = _plain(raw.get("company_name"), 160)
            website = _web_url(raw.get("website"))
            if not logo_id and not company_name:
                raise EmailComponentError("Header needs a logo or company name.")
            normalized.append(
                {
                    "id": component_id,
                    "type": kind,
                    "logo_media_id": logo_id or None,
                    "align": _choice(raw.get("align"), _ALIGN, "center"),
                    "logo_width": _choice(raw.get("logo_width"), _LOGO_WIDTH, "150"),
                    "company_name": company_name,
                    "website": website,
                }
            )
        elif kind == "image":
            media = _require_media(account_id, raw.get("media_id"), "image")
            normalized.append(
                {
                    "id": component_id,
                    "type": kind,
                    "media_id": media["id"],
                    "alt": _plain(raw.get("alt") or media["original_name"]),
                    "size": _choice(raw.get("size"), _IMAGE_SIZE, "full"),
                    "align": _choice(raw.get("align"), _ALIGN, "center"),
                }
            )
        elif kind == "image_text":
            media = _require_media(account_id, raw.get("media_id"), "image")
            content = sanitize_text(raw.get("content"))
            if not content:
                raise EmailComponentError("Image + text needs written content.")
            normalized.append(
                {
                    "id": component_id,
                    "type": kind,
                    "media_id": media["id"],
                    "alt": _plain(raw.get("alt") or media["original_name"]),
                    "content": content,
                    "layout": _choice(raw.get("layout"), _IMAGE_TEXT_LAYOUT, "image_left"),
                }
            )
        elif kind == "brochure":
            media = _require_media(account_id, raw.get("media_id"), "document")
            normalized.append(
                {
                    "id": component_id,
                    "type": kind,
                    "media_id": media["id"],
                    "title": _plain(raw.get("title") or media["original_name"]),
                    "description": _plain(
                        raw.get("description") or "Learn more about our company.",
                        400,
                    ),
                }
            )
        elif kind == "video":
            video_url = _web_url(raw.get("video_url"))
            media_id = str(raw.get("media_id") or "").strip()
            if media_id:
                media = _require_media(account_id, media_id, "video")
                video_url = video_url or str(media["url"])
            if not video_url:
                raise EmailComponentError("Video requires an uploaded MP4 or a valid video URL.")
            thumbnail_id = str(raw.get("thumbnail_media_id") or "").strip()
            if thumbnail_id:
                _require_media(account_id, thumbnail_id, "image")
            normalized.append(
                {
                    "id": component_id,
                    "type": kind,
                    "media_id": media_id or None,
                    "video_url": video_url,
                    "thumbnail_media_id": thumbnail_id or None,
                    "title": _plain(raw.get("title") or "Watch video"),
                }
            )
        elif kind == "button":
            label = _plain(raw.get("label") or raw.get("text"), 120)
            url = _web_url(raw.get("url"))
            if not label or not url:
                raise EmailComponentError("Button requires a label and valid URL.")
            normalized.append(
                {
                    "id": component_id,
                    "type": kind,
                    "label": label,
                    "url": url,
                    "align": _choice(raw.get("align"), _ALIGN, "center"),
                    "style": _choice(raw.get("style"), _BUTTON_STYLE, "primary"),
                }
            )
        elif kind == "footer":
            company_name = _plain(raw.get("company_name"), 160)
            website = _web_url(raw.get("website"))
            linkedin_url = _web_url(raw.get("linkedin_url"))
            contact = _plain(raw.get("contact"), 160)
            copyright_text = _plain(raw.get("copyright"), 200)
            if not any((company_name, website, linkedin_url, contact)):
                raise EmailComponentError("Footer needs a company name, website, or contact.")
            normalized.append(
                {
                    "id": component_id,
                    "type": kind,
                    "company_name": company_name,
                    "website": website,
                    "linkedin_url": linkedin_url,
                    "contact": contact,
                    "copyright": copyright_text,
                }
            )
        else:
            normalized.append(
                {
                    "id": component_id,
                    "type": "divider",
                    "spacing": _choice(raw.get("spacing"), _DIVIDER_SPACE, "24"),
                }
            )
    return normalized


def _render_header(account_id: str, component: dict[str, Any]) -> str:
    align = _choice(component.get("align"), _ALIGN, "center")
    width = _choice(component.get("logo_width"), _LOGO_WIDTH, "150")
    parts: list[str] = []
    logo = _optional_media(account_id, component.get("logo_media_id"), "image")
    if logo:
        px = "auto" if width == "auto" else f"{width}px"
        parts.append(
            f'<img src="{escape(str(logo["url"]), quote=True)}" '
            f'alt="{escape(_plain(component.get("company_name") or "Logo"), quote=True)}" '
            f'style="display:inline-block;max-width:{px};width:{px};height:auto;border:0;">'
        )
    if component.get("company_name"):
        parts.append(
            f'<div style="margin:{"12px 0 0" if logo else "0"};font-size:18px;font-weight:700;'
            f'color:{TEXT_COLOR};letter-spacing:-0.02em;">{escape(component["company_name"])}</div>'
        )
    if component.get("website"):
        parts.append(
            f'<div style="margin-top:6px;font-size:13px;">'
            f'<a href="{escape(component["website"], quote=True)}" style="color:{BUTTON_BG};'
            f'text-decoration:none;">{escape(component["website"].replace("https://", "").replace("http://", ""))}</a></div>'
        )
    inner = "".join(parts) or "&nbsp;"
    return _row(
        inner,
        padding=f"32px {PAD_X}px 20px",
        extra=f"border-bottom:1px solid {BORDER};",
        align=align,
    )


def _render_image(account_id: str, component: dict[str, Any]) -> str:
    media = _require_media(account_id, component.get("media_id"), "image")
    size = _choice(component.get("size"), _IMAGE_SIZE, "full")
    align = _choice(component.get("align"), _ALIGN, "center")
    width = _img_width(size)
    img = (
        f'<img src="{escape(str(media["url"]), quote=True)}" '
        f'alt="{escape(_plain(component.get("alt")), quote=True)}" '
        f'width="{width}" '
        f'style="display:block;width:100%;max-width:{width}px;height:auto;border:0;border-radius:10px;'
        f'{"margin:0 auto;" if align == "center" else ""}">'
    )
    return _row(img, align=align)


def _render_image_text(account_id: str, component: dict[str, Any]) -> str:
    media = _require_media(account_id, component.get("media_id"), "image")
    text = _style_rich_text(str(component.get("content") or ""))
    img = (
        f'<img src="{escape(str(media["url"]), quote=True)}" '
        f'alt="{escape(_plain(component.get("alt")), quote=True)}" '
        'width="240" style="display:block;width:100%;max-width:240px;height:auto;border:0;border-radius:10px;">'
    )
    image_first = _choice(component.get("layout"), _IMAGE_TEXT_LAYOUT, "image_left") == "image_left"
    left, right = (img, text) if image_first else (text, img)
    table = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        "<tr>"
        f'<td width="46%" valign="top" style="padding-right:16px;font-family:{FONT};">{left}</td>'
        f'<td width="54%" valign="middle" style="font-family:{FONT};color:{TEXT_COLOR};">{right}</td>'
        "</tr></table>"
    )
    return _row(table)


def _render_brochure(account_id: str, component: dict[str, Any]) -> str:
    media = _require_media(account_id, component.get("media_id"), "document")
    title = escape(_plain(component.get("title") or media["original_name"]))
    description = escape(_plain(component.get("description") or "Learn more about our company.", 400))
    card = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid {BORDER};border-radius:12px;background:{SURFACE};">'
        "<tr><td style="
        f'"padding:22px 24px;text-align:center;font-family:{FONT};">'
        f'<div style="font-size:16px;font-weight:700;color:{TEXT_COLOR};margin:0 0 8px;">'
        f"&#128196; {title}</div>"
        f'<div style="font-size:14px;line-height:1.6;color:{MUTED_COLOR};margin:0 0 16px;">{description}</div>'
        f'<a href="{escape(str(media["url"]), quote=True)}" target="_blank" '
        f'style="display:inline-block;background:{BUTTON_BG};color:#ffffff;padding:11px 22px;'
        f'border-radius:8px;text-decoration:none;font-weight:700;font-size:13px;">VIEW BROCHURE</a>'
        "</td></tr></table>"
    )
    return _row(card)


def _render_video(account_id: str, component: dict[str, Any]) -> str:
    video_url = _web_url(component.get("video_url"))
    if component.get("media_id"):
        video = _require_media(account_id, component["media_id"], "video")
        video_url = video_url or str(video["url"])
    if not video_url:
        raise EmailComponentError("Template video is missing.")
    title = escape(_plain(component.get("title") or "Watch video"))
    thumbnail_id = component.get("thumbnail_media_id")
    if thumbnail_id:
        thumb = _require_media(account_id, thumbnail_id, "image")
        visual = (
            '<div style="position:relative;line-height:0;">'
            f'<img src="{escape(str(thumb["url"]), quote=True)}" alt="{title}" '
            'style="display:block;width:100%;max-width:568px;height:auto;margin:0 auto;border:0;border-radius:12px;">'
            "</div>"
        )
    else:
        visual = (
            f'<div style="background:#0f172a;border-radius:12px;min-height:180px;'
            f'display:block;text-align:center;padding:56px 16px;color:#ffffff;">&nbsp;</div>'
        )
    card = (
        f'<a href="{escape(video_url, quote=True)}" target="_blank" style="text-decoration:none;display:block;">'
        f"{visual}"
        f'<div style="text-align:center;margin-top:12px;">'
        f'<span style="display:inline-block;background:{BUTTON_BG};color:#ffffff;width:48px;height:48px;'
        f'line-height:48px;border-radius:999px;font-size:18px;">&#9654;</span>'
        f'<div style="margin-top:8px;font-weight:700;color:{BUTTON_BG};font-size:14px;">{title}</div>'
        "</div></a>"
    )
    return _row(card, align="center")


def _render_footer(component: dict[str, Any]) -> str:
    links: list[str] = []
    if component.get("website"):
        label = component["website"].replace("https://", "").replace("http://", "").rstrip("/")
        links.append(
            f'<a href="{escape(component["website"], quote=True)}" style="color:{BUTTON_BG};text-decoration:none;">{escape(label)}</a>'
        )
    if component.get("linkedin_url"):
        links.append(
            f'<a href="{escape(component["linkedin_url"], quote=True)}" style="color:{BUTTON_BG};text-decoration:none;">LinkedIn</a>'
        )
    if component.get("contact"):
        contact = component["contact"]
        href = f"mailto:{contact}" if "@" in contact and not contact.startswith("http") else _web_url(contact)
        if href:
            links.append(
                f'<a href="{escape(href, quote=True)}" style="color:{BUTTON_BG};text-decoration:none;">{escape(contact)}</a>'
            )
        else:
            links.append(escape(contact))
    company = escape(_plain(component.get("company_name")))
    copyright_text = escape(_plain(component.get("copyright")))
    inner = ""
    if company:
        inner += f'<div style="font-size:15px;font-weight:700;color:{TEXT_COLOR};margin:0 0 8px;">{company}</div>'
    if links:
        inner += f'<div style="font-size:13px;color:{MUTED_COLOR};">{ " &nbsp;|&nbsp; ".join(links) }</div>'
    if copyright_text:
        inner += f'<div style="margin-top:12px;font-size:12px;color:{MUTED_COLOR};">{copyright_text}</div>'
    return _row(
        inner or "&nbsp;",
        padding=f"24px {PAD_X}px 32px",
        extra=f"background:{SURFACE};border-top:1px solid {BORDER};text-align:center;",
        align="center",
    )


def render_components(account_id: str, components: Any) -> str:
    if not isinstance(components, list) or not components:
        raise EmailComponentError("Template has no structured content.")

    rows: list[str] = []
    for component in components:
        kind = str(component.get("type") or "")
        if kind == "header":
            rows.append(_render_header(account_id, component))
        elif kind == "text":
            rows.append(_row(_style_rich_text(str(component.get("content") or ""))))
        elif kind == "image":
            rows.append(_render_image(account_id, component))
        elif kind == "image_text":
            rows.append(_render_image_text(account_id, component))
        elif kind == "brochure":
            rows.append(_render_brochure(account_id, component))
        elif kind == "video":
            rows.append(_render_video(account_id, component))
        elif kind == "button":
            url = _web_url(component.get("url"))
            if not url:
                raise EmailComponentError("Template button URL is invalid.")
            rows.append(
                _row(
                    _button_html(
                        _plain(component.get("label") or "Open", 120),
                        url,
                        _choice(component.get("style"), _BUTTON_STYLE, "primary"),
                        _choice(component.get("align"), _ALIGN, "center"),
                    )
                )
            )
        elif kind == "divider":
            space = _choice(component.get("spacing"), _DIVIDER_SPACE, "24")
            rows.append(
                _row(
                    f'<hr style="margin:0;border:0;border-top:1px solid {BORDER};">',
                    padding=f"{space}px {PAD_X}px",
                )
            )
        elif kind == "footer":
            rows.append(_render_footer(component))
        else:
            raise EmailComponentError(f"Unsupported stored component type: {kind}.")

    canvas = (
        f'<table role="presentation" width="{CANVAS_WIDTH}" cellpadding="0" cellspacing="0" '
        f'style="width:100%;max-width:{CANVAS_WIDTH}px;background:#ffffff;border-collapse:collapse;'
        f'font-family:{FONT};color:{TEXT_COLOR};">'
        f"{''.join(rows)}"
        "</table>"
    )
    return (
        '<tr><td align="center" style="padding:0;">'
        f"{canvas}"
        "</td></tr>"
    )
