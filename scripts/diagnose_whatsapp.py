"""Quick WhatsApp + local API diagnostics."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.env_loader import load_env  # noqa: E402

load_env()

token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
waba = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "").strip()
pid = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
scan_tpl = os.getenv("WHATSAPP_SCAN_TEMPLATE_NAME", "cardsync_scan_thanks").strip()
lang = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE_CODE", "en_US").strip()
v = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v25.0").strip()
h = {"Authorization": f"Bearer {token}"}


def main() -> int:
    print("=== PHONE ===")
    r = requests.get(
        f"https://graph.facebook.com/{v}/{pid}?fields=display_phone_number,verified_name,status,name_status,quality_rating",
        headers=h,
        timeout=30,
    )
    print(json.dumps(r.json(), indent=2))

    print("\n=== RELEVANT TEMPLATES ===")
    r2 = requests.get(
        f"https://graph.facebook.com/{v}/{waba}/message_templates?limit=50",
        headers=h,
        timeout=30,
    )
    data = r2.json()
    found_scan = False
    for t in data.get("data", []):
        name = t.get("name", "")
        if "cardsync" in name:
            print(
                f"  {name} | lang={t.get('language')} | status={t.get('status')} | cat={t.get('category')}"
            )
            if name == scan_tpl and t.get("status") == "APPROVED":
                found_scan = True

    print(f"\n  Expected scan template '{scan_tpl}' ({lang}) approved: {found_scan}")

    print("\n=== SEND TEST (cardsync_scan_thanks -> 9884993074) ===")
    from services.whatsapp_service import send_whatsapp_template  # noqa: E402

    try:
        result = send_whatsapp_template(
            "9884993074",
            template_name=scan_tpl,
            language_code=lang,
            components=[
                {"type": "body", "parameters": [{"type": "text", "text": "Test"}]},
            ],
        )
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"FAILED: {exc}")

    print("\n=== WABA WEBHOOK SUBSCRIPTION (required for inbound messages) ===")
    try:
        r_sub = requests.get(
            f"https://graph.facebook.com/{v}/{waba}/subscribed_apps",
            headers=h,
            timeout=30,
        )
        sub = r_sub.json()
        apps = sub.get("data") or []
        print(f"  subscribed_apps count: {len(apps)}")
        for item in apps:
            meta = item.get("whatsapp_business_api_data") or item
            print(f"  - {meta.get('name')} (id={meta.get('id')})")
        if not apps:
            print("  FIX: POST /{waba}/subscribed_apps (backend does this on startup)")
            fix = requests.post(
                f"https://graph.facebook.com/{v}/{waba}/subscribed_apps",
                headers=h,
                timeout=30,
            )
            print(f"  auto-subscribe: {fix.status_code} {fix.text[:120]}")
    except Exception as exc:
        print(f"  ERROR: {exc}")

    print("\n=== LOCAL API ===")
    base = os.getenv("BACKEND_BASE_URL") or os.getenv("API_BASE_URL") or os.getenv("PUBLIC_API_URL")
    if not base:
        raise SystemExit("Set BACKEND_BASE_URL (or API_BASE_URL) in .env before running diagnose_whatsapp.")
    base = base.rstrip("/")
    for url in (
        f"{base}/health",
        f"{base}/webhook?hub.mode=subscribe&hub.verify_token=cardsync_whatsapp_verify_token&hub.challenge=diag123",
    ):
        try:
            resp = requests.get(url, timeout=45)
            path = url.split(base, 1)[-1][:40]
            print(f"  {path} -> {resp.status_code} {resp.text[:80]}")
        except Exception as exc:
            print(f"  ERROR: {exc}")

    print("\n=== LOCAL CONFIG (public) ===")
    try:
        resp = requests.get(f"{base}/config", timeout=45)
        if resp.ok:
            cfg = resp.json().get("whatsapp", {})
            print(json.dumps(cfg, indent=2))
        else:
            print(f"  /config -> {resp.status_code}")
    except Exception as exc:
        print(f"  ERROR: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
