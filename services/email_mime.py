"""Shared MIME helpers for authenticated, standards-compliant outbound email."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr


def _normalize_env(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


def domain_of_email(address: str | None) -> str:
    _, addr = parseaddr(str(address or "").strip())
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].strip().lower()


def email_only(address: str | None) -> str:
    _, addr = parseaddr(str(address or "").strip())
    return addr.strip()


def apply_standard_headers(
    message: EmailMessage,
    *,
    from_address: str,
    to_address: str,
    subject: str,
    reply_to: str | None = None,
    cc_addresses: list[str] | None = None,
    message_id_domain: str | None = None,
) -> str:
    """
    Set RFC-compliant headers used by inbox providers for authentication/reputation.

    Returns the Message-ID value that was applied. Does not change subject or body content.
    """
    domain = (message_id_domain or domain_of_email(from_address) or "localhost").strip(".")
    message_id = make_msgid(domain=domain)

    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = to_address
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = message_id
    message["MIME-Version"] = "1.0"

    if reply_to:
        message["Reply-To"] = reply_to
    if cc_addresses:
        message["Cc"] = ", ".join(cc_addresses)

    return message_id


def resolve_envelope_mail_from(default_from: str) -> str:
    """
    Return-Path / SMTP MAIL FROM address.

    Prefer EMAIL_RETURN_PATH / SMTP_MAIL_FROM when set (custom MAIL FROM domain).
    """
    custom = (
        _normalize_env(os.getenv("EMAIL_RETURN_PATH"))
        or _normalize_env(os.getenv("SMTP_MAIL_FROM"))
    )
    return email_only(custom) or email_only(default_from)
