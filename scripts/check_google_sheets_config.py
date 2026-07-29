"""Verify Google Sheets sync config on this machine (local or EC2).

Usage (from BusinessCardScanner_Backend):
    python scripts/check_google_sheets_config.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from services.google_sheets_service import (  # noqa: E402
    _load_service_account,
    _resolve_service_account_path,
    _sheet_id,
    _sheet_name,
    is_sheets_configured,
)


def main() -> int:
    sheet_id = _sheet_id()
    sheet_name = _sheet_name()
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

    print("GOOGLE_SHEET_ID:", sheet_id or "(optional fallback workbook)")
    print("GOOGLE_SHEET_NAME:", sheet_name, "(fallback tab / default Day 1)")
    print("GOOGLE_DRIVE_FOLDER_ID:", os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip() or "(optional)")
    print("GOOGLE_SERVICE_ACCOUNT_JSON:", raw[:80] + ("…" if len(raw) > 80 else "") or "(missing)")

    if raw and not raw.startswith("{"):
        resolved = _resolve_service_account_path(raw)
        print("Resolved credentials file:", resolved or "(NOT FOUND)")
        secrets_dir = ROOT / "secrets"
        print("secrets/ exists:", secrets_dir.is_dir())
        if secrets_dir.is_dir():
            files = sorted(p.name for p in secrets_dir.glob("*.json"))
            print("secrets/*.json:", files or "(empty — copy the service-account JSON here)")

    configured = is_sheets_configured()
    creds = _load_service_account() if configured else None
    print("is_sheets_configured:", configured)
    if creds:
        print("client_email:", creds.get("client_email"))
        print()
        print("OK — share your Drive folder (or fallback sheet) with that client_email as Editor.")
        print("Workbooks are created per Event Name; worksheets per Event Day.")
        return 0

    print()
    print("NOT CONFIGURED — Sheets sync will silently skip.")
    print("On EC2:")
    print("  1. mkdir -p secrets")
    print("  2. scp/upload the service-account JSON into secrets/")
    print("  3. In .env set (Linux path, not Windows):")
    print("       GOOGLE_SERVICE_ACCOUNT_JSON=secrets/<filename>.json")
    print("       GOOGLE_DRIVE_FOLDER_ID=<shared folder id>   # recommended")
    print("       GOOGLE_SHEET_ID=<optional fallback sheet id>")
    print("       GOOGLE_SHEET_NAME=Day 1")
    print("  4. Share the Drive folder with the service account email as Editor")
    print("  5. sudo systemctl restart business-card")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
