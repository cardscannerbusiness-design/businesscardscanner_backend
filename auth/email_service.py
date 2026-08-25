"""Auth-related email sending — welcome, verification, forgot-password OTP, invitations."""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

# import requests  # Brevo REST client — commented out

from config.urls import get_frontend_base_url

logger = logging.getLogger(__name__)

# BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
BREVO_API_URL = ""

_SMTP_AUTH_HELP = (
    "SMTP authentication failed. For Amazon SES: open SES console → SMTP settings → "
    "Create SMTP credentials, then set SMTP_USER and SMTP_PASSWORD. "
    "From address (SMTP_FROM / BUSINESS_EMAIL) must be on a verified SES identity. "
    "For Gmail fallback, use a Google App Password."
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
    smtp_from = _normalize_env(os.getenv("SMTP_FROM"))
    business = _normalize_env(os.getenv("BUSINESS_EMAIL"))
    # Gmail: SMTP_USER is the mailbox email and must be From.
    # SES: SMTP_USER is an IAM access key — From must be a verified identity.
    if user and "@" in user:
        from_addr = user
    else:
        from_addr = smtp_from or business or "noreply@cardsync.ai"
    reply_to = business or smtp_from or from_addr
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


# def _brevo_config() -> dict[str, str]:
#     sender = (
#         _normalize_env(os.getenv("BREVO_SENDER_EMAIL"))
#         or _normalize_env(os.getenv("BUSINESS_EMAIL"))
#     )
#     reply_to = _normalize_env(os.getenv("BUSINESS_EMAIL")) or sender
#     return {
#         "api_key": _normalize_env(os.getenv("BREVO_API_KEY")),
#         "sender": sender,
#         "company": _normalize_env(os.getenv("BUSINESS_COMPANY_NAME")) or "NameCardScan",
#         "reply_to": reply_to,
#     }


def _send_email(to: str, subject: str, html_body: str) -> dict:
    # --- Brevo email send (commented out) ---
    # brevo = _brevo_config()
    # if not brevo["api_key"] or "@" not in brevo["sender"]:
    #     logger.warning("Brevo not configured — skipping email to %s", to)
    #     return {
    #         "sent": False,
    #         "reason": "Brevo is not configured. Set BREVO_API_KEY and BREVO_SENDER_EMAIL in .env.",
    #     }
    #
    # payload: dict = {
    #     "sender": {"name": brevo["company"], "email": brevo["sender"]},
    #     "to": [{"email": to}],
    #     "subject": subject,
    #     "htmlContent": html_body,
    # }
    # if brevo["reply_to"]:
    #     payload["replyTo"] = {"email": brevo["reply_to"]}
    #
    # try:
    #     response = requests.post(
    #         BREVO_API_URL,
    #         headers={
    #             "accept": "application/json",
    #             "api-key": brevo["api_key"],
    #             "content-type": "application/json",
    #         },
    #         json=payload,
    #         timeout=30,
    #     )
    # except requests.RequestException as exc:
    #     logger.error("Failed to send email to %s: %s", to, exc)
    #     return {"sent": False, "error": f"Network error connecting to Brevo: {exc}"}
    #
    # if response.status_code in (200, 201, 202):
    #     logger.info("Auth email sent via Brevo to %s (%s)", to, subject)
    #     return {"sent": True}
    #
    # detail = (response.text or "").strip() or response.reason
    # try:
    #     data = response.json()
    #     if isinstance(data, dict):
    #         detail = str(data.get("message") or data.get("error") or detail)
    # except ValueError:
    #     pass
    # logger.error("Failed to send email to %s: %s", to, detail)
    # return {"sent": False, "error": f"Brevo rejected the send ({response.status_code}): {detail}"}

    cfg = _smtp_config()
    if not cfg["user"] or not cfg["password"]:
        logger.warning("SMTP not configured — skipping email to %s", to)
        return {"sent": False, "reason": "SMTP not configured"}

    company = _normalize_env(os.getenv("BUSINESS_COMPANY_NAME")) or "NameCardScan"
    from_header = f"{company} <{cfg['from']}>" if cfg["from"] else company

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_header
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


def send_mobile_verification_otp(to_email: str, otp_code: str, phone: str) -> dict:
    subject = "NameCardScan — Mobile Verification Code"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: auto;">
      <h2 style="color: #0891b2;">Verify Your Mobile Number</h2>
      <p>Enter this code to verify <strong>{phone}</strong> and continue after Freemium expiry:</p>
      <div style="background: #f1f5f9; padding: 16px; border-radius: 8px; text-align: center; margin: 16px 0;">
        <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #0891b2;">{otp_code}</span>
      </div>
      <p style="color: #64748b;">This code expires in 10 minutes. Do not share it with anyone.</p>
    </div>
    """
    return _send_email(to_email, subject, html)


def send_registration_received_email(
    *,
    to_email: str,
    applicant_name: str,
    applicant_email: str,
    company_name: str,
    role: str = "ADMIN",
    phone: str = "",
    designation: str = "",
    company_code: str = "",
) -> dict:
    """Notify SuperAdmin that a client completed signup and is awaiting approval."""
    role_label = "Admin" if role == "ADMIN" else "User" if role == "USER" else role
    try:
        base = _frontend_base()
    except RuntimeError:
        base = ""
    review_link = f"{base}/companies" if base else "/companies"

    rows = [
        ("Name", applicant_name or "—"),
        ("Email", applicant_email or "—"),
        ("Role", role_label),
        ("Company", company_name or "—"),
    ]
    if company_code:
        rows.append(("Company code", company_code))
    if phone:
        rows.append(("Mobile", phone))
    if designation:
        rows.append(("Designation", designation))
    details = "".join(
        f"<tr><td style='padding:6px 12px 6px 0;color:#64748b;vertical-align:top;'>{label}</td>"
        f"<td style='padding:6px 0;font-weight:600;'>{value}</td></tr>"
        for label, value in rows
    )

    subject = f"New {role_label} signup pending your approval"
    html = f"""
    <div style="font-family: sans-serif; max-width: 520px; margin: auto; color: #1e293b;">
      <h2 style="color: #0891b2;">New {role_label} registration</h2>
      <p>A client completed signup and is waiting for Super Admin approval.</p>
      <table style="width:100%;border-collapse:collapse;margin:16px 0;">{details}</table>
      <p>Review and approve or reject this request in NameCardScan.</p>
      <p style="margin: 24px 0;">
        <a href="{review_link}"
           style="display: inline-block; padding: 12px 24px; background: #0891b2; color: white;
                  border-radius: 8px; text-decoration: none; font-weight: bold;">
          Review &amp; approve
        </a>
      </p>
      <p style="color: #64748b; font-size: 13px;">
        The applicant cannot sign in until you approve this request.
      </p>
    </div>
    """
    return _send_email(to_email, subject, html)


def send_invitation_accepted_email(
    *,
    to_email: str,
    invitee_name: str,
    invitee_email: str,
    role: str,
    company_name: str,
    inviter_name: str = "",
) -> dict:
    """Notify SuperAdmin that an invited client finished registration."""
    role_label = "Admin" if role == "ADMIN" else "User" if role == "USER" else role
    try:
        base = _frontend_base()
    except RuntimeError:
        base = ""
    review_link = f"{base}/companies" if base else "/companies"
    inviter_line = (
        f"<p>Originally invited by <strong>{inviter_name}</strong>.</p>"
        if (inviter_name or "").strip()
        else ""
    )
    subject = f"Invitation accepted — {role_label} account created"
    html = f"""
    <div style="font-family: sans-serif; max-width: 520px; margin: auto; color: #1e293b;">
      <h2 style="color: #0891b2;">Invitation accepted</h2>
      <p><strong>{invitee_name or invitee_email}</strong> ({invitee_email}) completed
         signup as a <strong>{role_label}</strong> for
         <strong>{company_name or "the workspace"}</strong>.</p>
      {inviter_line}
      <p>The account is active and they can sign in.</p>
      <p style="margin: 24px 0;">
        <a href="{review_link}"
           style="display: inline-block; padding: 12px 24px; background: #0891b2; color: white;
                  border-radius: 8px; text-decoration: none; font-weight: bold;">
          Open Manage Team
        </a>
      </p>
    </div>
    """
    return _send_email(to_email, subject, html)


def send_registration_approved_email(*, to_email: str, full_name: str, role: str = "ADMIN") -> dict:
    try:
        base = _frontend_base()
    except RuntimeError:
        base = ""
    sign_in = f"{base}/auth/sign-in" if base else "/auth/sign-in"
    role_label = "Admin" if role == "ADMIN" else "User" if role == "USER" else role
    subject = "Your NameCardScan registration has been approved"
    html = f"""
    <div style="font-family: sans-serif; max-width: 520px; margin: auto; color: #1e293b;">
      <h2 style="color: #0891b2;">Registration approved</h2>
      <p>Hi {full_name or "there"},</p>
      <p>Your NameCardScan {role_label} registration has been approved. You can now sign in.</p>
      <p style="margin: 24px 0;">
        <a href="{sign_in}"
           style="display: inline-block; padding: 12px 24px; background: #0891b2; color: white;
                  border-radius: 8px; text-decoration: none; font-weight: bold;">
          Sign in
        </a>
      </p>
    </div>
    """
    return _send_email(to_email, subject, html)


def send_registration_rejected_email(
    *,
    to_email: str,
    full_name: str,
    reason: str = "",
    role: str = "ADMIN",
) -> dict:
    reason_html = (
        f"<p><strong>Reason:</strong> {reason}</p>"
        if (reason or "").strip()
        else ""
    )
    role_label = "Admin" if role == "ADMIN" else "User" if role == "USER" else role
    subject = "Your NameCardScan registration request was rejected"
    html = f"""
    <div style="font-family: sans-serif; max-width: 520px; margin: auto; color: #1e293b;">
      <h2 style="color: #0891b2;">Registration not approved</h2>
      <p>Hi {full_name or "there"},</p>
      <p>Your NameCardScan {role_label} registration request was rejected.</p>
      {reason_html}
      <p>You cannot sign in with this account. You may submit a new registration if appropriate.</p>
    </div>
    """
    return _send_email(to_email, subject, html)


def send_admin_registration_received_email(
    *,
    to_email: str,
    applicant_name: str,
    applicant_email: str,
    company_name: str,
    role: str = "ADMIN",
    phone: str = "",
    designation: str = "",
    company_code: str = "",
) -> dict:
    return send_registration_received_email(
        to_email=to_email,
        applicant_name=applicant_name,
        applicant_email=applicant_email,
        company_name=company_name,
        role=role,
        phone=phone,
        designation=designation,
        company_code=company_code,
    )


def send_admin_registration_approved_email(*, to_email: str, full_name: str) -> dict:
    return send_registration_approved_email(to_email=to_email, full_name=full_name, role="ADMIN")


def send_admin_registration_rejected_email(
    *,
    to_email: str,
    full_name: str,
    reason: str = "",
) -> dict:
    return send_registration_rejected_email(
        to_email=to_email, full_name=full_name, reason=reason, role="ADMIN"
    )


def send_data_deletion_confirmation(to_email: str, kind: str) -> dict:
    """Notify the user that local or organisation data was deleted."""
    if kind == "organisation":
        subject = "NameCardScan — Organisation data deleted"
        body = (
            "Your organisation data deletion request was completed. "
            "Contacts and related records for your workspace have been removed as requested."
        )
    else:
        subject = "NameCardScan — Local data deleted"
        body = (
            "Your Delete My Data request was completed. "
            "All scans stored in your local offline queue on that device have been permanently deleted."
        )
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: auto;">
      <h2 style="color: #0891b2;">Deletion confirmed</h2>
      <p>{body}</p>
      <p style="color: #64748b; font-size: 12px; margin-top: 16px;">
        If you did not request this action, contact support immediately.
      </p>
    </div>
    """
    return _send_email(to_email, subject, html)
