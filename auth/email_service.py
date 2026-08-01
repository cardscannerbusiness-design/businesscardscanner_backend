"""Auth-related email sending — welcome, verification, forgot-password OTP, invitations."""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from config.urls import get_frontend_base_url

logger = logging.getLogger(__name__)

_SMTP_AUTH_HELP = (
    "SMTP authentication failed. If using Gmail, create a Google App Password "
    "(not your login password): enable 2-Step Verification, then visit "
    "https://myaccount.google.com/apppasswords and set GMAIL_APP_PASSWORD "
    "to the 16-character password for GMAIL_USER."
)

_SMTP_NETWORK_HINT = (
    "Some hosts block outbound SMTP on port 587. "
    "Use a host/network that permits SMTP, or an SMTP relay that allows it."
)


def _normalize_env(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


def _normalize_smtp_password(value: str | None) -> str:
    """Gmail app passwords are 16 chars; strip spaces copied from the Google UI."""
    return _normalize_env(value).replace(" ", "")


def _smtp_config() -> dict[str, str]:
    user = _normalize_env(os.getenv("SMTP_USER")) or _normalize_env(os.getenv("GMAIL_USER"))
    password = _normalize_smtp_password(os.getenv("SMTP_PASSWORD")) or _normalize_smtp_password(
        os.getenv("GMAIL_APP_PASSWORD")
    )
    # From MUST match the authenticated mailbox (Gmail blocks spoofed BUSINESS_EMAIL From).
    from_addr = user or _normalize_env(os.getenv("SMTP_FROM")) or "noreply@cardsync.ai"
    reply_to = (
        _normalize_env(os.getenv("BUSINESS_EMAIL"))
        or _normalize_env(os.getenv("SMTP_FROM"))
        or from_addr
    )
    return {
        "host": _normalize_env(os.getenv("SMTP_HOST"))
        or _normalize_env(os.getenv("GMAIL_SMTP_HOST"))
        or "smtp.gmail.com",
        "port": _normalize_env(os.getenv("SMTP_PORT"))
        or _normalize_env(os.getenv("GMAIL_SMTP_PORT"))
        or "587",
        "user": user,
        "password": password,
        "from": from_addr,
        "reply_to": reply_to,
    }


def _send_email(to: str, subject: str, html_body: str) -> dict:
    cfg = _smtp_config()
    if not cfg["user"] or not cfg["password"]:
        logger.warning("SMTP not configured — skipping email to %s", to)
        return {"sent": False, "reason": "SMTP not configured"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = to
    if cfg.get("reply_to") and cfg["reply_to"].lower() != cfg["from"].lower():
        msg["Reply-To"] = cfg["reply_to"]
    msg.set_content(html_body, subtype="html")

    try:
        with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg, from_addr=cfg["from"], to_addrs=[to])
        logger.info("Auth email sent to %s (%s)", to, subject)
        return {"sent": True}
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("Failed to send email to %s: %s", to, exc)
        return {"sent": False, "error": _SMTP_AUTH_HELP}
    except OSError as exc:
        logger.error("Failed to send email to %s: %s", to, exc)
        return {
            "sent": False,
            "error": f"Network error connecting to SMTP: {exc}. {_SMTP_NETWORK_HINT}",
        }
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to, exc)
        return {"sent": False, "error": str(exc)}


def _frontend_base(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    return get_frontend_base_url()


def send_welcome_email(to_email: str, full_name: str) -> dict:
    subject = "Welcome to NameCardScan"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: auto;">
      <h2 style="color: #0891b2;">Welcome, {full_name}!</h2>
      <p>Your NameCardScan account has been created successfully.</p>
      <p>You can now log in and start managing your contacts and CRM leads.</p>
      <hr style="border: none; border-top: 1px solid #e2e8f0;" />
      <p style="color: #64748b; font-size: 12px;">If you did not expect this email, please ignore it.</p>
    </div>
    """
    return _send_email(to_email, subject, html)


def send_forgot_password_otp(to_email: str, otp_code: str) -> dict:
    subject = "NameCardScan — Password Reset Code"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: auto;">
      <h2 style="color: #0891b2;">Password Reset Code</h2>
      <p>Your one-time verification code is:</p>
      <div style="background: #f1f5f9; padding: 16px; border-radius: 8px; text-align: center; margin: 16px 0;">
        <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #0891b2;">{otp_code}</span>
      </div>
      <p style="color: #64748b;">This code expires in 10 minutes. Do not share it with anyone.</p>
    </div>
    """
    return _send_email(to_email, subject, html)


def send_email_verification(to_email: str, token: str, frontend_url: str | None = None) -> dict:
    try:
        base = _frontend_base(frontend_url)
    except RuntimeError as exc:
        logger.error("Cannot build verification link: %s", exc)
        return {"sent": False, "error": str(exc)}
    verify_link = f"{base}/verify-email?token={token}"
    subject = "Verify your NameCardScan email"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: auto;">
      <h2 style="color: #0891b2;">Verify Your Email</h2>
      <p>Click the button below to verify your email address:</p>
      <a href="{verify_link}"
         style="display: inline-block; padding: 12px 24px; background: #0891b2; color: white;
                border-radius: 8px; text-decoration: none; font-weight: bold;">
        Verify Email
      </a>
      <p style="color: #64748b; margin-top: 16px;">This link expires in 24 hours.</p>
    </div>
    """
    return _send_email(to_email, subject, html)


def send_email_change_verification(to_email: str, token: str, frontend_url: str | None = None) -> dict:
    try:
        base = _frontend_base(frontend_url)
    except RuntimeError as exc:
        logger.error("Cannot build email-change link: %s", exc)
        return {"sent": False, "error": str(exc)}
    verify_link = f"{base}/verify-email-change?token={token}"
    subject = "NameCardScan — Confirm Email Change"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: auto;">
      <h2 style="color: #0891b2;">Confirm Email Change</h2>
      <p>You requested to change your email to <strong>{to_email}</strong>.</p>
      <a href="{verify_link}"
         style="display: inline-block; padding: 12px 24px; background: #0891b2; color: white;
                border-radius: 8px; text-decoration: none; font-weight: bold;">
        Confirm Change
      </a>
      <p style="color: #64748b; margin-top: 16px;">This link expires in 24 hours.</p>
    </div>
    """
    return _send_email(to_email, subject, html)


def send_invitation_email(
    *,
    to_email: str,
    inviter_name: str,
    role: str,
    organization_name: str,
    raw_token: str,
    expires_hours: int = 48,
    frontend_url: str | None = None,
) -> dict:
    try:
        base = _frontend_base(frontend_url)
    except RuntimeError as exc:
        logger.error("Cannot build invitation link: %s", exc)
        return {"sent": False, "error": str(exc)}
    register_link = f"{base}/register?token={raw_token}"
    role_label = "Admin" if role == "ADMIN" else "User" if role == "USER" else role
    subject = f"You're invited to join {organization_name} on NameCardScan"
    html = f"""
    <div style="font-family: sans-serif; max-width: 520px; margin: auto; color: #1e293b;">
      <h2 style="color: #0891b2;">You're invited</h2>
      <p><strong>{inviter_name}</strong> invited you to join
         <strong>{organization_name}</strong> as a <strong>{role_label}</strong>.</p>
      <p>Click the button below to create your account and set your own password.
         You cannot sign in until registration is complete.
         No password is shared by the inviter.</p>
      <p style="margin: 24px 0;">
        <a href="{register_link}"
           style="display: inline-block; padding: 12px 24px; background: #0891b2; color: white;
                  border-radius: 8px; text-decoration: none; font-weight: bold;">
          Accept invitation
        </a>
      </p>
      <p style="color: #64748b; font-size: 13px;">
        This invitation expires in {expires_hours} hours.
        If you did not expect this email, you can ignore it.
      </p>
    </div>
    """
    return _send_email(to_email, subject, html)
