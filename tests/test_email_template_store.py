from contextlib import contextmanager

import pytest

import services.email_template_store as store
import services.email_component_service as components
from services.email_component_service import normalize_components, render_components
from services.email_template_store import (
    EmailTemplateError,
    normalize_template_key,
    sanitize_rich_text,
    validate_template_content,
)
from services.template_token_service import (
    apply_named_tokens,
    missing_named_placeholder_values,
    named_token_values,
    unresolved_placeholders,
)


def test_template_key_is_stable_and_normalized():
    assert normalize_template_key("event-follow-up") == "EVENT_FOLLOW_UP"
    assert normalize_template_key("card follow up") == "CARD_FOLLOW_UP"


def test_unknown_placeholder_is_rejected_before_save():
    with pytest.raises(EmailTemplateError, match="unsupported"):
        validate_template_content("Hello {{name}}", "<p>{{account_secret}}</p>")


def test_rich_text_is_sanitized_but_email_components_are_preserved():
    value = sanitize_rich_text(
        '<p style="text-align:center;position:fixed">Hello <strong>{{name}}</strong></p>'
        '<script>alert(1)</script>'
        '<a href="javascript:alert(1)">bad</a>'
        '<img src="https://example.com/banner.png" onerror="alert(1)">'
    )
    assert "text-align:center" in value
    assert "position" not in value
    assert "script" not in value
    assert "alert(1)" not in value
    assert "onerror" not in value
    assert 'src="https://example.com/banner.png"' in value


def test_supported_named_placeholders_render_without_leaking_tokens():
    values = named_token_values(
        {
            "fullName": "John Doe",
            "emailAddress": "john@example.com",
            "phoneNumber": "+91 99999 99999",
            "designation": "Director",
        },
        company="ABC Technologies",
        event_name="Tech Expo",
        sender_name="Priya",
    )
    rendered = apply_named_tokens(
        "Hello {{name}} from {{company}} at {{event_name}} — {{sender_name}}",
        values,
    )
    assert rendered == "Hello John Doe from ABC Technologies at Tech Expo — Priya"
    assert unresolved_placeholders(rendered) == []


def test_unresolved_placeholder_detection_is_explicit():
    assert unresolved_placeholders("Hello {{name}} and {{ missing }}") == [
        "missing",
        "name",
    ]


def test_missing_placeholder_values_are_detected_before_send():
    assert missing_named_placeholder_values(
        "Hi {{name}} from {{company}}",
        {"name": "", "company": "ABC"},
    ) == ["name"]


def test_template_list_query_is_always_scoped_to_authenticated_account(monkeypatch):
    calls = []

    class Cursor:
        def execute(self, query, params):
            calls.append((query, params))

        def fetchall(self):
            return []

    @contextmanager
    def fake_db_cursor(*, commit=False):
        assert commit is False
        yield Cursor()

    monkeypatch.setattr(store, "db_cursor", fake_db_cursor)
    assert store.list_email_templates("account-a") == []
    query, params = calls[0]
    assert "WHERE t.account_id = %s" in query
    assert params == ("account-a",)


def test_template_lookup_cannot_return_another_accounts_id(monkeypatch):
    calls = []

    class Cursor:
        def execute(self, query, params):
            calls.append((query, params))

        def fetchone(self):
            return None

    @contextmanager
    def fake_db_cursor(*, commit=False):
        assert commit is False
        yield Cursor()

    monkeypatch.setattr(store, "db_cursor", fake_db_cursor)
    assert store.get_email_template("account-a", "template-owned-by-b") is None
    query, params = calls[0]
    assert "t.id = %s AND t.account_id = %s" in query
    assert params == ("template-owned-by-b", "account-a")


def test_structured_media_must_belong_to_template_account(monkeypatch):
    def media(account_id, media_id):
        if account_id == "account-a" and media_id == "image-a":
            return {
                "id": "image-a",
                "kind": "image",
                "url": "https://cdn.example/a.png",
                "original_name": "a.png",
            }
        return None

    monkeypatch.setattr(components, "get_account_media", media)
    valid = normalize_components(
        "account-a",
        [{"id": "one", "type": "image", "media_id": "image-a"}],
        sanitize_text=sanitize_rich_text,
    )
    assert valid[0]["media_id"] == "image-a"
    with pytest.raises(ValueError, match="another account"):
        normalize_components(
            "account-b",
            [{"id": "one", "type": "image", "media_id": "image-a"}],
            sanitize_text=sanitize_rich_text,
        )


def test_structured_renderer_outputs_email_safe_components(monkeypatch):
    monkeypatch.setattr(
        components,
        "get_account_media",
        lambda account_id, media_id: {
            "id": media_id,
            "kind": "document",
            "url": "https://cdn.example/brochure.pdf",
            "original_name": "brochure.pdf",
        },
    )
    html = render_components(
        "account-a",
        [
            {"id": "text", "type": "text", "content": "Hi {{name}}"},
            {
                "id": "pdf",
                "type": "brochure",
                "media_id": "pdf-a",
                "title": "Company Brochure",
            },
            {"id": "divider", "type": "divider"},
        ],
    )
    assert "Hi {{name}}" in html
    assert "https://cdn.example/brochure.pdf" in html
    assert "Company Brochure" in html
    assert "VIEW BROCHURE" in html
    assert "<hr" in html
    assert 'max-width:600px' in html


def test_header_image_text_and_footer_render(monkeypatch):
    def media(account_id, media_id):
        kinds = {"logo": "image", "photo": "image"}
        return {
            "id": media_id,
            "kind": kinds.get(media_id, "image"),
            "url": f"https://cdn.example/{media_id}.png",
            "original_name": f"{media_id}.png",
        }

    monkeypatch.setattr(components, "get_account_media", media)
    html = render_components(
        "account-a",
        [
            {
                "id": "hdr",
                "type": "header",
                "logo_media_id": "logo",
                "align": "center",
                "logo_width": "150",
                "company_name": "ABC Technologies",
                "website": "https://abc.example",
            },
            {
                "id": "mix",
                "type": "image_text",
                "media_id": "photo",
                "content": "<p>Hello {{name}}</p>",
                "layout": "image_left",
            },
            {
                "id": "ft",
                "type": "footer",
                "company_name": "ABC Technologies",
                "website": "https://abc.example",
                "linkedin_url": "https://linkedin.com/company/abc",
                "contact": "hello@abc.example",
                "copyright": "© 2026 ABC Technologies",
            },
        ],
    )
    assert "ABC Technologies" in html
    assert "https://cdn.example/logo.png" in html
    assert "Hello {{name}}" in html
    assert "LinkedIn" in html
    assert "© 2026 ABC Technologies" in html


def test_header_requires_logo_or_company(monkeypatch):
    monkeypatch.setattr(components, "get_account_media", lambda *_args: None)
    with pytest.raises(ValueError, match="logo or company"):
        normalize_components(
            "account-a",
            [{"id": "hdr", "type": "header", "logo_media_id": "", "company_name": ""}],
            sanitize_text=sanitize_rich_text,
        )
