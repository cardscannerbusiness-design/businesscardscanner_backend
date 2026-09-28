"""Diagnose WhatsApp delivery for a recipient phone."""
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
pid = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
waba = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "").strip()
tpl = os.getenv("WHATSAPP_SCAN_TEMPLATE_NAME", "").strip()
v = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v25.0")
h = {"Authorization": f"Bearer {token}"}
phone = sys.argv[1] if len(sys.argv) > 1 else "6309248193"


def norm(p: str) -> str:
    digits = "".join(c for c in p if c.isdigit())
    if len(digits) == 10 and digits[0] in "6789":
        return f"91{digits}"
    return digits


normalized = norm(phone)
print("=== SENDER PHONE ===")
r = requests.get(
    f"https://graph.facebook.com/{v}/{pid}?fields=display_phone_number,verified_name,status,name_status,quality_rating,account_mode,is_official_business_account,webhook_configuration",
    headers=h,
    timeout=30,
)
print(json.dumps(r.json(), indent=2))

print("\n=== TEMPLATE ===")
r2 = requests.get(
    f"https://graph.facebook.com/{v}/{waba}/message_templates?name={tpl}",
    headers=h,
    timeout=30,
)
data = r2.json().get("data", [])
if data:
    t = data[0]
    print(json.dumps({k: t.get(k) for k in ("name", "status", "category", "language")}, indent=2))
    body = next((c.get("text") for c in t.get("components", []) if c.get("type") == "BODY"), "")
    print("body:", body)
else:
    print("Template not found:", tpl)

print("\n=== SEND custom template ===")
from services.whatsapp_service import send_whatsapp_template  # noqa: E402

try:
    result = send_whatsapp_template(
        phone,
        template_name=tpl,
        language_code="en_US",
        components=[{"type": "body", "parameters": [{"type": "text", "text": "Test"}]}],
    )
    mid = (result.get("messages") or [{}])[0].get("id")
    print("accepted message_id:", mid)
    print(json.dumps(result, indent=2))
except Exception as exc:
    print("FAILED:", exc)

print("\n=== NOTES ===")
print(f"Recipient normalized: {normalized}")
print("If API says accepted but phone has no message:")
print("1) Open WhatsApp -> Updates tab (not Chats)")
print("2) Search sender number 7448364850")
print("3) Check WhatsApp Manager message logs for failed delivery")
print("4) Recipient must have WhatsApp installed on this exact number")
