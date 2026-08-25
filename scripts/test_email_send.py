"""Send a test business thank-you email using SES SMTP credentials from .env.

Prefer Swagger: POST /health/email/test with your own contact_email.
This CLI is optional and has no hardcoded inbox.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.env_loader import load_env

load_env()

from services.email_service import (  # noqa: E402
    extract_primary_email,
    is_email_configured,
    send_business_thank_you_email,
    send_thank_you_to_contact,
    validate_email_address,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a business thank-you email via Amazon SES SMTP.")
    parser.add_argument(
        "--email",
        required=True,
        help="Recipient email address (no default — pass the inbox you want to test).",
    )
    parser.add_argument(
        "--from-contact",
        action="store_true",
        help="Treat --email as parsed contact data by wrapping it in a contact dict.",
    )
    parser.add_argument(
        "--override",
        default="",
        help="Optional. Force delivery to this address instead of --email.",
    )
    parser.add_argument(
        "--lane",
        choices=("internal", "external"),
        default="",
        help="Force SMTP lane (internal=SUPER_ADMIN SES, external=ADMIN/USER SES).",
    )
    parser.add_argument(
        "--role",
        default="",
        help="Optional role hint (SUPER_ADMIN, ADMIN, USER). Overrides --lane when set.",
    )
    args = parser.parse_args()

    sender_role = args.role or None
    smtp_lane = args.lane or None

    if not is_email_configured():
        print(
            "Email not configured. Set SMTP_INTERNAL_* and SMTP_EXTERNAL_* in .env",
            file=sys.stderr,
        )
        return 1

    override = args.override or None
    try:
        if args.from_contact:
            contact = {"email": args.email, "name": "Test Contact", "company": "Test Co"}
            extracted = extract_primary_email(contact)
            ok, detail = validate_email_address(extracted)
            print(f"Extracted email: {extracted!r} (valid={ok}, detail={detail!r})")
            result = send_thank_you_to_contact(
                contact,
                test_override=override,
                sender_role=sender_role,
                smtp_lane=smtp_lane,
            )
        else:
            ok, detail = validate_email_address(args.email)
            print(f"Recipient email: {args.email!r} (valid={ok}, detail={detail!r})")
            result = send_business_thank_you_email(
                args.email,
                test_override=override,
                sender_role=sender_role,
                smtp_lane=smtp_lane,
            )
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(result.get("message") or ("SUCCESS" if result.get("success") else "FAILED"))
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
