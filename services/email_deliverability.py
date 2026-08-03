"""DNS-based email deliverability health checks (SPF / DKIM / DMARC / alignment).

Standalone diagnostic utility — run directly:

    python -m services.email_deliverability

Not wired to API routes or admin Settings pages.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from services.email_mime import domain_of_email, email_only
from services.email_service import (
    BUSINESS_COMPANY_NAME,
    BUSINESS_EMAIL,
    SMTP_HOST,
    SMTP_USER,
    get_email_provider,
    is_email_configured,
    smtp_sender_email,
)

logger = logging.getLogger(__name__)

_DOH_URL = "https://cloudflare-dns.com/dns-query"

# Common DKIM selector names across Gmail Workspace, SES, Brevo, Microsoft, etc.
_DKIM_SELECTORS = (
    "google",
    "default",
    "selector1",
    "selector2",
    "s1",
    "s2",
    "k1",
    "brevo",
    "mail",
    "smtp",
    "dkim",
    "ses",
    os.getenv("DKIM_SELECTOR", "").strip(),
)


def _normalize_env(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


def _txt_records(name: str) -> list[str]:
    """Resolve TXT via DNS-over-HTTPS (no local dig/dnspython required)."""
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(
                _DOH_URL,
                params={"name": name, "type": "TXT"},
                headers={"Accept": "application/dns-json"},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("DNS TXT lookup failed for %s: %s", name, exc)
        return []

    answers = data.get("Answer") or []
    records: list[str] = []
    for answer in answers:
        if int(answer.get("type") or 0) != 16:
            continue
        raw = str(answer.get("data") or "").strip()
        # Cloudflare wraps TXT in quotes; flatten concatenated chunks.
        raw = raw.replace('" "', "").strip('"')
        if raw:
            records.append(raw)
    return records


def _find_spf(domain: str) -> dict[str, Any]:
    records = _txt_records(domain)
    spf = next((r for r in records if r.lower().startswith("v=spf1")), None)
    return {
        "present": bool(spf),
        "record": spf,
        "lookup": domain,
        "ok": bool(spf),
        "detail": "SPF TXT found." if spf else "No v=spf1 TXT record on the sending domain.",
    }


def _find_dmarc(domain: str) -> dict[str, Any]:
    name = f"_dmarc.{domain}"
    records = _txt_records(name)
    dmarc = next((r for r in records if r.lower().startswith("v=dmarc1")), None)
    policy = None
    if dmarc:
        for part in dmarc.split(";"):
            part = part.strip()
            if part.lower().startswith("p="):
                policy = part.split("=", 1)[-1].strip().lower()
    return {
        "present": bool(dmarc),
        "record": dmarc,
        "policy": policy,
        "lookup": name,
        "ok": bool(dmarc),
        "detail": (
            f"DMARC present (p={policy})."
            if dmarc
            else "No _dmarc TXT record. Publish at least v=DMARC1; p=none; rua=mailto:…"
        ),
    }


def _find_dkim(domain: str) -> dict[str, Any]:
    found: list[dict[str, str]] = []
    selectors = [s for s in _DKIM_SELECTORS if s]
    for selector in selectors:
        name = f"{selector}._domainkey.{domain}"
        records = _txt_records(name)
        for record in records:
            lowered = record.lower()
            if "v=dkim1" in lowered or "p=" in lowered:
                found.append({"selector": selector, "lookup": name, "record": record[:180]})
                break
    return {
        "present": bool(found),
        "ok": bool(found),
        "matches": found,
        "selectors_checked": selectors,
        "detail": (
            f"DKIM found for selector(s): {', '.join(m['selector'] for m in found)}."
            if found
            else "No common DKIM selector answered. Set DKIM_SELECTOR if your provider uses a custom name."
        ),
    }


def _alignment_checks(from_email: str, smtp_user: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    from_domain = domain_of_email(from_email)
    smtp_domain = domain_of_email(smtp_user)
    checks.append(
        {
            "id": "from_verified",
            "ok": bool(from_email and "@" in from_email),
            "label": "Verified From address configured",
            "detail": from_email or "Missing BUSINESS_EMAIL / SMTP_FROM / SMTP_USER",
        }
    )
    aligned = bool(from_domain and smtp_domain and from_domain == smtp_domain)
    checks.append(
        {
            "id": "from_smtp_aligned",
            "ok": aligned or not smtp_user,
            "label": "From domain matches SMTP login domain",
            "detail": (
                f"From={from_domain or '?'} SMTP={smtp_domain or '?'}. "
                "Misalignment is a common spam trigger (especially with Gmail SMTP)."
                if not aligned
                else "From domain aligns with the authenticated SMTP mailbox."
            ),
        }
    )
    host = (SMTP_HOST or "").lower()
    using_gmail_relay = "gmail.com" in host or "google.com" in host
    checks.append(
        {
            "id": "provider_choice",
            "ok": not using_gmail_relay or (smtp_domain in {"gmail.com", "googlemail.com"}),
            "label": "Sending provider suited for transactional mail",
            "detail": (
                "Gmail SMTP is fine for low-volume personal sends but scores poorly for "
                "first-time business outreach. Prefer Amazon SES, Brevo, or Workspace with "
                "domain DKIM for production deliverability."
                if using_gmail_relay and smtp_domain not in {"gmail.com", "googlemail.com"}
                else (
                    "Using Gmail consumer SMTP — expect stricter spam filtering for cold recipients."
                    if using_gmail_relay
                    else f"SMTP host: {SMTP_HOST or '(unset)'}"
                )
            ),
        }
    )
    return checks


def run_deliverability_health_check() -> dict[str, Any]:
    """Full deliverability report for local/ops diagnostics (not an API endpoint)."""
    from_email = email_only(smtp_sender_email() or BUSINESS_EMAIL or SMTP_USER)
    domain = domain_of_email(from_email)
    configured = is_email_configured()
    provider = get_email_provider()

    spf = _find_spf(domain) if domain else {
        "present": False,
        "ok": False,
        "detail": "Cannot check SPF — no From domain.",
        "record": None,
        "lookup": None,
    }
    dkim = _find_dkim(domain) if domain else {
        "present": False,
        "ok": False,
        "detail": "Cannot check DKIM — no From domain.",
        "matches": [],
        "selectors_checked": [],
    }
    dmarc = _find_dmarc(domain) if domain else {
        "present": False,
        "ok": False,
        "detail": "Cannot check DMARC — no From domain.",
        "record": None,
        "policy": None,
        "lookup": None,
    }

    alignment = _alignment_checks(from_email, SMTP_USER)
    return_path = (
        _normalize_env(os.getenv("EMAIL_RETURN_PATH"))
        or _normalize_env(os.getenv("SMTP_MAIL_FROM"))
        or from_email
    )

    checks = [
        {
            "id": "smtp_configured",
            "ok": configured,
            "label": "SMTP credentials configured",
            "detail": "GMAIL_* or SMTP_* credentials present." if configured else "Missing SMTP credentials.",
        },
        {
            "id": "spf",
            "ok": spf.get("ok", False),
            "label": "SPF",
            "detail": spf.get("detail"),
            "record": spf.get("record"),
        },
        {
            "id": "dkim",
            "ok": dkim.get("ok", False),
            "label": "DKIM",
            "detail": dkim.get("detail"),
            "matches": dkim.get("matches") or [],
        },
        {
            "id": "dmarc",
            "ok": dmarc.get("ok", False),
            "label": "DMARC",
            "detail": dmarc.get("detail"),
            "record": dmarc.get("record"),
            "policy": dmarc.get("policy"),
        },
        {
            "id": "return_path",
            "ok": bool(return_path),
            "label": "Return-Path / MAIL FROM",
            "detail": (
                f"Envelope sender: {return_path}. "
                "Set EMAIL_RETURN_PATH or SMTP_MAIL_FROM to a domain you control for SES/Brevo."
            ),
        },
        *alignment,
    ]

    score = sum(1 for c in checks if c.get("ok"))
    total = len(checks)
    recommendations: list[str] = []
    if not configured:
        recommendations.append("Configure SMTP_USER/SMTP_PASSWORD or GMAIL_USER/GMAIL_APP_PASSWORD.")
    if domain and not spf.get("ok"):
        recommendations.append(
            f"Add a TXT record on {domain}: v=spf1 include:_spf.google.com ~all "
            "(or your ESP include) matching the provider that actually sends mail."
        )
    if domain and not dkim.get("ok"):
        recommendations.append(
            f"Enable Easy DKIM (SES) or domain DKIM (Brevo/Workspace) and publish the "
            f"selector TXT under _domainkey.{domain}."
        )
    if domain and not dmarc.get("ok"):
        recommendations.append(
            f"Publish _dmarc.{domain} TXT: v=DMARC1; p=none; rua=mailto:dmarc@{domain}; "
            "raise to p=quarantine once aligned."
        )
    for item in alignment:
        if not item.get("ok"):
            recommendations.append(str(item.get("detail") or item.get("label")))
    recommendations.append(
        "Warm new domains gradually; first-time recipients are more likely to Junk until reputation builds."
    )
    recommendations.append(
        "Validate inbox placement with Gmail / Outlook / Yahoo after DNS changes (see docs/email-deliverability.md)."
    )

    status = "healthy" if score == total and configured else "needs_attention" if configured else "not_configured"

    return {
        "status": status,
        "score": score,
        "total": total,
        "provider": provider,
        "smtp_host": SMTP_HOST,
        "company": BUSINESS_COMPANY_NAME,
        "from_email": from_email,
        "reply_to": email_only(BUSINESS_EMAIL or from_email),
        "return_path": return_path,
        "domain": domain,
        "checks": checks,
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        "recommendations": recommendations,
        "ses_hints": {
            "verify_domain": "Amazon SES → Verified identities → Create domain",
            "easy_dkim": "Enable Easy DKIM (RSA 2048) and publish CNAME records SES provides",
            "mail_from": "Optional custom MAIL FROM subdomain (e.g. mail.example.com) + SPF",
            "env": "Set SMTP_HOST to email-smtp.<region>.amazonaws.com with SES SMTP credentials; "
            "set EMAIL_RETURN_PATH to the custom MAIL FROM address.",
        },
        "brevo_hints": {
            "verify_domain": "Brevo → Senders, domains → Domains → Add domain",
            "dns": "Publish Brevo SPF include + DKIM + DMARC as shown in the Brevo console",
            "env": "Set SMTP_HOST=smtp-relay.brevo.com, SMTP_PORT=587, SMTP_USER/SMTP_PASSWORD "
            "from Brevo SMTP & API keys; BUSINESS_EMAIL must be a verified sender.",
        },
    }


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)
    report = run_deliverability_health_check()
    print(json.dumps(report, indent=2, default=str))
