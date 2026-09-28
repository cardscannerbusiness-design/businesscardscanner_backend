"""Account-isolated CRUD and selection for dynamically managed email templates."""

from __future__ import annotations

import json
import re
from html import escape
from html.parser import HTMLParser
from typing import Any

from db.pool import db_cursor
from services.email_component_service import (
    EmailComponentError,
    normalize_components,
    render_components,
)
from services.template_token_service import normalize_token_map

DEFAULT_TEMPLATE_KEY = "CARD_FOLLOW_UP"
TEMPLATE_STATUSES = {"active", "inactive"}

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_SUPPORTED_NAMED_PLACEHOLDERS = {
    "name",
    "first_name",
    "last_name",
    "company",
    "email",
    "phone",
    "designation",
    "event_name",
    "sender_name",
    "GREETING",
    "DISPLAY_NAME",
    "COMPANY",
    "SUBJECT",
    "REPLY_HREF",
    "CONTACT_ROWS",
    "YEAR",
    "EVENT_NAME",
    "BRAND_PRIMARY",
    "BRAND_PRIMARY_DARK",
    "BRAND_ACCENT",
    "BRAND_TEXT",
    "BRAND_MUTED",
    "BRAND_SURFACE",
    "BRAND_BORDER",
    "PDF_DOWNLOAD_HREF",
    "ASSETS_BASE",
}
_ALLOWED_RICH_TEXT_TAGS = {
    "a", "b", "br", "div", "em", "font", "hr", "i", "img", "li", "ol", "p",
    "span", "strong", "u", "ul",
}
_VOID_TAGS = {"br", "hr", "img"}
_ALLOWED_STYLE_PROPERTIES = {
    "color", "background-color", "font-size", "font-weight", "font-style",
    "text-align", "text-decoration", "line-height", "margin", "margin-top",
    "margin-bottom", "padding", "padding-top", "padding-bottom", "border",
    "border-radius", "display", "max-width", "width", "height",
}


class EmailTemplateError(ValueError):
    """A safe, user-facing template validation or lookup error."""


def _safe_url(value: str) -> str:
    url = str(value or "").strip()
    if url.startswith(("https://", "http://", "mailto:")):
        return url
    return ""


def _safe_style(value: str) -> str:
    declarations: list[str] = []
    for declaration in str(value or "").split(";"):
        if ":" not in declaration:
            continue
        prop, raw_value = declaration.split(":", 1)
        prop = prop.strip().lower()
        clean_value = raw_value.strip()
        lowered = clean_value.lower()
        if (
            prop in _ALLOWED_STYLE_PROPERTIES
            and "url(" not in lowered
            and "expression" not in lowered
            and "javascript:" not in lowered
        ):
            declarations.append(f"{prop}:{clean_value}")
    return ";".join(declarations)


class _RichTextSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.open_tags: list[str] = []
        self.blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "iframe", "object"}:
            self.blocked_depth += 1
            return
        if self.blocked_depth:
            return
        if tag not in _ALLOWED_RICH_TEXT_TAGS:
            return
        clean_attrs: list[str] = []
        for name, raw_value in attrs:
            name = name.lower()
            value = str(raw_value or "")
            if name == "style":
                value = _safe_style(value)
            elif name in {"href", "src"}:
                value = _safe_url(value)
            elif name not in {"alt", "title", "target", "width", "height", "size"}:
                continue
            if value:
                clean_attrs.append(f' {name}="{escape(value, quote=True)}"')
        if tag == "a" and not any(part.startswith(" href=") for part in clean_attrs):
            return
        self.output.append(f"<{tag}{''.join(clean_attrs)}>")
        if tag not in _VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "iframe", "object"} and self.blocked_depth:
            self.blocked_depth -= 1
            return
        if self.blocked_depth:
            return
        if tag not in self.open_tags:
            return
        while self.open_tags:
            opened = self.open_tags.pop()
            self.output.append(f"</{opened}>")
            if opened == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.output.append(escape(data))

    def result(self) -> str:
        while self.open_tags:
            self.output.append(f"</{self.open_tags.pop()}>")
        return "".join(self.output).strip()


def sanitize_rich_text(value: Any) -> str:
    sanitizer = _RichTextSanitizer()
    sanitizer.feed(str(value or ""))
    sanitizer.close()
    return sanitizer.result()


def normalize_template_key(value: Any) -> str:
    key = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not _KEY_RE.fullmatch(key):
        raise EmailTemplateError(
            "template_key must be 2-64 uppercase letters, numbers, or underscores."
        )
    return key


def validate_template_content(subject: Any, body: Any) -> tuple[str, str]:
    clean_subject = str(subject or "").strip()
    clean_body = sanitize_rich_text(body)
    if not clean_subject:
        raise EmailTemplateError("Email subject is required.")
    if not clean_body:
        raise EmailTemplateError("Email body is required.")

    unknown = sorted(
        {
            token
            for token in _PLACEHOLDER_RE.findall(f"{clean_subject}\n{clean_body}")
            if not token.isdigit() and token not in _SUPPORTED_NAMED_PLACEHOLDERS
        }
    )
    if unknown:
        raise EmailTemplateError(
            "Unsupported template placeholders: "
            + ", ".join(f"{{{{{token}}}}}" for token in unknown)
        )
    return clean_subject, clean_body


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for key in (
        "id", "account_id", "admin_user_id", "event_id", "created_by", "updated_by"
    ):
        if item.get(key) is not None:
            item[key] = str(item[key])
    for key in ("created_at", "updated_at"):
        if item.get(key) is not None and hasattr(item[key], "isoformat"):
            item[key] = item[key].isoformat()
    item["token_map"] = normalize_token_map(item.get("token_map"))
    item["components"] = item.get("components") if isinstance(item.get("components"), list) else []
    item["active"] = item.get("status") == "active"
    return item


def validate_account_event(account_id: str, event_id: Any) -> tuple[str | None, str]:
    value = str(event_id or "").strip()
    if not value:
        return None, ""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, name
            FROM managed_events
            WHERE id = %s AND company_id = %s AND deleted_at IS NULL
            """,
            (value, account_id),
        )
        row = cur.fetchone()
    if not row:
        raise EmailTemplateError("Selected event is invalid or belongs to another account.")
    return str(row["id"]), str(row["name"])


def list_account_events(account_id: str) -> list[dict[str, Any]]:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, name, status, start_date, end_date
            FROM managed_events
            WHERE company_id = %s AND deleted_at IS NULL
            ORDER BY created_at DESC
            """,
            (account_id,),
        )
        rows = cur.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["id"] = str(item["id"])
        for key in ("start_date", "end_date"):
            if item.get(key) is not None and hasattr(item[key], "isoformat"):
                item[key] = item[key].isoformat()
        items.append(item)
    return items


def resolve_account_event_id_by_name(account_id: str, event_name: Any) -> str | None:
    name = str(event_name or "").strip()
    if not name:
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id
            FROM managed_events
            WHERE company_id = %s AND LOWER(name) = LOWER(%s)
              AND deleted_at IS NULL
            """,
            (account_id, name),
        )
        row = cur.fetchone()
    return str(row["id"]) if row else None


def get_admin_scope(admin_user_id: str) -> tuple[str, str]:
    """Return (account_id, admin_user_id), rejecting non-Admin or deleted owners."""
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT u.company_id, u.id AS admin_user_id
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s
              AND u.company_id IS NOT NULL
              AND u.deleted_at IS NULL
              AND r.name = 'ADMIN'
            """,
            (admin_user_id,),
        )
        row = cur.fetchone()
    if not row:
        raise EmailTemplateError("Account owner not found.")
    return str(row["company_id"]), str(row["admin_user_id"])


def list_email_templates(account_id: str) -> list[dict[str, Any]]:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT t.id, t.account_id, t.admin_user_id, t.template_name, t.template_key,
                   t.subject, t.body, t.components, t.event_id, e.name AS event_name,
                   t.token_map, t.status, t.created_by, t.updated_by,
                   t.created_at, t.updated_at
            FROM email_templates t
            LEFT JOIN managed_events e ON e.id = t.event_id
            WHERE t.account_id = %s AND t.deleted_at IS NULL
            ORDER BY t.template_name ASC, t.created_at ASC
            """,
            (account_id,),
        )
        rows = cur.fetchall()
    return [_serialize(dict(row)) for row in rows]


def account_has_email_templates(account_id: str) -> bool:
    with db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT 1 FROM email_templates WHERE account_id = %s LIMIT 1",
            (account_id,),
        )
        return cur.fetchone() is not None


def account_has_event_templates(account_id: str) -> bool:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT 1 FROM email_templates
            WHERE account_id = %s AND event_id IS NOT NULL
            LIMIT 1
            """,
            (account_id,),
        )
        return cur.fetchone() is not None


def get_email_template(account_id: str, template_id: str) -> dict[str, Any] | None:
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT t.id, t.account_id, t.admin_user_id, t.template_name, t.template_key,
                   t.subject, t.body, t.components, t.event_id, e.name AS event_name,
                   t.token_map, t.status, t.created_by, t.updated_by,
                   t.created_at, t.updated_at
            FROM email_templates t
            LEFT JOIN managed_events e ON e.id = t.event_id
            WHERE t.id = %s AND t.account_id = %s AND t.deleted_at IS NULL
            """,
            (template_id, account_id),
        )
        row = cur.fetchone()
    return _serialize(dict(row)) if row else None


def get_active_email_template(
    admin_user_id: str,
    template_key: str = DEFAULT_TEMPLATE_KEY,
) -> dict[str, Any] | None:
    try:
        account_id, _ = get_admin_scope(admin_user_id)
        key = normalize_template_key(template_key)
    except EmailTemplateError:
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT t.id, t.account_id, t.admin_user_id, t.template_name, t.template_key,
                   t.subject, t.body, t.components, t.event_id, e.name AS event_name,
                   t.token_map, t.status, t.created_by, t.updated_by,
                   t.created_at, t.updated_at
            FROM email_templates t
            LEFT JOIN managed_events e ON e.id = t.event_id
            WHERE t.account_id = %s AND t.template_key = %s
              AND t.event_id IS NULL
              AND t.status = 'active' AND t.deleted_at IS NULL
            """,
            (account_id, key),
        )
        row = cur.fetchone()
    return _serialize(dict(row)) if row else None


def get_email_template_for_key(
    admin_user_id: str,
    template_key: str = DEFAULT_TEMPLATE_KEY,
) -> dict[str, Any] | None:
    try:
        account_id, _ = get_admin_scope(admin_user_id)
        key = normalize_template_key(template_key)
    except EmailTemplateError:
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT t.id, t.account_id, t.admin_user_id, t.template_name, t.template_key,
                   t.subject, t.body, t.components, t.event_id, e.name AS event_name,
                   t.token_map, t.status, t.created_by, t.updated_by,
                   t.created_at, t.updated_at
            FROM email_templates t
            LEFT JOIN managed_events e ON e.id = t.event_id
            WHERE t.account_id = %s AND t.template_key = %s
              AND t.event_id IS NULL AND t.deleted_at IS NULL
            """,
            (account_id, key),
        )
        row = cur.fetchone()
    return _serialize(dict(row)) if row else None


def get_email_template_for_event(
    admin_user_id: str,
    event_id: str,
) -> dict[str, Any] | None:
    try:
        account_id, _ = get_admin_scope(admin_user_id)
        valid_event_id, _ = validate_account_event(account_id, event_id)
    except EmailTemplateError:
        return None
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT t.id, t.account_id, t.admin_user_id, t.template_name, t.template_key,
                   t.subject, t.body, t.components, t.event_id, e.name AS event_name,
                   t.token_map, t.status, t.created_by, t.updated_by,
                   t.created_at, t.updated_at
            FROM email_templates t
            JOIN managed_events e ON e.id = t.event_id
            WHERE t.account_id = %s AND t.event_id = %s AND t.deleted_at IS NULL
            """,
            (account_id, valid_event_id),
        )
        row = cur.fetchone()
    return _serialize(dict(row)) if row else None


def create_email_template(
    *,
    account_id: str,
    admin_user_id: str,
    template_name: Any,
    template_key: Any,
    subject: Any,
    body: Any = "",
    components: Any = None,
    event_id: Any = None,
    token_map: Any = None,
    status: Any = "active",
    actor_id: str | None = None,
) -> dict[str, Any]:
    name = str(template_name or "").strip()
    if not name:
        raise EmailTemplateError("Template name is required.")
    key = normalize_template_key(template_key)
    valid_event_id, _ = validate_account_event(account_id, event_id)
    try:
        clean_components = (
            normalize_components(
                account_id,
                components,
                sanitize_text=sanitize_rich_text,
            )
            if components
            else []
        )
        generated_body = (
            render_components(account_id, clean_components)
            if clean_components
            else body
        )
    except EmailComponentError as exc:
        raise EmailTemplateError(str(exc)) from exc
    clean_subject, clean_body = validate_template_content(subject, generated_body)
    clean_status = str(status or "active").strip().lower()
    if clean_status not in TEMPLATE_STATUSES:
        raise EmailTemplateError("status must be active or inactive.")
    clean_map = normalize_token_map(token_map)

    with db_cursor() as cur:
        if valid_event_id:
            cur.execute(
                """
                SELECT 1 FROM email_templates
                WHERE account_id = %s AND event_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (account_id, valid_event_id),
            )
            conflict_message = "An email template already exists for this event."
        else:
            cur.execute(
                """
                SELECT 1 FROM email_templates
                WHERE account_id = %s AND event_id IS NULL
                  AND template_key = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (account_id, key),
            )
            conflict_message = f"A template with key {key} already exists for this account."
        if cur.fetchone():
            raise EmailTemplateError(conflict_message)
        cur.execute(
            """
            INSERT INTO email_templates (
                account_id, admin_user_id, template_name, template_key,
                subject, body, components, event_id, token_map, status,
                created_by, updated_by
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                %s::jsonb, %s, %s, %s
            )
            RETURNING id
            """,
            (
                account_id,
                admin_user_id,
                name[:160],
                key,
                clean_subject,
                clean_body,
                json.dumps(clean_components),
                valid_event_id,
                json.dumps(clean_map),
                clean_status,
                actor_id,
                actor_id,
            ),
        )
        row = cur.fetchone()
    item = get_email_template(account_id, str(row["id"]))
    if not item:
        raise EmailTemplateError("Template could not be loaded after creation.")
    return item


def update_email_template(
    account_id: str,
    template_id: str,
    changes: dict[str, Any],
    *,
    actor_id: str | None = None,
) -> dict[str, Any] | None:
    current = get_email_template(account_id, template_id)
    if not current:
        return None

    name = str(changes.get("template_name", current["template_name"]) or "").strip()
    if not name:
        raise EmailTemplateError("Template name is required.")
    key = normalize_template_key(changes.get("template_key", current["template_key"]))
    event_id, _ = validate_account_event(
        account_id,
        changes.get("event_id", current.get("event_id")),
    )
    raw_components = changes.get("components", current.get("components"))
    try:
        components = (
            normalize_components(
                account_id,
                raw_components,
                sanitize_text=sanitize_rich_text,
            )
            if raw_components
            else []
        )
        generated_body = (
            render_components(account_id, components)
            if components
            else changes.get("body", current["body"])
        )
    except EmailComponentError as exc:
        raise EmailTemplateError(str(exc)) from exc
    subject, body = validate_template_content(
        changes.get("subject", current["subject"]),
        generated_body,
    )
    status = str(changes.get("status", current["status"]) or "").strip().lower()
    if status not in TEMPLATE_STATUSES:
        raise EmailTemplateError("status must be active or inactive.")
    token_map = normalize_token_map(changes.get("token_map", current["token_map"]))

    with db_cursor() as cur:
        if event_id:
            cur.execute(
                """
                SELECT 1 FROM email_templates
                WHERE account_id = %s AND event_id = %s AND id <> %s
                  AND deleted_at IS NULL
                LIMIT 1
                """,
                (account_id, event_id, template_id),
            )
            conflict_message = "An email template already exists for this event."
        else:
            cur.execute(
                """
                SELECT 1 FROM email_templates
                WHERE account_id = %s AND event_id IS NULL
                  AND template_key = %s AND id <> %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (account_id, key, template_id),
            )
            conflict_message = f"A template with key {key} already exists for this account."
        if cur.fetchone():
            raise EmailTemplateError(conflict_message)
        cur.execute(
            """
            UPDATE email_templates
            SET template_name = %s, template_key = %s, subject = %s, body = %s,
                components = %s::jsonb, event_id = %s, token_map = %s::jsonb,
                status = %s, updated_by = %s, updated_at = NOW()
            WHERE id = %s AND account_id = %s AND deleted_at IS NULL
            RETURNING id
            """,
            (
                name[:160],
                key,
                subject,
                body,
                json.dumps(components),
                event_id,
                json.dumps(token_map),
                status,
                actor_id,
                template_id,
                account_id,
            ),
        )
        row = cur.fetchone()
    return get_email_template(account_id, str(row["id"])) if row else None


def delete_email_template(account_id: str, template_id: str) -> bool:
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE email_templates
            SET deleted_at = NOW(), updated_at = NOW()
            WHERE id = %s AND account_id = %s AND deleted_at IS NULL
            """,
            (template_id, account_id),
        )
        return cur.rowcount > 0
