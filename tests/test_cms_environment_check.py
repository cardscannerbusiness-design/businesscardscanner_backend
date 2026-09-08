"""CMS environment connection check (no live Graph calls)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret-not-for-production")

from services.cms_environment_check import check_admin_environment


class CheckAdminEnvironmentTests(unittest.TestCase):
    def test_missing_admin_raises(self) -> None:
        with patch(
            "services.cms_environment_check.get_admin_env_settings",
            return_value=None,
        ):
            with self.assertRaises(ValueError):
                check_admin_environment("missing")

    def test_stored_unlocked_complete_reports_bridge(self) -> None:
        item = {
            "admin_id": "a1",
            "tenant_id": "c1",
            "company_id": "c1",
            "company_name": "yogesh's Company",
            "email": "yogesh@example.com",
            "has_settings": True,
            "settings_updated_at": "2026-01-01T00:00:00+00:00",
            "channel_locks": {"whatsapp": False, "email": False, "google_sheets": False},
            "google_sheets": {"enabled": False},
        }
        raw = {
            "whatsapp": {
                "enabled": False,
                "access_token": "tok",
                "phone_number_id": "pid",
                "business_account_id": "waba",
                "card_received_template_name": "journey_stack1",
            },
            "email": {},
            "templates": {},
        }
        probe = {
            "ok": True,
            "status": "pass",
            "message": "Connected as +91 90000",
            "phone": {"display_phone_number": "+91 90000"},
            "template": {
                "name": "journey_stack1",
                "language": "en",
                "status": "APPROVED",
            },
        }
        with patch(
            "services.cms_environment_check.get_admin_env_settings",
            return_value=item,
        ), patch(
            "services.cms_environment_check.load_admin_env_raw",
            return_value=raw,
        ), patch(
            "services.cms_environment_check._probe_whatsapp_graph",
            return_value=probe,
        ), patch(
            "services.cms_environment_check.is_email_configured",
            return_value=True,
        ):
            result = check_admin_environment("a1")

        self.assertTrue(result["success"])
        self.assertEqual(result["integrations"]["whatsapp"]["status"], "pass")
        self.assertTrue(result["checks"]["whatsappCredentialsComplete"])
        self.assertTrue(result["checks"]["whatsappChannelUnlocked"])
        self.assertTrue(result["whatsapp_runtime"]["uses_cms_credentials"])
        self.assertEqual(result["whatsapp_runtime"]["template"], "journey_stack1")

    def test_locked_whatsapp_is_disabled_not_hard_fail(self) -> None:
        item = {
            "admin_id": "a1",
            "tenant_id": "c1",
            "company_name": "Co",
            "email": "a@b.c",
            "has_settings": True,
            "settings_updated_at": None,
            "channel_locks": {"whatsapp": True, "email": False, "google_sheets": False},
            "google_sheets": {},
        }
        raw = {
            "whatsapp": {
                "enabled": False,
                "access_token": "tok",
                "phone_number_id": "pid",
            },
            "email": {},
            "templates": {},
        }
        with patch(
            "services.cms_environment_check.get_admin_env_settings",
            return_value=item,
        ), patch(
            "services.cms_environment_check.load_admin_env_raw",
            return_value=raw,
        ), patch(
            "services.cms_environment_check.is_email_configured",
            return_value=False,
        ):
            result = check_admin_environment("a1")

        self.assertTrue(result["success"])
        self.assertEqual(result["integrations"]["whatsapp"]["status"], "disabled")
        self.assertFalse(result["checks"]["whatsappChannelUnlocked"])
        self.assertIn("locked", (result["reason"] or "").lower())


if __name__ == "__main__":
    unittest.main()
