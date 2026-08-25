"""Live SMTP lane + cross-lane fallback smoke tests (uses .env credentials).

Forces primary failure by temporarily breaking the lane password, then sends
a real thank-you email so fallback credentials are exercised.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.env_loader import load_env

load_env()

import services.email_service as outreach  # noqa: E402

importlib.reload(outreach)

TEST_TO = "yogeshvanaparthi@gmail.com"


def _run_case(label: str, role: str, break_lane: str) -> dict:
    """break_lane: 'internal' or 'external' — corrupt that lane's password."""
    original_internal_pw = outreach.SMTP_INTERNAL_PASSWORD
    original_external_pw = outreach.SMTP_EXTERNAL_PASSWORD
    try:
        if break_lane == "internal":
            outreach.SMTP_INTERNAL_PASSWORD = "intentionally-bad-password"
        else:
            outreach.SMTP_EXTERNAL_PASSWORD = "intentionally-bad-password"

        print(f"\n=== {label} (role={role}, break primary={break_lane}) ===")
        result = outreach.send_business_thank_you_email(
            TEST_TO,
            sender_role=role,
        )
        print(
            f"success={result.get('success')} "
            f"smtp_lane={result.get('smtp_lane')} "
            f"smtp_profile={result.get('smtp_profile')}"
        )
        if not result.get("success"):
            print("error:", result.get("error"))
        return result
    finally:
        outreach.SMTP_INTERNAL_PASSWORD = original_internal_pw
        outreach.SMTP_EXTERNAL_PASSWORD = original_external_pw


def main() -> int:
    cases = [
        ("SUPER_ADMIN primary OK", "SUPER_ADMIN", None),
        ("SUPER_ADMIN fallback internal to external", "SUPER_ADMIN", "internal"),
        ("ADMIN primary OK", "ADMIN", None),
        ("ADMIN fallback external to internal", "ADMIN", "external"),
    ]
    results: list[tuple[str, dict]] = []
    for label, role, break_lane in cases:
        if break_lane:
            results.append((label, _run_case(label, role, break_lane)))
        else:
            print(f"\n=== {label} (role={role}) ===")
            r = outreach.send_business_thank_you_email(TEST_TO, sender_role=role)
            print(
                f"success={r.get('success')} "
                f"smtp_lane={r.get('smtp_lane')} "
                f"smtp_profile={r.get('smtp_profile')}"
            )
            results.append((label, r))

    print("\n=== SUMMARY ===")
    ok = 0
    for label, r in results:
        status = "PASS" if r.get("success") else "FAIL"
        if r.get("success"):
            ok += 1
        print(
            f"{status}: {label} -> lane={r.get('smtp_lane')} profile={r.get('smtp_profile')}"
        )
    print(f"\n{ok}/{len(results)} passed")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
