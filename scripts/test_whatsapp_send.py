"""Send a test WhatsApp message using credentials from the project root .env."""
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

from services.whatsapp_service import (  # noqa: E402
    is_whatsapp_configured,
    send_business_card_template,
    send_whatsapp_message,
    send_whatsapp_template,
    send_whatsapp_text,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a WhatsApp test message via Meta Cloud API.")
    parser.add_argument("--phone", required=True, help="Recipient phone from a scanned card (E.164 or local digits).")
    parser.add_argument("--message", default="hai", help="Free-text message body.")
    parser.add_argument(
        "--mode",
        choices=("auto", "text", "template", "business-card"),
        default="auto",
        help="auto tries text then template; template uses WHATSAPP_TEMPLATE_NAME or the active card-received template; business-card uses cardsync_contact_saved.",
    )
    parser.add_argument("--name", default="Yogesh", help="Contact name for business-card template.")
    parser.add_argument("--company", default="CardSync", help="Company for business-card template.")
    parser.add_argument("--title", default="Founder", help="Job title for business-card template.")
    args = parser.parse_args()

    if not is_whatsapp_configured():
        print("Missing WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID in .env", file=sys.stderr)
        return 1

    try:
        if args.mode == "text":
            result = send_whatsapp_text(args.phone, args.message)
        elif args.mode == "template":
            result = send_whatsapp_template(args.phone)
        elif args.mode == "business-card":
            result = send_business_card_template(args.phone, args.name, args.company, args.title)
        else:
            result = send_whatsapp_message(args.phone, args.message)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
