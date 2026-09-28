"""Auth-related email sending — welcome, verification, forgot-password OTP, invitations."""

from __future__ import annotations

import html
import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from config.urls import get_frontend_base_url

logger = logging.getLogger(__name__)

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


def _smtp_lane_config(lane: str) -> dict[str, str] | None:
    """Build INTERNAL or EXTERNAL SES profile from env."""
    key = "INTERNAL" if lane == "internal" else "EXTERNAL"
    user = _normalize_env(os.getenv(f"SMTP_{key}_USER")) or _normalize_env(
        os.getenv("SMTP_USER")
    )
    password = _normalize_smtp_password(
        os.getenv(f"SMTP_{key}_PASSWORD")
    ) or _normalize_smtp_password(os.getenv("SMTP_PASSWORD"))
    if not user or not password:
        return None
    smtp_from = (
        _normalize_env(os.getenv(f"SMTP_{key}_FROM"))
        or _normalize_env(os.getenv("SMTP_FROM"))
        or _normalize_env(os.getenv("BUSINESS_EMAIL"))
    )
    if user and "@" in user:
        from_addr = user
    else:
        from_addr = smtp_from
    if not from_addr or "@" not in from_addr:
        return None
    business = _normalize_env(os.getenv("BUSINESS_EMAIL"))
    return {
        "host": _normalize_env(os.getenv(f"SMTP_{key}_HOST"))
        or _normalize_env(os.getenv("SMTP_HOST"))
        or _normalize_env(os.getenv("GMAIL_SMTP_HOST"))
        or "smtp.gmail.com",
        "port": _normalize_env(os.getenv(f"SMTP_{key}_PORT"))
        or _normalize_env(os.getenv("SMTP_PORT"))
        or _normalize_env(os.getenv("GMAIL_SMTP_PORT"))
        or "587",
        "user": user,
        "password": password,
        "from": from_addr,
        "reply_to": business or from_addr,
        "label": lane,
    }


def _smtp_lane_configs(lane: str) -> list[dict[str, str]]:
    """Primary lane, then the other lane as fallback."""
    configs: list[dict[str, str]] = []
    fallback_lane = "external" if lane == "internal" else "internal"
    primary = _smtp_lane_config(lane)
    fallback = _smtp_lane_config(fallback_lane)
    if primary:
        configs.append(primary)
    if fallback and (
        not primary
        or fallback["user"] != primary["user"]
        or fallback["password"] != primary["password"]
    ):
        configs.append(fallback)
    return configs


def resolve_auth_smtp_lane(sender_role: str | None = None, smtp_lane: str | None = None) -> str:
    explicit = str(smtp_lane or "").strip().lower()
    if explicit in {"internal", "external"}:
        return explicit
    if str(sender_role or "").strip().upper() == "SUPER_ADMIN":
        return "internal"
    return "external"


_ADMIN_SIGNUP_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "templates" / "new-admin-registration.html"
)
_DEFAULT_ADMIN_SIGNUP_NOTIFICATION_EMAILS = (
    "onboarding@namecardscan.com,"
    "sugitha.ulavi@gmail.com,"
    "nanduja.ulacab@gmail.com,"
    "dhana@ulavitech.com,"
    "shwethars.ulavi@gmail.com"
)


def _unique_emails(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        email = str(value or "").strip()
        if not email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(email)
    return unique


def parse_notification_emails(raw: str | None) -> list[str]:
    """Split a comma-separated address list and drop blanks."""
    return _unique_emails(str(raw or "").split(","))


def admin_signup_notification_recipients() -> list[str]:
    """Recipients for the Admin signup notice.

    ADMIN_SIGNUP_NOTIFICATION_EMAILS is the source of truth. When that variable
    is unset, the five onboarding addresses are used. An explicit empty value
    falls back to BUSINESS_EMAIL so older deployments still notify someone.
    """
    if "ADMIN_SIGNUP_NOTIFICATION_EMAILS" in os.environ:
        configured = parse_notification_emails(os.getenv("ADMIN_SIGNUP_NOTIFICATION_EMAILS"))
        if configured:
            return configured
        return parse_notification_emails(os.getenv("BUSINESS_EMAIL"))
    return parse_notification_emails(_DEFAULT_ADMIN_SIGNUP_NOTIFICATION_EMAILS)


def _send_with_cfg(
    to: str,
    subject: str,
    html_body: str,
    cfg: dict[str, str],
    bcc: list[str] | None = None,
) -> dict:
    company = _normalize_env(os.getenv("BUSINESS_COMPANY_NAME")) or "NameCardScan"
    from_header = f"{company} <{cfg['from']}>" if cfg["from"] else company
    hidden = _unique_emails(bcc or [])

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_header
    # BCC recipients must not appear in To. A visible To is still required;
    # use the sender address so the staff list stays off the message.
    msg["To"] = cfg["from"] if hidden else to
    if hidden:
        msg["Bcc"] = ", ".join(hidden)
    if cfg.get("reply_to") and cfg["reply_to"].lower() != cfg["from"].lower():
        msg["Reply-To"] = cfg["reply_to"]
    html_to_send = html_body
    related: list[tuple[str, bytes, str]] = []
    if "logo-mark.png" in html_body:
        from services.email_service import _prepare_inline_asset_images

        html_to_send, related = _prepare_inline_asset_images(html_body)
    if related:
        msg.set_content("New Admin Registration - NameCardScan")
        msg.add_alternative(html_to_send, subtype="html")
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is not None:
            for cid, data, subtype in related:
                safe_name = cid if "." in cid else f"{cid}.{subtype}"
                cid_header = cid if cid.startswith("<") and cid.endswith(">") else f"<{cid}>"
                html_part.add_related(
                    data,
                    maintype="image",
                    subtype=subtype,
                    cid=cid_header,
                    filename=safe_name,
                    disposition="inline",
                )
    else:
        msg.set_content(html_body, subtype="html")

    envelope = hidden or [to]
    with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(cfg["user"], cfg["password"])
        server.send_message(msg, from_addr=cfg["from"], to_addrs=envelope)
    logger.info(
        "Auth email sent via SMTP (%s) to %s (%s)",
        cfg.get("label") or "smtp",
        ", ".join(envelope),
        subject,
    )
    return {"sent": True, "smtp_profile": cfg.get("label") or "external", "recipients": envelope}


def _send_email(
    to: str,
    subject: str,
    html_body: str,
    *,
    sender_role: str | None = None,
    smtp_lane: str | None = None,
    bcc: list[str] | None = None,
) -> dict:
    lane = resolve_auth_smtp_lane(sender_role=sender_role, smtp_lane=smtp_lane)
    configs = _smtp_lane_configs(lane)
    if not configs:
        logger.warning(
            "SMTP lane %s not configured — skipping email to %s",
            lane,
            to,
        )
        return {
            "sent": False,
            "reason": f"SMTP not configured for lane '{lane}'",
            "smtp_lane": lane,
        }

    last_error: str | None = None
    for index, cfg in enumerate(configs):
        try:
            result = _send_with_cfg(to, subject, html_body, cfg, bcc=bcc)
            result["smtp_lane"] = lane
            return result
        except smtplib.SMTPAuthenticationError as exc:
            last_error = _SMTP_AUTH_HELP
            logger.warning("Auth SMTP profile %s failed for %s: %s", cfg.get("label"), to, exc)
        except OSError as exc:
            last_error = f"Network error connecting to SMTP: {exc}. {_SMTP_NETWORK_HINT}"
            logger.warning(
                "Auth SMTP profile %s network error for %s: %s",
                cfg.get("label"),
                to,
                exc,
            )
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "Auth SMTP profile %s error for %s: %s",
                cfg.get("label"),
                to,
                exc,
            )
        if index < len(configs) - 1:
            logger.warning("Trying %s lane fallback credentials for auth email to %s", lane, to)
            continue

    logger.error("Failed to send email to %s: %s", to, last_error)
    return {"sent": False, "error": last_error or _SMTP_AUTH_HELP, "smtp_lane": lane}


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


def render_admin_signup_notification_html(
    *,
    admin_name: str,
    company_name: str,
    designation: str,
    admin_email: str,
    phone: str,
    signup_at: str,
) -> str:
    """Fill the New Admin Registration template. Values are escaped; no password."""
    template = _ADMIN_SIGNUP_TEMPLATE.read_text(encoding="utf-8")
    values = {
        "admin_name": admin_name,
        "company_name": company_name,
        "designation": designation,
        "email": admin_email,
        "phone": phone,
        "signup_datetime": signup_at,
    }
    rendered = template
    for key, value in values.items():
        safe = html.escape(str(value or "").strip() or "—")
        rendered = rendered.replace("{{" + key + "}}", safe)
    return rendered


def send_admin_signup_notification_email(
    *,
    admin_name: str,
    company_name: str,
    designation: str,
    admin_email: str,
    phone: str,
    signup_at: str,
    recipients: list[str] | None = None,
    to_email: str | None = None,
) -> dict:
    """Notify staff that a public Admin signup completed (account is active)."""
    targets = _unique_emails(list(recipients or []))
    if not targets and to_email:
        targets = parse_notification_emails(to_email)
    if not targets:
        logger.warning("Admin signup notification skipped: no recipients configured.")
        return {"sent": False, "reason": "No notification recipients configured."}

    try:
        body = render_admin_signup_notification_html(
            admin_name=admin_name,
            company_name=company_name,
            designation=designation,
            admin_email=admin_email,
            phone=phone,
            signup_at=signup_at,
        )
    except OSError as exc:
        logger.error("Admin signup notification template could not be read: %s", exc)
        return {"sent": False, "error": str(exc)}

    subject = "New Admin Registration - NameCardScan"
    result = _send_email(targets[0], subject, body, bcc=targets)
    result["recipients"] = targets
    return result


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


def send_data_deletion_confirmation(to_email: str, kind: str, reason: str = "") -> dict:
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
    safe_reason = html.escape((reason or "").strip()[:500])
    reason_html = (
        f'<p style="margin-top: 12px;"><strong>Reason:</strong> {safe_reason}</p>'
        if safe_reason
        else ""
    )
    html_body = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: auto;">
      <h2 style="color: #0891b2;">Deletion confirmed</h2>
      <p>{body}</p>
      {reason_html}
      <p style="color: #64748b; font-size: 12px; margin-top: 16px;">
        If you did not request this action, contact support immediately.
      </p>
    </div>
    """
    return _send_email(to_email, subject, html_body)
